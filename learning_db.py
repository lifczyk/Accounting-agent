import sqlite3

class LearningDatabase:
    def __init__(self, db_path: str = "learning_memory.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS item_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_name_pattern TEXT UNIQUE,
                    intent TEXT,
                    suggested_account TEXT,
                    correction_count INTEGER DEFAULT 1,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_correction(self, item_name: str, intent: str, account: str):
        clean_name = item_name.strip().lower()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO item_rules (item_name_pattern, intent, suggested_account, correction_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(item_name_pattern) DO UPDATE SET
                    intent = excluded.intent,
                    suggested_account = excluded.suggested_account,
                    correction_count = item_rules.correction_count + 1,
                    last_updated = CURRENT_TIMESTAMP
            """, (clean_name, intent, account))
            conn.commit()

    def get_recent_feedback_context(self, limit: int = 15) -> str:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT item_name_pattern, intent, suggested_account 
                FROM item_rules 
                ORDER BY correction_count DESC, last_updated DESC LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            
        if not rows:
            return "Правил оператора пока нет."
        
        rules = []
        for name, intent, acc in rows:
            intent_str = "Реализация" if intent == "for_resale" else "Внутренние нужды"
            rules.append(f"- '{name}': назначение = {intent_str}, счет = {acc}")
        return "\n".join(rules)
