from fastapi import FastAPI, Depends, Form, HTTPException, Request, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional
import os
import io
import re
import httpx
import shutil

# Импорты для работы с Google Drive API
from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting Agent", version="1.1.0")

store = DocumentStore()
learning_db = LearningDatabase()

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

UPLOAD_DIR = "/tmp/accounting_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Berlin2015!")
SESSION_COOKIE_NAME = "session_token"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_MODEL = os.getenv("OPENROUTER_MODEL_EXTRACT", "google/gemini-2.5-flash-lite")

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
    """Скачивает все файлы из папки Google Диска в UPLOAD_DIR."""
    downloaded_files = []
    service = get_drive_service()
    if not service:
        return downloaded_files

    # Извлекаем ID папки из ссылки
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', folder_url_or_id)
    folder_id = match.group(1) if match else folder_url_or_id.strip()

    try:
        query = f"'{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, pageSize=50, fields="files(id, name, mimeType)").execute()
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
        print(f"Ошибка скачивания из папки Drive: {e}")

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
async def dashboard(request: Request, _ = Depends(verify_session)):
    return templates.TemplateResponse(request, "index.html", {})

@app.post("/process-documents", response_class=HTMLResponse)
async def process_documents(
    request: Request,
    gdrive_link: Optional[str] = Form(None),
    files: List[UploadFile] = File(None),
    _ = Depends(verify_session)
):
    all_docs = []

    # 1. Загрузка папки с Google Диска
    if gdrive_link and gdrive_link.strip():
        drive_files = fetch_files_from_drive_folder(gdrive_link)
        all_docs.extend(drive_files)

    # 2. Загрузка файлов с локального ПК
    if files:
        for file in files:
            if file.filename:
                file_path = os.path.join(UPLOAD_DIR, file.filename)
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                all_docs.append({"name": file.filename, "path": file_path})

    batch_results = []

    # 3. Потоковая обработка каждого документа в пачке
    for doc in all_docs:
        file_name = doc["name"]
        ai_response_text = "Ошибка распознавания."

        if OPENROUTER_API_KEY:
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
                        "content": "Ты профессиональный бухгалтерский агент. Извлеки из накладной данные в формате JSON или структурированного отчета: поставщик, дата, номер, список позиций (наименование, кол-во, цена, сумма, счет учета)."
                    },
                    {
                        "role": "user",
                        "content": f"Проанализируй накладную: {file_name}"
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
                        ai_response_text = f"Ошибка API ({response.status_code}): {response.text}"
                except Exception as e:
                    ai_response_text = f"Ошибка сети: {str(e)}"

        batch_results.append({
            "filename": file_name,
            "status": "Требует проверки",
            "result": ai_response_text
        })

    return templates.TemplateResponse(request, "result.html", {
        "batch_results": batch_results,
        "total_processed": len(batch_results)
    })

@app.get("/api/export/excel")
async def export_excel(_ = Depends(verify_session)):
    filepath = export_processed_documents_to_excel()
    return FileResponse(filepath, filename=filepath, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
