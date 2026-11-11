from fastapi import FastAPI, Depends, Form, HTTPException, Request, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional
import os
import httpx
import shutil

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting IDP Operator Interface", version="1.0.0")

store = DocumentStore()
learning_db = LearningDatabase()

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "/tmp/accounting_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "depot2026")
SESSION_COOKIE_NAME = "session_token"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "google/gemini-2.5-flash")

class ItemCorrection(BaseModel):
    line_number: int
    name: str
    quantity: float
    price: float
    total_amount: float
    intent: str
    account: str

class DocumentApprovalPayload(BaseModel):
    document_id: str
    global_intent: str
    items: List[ItemCorrection]

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
async def dashboard(request: Request, _ = Depends(verify_session)):
    return templates.TemplateResponse(request, "index.html", {})

@app.post("/process-documents", response_class=HTMLResponse)
async def process_documents(
    request: Request,
    gdrive_link: Optional[str] = Form(None),
    files: List[UploadFile] = File(None),
    _ = Depends(verify_session)
):
    extracted_text = ""

    if gdrive_link and gdrive_link.strip():
        extracted_text += f"\n[Источник: Google Диск]\nСсылка: {gdrive_link.strip()}\n"

    if files:
        for file in files:
            if file.filename:
                file_path = os.path.join(UPLOAD_DIR, file.filename)
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                extracted_text += f"\n[Источник: Файл с ПК]\nИмя файла: {file.filename}\n"

    ai_response_text = "Документы не переданы или отсутствует OPENROUTER_API_KEY."

    if OPENROUTER_API_KEY and extracted_text.strip():
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://aa.depot.by",
            "X-Title": "Accounting Agent",
            "Content-Type": "application/json"
        }
        payload = {
            "model": GEMINI_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "Ты профессиональный бухгалтерский ассистент. Проанализируй данные первичных документов."
                },
                {
                    "role": "user",
                    "content": f"Вот данные первичных документов для распознавания:\n{extracted_text}"
                }
            ]
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                if response.status_code == 200:
                    res_json = response.json()
                    ai_response_text = res_json["choices"][0]["message"]["content"]
                else:
                    ai_response_text = f"Ошибка OpenRouter API ({response.status_code}): {response.text}"
            except Exception as e:
                ai_response_text = f"Ошибка подключения к OpenRouter: {str(e)}"

    return templates.TemplateResponse(request, "result.html", {
        "result": ai_response_text,
        "source_info": extracted_text
    })

@app.get("/api/export/excel")
async def export_excel(_ = Depends(verify_session)):
    filepath = export_processed_documents_to_excel()
    return FileResponse(filepath, filename=filepath, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.post("/api/documents/approve")
async def approve_document(payload: DocumentApprovalPayload, _ = Depends(verify_session)):
    for item in payload.items:
        learning_db.save_correction(item.name, item.intent, item.account)
    return {"status": "success", "message": "Документ записан, ИИ обучен на правилах оператора"}
