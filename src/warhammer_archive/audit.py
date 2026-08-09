from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pymupdf


SCHEMA_VERSION = "1.0"
ProgressCallback = Callable[[int, int, str, str], None]


@dataclass(frozen=True, slots=True)
class AuditConfig:
    library_root: Path
    output_dir: Path
    min_text_chars: int = 80

    def validated(self) -> "AuditConfig":
        root = self.library_root.expanduser().resolve()
        output = self.output_dir.expanduser().resolve()

        if not root.exists():
            raise ValueError(f"Папка библиотеки не найдена: {root}")
        if not root.is_dir():
            raise ValueError(f"Путь библиотеки не является папкой: {root}")
        if self.min_text_chars < 1:
            raise ValueError("min_text_chars должен быть больше нуля")

        return AuditConfig(
            library_root=root,
            output_dir=output,
            min_text_chars=self.min_text_chars,
        )


def iter_pdf_files(root: Path) -> Iterator[Path]:
    """Yield PDFs recursively in a deterministic order."""

    paths = (
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pdf"
    )
    yield from sorted(paths, key=lambda path: path.as_posix().casefold())


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    if not metadata:
        return {}

    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        text = str(value).replace("\x00", "").strip()
        if text:
            cleaned[str(key)] = text
    return cleaned


def _non_whitespace_chars(text: str) -> int:
    return sum(1 for character in text if not character.isspace())


def _image_metrics(page: pymupdf.Page) -> tuple[int, float]:
    """Return displayed image count and approximate covered page fraction."""

    try:
        images = page.get_image_info()
    except Exception:
        return 0, 0.0

    page_rect = page.rect
    page_area = max(float(page_rect.width * page_rect.height), 1.0)
    covered_area = 0.0

    for image in images:
        raw_bbox = image.get("bbox")
        if not raw_bbox:
            continue
        try:
            bbox = pymupdf.Rect(raw_bbox) & page_rect
        except Exception:
            continue
        if bbox.is_empty or bbox.is_infinite:
            continue
        covered_area += max(float(bbox.width * bbox.height), 0.0)

    return len(images), round(min(covered_area / page_area, 1.0), 4)


def _page_kind(
    text_chars: int,
    image_count: int,
    image_coverage: float,
    min_text_chars: int,
) -> str:
    if text_chars >= min_text_chars:
        return "text"
    if image_count and image_coverage >= 0.6:
        return "scan_candidate"
    if text_chars:
        return "sparse_text"
    if image_count:
        return "image_only"
    return "empty"


def _document_kind(page_kinds: Counter[str]) -> str:
    searchable = page_kinds["text"] + page_kinds["sparse_text"]
    image_based = page_kinds["scan_candidate"] + page_kinds["image_only"]

    if searchable and image_based:
        return "mixed"
    if image_based:
        return "scanned"
    if searchable:
        return "text"
    if page_kinds["page_error"]:
        return "unreadable"
    return "empty"


def _page_label(page: pymupdf.Page, page_index: int) -> str:
    try:
        label = page.get_label()
    except Exception:
        label = ""
    return label or str(page_index + 1)


