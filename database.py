import sqlite3
from config import DB_PATH

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            doc_type TEXT,
            doc_date TEXT,
            processed_at TEXT,
            sender TEXT,
            recipient TEXT,
            items_summary TEXT,
            intent TEXT,
            full_json TEXT
        )
    """)
    conn.commit()
    conn.close()
