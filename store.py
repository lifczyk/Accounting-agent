import sqlite3
import json

class DocumentStore:
    def __init__(self, db_path: str = "accounting_agent.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_file TEXT,
                    json_data TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_record(self, record: dict) -> int:
        file_name = record.get("source", {}).get("file_name", "unknown")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO documents (source_file, json_data, status) VALUES (?, ?, ?)",
                (file_name, json.dumps(record, ensure_ascii=False), "processed")
            )
            conn.commit()
            return cursor.lastrowid

    def fetch_all(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, source_file, json_data, status, created_at FROM documents ORDER BY id DESC")
            return cursor.fetchall()
