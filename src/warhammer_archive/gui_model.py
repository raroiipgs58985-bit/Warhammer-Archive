from __future__ import annotations

from pathlib import Path
from typing import Any


OUTCOME_LABELS = {
    "analyzed": "проверен",
    "changed_during_scan": "изменился во время проверки",
    "duplicate": "точный дубликат",
    "error": "ошибка",
    "moved": "перемещён",
    "moved_and_analyzed": "перемещён и проверен",
    "reused_content": "готовый анализ переиспользован",
    "reused_unchanged": "без изменений",
}


def outcome_label(outcome: str) -> str:
    return OUTCOME_LABELS.get(outcome, outcome.replace("_", " "))


def default_output_directory(library_root: Path) -> Path:
    return library_root.resolve().parent / "Warhammer Archive Data"


def _formatted_number(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}".replace(",", " ")


def summary_cards(summary: dict[str, Any]) -> list[tuple[str, str]]:
    files = summary.get("files", {})
    pages = summary.get("pages", {})
    statuses = files.get("by_status", {})
    page_kinds = pages.get("by_kind_unique_contents", {})

    scan_pages = int(page_kinds.get("scan_candidate", 0)) + int(
        page_kinds.get("image_only", 0)
    )
    problems = (
        int(statuses.get("error", 0))
        + int(statuses.get("unreadable", 0))
        + int(statuses.get("encrypted", 0))
    )
    return [
        ("PDF", _formatted_number(files.get("discovered", 0))),
        ("Уникальные", _formatted_number(files.get("unique_by_sha256", 0))),
        ("Скан-страницы", _formatted_number(scan_pages)),
        ("Дубликаты", _formatted_number(statuses.get("duplicate", 0))),
        ("Проблемы", _formatted_number(problems)),
        ("Отсутствуют", _formatted_number(files.get("missing_from_library", 0))),
    ]

