from fastapi import FastAPI, Depends, Form, HTTPException, Request, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional
import os
import re
import html
import httpx
import shutil
import base64
import json
import sqlite3
from datetime import datetime, timedelta

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload
import fitz  # PyMuPDF

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting Agent", version="3.7.0")

store = DocumentStore()
learning_db = LearningDatabase()

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "/tmp/accounting_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")

DB_PATH = "accounting_journal.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            doc_type TEXT,
            doc_date TEXT,
            processed_at TEXT,
            sender TEXT,
            recipient TEXT,
            items_summary TEXT,
            intent TEXT,
            full_json TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Berlin2015!")
SESSION_COOKIE_NAME = "session_token"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_MODEL = os.getenv("OPENROUTER_MODEL_EXTRACT", "google/gemini-2.5-flash-lite")


class ChatQuery(BaseModel):
    prompt: str


def verify_session(request: Request):
    session = request.cookies.get(SESSION_COOKIE_NAME)
    if session != "authenticated":
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"}
        )
    return True


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=SESSION_COOKIE_NAME, value="authenticated", httponly=True, max_age=86400)
        return response
    return templates.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль"})


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    proc_from: Optional[str] = None,
    proc_to: Optional[str] = None,
    counterparty: Optional[str] = None,
    doc_type: Optional[str] = None,
    _=Depends(verify_session)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    query = "SELECT * FROM processed_journal WHERE 1=1"
    params = []

    if date_from:
        query += " AND doc_date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND doc_date <= ?"
        params.append(date_to)
    if proc_from:
        query += " AND processed_at >= ?"
        params.append(proc_from)
    if proc_to:
        query += " AND processed_at <= ?"
        params.append(proc_to)
    if counterparty and counterparty.strip():
        query += " AND (sender LIKE ? OR recipient LIKE ?)"
        params.extend([f"%{counterparty}%", f"%{counterparty}%"])
    if doc_type and doc_type != "all":
        query += " AND doc_type LIKE ?"
        params.append(f"%{doc_type}%")

    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    journal_html = ""
    for r in rows:
        journal_html += f"""
        <tr>
            <td class="text-center align-middle">{r['id']}</td>
            <td class="align-middle"><span class="badge bg-secondary">{r['doc_type']}</span></td>
            <td class="align-middle small">{r['doc_date'] or '-'}</td>
            <td class="align-middle small">{r['processed_at']}</td>
            <td class="align-middle small text-info fw-semibold">{r['sender']}</td>
            <td class="align-middle small">{r['recipient']}</td>
            <td class="align-middle small">{r['items_summary']}</td>
            <td class="align-middle small text-center">
                <a href="/files/{r['filename']}" target="_blank" class="btn btn-sm btn-outline-info py-0 px-2">👁 Просмотр</a>
            </td>
        </tr>
        """

    html_content = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Accounting Agent — Главная дашборд</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
            .card {{ background-color: #161b22; border: 1px solid #30363d; color: #c9d1d9; border-radius: 10px; }}
            .form-control, .form-select {{ background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; }}
            .form-control:focus, .form-select:focus {{ background-color: #0d1117; color: #c9d1d9; border-color: #58a6ff; box-shadow: none; }}
            .custom-table {{ width: 100%; border-collapse: collapse; color: #c9d1d9; }}
            .custom-table thead th {{ background-color: #21262d; border-bottom: 1px solid #30363d; padding: 10px; font-size: 0.85rem; }}
            .custom-table tbody td {{ border-bottom: 1px solid #30363d; padding: 10px; }}
            .chat-box {{ height: 220px; overflow-y: auto; background-color: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px; font-size: 0.88rem; }}
        </style>
    </head>
    <body class="p-4">
        <div class="container-fluid">
            <div class="d-flex justify-content-between align-items-center mb-4 pb-3 border-bottom border-secondary">
                <h3 class="text-white m-0">Accounting Agent — Управление и Журнал</h3>
                <div class="d-flex gap-2">
                    <a href="/api/export/excel" class="btn btn-success btn-sm">📊 Скачать Excel</a>
                    <a href="/logout" class="btn btn-outline-danger btn-sm">Выйти</a>
                </div>
            </div>

            <!-- СЕТКА: ВВЕРХУ ЗАГРУЗКА И AI-АССИСТЕНТ -->
            <div class="row g-4 mb-4">
                <!-- БЛОК ЗАГРУЗКИ -->
                <div class="col-md-7">
                    <div class="card p-4 shadow-sm h-100">
                        <h5 class="text-white mb-3">📥 Загрузка новых документов в обработку</h5>
                        <form action="/process-documents" method="post" enctype="multipart/form-data" class="row g-3">
                            <div class="col-md-12">
                                <label class="form-label text-secondary small">Ссылка на папку Google Диск:</label>
                                <input type="text" name="gdrive_link" class="form-control" placeholder="https://drive.google.com/drive/folders/...">
                            </div>
                            <div class="col-md-12">
                                <label class="form-label text-secondary small">Прикрепить файлы с ПК (PDF, сканы, фото):</label>
                                <input type="file" name="files" multiple class="form-control">
                            </div>
                            <div class="col-md-12 text-end">
                                <button type="submit" class="btn btn-primary px-4 fw-semibold">🚀 Запустить анализ</button>
                            </div>
                        </form>
                    </div>
                </div>

                <!-- БЛОК AI-АССИСТЕНТА -->
                <div class="col-md-5">
                    <div class="card p-4 shadow-sm h-100 d-flex flex-column justify-content-between">
                        <div>
                            <div class="d-flex justify-content-between align-items-center mb-2">
                                <h5 class="text-white m-0">🤖 ИИ-Ассистент по базе</h5>
                                <span class="badge bg-primary">OpenRouter API</span>
                            </div>
                            <div id="chatMessages" class="chat-box mb-3">
                                <div class="text-muted">Привет! Задайте вопрос по обработанным документам, например: <em>«Выведи документы по компании ХХХ за последний месяц»</em>.</div>
                            </div>
                        </div>
                        <div class="input-group input-group-sm">
                            <input type="text" id="chatInput" class="form-control" placeholder="Спросите ассистента...">
                            <button class="btn btn-primary" type="button" onclick="sendAiQuery()">Спросить</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- ФИЛЬТРЫ И ЖУРНАЛ -->
            <div class="card p-4 shadow-sm">
                <h5 class="text-white mb-3">📋 Журнал обработанных документов</h5>
                
                <form method="get" action="/" class="row g-3 mb-4 p-3 rounded border border-secondary bg-dark">
                    <div class="col-md-3">
                        <label class="form-label text-secondary small">Дата документа (от - до):</label>
                        <div class="input-group input-group-sm">
                            <input type="date" name="date_from" value="{date_from or ''}" class="form-control">
                            <input type="date" name="date_to" value="{date_to or ''}" class="form-control">
                        </div>
                    </div>
                    <div class="col-md-3">
                        <label class="form-label text-secondary small">Дата обработки (от - до):</label>
                        <div class="input-group input-group-sm">
                            <input type="date" name="proc_from" value="{proc_from or ''}" class="form-control">
                            <input type="date" name="proc_to" value="{proc_to or ''}" class="form-control">
                        </div>
                    </div>
                    <div class="col-md-2">
                        <label class="form-label text-secondary small">Контрагент (поиск):</label>
                        <input type="text" name="counterparty" value="{counterparty or ''}" class="form-control form-control-sm" placeholder="Отправитель / Получатель">
                    </div>
                    <div class="col-md-2">
                        <label class="form-label text-secondary small">Тип документа:</label>
                        <select name="doc_type" class="form-select form-select-sm">
                            <option value="all" {'selected' if not doc_type or doc_type == 'all' else ''}>Все типы</option>
                            <option value="ТН" {'selected' if doc_type == 'ТН' else ''}>Накладная (ТН/ТТН)</option>
                            <option value="Акт" {'selected' if doc_type == 'Акт' else ''}>Акт</option>
                            <option value="Чек" {'selected' if doc_type == 'Чек' else ''}>Чек</option>
                        </select>
                    </div>
                    <div class="col-md-2 d-flex align-items-end gap-2">
                        <button type="submit" class="btn btn-primary btn-sm w-100">🔍 Сформировать</button>
                        <a href="/" class="btn btn-outline-secondary btn-sm">Сброс</a>
                    </div>
                </form>

                <div class="table-responsive">
                    <table class="custom-table">
                        <thead>
                            <tr>
                                <th class="text-center" style="width: 40px;">#</th>
                                <th style="width: 130px;">Тип</th>
                                <th style="width: 100px;">Дата док.</th>
                                <th style="width: 140px;">Обработан</th>
                                <th style="width: 220px;">Отправитель (Поставщик / Исполнитель)</th>
                                <th style="width: 200px;">Получатель (Покупатель)</th>
                                <th>Список позиций</th>
                                <th class="text-center" style="width: 100px;">Просмотр</th>
                            </tr>
                        </thead>
                        <tbody>
                            {journal_html if journal_html else '<tr><td colspan="8" class="text-center py-4 text-secondary">Нет данных в журнале по заданным фильтрам</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            async function sendAiQuery() {
                const input = document.getElementById('chatInput');
                const chatBox = document.getElementById('chatMessages');
                const prompt = input.value.trim();
                if (!prompt) return;

                // Вывод запроса пользователя
                chatBox.innerHTML += `<div class="mb-2 text-white"><strong>Вы:</strong> ${prompt}</div>`;
                input.value = '';
                chatBox.scrollTop = chatBox.scrollHeight;

                // Индикатор загрузки
                const loadingId = 'load-' + Date.now();
                chatBox.innerHTML += `<div id="${loadingId}" class="mb-2 text-warning"><strong>Ассистент:</strong> думает...</div>`;
                chatBox.scrollTop = chatBox.scrollHeight;

                try {
                    const response = await fetch('/api/ai-chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ prompt })
                    });
                    const data = await response.json();
                    
                    document.getElementById(loadingId).remove();
                    if (response.ok) {
                        chatBox.innerHTML += `<div class="mb-2 text-success"><strong>Ассистент:</strong> ${data.answer}</div>`;
                    } else {
                        chatBox.innerHTML += `<div class="mb-2 text-danger"><strong>Ошибка:</strong> ${data.detail || 'Не удалось получить ответ'}</div>`;
                    }
                } catch(err) {
                    document.getElementById(loadingId).remove();
                    chatBox.innerHTML += `<div class="mb-2 text-danger"><strong>Ошибка сети</strong></div>`;
                }
                chatBox.scrollTop = chatBox.scrollHeight;
            }

            // Отправка по нажатию Enter
            document.getElementById('chatInput').addEventListener('keypress', function (e) {
                if (e.key === 'Enter') {
                    sendAiQuery();
                }
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/api/ai-chat")
async def ai_chat(query: ChatQuery, _=Depends(verify_session)):
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=400, detail="Не задан OPENROUTER_API_KEY на сервере.")

    # 1. Выгружаем все документы из базы журнала за последнее время, чтобы дать контекст ИИ
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, doc_type, doc_date, processed_at, sender, recipient, items_summary, intent FROM processed_journal ORDER BY id DESC LIMIT 50")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    context_text = json.dumps(rows, ensure_ascii=False, indent=2)

    system_prompt = (
        "Ты профессиональный бухгалтерский ИИ-помощник. У тебя есть доступ к базе данных обработанных первичных документов компании в формате JSON. "
        "Отвечай на вопросы пользователя четко, опираясь строго на предоставленные данные ниже. Если документ не найден, так и скажи.\n\n"
        f"Данные документов в базе:\n{context_text}"
    )

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://aa.depot.by",
        "X-Title": "Accounting Agent",
        "Content-Type": "application/json"
    }

    payload = {
        "model": GEMINI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query.prompt}
        ]
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        try:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code == 200:
                res_data = resp.json()
                answer = res_data["choices"][0]["message"]["content"]
                return {"answer": answer}
            else:
                raise HTTPException(status_code=500, detail=f"OpenRouter API error: {resp.status_code}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
