import os
from dotenv import load_dotenv

load_dotenv()

# OpenRouter
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL_CLASSIFY = os.getenv("OPENROUTER_MODEL_CLASSIFY", "google/gemini-2.5-flash-lite")
OPENROUTER_MODEL_EXTRACT = os.getenv("OPENROUTER_MODEL_EXTRACT", "google/gemini-2.5-flash-lite")

# Google Drive
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GOOGLE_DRIVE_DONE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_DONE_FOLDER_ID")

# 1С — заполнить после получения реальных эндпоинтов у ИТ-специалиста 1С
ONEC_BASE_URL = os.getenv("ONEC_BASE_URL")
ONEC_USER = os.getenv("ONEC_USER")
ONEC_PASSWORD = os.getenv("ONEC_PASSWORD")

# Пороги
CLASSIFICATION_CONFIDENCE_THRESHOLD = int(os.getenv("CLASSIFICATION_CONFIDENCE_THRESHOLD", 70))
FIELD_CONFIDENCE_THRESHOLD = int(os.getenv("FIELD_CONFIDENCE_THRESHOLD", 85))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 180))

STORE_PATH = os.getenv("STORE_PATH", "agent_store.db")
