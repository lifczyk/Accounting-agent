import base64
import json
import os

import requests

from config import OPENROUTER_API_KEY, OPENROUTER_MODEL_CLASSIFY, OPENROUTER_MODEL_EXTRACT

SCHEMAS_DIR = os.path.join(os.path.dirname(__file__), "schemas")

CLASSIFIER_SYSTEM_PROMPT = """Ты — классификатор бухгалтерских документов. Твоя единственная задача — \
определить тип документа по изображению и вернуть структурированный JSON. Не извлекай содержимое \
документа, не считай суммы, не анализируй товарные позиции — только классификация.

Возможные типы документов:
- torg12 — товарная накладная (унифицированная форма ТОРГ-12), обычно явно указано \
"Товарная накладная" в заголовке, табличная часть с графами количество/цена/сумма/НДС
- upd — универсальный передаточный документ, явно указано "УПД" в заголовке, есть поле \
"Статус" со значением 1 или 2
- ttn — товарно-транспортная накладная, есть транспортный раздел: перевозчик, транспортное \
средство, водитель, пункты погрузки/разгрузки
- act — акт приёма-передачи, акт об оказании услуг, акт выполненных работ; заголовок содержит \
слово "Акт"; часто без детальной табличной части количество/цена
- receipt — кассовый чек; узкий формат (чековая лента), QR-код, реквизиты ФН/ФД/ФП, слова \
"кассовый чек"
- unknown — документ не подходит ни под один из перечисленных типов (договор, письмо, прочее)

Если в одном файле видно несколько разных документов — укажи это в поле \
multiple_documents_detected и классифицируй по первому/основному документу.

Отвечай строго в формате JSON согласно схеме. Никакого текста до или после JSON."""

TORG12_SYSTEM_PROMPT = """Ты — агент извлечения данных из товарных накладных формы ТОРГ-12. Твоя \
задача — извлечь все значимые поля документа и для каждого поля указать уверенность в \
распознавании.

Правила:
1. Если поле на документе отсутствует или нечитаемо — верни null и confidence 0, не придумывай \
значение.
2. Confidence отражай честно: 90-100 — текст чёткий и однозначный; 60-89 — текст читается, но \
есть сомнение (нечёткая печать, похожие цифры); ниже 60 — скорее догадка, чем чтение.
3. Суммы и количества возвращай как числа (не строки), разделитель — точка.
4. ИНН и КПП возвращай как строки (могут содержать ведущие нули).
5. Если сумма строк табличной части не сходится с итоговой суммой документа — укажи это в поле \
totals_mismatch и всё равно верни оба значения как есть на документе.
6. Отвечай строго в формате JSON согласно схеме. Никакого текста до или после JSON."""


def _load_schema(filename: str) -> dict:
    with open(os.path.join(SCHEMAS_DIR, filename), encoding="utf-8") as f:
        return json.load(f)


CLASSIFICATION_SCHEMA = _load_schema("classification_schema.json")
TORG12_SCHEMA = _load_schema("torg12_schema.json")


class OpenRouterError(Exception):
    pass


def _call_model(model: str, system_prompt: str, user_text: str, image_bytes: bytes,
                 mime_type: str, json_schema: dict) -> dict:
    if not OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY не задан — проверьте .env")

    image_b64 = base64.b64encode(image_bytes).decode()

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_schema", "json_schema": json_schema},
        },
        timeout=90,
    )

    if response.status_code != 200:
        raise OpenRouterError(f"OpenRouter вернул {response.status_code}: {response.text[:500]}")

    try:
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise OpenRouterError(f"Не удалось разобрать ответ модели: {exc}") from exc


def classify_document(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Возвращает dict согласно classification_schema.json."""
    return _call_model(
        OPENROUTER_MODEL_CLASSIFY,
        CLASSIFIER_SYSTEM_PROMPT,
        "Определи тип документа на изображении.",
        image_bytes,
        mime_type,
        CLASSIFICATION_SCHEMA,
    )


def extract_torg12(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    """Возвращает dict согласно torg12_schema.json."""
    return _call_model(
        OPENROUTER_MODEL_EXTRACT,
        TORG12_SYSTEM_PROMPT,
        "Извлеки все поля из товарной накладной (ТОРГ-12) на изображении согласно схеме.",
        image_bytes,
        mime_type,
        TORG12_SCHEMA,
    )


# Реестр: тип документа -> функция извлечения.
# Для upd/ttn/act/receipt схемы и промпты ещё не написаны (следующий шаг после обкатки
# пайплайна на ТОРГ-12) — пока pipeline.py будет откладывать такие документы в очередь
# ручной разметки, а не падать.
EXTRACTORS = {
    "torg12": extract_torg12,
}
