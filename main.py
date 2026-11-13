from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from database import init_db
from config import UPLOAD_DIR
from routers import auth, dashboard, ai_chat

app = FastAPI(title="Accounting Agent", version="4.3.0")

# Инициализация базы при старте
init_db()

# Монтирование статики для превью файлов
app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="files")

# Подключение роутеров
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(ai_chat.router)
