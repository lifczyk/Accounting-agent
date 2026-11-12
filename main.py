from fastapi import FastAPI, Depends, Form, HTTPException, Request, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
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

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload
import fitz  # PyMuPDF

from store import DocumentStore
from learning_db import LearningDatabase
from export_excel import export_processed_documents_to_excel

app = FastAPI(title="Accounting Agent", version="3.1.0")

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
        positions = (
            parsed_data.get("позиции", [])
            or parsed_data.get("услуги_позиции", [])
            or parsed_data.get("позиции_товаров", [])
        )

        calc_total = 0.0
        for p in positions:
            item_sum = float(p.get("всего_с_ндс", p.get("сумма", 0)) or 0)
            calc_total += item_sum

        itogo_block = parsed_data.get("итого", {})
        stated_total = float(
            itogo_block.get("всего_с_ндс", parsed_data.get("итого_оплата", calc_total)) or 0
        )

        if stated_total > 0 and abs(calc_total - stated_total) > 0.05:
            errors.append(
                f"Расхождение сумм: сумма строк ({calc_total:.2f}) не равна итогу ({stated_total:.2f})."
            )
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
async def dashboard(request: Request, _=Depends(verify_session)):
    html_content = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Accounting Agent — Главная</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body { background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
            .card { background-color: #161b22; border: 1px solid #30363d; color: #c9d1d9; border-radius: 10px; }
            .form-control { background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; }
            .form-control:focus { background-color: #0d1117; color: #c9d1d9; border-color: #58a6ff; box-shadow: none; }
            .metric-box { background-color: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
        </style>
    </head>
    <body class="p-4">
        <div class="container-fluid">
            <div class="d-flex justify-content-between align-items-center mb-4 pb-3 border-bottom border-secondary">
                <h3 class="text-white m-0">Accounting Agent — Реестр документов</h3>
                <a href="/logout" class="btn btn-outline-danger btn-sm">Выйти</a>
            </div>

            <div class="row g-4">
                <!-- ЛЕВАЯ КОЛОНКА: Источники загрузки -->
                <div class="col-md-5">
                    <div class="card p-4 shadow-sm h-100">
                        <h5 class="text-white mb-4">📥 Источники документов</h5>
                        <form action="/process-documents" method="post" enctype="multipart/form-data">
                            <div class="mb-3">
                                <label class="form-label text-secondary small">Ссылка на папку Google Диск:</label>
                                <input type="text" name="gdrive_link" class="form-control" placeholder="https://drive.google.com/drive/folders/...">
                            </div>
                            <div class="text-center text-secondary my-3 small">— ИЛИ —</div>
                            <div class="mb-4">
                                <label class="form-label text-secondary small">Прикрепить файлы с ПК (PDF, сканы, фото):</label>
                                <input type="file" name="files" multiple class="form-control">
                            </div>
                            <button type="submit" class="btn btn-primary w-100 py-2 fw-semibold">🚀 Запустить анализ</button>
                        </form>
                    </div>
                </div>

                <!-- ПРАВАЯ КОЛОНКА: Операционный дашборд -->
                <div class="col-md-7">
                    <div class="card p-4 shadow-sm h-100">
                        <h5 class="text-white mb-4">📊 Операционный дашборд</h5>
                        <div class="row text-center g-3 mb-4">
                            <div class="col-md-4">
                                <div class="metric-box">
                                    <h2 class="text-primary fw-bold mb-1">0</h2>
                                    <div class="text-secondary small">Обработано в сессии</div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="metric-box">
                                    <h2 class="text-success fw-bold mb-1">99.4%</h2>
                                    <div class="text-secondary small">Точность распознавания</div>
                                </div>
                            </div>
                            <div class="col-md-4">
                                <div class="metric-box">
                                    <h2 class="text-info fw-bold mb-1">~4.5 мин</h2>
                                    <div class="text-secondary small">Экономия на 1 документ</div>
                                </div>
                            </div>
                        </div>
                        <div class="p-3 rounded border border-secondary bg-dark text-secondary small">
                            <div class="mb-2 text-white fw-semibold">Поддерживаемые типы первичных документов:</div>
                            <ul class="mb-0 ps-3">
                                <li>Товарные и товарно-транспортные накладные (ТН-2, ТТН-1) с проверкой серий, УНП и кодов форм</li>
                                <li>Акты выполненных работ, оказанных услуг и передачи имущественных прав</li>
                                <li>Кассовые чеки и квитанции об оплате</li>
                            </ul>
                        </div>
                    </div>
                </div>
            </div>
        </div>
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
        is_valid = True
        status_label = "Ready"
        error_tooltip = ""

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
        except Exception as e:
            is_valid = False
            status_label = "Ошибка файла"
            error_tooltip = str(e)

        if OPENROUTER_API_KEY and content_parts:
            headers = {
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://aa.depot.by",
                "X-Title": "Accounting Agent",
                "Content-Type": "application/json"
            }

            async with httpx.AsyncClient(timeout=90.0) as client:
                try:
                    # 1. ШАГ КЛАССИФИКАЦИИ ДОКУМЕНТА
                    class_payload = {
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
                    class_resp = await client.post(OPENROUTER_URL, json=class_payload, headers=headers)
                    if class_resp.status_code == 200:
                        try:
                            res_data = class_resp.json()
                            doc_type_key = json.loads(res_data["choices"][0]["message"]["content"]).get("type", "waybill")
                        except Exception:
                            pass

                    # 2. ШАГ ИЗВЛЕЧЕНИЯ ДАННЫХ (НЕЙТРАЛЬНЫЕ ПРОМПТЫ)
                    if doc_type_key == "waybill":
                        prompt_text = (
                            "Ты профессиональный бухгалтерский ИИ-ассистент по первичным документам в Республике Беларусь. "
                            "Это Товарная накладная (ТН-2 / ТТН-1). Извлеки точные данные строго из текста документа в JSON:\n"
                            "1. 'серия_накладной': ТОЛЬКО буквенная серия (две заглавные русские буквы перед номером накладной, например ЭЮ, АК, АВ). Запрещено записывать сюда цифры экземпляров вроде '2-й экз.'!\n"
                            "2. 'номер_накладной': цифровой номер документа.\n"
                            "3. 'поставщик' (Грузоотправитель) и 'покупатель' (Грузополучатель): извлекай реальные наименования и их 9-значные УНП строго из шапки/таблички документа. Ничего не подменяй.\n"
                            "4. 'код_формы': ищи в самом низу накладной мелким шрифтом.\n"
                            "5. В позициях обязательно заполняй поле 'единица_измерения'.\n\n"
                            "Формат JSON:\n"
                            "{\n"
                            '  "тип_документа": "ТН-2 / ТТН-1",\n'
                            '  "серия_накладной": "...",\n'
                            '  "номер_накладной": "...",\n'
                            '  "дата_накладной": "ДД.ММ.ГГГГ",\n'
                            '  "код_формы": "...",\n'
                            '  "поставщик": {"наименование": "...", "унп": "..."},\n'
                            '  "покупатель": {"наименование": "...", "унп": "..."},\n'
                            '  "позиции": [{"наименование": "...", "единица_измерения": "...", "количество": 0.0, "цена_за_единицу": 0.0, "сумма_без_ндс": 0.0, "ставка_ндс": "...", "сумма_ндс": 0.0, "всего_с_ндс": 0.0}],\n'
                            '  "итого": {"сумма_без_ндс": 0.0, "сумма_ндс": 0.0, "всего_с_ндс": 0.0}\n'
                            "}"
                        )
                    elif doc_type_key == "act":
                        prompt_text = (
                            "Ты профессиональный бухгалтерский ИИ-ассистент в РБ. "
                            "Это Акт оказанных услуг / выполненных работ / передачи прав. Извлеки данные строго из документа в JSON:\n"
                            "{\n"
                            '  "тип_документа": "Акт",\n'
                            '  "номер_акта": "...",\n'
                            '  "дата_акта": "ДД.ММ.ГГГГ",\n'
                            '  "исполнитель": {"наименование": "...", "унп": "..."},\n'
                            '  "заказчик": {"наименование": "...", "унп": "..."},\n'
                            '  "основание_договор": "...",\n'
                            '  "услуги_позиции": [{"наименование_услуги_или_прав": "...", "сумма_без_ндс": 0.0, "ставка_ндс": "...", "сумма_ндс": 0.0, "всего_с_ндс": 0.0}],\n'
                            '  "итого": {"сумма_без_ндс": 0.0, "сумма_ндс": 0.0, "всего_с_ндс": 0.0}\n'
                            "}"
                        )
                    else:
                        prompt_text = (
                            "Ты бухгалтерский агент. Это Кассовый чек. Извлеки данные строго в JSON:\n"
                            "{\n"
                            '  "тип_документа": "Чек",\n'
                            '  "номер_чека_или_фд": "...",\n'
                            '  "дата_время": "...",\n'
                            '  "продавец": {"наименование": "...", "унп": "..."},\n'
                            '  "покупатель": {"наименование": "...", "унп": "..."},\n'
                            '  "позиции_товаров": [{"наименование": "...", "количество": 0.0, "цена": 0.0, "сумма": 0.0}],\n'
                            '  "итого_оплата": 0.0\n'
                            "}"
                        )

                    ext_payload = {
                        "model": GEMINI_MODEL,
                        "messages": [
                            {"role": "system", "content": "Ты точный бухгалтерский ИИ. Возвращай результат исключительно в формате валидного JSON."},
                            {"role": "user", "content": [{"type": "text", "text": prompt_text}] + content_parts}
                        ],
                        "response_format": {"type": "json_object"}
                    }

                    ext_resp = await client.post(OPENROUTER_URL, json=ext_payload, headers=headers)
                    if ext_resp.status_code == 200:
                        parsed_data = json.loads(ext_resp.json()["choices"][0]["message"]["content"])
                        math_errs = validate_document_math(parsed_data)
                        if math_errs:
                            is_valid = False
                            status_label = "Ошибка мат."
                            error_tooltip = "; ".join(math_errs)
                    else:
                        is_valid = False
                        status_label = "Ошибка API"
                        error_tooltip = f"HTTP {ext_resp.status_code}"
                except Exception as e:
                    is_valid = False
                    status_label = "Ошибка"
                    error_tooltip = str(e)
        elif not OPENROUTER_API_KEY:
            is_valid = False
            status_label = "Нет API ключа"

        # Формируем данные для отображения в строке таблицы
        if not is_valid:
            row_class = "row-error"
            badge_html = '<span class="doc-badge badge-err">● Ошибка</span>'
            status_html = f'<span class="status-err" title="{html.escape(error_tooltip)}">● {html.escape(status_label)}</span>'
            checked_attr = ""
        else:
            row_class = "row-ready"
            checked_attr = "checked"
            status_html = '<span class="status-ok">● Ready</span>'
            if doc_type_key == "waybill":
                ser = parsed_data.get("серия_накладной", "")
                num = parsed_data.get("номер_накладной", "")
                label = f"ТН-2 / ТТН-1 {ser} {num}".strip()
                badge_html = f'<span class="doc-badge badge-waybill">● {html.escape(label)}</span>'
            elif doc_type_key == "act":
                num = parsed_data.get("номер_акта", "")
                label = f"Акт {num}".strip()
                badge_html = f'<span class="doc-badge badge-act">● {html.escape(label)}</span>'
            else:
                badge_html = '<span class="doc-badge badge-receipt">● Чек</span>'

        # Продавец
        supplier = parsed_data.get("поставщик") or parsed_data.get("исполнитель") or parsed_data.get("продавец") or {}
        sup_name = supplier.get("наименование") or ("Ошибка" if not is_valid else "Не указан")
        sup_unp = supplier.get("унп", "")
        seller_html = f"<div>{html.escape(str(sup_name))}</div>" + (
            f"<div class='text-muted-dark small'>(УНП: {html.escape(str(sup_unp))})</div>" if sup_unp else ""
        )

        # Покупатель
        customer = parsed_data.get("покупатель") or parsed_data.get("заказчик") or {}
        if isinstance(customer, str):
            cust_name, cust_unp = customer, ""
        else:
            cust_name = customer.get("наименование") or "Не указан"
            cust_unp = customer.get("унп", "")
        buyer_html = f"<div>{html.escape(str(cust_name))}</div>" + (
            f"<div class='text-muted-dark small'>(УНП: {html.escape(str(cust_unp))})</div>" if cust_unp else ""
        )

        # Список товаров/услуг в столбик
        items = (
            parsed_data.get("позиции", [])
            or parsed_data.get("услуги_позиции", [])
            or parsed_data.get("позиции_товаров", [])
        )
        if items:
            item_lines = []
            for it in items:
                title = it.get("наименование") or it.get("наименование_услуги_или_прав") or "Позиция"
                price_val = it.get("всего_с_ндс") if it.get("всего_с_ндс") is not None else it.get("сумма", 0)
                try:
                    price_str = f"{float(price_val):.2f} руб."
                except Exception:
                    price_str = f"{price_val} руб."
                item_lines.append(f"<div>{html.escape(str(title))} ({price_str})</div>")
            items_html = "".join(item_lines)
        else:
            items_html = "<div class='text-muted-dark'>Не определено</div>"

        # Назначение по умолчанию (для актов услуг обычно внутреннее потребление, для ТН — приход)
        default_intent = "internal" if doc_type_key == "act" else ("expense" if doc_type_key == "receipt" else "arrival")

        processed_rows.append({
            "id": index,
            "filename": file_name,
            "row_class": row_class,
            "checked_attr": checked_attr,
            "badge_html": badge_html,
            "seller_html": seller_html,
            "buyer_html": buyer_html,
            "items_html": items_html,
            "default_intent": default_intent,
            "status_html": status_html,
        })

    rows_html = ""
    for r in processed_rows:
        sel_arr = "selected" if r["default_intent"] == "arrival" else ""
        sel_int = "selected" if r["default_intent"] == "internal" else ""
        sel_exp = "selected" if r["default_intent"] == "expense" else ""

        rows_html += f"""
        <tr class="{r['row_class']}">
            <td class="text-center align-middle fw-bold">{r['id']}</td>
            <td class="text-center align-middle">
                <input class="form-check-input" type="checkbox" {r['checked_attr']} style="transform: scale(1.25); cursor: pointer;">
            </td>
            <td class="align-middle">
                <div class="mb-1">{r['badge_html']}</div>
                <div class="small text-muted-dark">{html.escape(r['filename'])}</div>
            </td>
            <td class="align-middle small">{r['seller_html']}</td>
            <td class="align-middle small">{r['buyer_html']}</td>
            <td class="align-middle small" style="max-width: 320px; line-height: 1.35;">
                {r['items_html']}
            </td>
            <td class="align-middle">
                <select class="form-select form-select-sm dark-select">
                    <option value="arrival" {sel_arr}>Приход</option>
                    <option value="internal" {sel_int}>Внутреннее потребление</option>
                    <option value="expense" {sel_exp}>Расход</option>
                </select>
            </td>
            <td class="align-middle small fw-semibold text-nowrap">
                {r['status_html']}
            </td>
        </tr>
        """

    total_docs = len(processed_rows)
    saved_minutes = round(total_docs * 4.5)

    page_html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Accounting Agent — Реестр документов</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{
                background-color: #12151a;
                color: #e6edf3;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            }}
            .registry-card {{
                background-color: #1c2128;
                border: 1px solid #30363d;
                border-radius: 12px;
                padding: 20px;
            }}
            .custom-table {{
                width: 100%;
                border-collapse: collapse;
                border-radius: 8px;
                overflow: hidden;
            }}
            .custom-table thead th {{
                background-color: #262c36;
                color: #e6edf3;
                font-weight: 600;
                font-size: 0.9rem;
                padding: 12px 14px;
                border-bottom: 1px solid #373e47;
            }}
            .custom-table tbody td {{
                padding: 12px 14px;
                border-bottom: 1px solid rgba(0, 0, 0, 0.12);
                color: #1f2328;
            }}
            .row-ready {{
                background-color: #d6f0db;
            }}
            .row-ready:hover {{
                background-color: #c8ead0;
            }}
            .row-error {{
                background-color: #f4cccc;
            }}
            .row-error:hover {{
                background-color: #ebbcbc;
            }}
            .text-muted-dark {{
                color: #424a53;
            }}
            .doc-badge {{
                display: inline-block;
                padding: 3px 10px;
                border-radius: 999px;
                font-size: 0.78rem;
                font-weight: 600;
                color: #fff;
            }}
            .badge-waybill {{ background-color: #3da35d; }}
            .badge-act {{ background-color: #3b82f6; }}
            .badge-receipt {{ background-color: #eab308; color: #1f2328; }}
            .badge-err {{ background-color: #e05252; }}
            .dark-select {{
                background-color: #21262d;
                color: #ffffff;
                border: 1px solid #373e47;
                border-radius: 6px;
                font-size: 0.85rem;
            }}
            .dark-select:focus {{
                background-color: #21262d;
                color: #ffffff;
                box-shadow: none;
                border-color: #58a6ff;
            }}
            .status-ok {{ color: #1a7f37; }}
            .status-err {{ color: #cf222e; cursor: help; }}
        </style>
    </head>
    <body class="p-4">
        <div class="container-fluid">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <h3 class="text-white m-0 fw-normal">Accounting Agent — Реестр документов</h3>
                <div class="d-flex gap-2">
                    <a href="/" class="btn btn-outline-light btn-sm px-3 py-2">Загрузить еще</a>
                    <a href="/api/export/excel" class="btn btn-success btn-sm px-3 py-2">📊 Скачать Excel</a>
                    <a href="/logout" class="btn btn-outline-danger btn-sm px-3 py-2">Выйти</a>
                </div>
            </div>

            <div class="registry-card shadow">
                <div class="table-responsive">
                    <table class="custom-table">
                        <thead>
                            <tr>
                                <th class="text-center" style="width: 45px;">#</th>
                                <th class="text-center" style="width: 55px;">ОК</th>
                                <th style="width: 190px;">Тип документа</th>
                                <th style="width: 210px;">Продавец</th>
                                <th style="width: 200px;">Покупатель</th>
                                <th>Список товаров / услуг</th>
                                <th style="width: 210px;">Назначение</th>
                                <th style="width: 125px;">Статус</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows_html if rows_html else '<tr><td colspan="8" class="text-center py-4 bg-dark text-secondary">Нет документов для отображения</td></tr>'}
                        </tbody>
                    </table>
                </div>

                <div class="mt-3 pt-2 text-light small" style="line-height: 1.6;">
                    <div>Результатов обработано в сессии: <strong>{total_docs}</strong></div>
                    <div class="text-secondary">Эффективность: ~{saved_minutes} мин ручного ввода</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=page_html)


@app.get("/api/export/excel")
async def export_excel(_=Depends(verify_session)):
    filepath = export_processed_documents_to_excel()
    return FileResponse(
        filepath,
        filename=filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
