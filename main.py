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

app = FastAPI(title="Accounting Agent", version="1.3.0")

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
    sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service
