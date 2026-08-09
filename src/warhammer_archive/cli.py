from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .audit import AuditConfig, audit_library


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warhammer-archive",
        description="Локальные инструменты Warhammer Archive",
    )
    parser.add_argument("--version", action="version", version=__version__)

    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser(
        "audit",
        help=(
            "Проверить PDF-библиотеку без изменения исходных файлов"
        ),
    )
    audit.add_argument(
        "library",
        type=Path,
        help="Корневая папка библиотеки",
    )
    audit.add_argument(
        "--output",
        type=Path,
        default=Path("audit-report"),
        help="Папка отчёта (по умолчанию: ./audit-report)",
    )
    audit.add_argument(
        "--min-text-chars",
        type=int,
        default=80,
        help=(
            "Минимум непробельных символов для текстовой страницы"
        ),
    )
    audit.add_argument(
        "--quiet",
        action="store_true",
        help="Не показывать прогресс по каждому файлу",
    )
    audit.add_argument(
        "--full-rescan",
        action="store_true",
        help=(
            "Повторно проанализировать все PDF, "
            "игнорируя сохранённый результат"
        ),
    )
    return parser


def _show_progress(index: int, total: int, path: str, status: str) -> None:
    print(f"[{index}/{total}] {status}: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "audit":
        parser.error(f"Неизвестная команда: {args.command}")

    config = AuditConfig(
        library_root=args.library,
        output_dir=args.output,
        min_text_chars=args.min_text_chars,
        full_rescan=args.full_rescan,
    )

    try:
        summary = audit_library(
            config,
            progress=None if args.quiet else _show_progress,
        )
    except (OSError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2

    print(json.dumps(summary["files"], ensure_ascii=False, indent=2))
    print(f"Отчёт сохранён: {Path(summary['output_dir']) / 'summary.json'}")
    return 0
