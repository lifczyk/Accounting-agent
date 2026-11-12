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
import base64
import json

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload
import fitz  # PyMuPDF

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting Agent", version="2.4.0")

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
    downloaded_files = []
    service = get_drive_service()
    if not service:
        print("❌ Google Drive service не инициализирован.")
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
        print(f"❌ Ошибка скачивания из папки Google Drive: {e}")

    return downloaded_files

def validate_document_math(parsed_data: dict) -> list:
    """Автоматическая проверка математических расхождений сумм и НДС"""
    errors = []
    try:
        positions = parsed_data.get("позиции", []) or parsed_data.get("услуги_позиции", []) or parsed_data.get("позиции_товаров", [])
        
        calc_total = 0.0
        for p in positions:
            item_sum = float(p.get("всего_с_ндс", p.get("сумма", 0)) or 0)
            calc_total += item_sum
            
        itogo_block = parsed_data.get("итого", {})
        stated_total = float(itogo_block.get("всего_с_ндс", parsed_data.get("итого_оплата", calc_total)) or 0)
        
        if stated_total > 0 and abs(calc_total - stated_total) > 0.05:
            errors.append(f"Расхождение сумм: сумма строк ({calc_total:.2f}) не равна итоговой сумме в документе ({stated_total:.2f}).")
    except Exception as e:
        print(f"Ошибка валидации математики: {e}")
    return errors

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

    batch_results = []

    for doc in all_docs:
        file_name = doc["name"]
        file_path = doc["path"]
        ai_response_text = "Ошибка распознавания."
        doc_status = "Требует проверки"

        content_parts = []
        try:
            if file_path.endswith('.pdf'):
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
                    content_parts.append({"type": "text", "text": f"Текстовое содержимое:\n{text_content}"})
        except Exception as e:
            print(f"Ошибка подготовки файла {file_name}: {e}")

        if not OPENROUTER_API_KEY:
            batch_results.append({"filename": file_name, "status": "Ошибка", "result": "API ключ OpenRouter не задан."})
            continue

        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://aa.depot.by",
            "X-Title": "Accounting Agent",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            try:
                # 1. ШАГ КЛАССИФИКАЦИИ ДОКУМЕНТА
                classifier_payload = {
                    "model": GEMINI_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Определи тип документа. Верни строго JSON вида: {\"type\": \"waybill\"} для накладных (ТН/ТТН), {\"type\": \"act\"} для актов услуг/работ/прав, либо {\"type\": \"receipt\"} для чеков."
                        },
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "Классифицируй этот документ:"}] + content_parts
                        }
                    ],
                    "response_format": {"type": "json_object"}
                }

                class_resp = await client.post(OPENROUTER_URL, json=classifier_payload, headers=headers)
                doc_type = "waybill"
                if class_resp.status_code == 200:
                    try:
                        res_data = class_resp.json()
                        content_json = json.loads(res_data["choices"][0]["message"]["content"])
                        doc_type = content_json.get("type", "waybill")
                    except:
                        pass

                # 2. ШАГ ИЗВЛЕЧЕНИЯ ДАННЫХ ПО НЕЙТРАЛЬНОМУ ПРОМПТУ
                if doc_type == "waybill":
                    prompt_text = (
                        "Ты профессиональный бухгалтерский ИИ-ассистент по первичным документам в Республике Беларусь. "
                        "Это Товарная накладная (ТН-2 / ТТН-1). Извлеки точные данные строго из текста документа в формате JSON с учетом правил:\n"
                        "1. 'серия_накладной': ТОЛЬКО буквенная серия (две заглавные русские буквы перед номером накладной, например ЭЮ, АК, АВ). Запрещено записывать сюда цифры экземпляров вроде '2-й экз.'!\n"
                        "2. 'номер_накладной': цифровой номер документа.\n"
                        "3. 'поставщик' (Грузоотправитель) и 'покупатель' (Грузополучатель): извлекай реальные наименования и их 9-значные УНП строго из шапки/таблички документа. Ничего не подменяй.\n"
                        "4. 'код_формы': ищи в самом низу накладной мелким шрифтом (например, Код формы 401951).\n"
                        "5. В позициях обязательно заполняй поле 'единица_измерения' (шт, кг и т.д.).\n\n"
                        "Верни результат в формате JSON:\n"
                        "{\n"
                        '  "тип_документа": "ТН-2 / ТТН-1",\n'
                        '  "серия_накладной": "...",\n'
                        '  "номер_накладной": "...",\n'
                        '  "дата_накладной": "ДД.ММ.ГГГГ",\n'
                        '  "код_формы": "...",\n'
                        '  "поставщик": {"наименование": "...", "унп": "..."},\n'
                        '  "покупатель": {"наименование": "...", "унп": "..."},\n'
                        '  "основание": "...",\n'
                        '  "позиции": [{\n'
                        '    "наименование": "...",\n'
                        '    "единица_измерения": "...",\n'
                        '    "количество": 0.0,\n'
                        '    "цена_за_единицу": 0.0,\n'
                        '    "сумма_без_ндс": 0.0,\n'
                        '    "ставка_ндс": "...",\n'
                        '    "сумма_ндс": 0.0,\n'
                        '    "всего_с_ндс": 0.0,\n'
                        '    "страна_происхождения": "...",\n'
                        '    "номер_гтд": "...",\n'
                        '    "счет_учета": "10/41/08"\n'
                        '  }],\n'
                        '  "итого": {"сумма_без_ндс": 0.0, "сумма_ндс": 0.0, "всего_с_ндс": 0.0}\n'
                        "}"
                    )
                elif doc_type == "act":
                    prompt_text = (
                        "Ты профессиональный бухгалтерский ИИ-ассистент в РБ. "
                        "Это Акт оказанных услуг / выполненных работ / передачи прав. Извлеки данные строго из документа в JSON:\n"
                        "{\n"
                        '  "тип_документа": "Акт выполненных работ / оказанных услуг / передачи прав",\n'
                        '  "номер_акта": "...",\n'
                        '  "дата_акта": "ДД.ММ.ГГГГ",\n'
                        '  "исполнитель": {"наименование": "...", "унп": "..."},\n'
                        '  "заказчик": {"наименование": "...", "унп": "..."},\n'
                        '  "основание_договор": "...",\n'
                        '  "услуги_позиции": [{"наименование_услуги_или_прав": "...", "сумма_без_ндс": 0.0, "ставка_ндс": "...", "сумма_ндс": 0.0, "всего_с_ндс": 0.0, "счет_затрат": "20/26/44"}],\n'
                        '  "итого": {"сумма_без_ндс": 0.0, "сумма_ндс": 0.0, "всего_с_ндс": 0.0}\n'
                        "}"
                    )
                else:
                    prompt_text = (
                        "Ты бухгалтерский агент. Это Кассовый чек. Извлеки данные строго в JSON:\n"
                        "{\n"
                        '  "тип_документа": "Кассовый чек",\n'
                        '  "номер_чека_или_фд": "...",\n'
                        '  "дата_время": "...",\n'
                        '  "продавец": {"наименование": "...", "унп": "..."},\n'
                        '  "позиции_товаров": [{"наименование": "...", "количество": 0.0, "цена": 0.0, "сумма": 0.0}],\n'
                        '  "итого_оплата": 0.0\n'
                        "}"
                    )

                extraction_payload = {
                    "model": GEMINI_MODEL,
                    "messages": [
                        {"role": "system", "content": "Ты точный бухгалтерский ИИ. Возвращай результат исключительно в формате валидного JSON."},
                        {"role": "user", "content": [{"type": "text", "text": prompt_text}] + content_parts}
                    ],
                    "response_format": {"type": "json_object"}
                }

                response = await client.post(OPENROUTER_URL, json=extraction_payload, headers=headers)
                if response.status_code == 200:
                    res_json = response.json()
                    ai_response_text = res_json["choices"][0]["message"]["content"]
                    
                    # 3. ШАГ ВАЛИДАЦИИ
                    try:
                        parsed_res = json.loads(ai_response_text)
                        math_errors = validate_document_math(parsed_res)
                        if math_errors:
                            doc_status = "Ошибка матем."
                            parsed_res["_validation_errors"] = math_errors
                            ai_response_text = json.dumps(parsed_res, ensure_ascii=False, indent=2)
                    except:
                        pass
                else:
                    ai_response_text = f"Ошибка API ({response.status_code}): {response.text}"
                    doc_status = "Ошибка"
            except Exception as e:
                ai_response_text = f"Ошибка сети: {str(e)}"
                doc_status = "Ошибка"

        batch_results.append({
            "filename": file_name,
            "status": doc_status,
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
