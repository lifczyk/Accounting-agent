import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone

import drive_intake
import onec_client
import store
from config import (
    CLASSIFICATION_CONFIDENCE_THRESHOLD,
    FIELD_CONFIDENCE_THRESHOLD,
    POLL_INTERVAL_SECONDS,
)
from openrouter_client import EXTRACTORS, OpenRouterError, classify_document

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pipeline")


def normalize_document_number(raw: str | None) -> str:
    if not raw:
        return ""
    cleaned = raw.replace("№", "").replace("No", "").replace("N", "").strip()
    cleaned = cleaned.lstrip("0") or "0"
    return cleaned


def compare_lines(existing_lines: list[dict], extracted_lines: list[dict]) -> bool:
    """Грубое сравнение: совпадает количество строк и их суммы. Уточнить при реальной
    интеграции — например, сравнение по наименованию+количеству+сумме построчно."""
    if len(existing_lines) != len(extracted_lines):
        return False
    existing_sorted = sorted(existing_lines, key=lambda l: l.get("amount", 0))
    extracted_sorted = sorted(extracted_lines, key=lambda l: l.get("amount") or 0)
    for e, x in zip(existing_sorted, extracted_sorted):
        if abs((e.get("amount") or 0) - (x.get("amount") or 0)) > 0.01:
            return False
    return True


def check_duplicate(extraction: dict) -> dict:
    key = {
        "inn": extraction["seller"]["inn"],
        "document_number_normalized": normalize_document_number(extraction["document_number"]),
        "document_date": extraction["document_date"],
    }

    try:
        existing = onec_client.find_document(
            inn=key["inn"],
            number=key["document_number_normalized"],
            date=key["document_date"],
        )
    except onec_client.OneCUnavailableError as exc:
        log.warning("1С недоступна при проверке дубликата: %s", exc)
        return {
            "status": "not_checked",
            "normalized_key": key,
            "matched_document": None,
            "checked_at": None,
        }

    if existing is None:
        return {
            "status": "none",
            "normalized_key": key,
            "matched_document": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    amounts_match = abs(existing["amount_with_vat"] - (extraction["totals"]["amount_with_vat"] or 0)) < 0.01
    lines_match = compare_lines(existing.get("lines", []), extraction["lines"])
    status = "exact_match" if amounts_match and lines_match else "conflict"

    return {
        "status": status,
        "normalized_key": key,
        "matched_document": {
            "onec_id": existing["id"],
            "onec_url": existing.get("url"),
            "number": existing["number"],
            "date": existing["date"],
            "amount_with_vat": existing["amount_with_vat"],
            "amounts_match": amounts_match,
            "lines_match": lines_match,
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def needs_manual_review(classification: dict, extraction: dict | None) -> bool:
    if classification["confidence"] < CLASSIFICATION_CONFIDENCE_THRESHOLD:
        return True
    if classification["document_type"] == "unknown":
        return True
    if extraction is None:
        return False
    if extraction["overall_confidence"] < FIELD_CONFIDENCE_THRESHOLD:
        return True
    if extraction["seller"]["confidence"] < FIELD_CONFIDENCE_THRESHOLD:
        return True
    if extraction["buyer"]["confidence"] < FIELD_CONFIDENCE_THRESHOLD:
        return True
    if extraction["totals_mismatch"]:
        return True
    if any(line["confidence"] < FIELD_CONFIDENCE_THRESHOLD for line in extraction["lines"]):
        return True
    return False


def process_file(service, drive_file: dict) -> None:
    file_id = drive_file["id"]
    file_name = drive_file["name"]
    mime_type = drive_file["mimeType"]
    record_id = str(uuid.uuid4())

    log.info("Беру в обработку: %s (%s)", file_name, file_id)

    content = drive_intake.download_file(service, file_id)
    file_hash = "sha256:" + hashlib.sha256(content).hexdigest()

    if store.is_known_hash(file_hash):
        log.info("Файл %s уже обработан ранее (совпадение по hash) — пропуск", file_name)
        drive_intake.mark_done(service, file_id, success=True)
        return

    drive_intake.mark_in_progress(service, file_id, record_id)

    record = {
        "record_id": record_id,
        "source": {
            "channel": "google_drive",
            "file_id": file_id,
            "file_name": file_name,
            "file_hash": file_hash,
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
        "classification": None,
        "extraction": None,
        "duplicate_check": None,
        "review": {"status": "pending", "reviewed_by": None, "reviewed_at": None},
        "onec_integration": {"status": "not_sent", "onec_document_id": None, "error_message": None, "sent_at": None},
        "processing": {"drive_status": "in_progress", "moved_to_folder": None},
    }

    try:
        classification = classify_document(content, mime_type)
        record["classification"] = classification

        extraction = None
        extractor = EXTRACTORS.get(classification["document_type"])
        if extractor and classification["confidence"] >= CLASSIFICATION_CONFIDENCE_THRESHOLD:
            extraction = extractor(content, mime_type)
            record["extraction"] = extraction

            duplicate_check = check_duplicate(extraction)
            record["duplicate_check"] = duplicate_check

            if duplicate_check["status"] == "exact_match":
                record["onec_integration"]["status"] = "skipped_duplicate"
                record["review"]["status"] = "approved"  # авто-закрыто, оператору нечего проверять
        else:
            # Тип не torg12 (экстрактор ещё не реализован) или низкая уверенность
            # классификации — откладываем в очередь ручной разметки, не падаем.
            log.info(
                "Тип '%s' не имеет экстрактора или низкая уверенность классификации — в ручную очередь",
                classification["document_type"],
            )

        record["processing"]["drive_status"] = "done"
        store.save_record(record)
        drive_intake.mark_done(service, file_id, success=True)
        log.info("Готово: %s -> record %s", file_name, record_id)

    except OpenRouterError as exc:
        log.error("Ошибка распознавания для %s: %s", file_name, exc)
        record["processing"]["drive_status"] = "failed"
        record["onec_integration"]["error_message"] = str(exc)
        store.save_record(record)
        drive_intake.mark_done(service, file_id, success=False)


def run_once() -> None:
    service = drive_intake.get_service()
    new_files = drive_intake.list_new_files(service)
    log.info("Новых файлов в Drive: %d", len(new_files))
    for f in new_files:
        process_file(service, f)


def run_forever() -> None:
    store.init_store()
    while True:
        try:
            run_once()
        except Exception:
            log.exception("Ошибка цикла поллинга — продолжаем со следующей итерации")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
