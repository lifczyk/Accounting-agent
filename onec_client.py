"""
Клиент HTTP-сервиса 1С.

ВАЖНО: эндпоинты ниже — ожидаемый контракт, не подтверждённый API. Реальные пути,
формат авторизации и структура ответа зависят от того, что реализовано (или будет
реализовано) в HTTP-сервисе на стороне 1С. Уточнить у ИТ-специалиста, обслуживающего
1С, и обновить этот файл перед переходом к шагу тестового контура.

Пока методы вызывают NotImplementedError — pipeline.py перехватывает это как
"1С недоступна" и ставит duplicate_check.status = not_checked, не блокируя очередь.
"""
import requests

from config import ONEC_BASE_URL, ONEC_USER, ONEC_PASSWORD


class OneCUnavailableError(Exception):
    pass


class OneCNotConfiguredError(OneCUnavailableError):
    pass


def _require_config():
    if not ONEC_BASE_URL:
        raise OneCNotConfiguredError(
            "ONEC_BASE_URL не задан — интеграция с 1С ещё не настроена (см. .env.example)"
        )


def find_document(inn: str, number: str, date: str) -> dict | None:
    """
    Ожидаемый контракт: GET {ONEC_BASE_URL}/hs/agent/documents?inn=...&number=...&date=...
    Возвращает dict с полями id, url, number, date, amount_with_vat, lines — или None,
    если документ не найден.
    """
    _require_config()
    raise NotImplementedError(
        "Уточнить у ИТ-специалиста 1С точный путь и формат ответа метода поиска документа"
    )


def find_counterparty(inn: str) -> dict | None:
    """Ожидаемый контракт: GET {ONEC_BASE_URL}/hs/agent/counterparties?inn=..."""
    _require_config()
    raise NotImplementedError(
        "Уточнить у ИТ-специалиста 1С точный путь и формат ответа метода поиска контрагента"
    )


def find_nomenclature(name: str, supplier_article: str | None = None) -> dict | None:
    """Ожидаемый контракт: GET {ONEC_BASE_URL}/hs/agent/nomenclature?name=...&article=..."""
    _require_config()
    raise NotImplementedError(
        "Уточнить у ИТ-специалиста 1С точный путь и формат ответа метода поиска номенклатуры"
    )


def create_document(payload: dict) -> dict:
    """Ожидаемый контракт: POST {ONEC_BASE_URL}/hs/agent/documents с телом payload."""
    _require_config()
    raise NotImplementedError(
        "Уточнить у ИТ-специалиста 1С точный путь и формат тела запроса создания документа"
    )
