"""
Приём документов из Google Drive.

Механизм: поллинг (не push-уведомления) — проще в эксплуатации на старте,
не требует публичного HTTPS-эндпоинта и переподписки канала раз в 7 дней.

Файл считается "новым", если на нём нет appProperties.processing_status.
После взятия в обработку агент сразу проставляет processing_status=in_progress,
чтобы следующий цикл поллинга не схватил тот же файл повторно.
"""
import io

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from config import GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_DRIVE_FOLDER_ID, GOOGLE_DRIVE_DONE_FOLDER_ID

SCOPES = ["https://www.googleapis.com/auth/drive"]

SUPPORTED_MIME_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "application/pdf": "application/pdf",  # первая страница; см. note в README про многостраничные PDF
}


def get_service():
    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


def list_new_files(service) -> list[dict]:
    """Возвращает файлы в целевой папке без метки processing_status."""
    if not GOOGLE_DRIVE_FOLDER_ID:
        raise RuntimeError("GOOGLE_DRIVE_FOLDER_ID не задан в .env")

    query = f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents and trashed = false"
    result = service.files().list(
        q=query,
        fields="files(id, name, mimeType, appProperties, md5Checksum)",
        pageSize=100,
    ).execute()

    files = result.get("files", [])
    return [
        f for f in files
        if not f.get("appProperties", {}).get("processing_status")
        and f.get("mimeType") in SUPPORTED_MIME_TYPES
    ]


def download_file(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def mark_in_progress(service, file_id: str, record_id: str) -> None:
    service.files().update(
        fileId=file_id,
        body={"appProperties": {"processing_status": "in_progress", "record_id": record_id}},
    ).execute()


def mark_done(service, file_id: str, success: bool = True) -> None:
    service.files().update(
        fileId=file_id,
        body={"appProperties": {"processing_status": "done" if success else "failed"}},
    ).execute()

    if success and GOOGLE_DRIVE_DONE_FOLDER_ID:
        file_meta = service.files().get(fileId=file_id, fields="parents").execute()
        prev_parents = ",".join(file_meta.get("parents", []))
        service.files().update(
            fileId=file_id,
            addParents=GOOGLE_DRIVE_DONE_FOLDER_ID,
            removeParents=prev_parents,
            fields="id, parents",
        ).execute()
