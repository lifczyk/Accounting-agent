import os
from dotenv import load_dotenv

load_dotenv()

# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL_CLASSIFY = os.getenv("OPENROUTER_MODEL_CLASSIFY", "google/gemini-2.5-flash-lite")
OPENROUTER_MODEL_EXTRACT = os.getenv("OPENROUTER_MODEL_EXTRACT", "google/gemini-2.5-flash-lite")
CHAT_MODEL = os.getenv("OPENROUTER_MODEL_CHAT", "qwen/qwen-2.5-7b-instruct")

# Google Drive
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_DRIVE_DONE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_DONE_FOLDER_ID")

# 1С
ONEC_BASE_URL = os.getenv("ONEC_BASE_URL")
ONEC_USER = os.getenv("ONEC_USER")
ONEC_PASSWORD = os.getenv("ONEC_PASSWORD")

# Пороги и интервалы
CLASSIFICATION_CONFIDENCE_THRESHOLD = int(os.getenv("CLASSIFICATION_CONFIDENCE_THRESHOLD", 70))
FIELD_CONFIDENCE_THRESHOLD = int(os.getenv("FIELD_CONFIDENCE_THRESHOLD", 85))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 180))

# Хранилища и папки
STORE_PATH = os.getenv("STORE_PATH", "agent_store.db")
UPLOAD_DIR = "/tmp/accounting_uploads"
DB_PATH = "accounting_journal.db"

# Автосоздание необходимых директорий
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("templates", exist_ok=True)