def inspect_pdf(
    path: Path,
    library_root: Path,
    file_hash: str,
    min_text_chars: int,
) -> dict[str, Any]:
    stat = path.stat()
    result: dict[str, Any] = {
        "relative_path": path.relative_to(library_root).as_posix(),
        "file_name": path.name,
        "size_bytes": stat.st_size,
        "modified_time_ns": stat.st_mtime_ns,
        "sha256": file_hash,
        "status": "ok",
        "duplicate_of": None,
    }

    try:
        document = pymupdf.open(path)
    except Exception as error:
        result.update(
            status="unreadable",
            error=f"{type(error).__name__}: {error}",
            page_count=0,
            document_kind="unreadable",
            page_kind_counts={},
            pages=[],
        )
        return result

    with document:
        result["page_count"] = document.page_count
        result["pdf_metadata"] = _clean_metadata(document.metadata)

        if document.needs_pass:
            result.update(
                status="encrypted",
                document_kind="encrypted",
                page_kind_counts={},
                pages=[],
            )
            return result

        pages: list[dict[str, Any]] = []
        page_kinds: Counter[str] = Counter()

        for page_index in range(document.page_count):
            try:
                page = document.load_page(page_index)
                text = page.get_text("text") or ""
                text_chars = _non_whitespace_chars(text)
                word_count = len(text.split())
                image_count, image_coverage = _image_metrics(page)
                kind = _page_kind(
                    text_chars=text_chars,
                    image_count=image_count,
                    image_coverage=image_coverage,
                    min_text_chars=min_text_chars,
                )
                page_record = {
                    "pdf_page": page_index + 1,
                    "printed_label": _page_label(page, page_index),
                    "width": round(float(page.rect.width), 2),
                    "height": round(float(page.rect.height), 2),
                    "rotation": page.rotation,
                    "text_chars": text_chars,
                    "word_count": word_count,
                    "image_count": image_count,
                    "image_coverage": image_coverage,
                    "kind": kind,
                }
            except Exception as error:
                kind = "page_error"
                page_record = {
                    "pdf_page": page_index + 1,
                    "printed_label": str(page_index + 1),
                    "kind": kind,
                    "error": f"{type(error).__name__}: {error}",
                }

            pages.append(page_record)
            page_kinds[kind] += 1

        result["page_kind_counts"] = dict(sorted(page_kinds.items()))
        result["document_kind"] = _document_kind(page_kinds)
        result["pages"] = pages

    return result


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def audit_library(
    config: AuditConfig,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    config = config.validated()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    pdf_files = list(iter_pdf_files(config.library_root))

    documents_path = config.output_dir / "documents.jsonl"
    documents_temp_path = config.output_dir / "documents.jsonl.tmp"
    summary_path = config.output_dir / "summary.json"

    status_counts: Counter[str] = Counter()
    document_kind_counts: Counter[str] = Counter()
    page_kind_counts: Counter[str] = Counter()
    seen_hashes: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    pages_declared = 0
    pages_analyzed = 0

    try:
        with documents_temp_path.open("w", encoding="utf-8", newline="\n") as target:
            for index, path in enumerate(pdf_files, start=1):
                relative_path = path.relative_to(config.library_root).as_posix()
                try:
                    total_bytes += path.stat().st_size
                    file_hash = sha256_file(path)
                    first = seen_hashes.get(file_hash)

                    if first is not None:
                        result = {
                            "relative_path": relative_path,
                            "file_name": path.name,
                            "size_bytes": path.stat().st_size,
                            "modified_time_ns": path.stat().st_mtime_ns,
                            "sha256": file_hash,
                            "status": "duplicate",
                            "duplicate_of": first["relative_path"],
                            "page_count": first["page_count"],
                            "document_kind": first["document_kind"],
                            "page_kind_counts": first["page_kind_counts"],
                            "pages": [],
                            "analysis_reused": True,
                        }
                    else:
                        result = inspect_pdf(
                            path=path,
                            library_root=config.library_root,
                            file_hash=file_hash,
                            min_text_chars=config.min_text_chars,
                        )
                        seen_hashes[file_hash] = {
                            "relative_path": relative_path,
                            "page_count": result.get("page_count", 0),
                            "document_kind": result.get("document_kind", "unknown"),
                            "page_kind_counts": result.get("page_kind_counts", {}),
                        }
                        pages_analyzed += sum(
                            result.get("page_kind_counts", {}).values()
                        )

                    pages_declared += result.get("page_count", 0)
                except Exception as error:
                    result = {
                        "relative_path": relative_path,
                        "file_name": path.name,
                        "status": "error",
                        "error": f"{type(error).__name__}: {error}",
                        "page_count": 0,
                        "document_kind": "unreadable",
                        "page_kind_counts": {},
                        "pages": [],
                    }

                status = str(result.get("status", "unknown"))
                document_kind = str(result.get("document_kind", "unknown"))
                status_counts[status] += 1
                document_kind_counts[document_kind] += 1

                if status != "duplicate":
                    page_kind_counts.update(result.get("page_kind_counts", {}))

                target.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                target.write("\n")

                if progress is not None:
                    progress(index, len(pdf_files), relative_path, status)

            target.flush()
            os.fsync(target.fileno())

        os.replace(documents_temp_path, documents_path)
    except Exception:
        documents_temp_path.unlink(missing_ok=True)
        raise

    summary = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "library_root": str(config.library_root),
        "output_dir": str(config.output_dir),
        "settings": {"min_text_chars": config.min_text_chars},
        "files": {
            "discovered": len(pdf_files),
            "unique_by_sha256": len(seen_hashes),
            "total_bytes": total_bytes,
            "by_status": dict(sorted(status_counts.items())),
            "by_document_kind": dict(sorted(document_kind_counts.items())),
        },
        "pages": {
            "declared_including_duplicates": pages_declared,
            "analyzed_unique_files": pages_analyzed,
            "by_kind_unique_files": dict(sorted(page_kind_counts.items())),
        },
        "reports": {
            "documents": documents_path.name,
            "summary": summary_path.name,
        },
    }
    _atomic_write_json(summary_path, summary)
    return summary
