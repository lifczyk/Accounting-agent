import os
from typing import List
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting IDP Operator Interface")
store = DocumentStore()
learning_db = LearningDatabase()

os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

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

# Главная страница: передаем request внутрь словаря контекста
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # В новых версиях Starlette request передается первым аргументом
    return templates.TemplateResponse(request, "index.html", {})

@app.get("/api/export/excel")
async def export_excel():
    filepath = export_processed_documents_to_excel()
    return FileResponse(filepath, filename=filepath, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@app.post("/api/documents/approve")
async def approve_document(payload: DocumentApprovalPayload):
    for item in payload.items:
        learning_db.save_correction(item.name, item.intent, item.account)
    return {"status": "success", "message": "Документ записан, ИИ обучен на правилах оператора"}
