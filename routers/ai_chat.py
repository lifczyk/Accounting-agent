from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import sqlite3
import json
import httpx
import asyncio
from config import OPENROUTER_API_KEY, OPENROUTER_URL, CHAT_MODEL, DB_PATH
from routers.auth import verify_session

router = APIRouter()

class ChatQuery(BaseModel):
    prompt: str

@router.post("/api/ai-chat")
async def ai_chat(query: ChatQuery, _=Depends(verify_session)):
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=400, detail="Не задан OPENROUTER_API_KEY на сервере.")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, doc_type, doc_date, processed_at, sender, recipient, items_summary, intent FROM processed_journal ORDER BY id DESC LIMIT 60")
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    context_text = json.dumps(rows, ensure_ascii=False, indent=2)

    system_prompt = (
        "Ты профессиональный бухгалтерский ИИ-помощник. У тебя есть доступ к базе данных обработанных первичных документов. "
        "Отвечай на вопросы пользователя четко, на русском языке.\n"
        "ВАЖНО: При поиске компаний учитывай возможные опечатки пользователя, сокращения или особенности раскладки клавиатуры "
        "и сопоставляй их с точными наименованиями в базе данных.\n"
        "Если пользователь спрашивает о документах или количестве документов в отношении конкретной компании, "
        "верни ответ строго в JSON-формате:\n"
        "{\"answer\": \"текст ответа с подсчетом и деталями\", \"filter_action\": {\"company\": \"точное наименование компании из базы\", \"role\": \"sender\"}}\n"
        "Если фильтрация по компании не требуется, верни: {\"answer\": \"текст ответа\"}.\n\n"
        f"Данные документов в базе:\n{context_text}"
    )

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://aa.depot.by",
        "X-Title": "Accounting Agent",
        "Content-Type": "application/json"
    }

    payload = {
        "model": CHAT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query.prompt}
        ],
        "response_format": {"type": "json_object"}
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        for attempt in range(3):
            try:
                resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
                if resp.status_code == 200:
                    res_data = resp.json()
                    content_str = res_data["choices"][0]["message"]["content"]
                    try:
                        parsed_res = json.loads(content_str)
                        return {
                            "answer": parsed_res.get("answer", content_str),
                            "filter_action": parsed_res.get("filter_action", None)
                        }
                    except Exception:
                        return {"answer": content_str, "filter_action": None}
                elif resp.status_code == 429 and attempt < 2:
                    await asyncio.sleep(2)
                    continue
                else:
                    raise HTTPException(status_code=500, detail=f"OpenRouter API error: {resp.status_code} ({resp.text})")
            except HTTPException:
                raise
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                raise HTTPException(status_code=500, detail=str(e))
