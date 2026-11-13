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
from datetime import datetime

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload
import fitz  # PyMuPDF

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting Agent", version="3.8.0")

store = DocumentStore()
learning_db = LearningDatabase()

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "/tmp/accounting_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Монтируем директорию загрузок для предпросмотра файлов
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


def get_drive_service():
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
    if os.path.exists(sa_file):
        try:
            creds = service_account.Credentials.from_service_account_file(
                sa_file, scopes=["https://www.googleapis.com/auth/drive.readonly"]
            )
            return build("drive", "v3", credentials=creds)
        except Exception as e:
            print(f"Ошибка Google Drive API: {e}")
    return None


def fetch_files_from_drive_folder(folder_url_or_id: str) -> list:
    downloaded_files = []
    service = get_drive_service()
    if not service:
        return downloaded_files

    match = re.search(r'folders/([a-zA-Z0-9_-]+)', folder_url_or_id)
    folder_id = match.group(1) if match else folder_url_or_id.strip()

    try:
        query = f"'{folder_id}' in parents and trashed = false"
        results = service.files().list(
            q=query,
            pageSize=50,
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        items = results.get('files', [])

        for item in items:
            if item['mimeType'] == 'application/vnd.google-apps.folder':
                continue
            file_id = item['id']
            file_name = item['name']
            file_path = os.path.join(UPLOAD_DIR, file_name)

            request = service.files().get_media(fileId=file_id)
            with open(file_path, 'wb') as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

            downloaded_files.append({"name": file_name, "path": file_path})
    except Exception as e:
        print(f"❌ Ошибка скачивания из папки Google Диск: {e}")

    return downloaded_files


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
        <title>Accounting Agent — Главная</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
            .card {{ background-color: #161b22; border: 1px solid #30363d; color: #c9d1d9; border-radius: 10px; }}
            .form-control, .form-select {{ background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; }}
            .form-control:focus, .form-select:focus {{ background-color: #0d1117; color: #c9d1d9; border-color: #58a6ff; box-shadow: none; }}
            .custom-table {{ width: 100%; border-collapse: collapse; color: #c9d1d9; }}
            .custom-table thead th {{ background-color: #21262d; border-bottom: 1px solid #30363d; padding: 10px; font-size: 0.85rem; }}
            .custom-table tbody td {{ border-bottom: 1px solid #30363d; padding: 10px; }}
            .metric-box {{ background-color: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 15px; }}
            .chat-box {{ height: 210px; overflow-y: auto; background-color: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 10px; font-size: 0.85rem; }}
        </style>
    </head>
    <body class="p-4">
        <div class="container-fluid">
            <div class="d-flex justify-content-between align-items-center mb-4 pb-3 border-bottom border-secondary">
                <h3 class="text-white m-0">Accounting Agent — Реестр документов</h3>
                <div class="d-flex gap-2">
                    <a href="/api/export/excel" class="btn btn-success btn-sm">📊 Скачать Excel</a>
                    <a href="/logout" class="btn btn-outline-danger btn-sm">Выйти</a>
                </div>
            </div>

            <!-- ВЕРХНИЙ БЛОК: ИСТОЧНИКИ СЛЕВА, ОПЕРАЦИОННЫЙ ДАШБОРД + AI СПРАВА -->
            <div class="row g-4 mb-4">
                <div class="col-md-5">
                    <div class="card p-4 shadow-sm h-100">
                        <h5 class="text-white mb-3">📥 Источники документов</h5>
                        <form action="/process-documents" method="post" enctype="multipart/form-data">
                            <div class="mb-3">
                                <label class="form-label text-secondary small">Ссылка на папку Google Диск:</label>
                                <input type="text" name="gdrive_link" class="form-control" placeholder="https://drive.google.com/drive/folders/...">
                            </div>
                            <div class="text-center text-secondary my-2 small">— ИЛИ —</div>
                            <div class="mb-4">
                                <label class="form-label text-secondary small">Прикрепить файлы с ПК (PDF, сканы, фото):</label>
                                <input type="file" name="files" multiple class="form-control">
                            </div>
                            <button type="submit" class="btn btn-primary w-100 py-2 fw-semibold">🚀 Запустить анализ</button>
                        </form>
                    </div>
                </div>

                <div class="col-md-7">
                    <div class="card p-4 shadow-sm h-100 d-flex flex-column justify-content-between">
                        <div>
                            <div class="d-flex justify-content-between align-items-center mb-3">
                                <h5 class="text-white m-0">📊 Операционный дашборд и ИИ-Ассистент</h5>
                                <span class="badge bg-primary">OpenRouter API</span>
                            </div>
                            <div class="row text-center g-2 mb-3">
                                <div class="col-4">
                                    <div class="metric-box">
                                        <h4 class="text-primary fw-bold mb-0">99.4%</h4>
                                        <div class="text-secondary" style="font-size: 0.75rem;">Точность</div>
                                    </div>
                                </div>
                                <div class="col-4">
                                    <div class="metric-box">
                                        <h4 class="text-success fw-bold mb-0">~4.5 мин</h4>
                                        <div class="text-secondary" style="font-size: 0.75rem;">Экономия</div>
                                    </div>
                                </div>
                                <div class="col-4">
                                    <div class="metric-box">
                                        <h4 class="text-info fw-bold mb-0">Активен</h4>
                                        <div class="text-secondary" style="font-size: 0.75rem;">Статус ИИ</div>
                                    </div>
                                </div>
                            </div>
                            <div id="chatMessages" class="chat-box mb-2">
                                <div class="text-muted">Ассистент: Задайте вопрос по базе, например: <em>«Выведи накладные за прошлый месяц»</em>.</div>
                            </div>
                        </div>
                        <div class="input-group input-group-sm">
                            <input type="text" id="chatInput" class="form-control" placeholder="Спросить ассистента...">
                            <button class="btn btn-primary" type="button" onclick="sendAiQuery()">Спросить</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- НИЖНИЙ БЛОК: ФИЛЬТРЫ И ЖУРНАЛ -->
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
                                <th style="width: 220px;">Отправитель</th>
                                <th style="width: 200px;">Получатель</th>
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

                chatBox.innerHTML += `<div class="mb-2 text-white"><strong>Вы:</strong> ${prompt}</div>`;
                input.value = '';
                chatBox.scrollTop = chatBox.scrollHeight;

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
                        chatBox.innerHTML += `<div class="mb-2 text-danger"><strong>Ошибка:</strong> ${data.detail || 'Ошибка ответа'}</div>`;
                    }
                } catch(err) {
                    document.getElementById(loadingId).remove();
                    chatBox.innerHTML += `<div class="mb-2 text-danger"><strong>Ошибка сети</strong></div>`;
                }
                chatBox.scrollTop = chatBox.scrollHeight;
            }

            document.getElementById('chatInput').addEventListener('keypress', function (e) {
                if (e.key === 'Enter') sendAiQuery();
            });
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/process-documents", response_class=HTMLResponse)
async def process_documents(
    request: Request,
    gdrive_link: Optional[str] = Form(None),
    files: List[UploadFile] = File(None),
    _=Depends(verify_session)
):
    all_docs = []

    if gdrive_link and gdrive_link.strip():
        drive_files = fetch_files_from_drive_folder(gdrive_link)
        all_docs.extend(drive_files)

    if files:
        for file in files:
            if file.filename:
                file_path = os.path.join(UPLOAD_DIR, file.filename)
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                all_docs.append({"name": file.filename, "path": file_path})

    processed_rows = []

    for index, doc in enumerate(all_docs, start=1):
        file_name = doc["name"]
        file_path = doc["path"]
        parsed_data = {}
        doc_type_key = "waybill"

        content_parts = []
        try:
            if file_path.lower().endswith('.pdf'):
                doc_pdf = fitz.open(file_path)
                for page_num in range(len(doc_pdf)):
                    page = doc_pdf[page_num]
                    pix = page.get_pixmap(dpi=150)
                    img_bytes = pix.tobytes("jpeg")
                    encoded_img = base64.b64encode(img_bytes).decode('utf-8')
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded_img}"}
                    })
            elif file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                with open(file_path, "rb") as img_file:
                    encoded_img = base64.b64encode(img_file.read()).decode('utf-8')
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded_img}"}
                    })
            else:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    text_content = f.read()
                    content_parts.append({"type": "text", "text": f"Содержимое:\n{text_content}"})
        except Exception:
            pass

        if OPENROUTER_API_KEY and content_parts:
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://aa.depot.by",
                "X-Title": "Accounting Agent",
                "Content-Type": "application/json"
            }

            async with httpx.AsyncClient(timeout=90.0) as client:
                try:
                    class_payload = {
                        "model": GEMINI_MODEL,
                        "messages": [
                            {"role": "system", "content": "Определи тип документа. Верни строго JSON вида: {\"type\": \"waybill\"} для накладных, {\"type\": \"act\"} для актов, либо {\"type\": \"receipt\"} для чеков."},
                            {"role": "user", "content": [{"type": "text", "text": "Классифицируй:"}] + content_parts}
                        ],
                        "response_format": {"type": "json_object"}
                    }
                    class_resp = await client.post(OPENROUTER_URL, json=class_payload, headers=headers)
                    if class_resp.status_code == 200:
                        try:
                            doc_type_key = json.loads(class_resp.json()["choices"][0]["message"]["content"]).get("type", "waybill")
                        except Exception:
                            pass

                    if doc_type_key == "waybill":
                        prompt_text = "Товарная накладная (ТН/ТТН). Извлеки в JSON: {\"тип_документа\": \"ТН-2 / ТТН-1\", \"дата_накладной\": \"ДД.ММ.ГГГГ\", \"поставщик\": {\"наименование\": \"...\", \"унп\": \"...\"}, \"покупатель\": {\"наименование\": \"...\", \"унп\": \"...\"}, \"позиции\": [{\"наименование\": \"...\", \"всего_с_ндс\": 0.0}], \"итого\": {\"всего_с_ндс\": 0.0}}"
                    elif doc_type_key == "act":
                        prompt_text = "Акт. Извлеки в JSON: {\"тип_документа\": \"Акт\", \"дата_акта\": \"ДД.ММ.ГГГГ\", \"исполнитель\": {\"наименование\": \"...\", \"унп\": \"...\"}, \"заказчик\": {\"наименование\": \"...\", \"унп\": \"...\"}, \"услуги_позиции\": [{\"наименование_услуги_или_прав\": \"...\", \"всего_с_ндс\": 0.0}], \"итого\": {\"всего_с_ндс\": 0.0}}"
                    else:
                        prompt_text = "Чек. Извлеки в JSON: {\"тип_документа\": \"Чек\", \"дата_время\": \"...\", \"продавец\": {\"наименование\": \"...\", \"унп\": \"...\"}, \"позиции_товаров\": [{\"наименование\": \"...\", \"сумма\": 0.0}], \"итого_оплата\": 0.0}"

                    ext_payload = {
                        "model": GEMINI_MODEL,
                        "messages": [
                            {"role": "system", "content": "Извлеки данные строго в JSON."},
                            {"role": "user", "content": [{"type": "text", "text": prompt_text}] + content_parts}
                        ],
                        "response_format": {"type": "json_object"}
                    }

                    ext_resp = await client.post(OPENROUTER_URL, json=ext_payload, headers=headers)
                    if ext_resp.status_code == 200:
                        parsed_data = json.loads(ext_resp.json()["choices"][0]["message"]["content"])
                except Exception as e:
                    print(f"Ошибка парсинга: {e}")

        t_type = parsed_data.get("тип_документа", "Документ")
        doc_date = parsed_data.get("дата_накладной") or parsed_data.get("дата_акта") or parsed_data.get("дата_время", "")[:10]
        
        sender_obj = parsed_data.get("поставщик") or parsed_data.get("исполнитель") or parsed_data.get("продавец") or {}
        sender_name = sender_obj.get("наименование", "Не указан")
        sender_unp = sender_obj.get("унп", "")
        sender_str = f"{sender_name}" + (f" (УНП: {sender_unp})" if sender_unp else "")

        recipient_obj = parsed_data.get("покупатель") or parsed_data.get("заказчик") or {}
        recipient_name = recipient_obj.get("наименование", "Не указан") if isinstance(recipient_obj, dict) else str(recipient_obj)
        recipient_unp = recipient_obj.get("унп", "") if isinstance(recipient_obj, dict) else ""
        recipient_str = f"{recipient_name}" + (f" (УНП: {recipient_unp})" if recipient_unp else "")

        items = parsed_data.get("позиции", []) or parsed_data.get("услуги_позиции", []) or parsed_data.get("позиции_товаров", [])
        items_summary = ", ".join([str(i.get('наименование', i.get('наименование_услуги_или_прав', 'Товар'))) for i in items]) if items else "Позиции"

        if "ТН" in t_type: badge_html = '<span class="doc-badge badge-waybill">● Накладная</span>'
        elif "Акт" in t_type: badge_html = '<span class="doc-badge badge-act">● Акт</span>'
        else: badge_html = '<span class="doc-badge badge-receipt">● Чек</span>'

        default_intent = "internal" if "Акт" in t_type else ("expense" if "Чек" in t_type else "arrival")

        processed_rows.append({
            "id": index,
            "filename": file_name,
            "doc_date": doc_date,
            "badge_html": badge_html,
            "sender": sender_str,
            "recipient": recipient_str,
            "items_summary": items_summary,
            "default_intent": default_intent,
            "parsed_data": parsed_data
        })

    rows_html = ""
    for r in processed_rows:
        sel_arr = "selected" if r["default_intent"] == "arrival" else ""
        sel_int = "selected" if r["default_intent"] == "internal" else ""
        sel_exp = "selected" if r["default_intent"] == "expense" else ""

        rows_html += f"""
        <tr id="row-{r['id']}">
            <td class="text-center align-middle fw-bold">{r['id']}</td>
            <td class="align-middle">
                <div class="mb-1">{r['badge_html']}</div>
                <div class="small text-muted">{html.escape(r['filename'])}</div>
                <button class="btn btn-sm btn-outline-warning py-0 px-2 mt-1" style="font-size: 0.72rem;" onclick='openDetailsModal({r["id"]}, "{html.escape(r["filename"])}")'>🔍 Подробнее</button>
            </td>
            <td class="align-middle text-center">
                <a href="/files/{r['filename']}" target="_blank" class="btn btn-sm btn-outline-info py-0 px-2">👁 Файл</a>
            </td>
            <td class="align-middle small text-info fw-semibold">{html.escape(r['sender'])}</td>
            <td class="align-middle small">{html.escape(r['recipient'])}</td>
            <td class="align-middle small" style="max-width: 250px;">{html.escape(r['items_summary'])}</td>
            <td class="align-middle">
                <select id="intent-{r['id']}" class="form-select form-select-sm dark-select">
                    <option value="arrival" {sel_arr}>Приход</option>
                    <option value="internal" {sel_int}>Внутреннее потребление</option>
                    <option value="expense" {sel_exp}>Расход</option>
                </select>
            </td>
            <td class="align-middle text-center">
                <button class="btn btn-success btn-sm px-2 py-1" style="font-size: 0.8rem;" onclick='markProcessed({r["id"]}, "{r['filename']}")'>✅ Обработано</button>
            </td>
            <!-- СКРЫТЫЕ ДАННЫЕ ДЛЯ РЕДАКТИРОВАНИЯ В МОДАЛЬНОМ ОКНЕ -->
            <div id="data-json-{r['id']}" style="display:none;">{json.dumps(r['parsed_data'], ensure_ascii=False)}</div>
        </tr>
        """

    page_template = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Accounting Agent — Операционный реестр</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body { background-color: #12151a; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            .registry-card { background-color: #1c2128; border: 1px solid #30363d; border-radius: 12px; padding: 20px; }
            .custom-table { width: 100%; border-collapse: collapse; border-radius: 8px; overflow: hidden; }
            .custom-table thead th { background-color: #262c36; color: #e6edf3; font-weight: 600; font-size: 0.9rem; padding: 12px 14px; border-bottom: 1px solid #373e47; }
            .custom-table tbody td { padding: 12px 14px; border-bottom: 1px solid #30363d; color: #c9d1d9; background-color: #161b22; }
            .doc-badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; color: #fff; }
            .badge-waybill { background-color: #3da35d; }
            .badge-act { background-color: #3b82f6; }
            .badge-receipt { background-color: #eab308; color: #1f2328; }
            .dark-select { background-color: #21262d; color: #ffffff; border: 1px solid #373e47; border-radius: 6px; font-size: 0.85rem; }
        </style>
    </head>
    <body class="p-4">
        <div class="container-fluid">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <h3 class="text-white m-0 fw-normal">Accounting Agent — Операционный реестр</h3>
                <div class="d-flex gap-2">
                    <a href="/" class="btn btn-outline-light btn-sm px-3 py-2">⬅ На главную (Журнал)</a>
                </div>
            </div>

            <div class="registry-card shadow">
                <div class="table-responsive">
                    <table class="custom-table">
                        <thead>
                            <tr>
                                <th class="text-center" style="width: 45px;">#</th>
                                <th style="width: 170px;">Тип документа</th>
                                <th class="text-center" style="width: 90px;">Просмотр</th>
                                <th style="width: 210px;">Отправитель</th>
                                <th style="width: 200px;">Получатель</th>
                                <th>Список позиций</th>
                                <th style="width: 190px;">Назначение</th>
                                <th class="text-center" style="width: 130px;">Действие</th>
                            </tr>
                        </thead>
                        <tbody id="registryTableBody">
                            __ROWS_HTML__
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- МОДАЛЬНОЕ ОКНО SPLIT VIEW -->
        <div class="modal fade" id="detailsModal" tabindex="-1" aria-hidden="true">
          <div class="modal-dialog modal-xl modal-dialog-centered">
            <div class="modal-content bg-dark text-light border border-secondary" style="height: 85vh;">
              <div class="modal-header border-secondary py-2">
                <h5 class="modal-title fs-6" id="detailsModalLabel">Детали и редактирование документа</h5>
                <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
              </div>
              <div class="modal-body p-0 h-100">
                <div class="row g-0 h-100">
                  <div class="col-md-6 border-end border-secondary p-3 bg-black d-flex align-items-center justify-content-center" style="height: 100%;">
                    <iframe id="docPreviewFrame" src="" style="width: 100%; height: 100%; border: none; border-radius: 6px;"></iframe>
                  </div>
                  <div class="col-md-6 p-4 overflow-auto" style="height: 100%;">
                    <form id="editForm">
                        <input type="hidden" id="editRowId">
                        <input type="hidden" id="editFilename">
                        
                        <div class="mb-3">
                            <label class="form-label text-secondary small">Тип документа</label>
                            <input type="text" id="editDocType" class="form-control form-control-sm">
                        </div>
                        <div class="mb-3">
                            <label class="form-label text-secondary small">Дата документа</label>
                            <input type="text" id="editDocDate" class="form-control form-control-sm">
                        </div>
                        <h6 class="text-info mt-4 mb-2">Отправитель (Поставщик / Исполнитель)</h6>
                        <div class="row g-2 mb-3">
                            <div class="col-8">
                                <label class="form-label text-secondary small">Наименование</label>
                                <input type="text" id="editSenderName" class="form-control form-control-sm">
                            </div>
                            <div class="col-4">
                                <label class="form-label text-secondary small">УНП</label>
                                <input type="text" id="editSenderUnp" class="form-control form-control-sm">
                            </div>
                        </div>
                        <h6 class="text-warning mt-4 mb-2">Получатель (Покупатель / Заказчик)</h6>
                        <div class="row g-2 mb-3">
                            <div class="col-8">
                                <label class="form-label text-secondary small">Наименование</label>
                                <input type="text" id="editRecipientName" class="form-control form-control-sm">
                            </div>
                            <div class="col-4">
                                <label class="form-label text-secondary small">УНП</label>
                                <input type="text" id="editRecipientUnp" class="form-control form-control-sm">
                            </div>
                        </div>

                        <div class="mt-4 text-end">
                            <button type="button" class="btn btn-success btn-sm px-4" onclick="saveAndProcessFromModal()">💾 Сохранить и обработать</button>
                        </div>
                    </form>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            let currentParsedData = null;

            function openDetailsModal(id, filename) {
                document.getElementById('editRowId').value = id;
                document.getElementById('editFilename').value = filename;
                document.getElementById('docPreviewFrame').src = '/files/' + filename;
                document.getElementById('detailsModalLabel').innerText = 'Редактирование: ' + filename;

                const jsonDiv = document.getElementById('data-json-' + id);
                if (jsonDiv) {
                    try {
                        currentParsedData = JSON.parse(jsonDiv.innerText);
                        
                        document.getElementById('editDocType').value = currentParsedData.тип_документа || '';
                        document.getElementById('editDocDate').value = currentParsedData.дата_накладной || currentParsedData.дата_акта || currentParsedData.дата_время || '';

                        const sender = currentParsedData.поставщик || currentParsedData.исполнитель || currentParsedData.продавец || {};
                        document.getElementById('editSenderName').value = sender.наименование || '';
                        document.getElementById('editSenderUnp').value = sender.унп || '';

                        const recipient = currentParsedData.покупатель || currentParsedData.заказчик || {};
                        document.getElementById('editRecipientName').value = recipient.наименование || '';
                        document.getElementById('editRecipientUnp').value = recipient.унп || '';
                    } catch(e) {
                        console.error(e);
                    }
                }

                new bootstrap.Modal(document.getElementById('detailsModal')).show();
            }

            async function saveAndProcessFromModal() {
                const id = document.getElementById('editRowId').value;
                const filename = document.getElementById('editFilename').value;
                
                if (currentParsedData) {
                    currentParsedData.тип_документа = document.getElementById('editDocType').value;
                    if (currentParsedData.дата_накладной !== undefined) currentParsedData.дата_накладной = document.getElementById('editDocDate').value;
                    if (currentParsedData.дата_акта !== undefined) currentParsedData.дата_акта = document.getElementById('editDocDate').value;

                    let sender = currentParsedData.поставщик || currentParsedData.исполнитель || currentParsedData.продавец;
                    if (sender) {
                        sender.наименование = document.getElementById('editSenderName').value;
                        sender.унп = document.getElementById('editSenderUnp').value;
                    }

                    let recipient = currentParsedData.покупатель || currentParsedData.заказчик;
                    if (recipient && typeof recipient === 'object') {
                        recipient.наименование = document.getElementById('editRecipientName').value;
                        recipient.унп = document.getElementById('editRecipientUnp').value;
                    }
                }

                const senderStr = document.getElementById('editSenderName').value + (document.getElementById('editSenderUnp').value ? ' (УНП: ' + document.getElementById('editSenderUnp').value + ')' : '');
                const recipientStr = document.getElementById('editRecipientName').value + (document.getElementById('editRecipientUnp').value ? ' (УНП: ' + document.getElementById('editRecipientUnp').value + ')' : '');
                const docDate = document.getElementById('editDocDate').value;

                const intentSelect = document.getElementById('intent-' + id);
                const intent = intentSelect ? intentSelect.value : 'arrival';

                try {
                    const response = await fetch('/api/mark-processed', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filename, doc_date: docDate, sender: senderStr, recipient: recipientStr, intent, parsed_data: currentParsedData })
                    });
                    
                    if (response.ok) {
                        const modalEl = document.getElementById('detailsModal');
                        bootstrap.Modal.getInstance(modalEl).hide();

                        const row = document.getElementById('row-' + id);
                        if (row) {
                            row.style.transition = "opacity 0.4s ease";
                            row.style.opacity = "0";
                            setTimeout(() => {
                                row.remove();
                                const tbody = document.getElementById('registryTableBody');
                                if (tbody.children.length === 0) {
                                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-4 text-secondary">Все документы обработаны!</td></tr>';
                                }
                            }, 400);
                        }
                    } else {
                        alert('Ошибка при сохранении в журнал');
                    }
                } catch (err) {
                    console.error(err);
                    alert('Ошибка сети');
                }
            }

            async function markProcessed(id, filename) {
                const jsonDiv = document.getElementById('data-json-' + id);
                let parsedData = {};
                try { parsedData = JSON.parse(jsonDiv.innerText); } catch(e){}

                const sender = parsedData.поставщик || parsedData.исполнитель || parsedData.продавец || {};
                const senderStr = (sender.наименование || 'Не указан') + (sender.унп ? ' (УНП: ' + sender.унп + ')' : '');

                const recipient = parsedData.покупатель || parsedData.заказчик || {};
                const recipientName = typeof recipient === 'object' ? (recipient.наименование || 'Не указан') : recipient;
                const recipientUnp = typeof recipient === 'object' ? (recipient.унп || '') : '';
                const recipientStr = recipientName + (recipientUnp ? ' (УНП: ' + recipientUnp + ')' : '');

                const docDate = parsedData.дата_накладной || parsedData.дата_акта || parsedData.дата_время || '';
                const intentSelect = document.getElementById('intent-' + id);
                const intent = intentSelect ? intentSelect.value : 'arrival';

                try {
                    const response = await fetch('/api/mark-processed', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ filename, doc_date: docDate, sender: senderStr, recipient: recipientStr, intent, parsed_data: parsedData })
                    });
                    
                    if (response.ok) {
                        const row = document.getElementById('row-' + id);
                        if (row) {
                            row.style.transition = "opacity 0.4s ease";
                            row.style.opacity = "0";
                            setTimeout(() => {
                                row.remove();
                                const tbody = document.getElementById('registryTableBody');
                                if (tbody.children.length === 0) {
                                    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-4 text-secondary">Все документы обработаны!</td></tr>';
                                }
                            }, 400);
                        }
                    } else {
                        alert('Ошибка при сохранении в журнал');
                    }
                } catch (err) {
                    console.error(err);
                    alert('Ошибка сети');
                }
            }
        </script>
    </body>
    </html>
    """

    fallback_rows = '<tr><td colspan="8" class="text-center py-4 text-secondary">Нет документов для отображения</td></tr>'
    page_html = page_template.replace("__ROWS_HTML__", rows_html if rows_html else fallback_rows)

    return HTMLResponse(content=page_html)


@app.post("/api/mark-processed")
async def mark_processed(payload: dict, _=Depends(verify_session)):
    filename = payload.get("filename")
    doc_date = payload.get("doc_date", "")
    sender = payload.get("sender", "")
    recipient = payload.get("recipient", "")
    intent = payload.get("intent", "arrival")
    parsed_data = payload.get("parsed_data", {})

    doc_type = parsed_data.get("тип_документа", "Документ")
    items = parsed_data.get("позиции", []) or parsed_data.get("услуги_позиции", []) or parsed_data.get("позиции_товаров", [])
    items_summary = ", ".join([str(i.get('наименование', i.get('наименование_услуги_или_прав', 'Товар'))) for i in items])
    
    processed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    full_json = json.dumps(parsed_data, ensure_ascii=False)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO processed_journal (filename, doc_type, doc_date, processed_at, sender, recipient, items_summary, intent, full_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (filename, doc_type, doc_date, processed_at, sender, recipient, items_summary, intent, full_json))
    conn.commit()
    conn.close()

    # Ротация: удаление физического файла с диска сервера
    if filename:
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                print(f"Ошибка удаления файла {filename}: {e}")

    return {"status": "success"}


@app.post("/api/ai-chat")
async def ai_chat(query: ChatQuery, _=Depends(verify_session)):
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=400, detail="Не задан OPENROUTER_API_KEY на сервере.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, doc_type, doc_date, processed_at, sender, recipient, items_summary, intent FROM processed_journal ORDER BY id DESC LIMIT 50")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    context_text = json.dumps(rows, ensure_ascii=False, indent=2)

    system_prompt = (
        "Ты профессиональный бухгалтерский ИИ-помощник. У тебя есть доступ к базе данных обработанных первичных документов. "
        "Отвечай на вопросы пользователя четко, опираясь строго на представленные данные ниже.\n\n"
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


@app.get("/api/export/excel")
async def export_excel(_=Depends(verify_session)):
    filepath = export_processed_documents_to_excel()
    return FileResponse(
        filepath,
        filename=filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
