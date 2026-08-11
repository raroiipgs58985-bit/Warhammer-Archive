from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from .catalog import Catalog, utc_now


REPORT_SCHEMA_VERSION = "1.1"
ANALYZER_VERSION = "1.1"
ProgressCallback = Callable[[int, int, str, str], None]
CancelCallback = Callable[[], bool]


class AuditCancelled(RuntimeError):
    """Raised after a cooperative cancellation request between PDF files."""


@dataclass(frozen=True, slots=True)
class AuditConfig:
    library_root: Path
    output_dir: Path
    min_text_chars: int = 80
    full_rescan: bool = False

    def validated(self) -> "AuditConfig":
        root = self.library_root.expanduser().resolve()
        output = self.output_dir.expanduser().resolve()

        if not root.exists():
            raise ValueError(f"Папка библиотеки не найдена: {root}")
        if not root.is_dir():
            raise ValueError(
                f"Путь библиотеки не является папкой: {root}"
            )
        if self.min_text_chars < 1:
            raise ValueError("min_text_chars должен быть больше нуля")

        return AuditConfig(
            library_root=root,
            output_dir=output,
            min_text_chars=self.min_text_chars,
            full_rescan=self.full_rescan,
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
            error=_error_text(error),
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
                    "error": _error_text(error),
                }

            pages.append(page_record)
            page_kinds[kind] += 1

        result["page_kind_counts"] = dict(sorted(page_kinds.items()))
        result["document_kind"] = _document_kind(page_kinds)
        result["pages"] = pages

    return result


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _analysis_key(min_text_chars: int) -> str:
    return f"{ANALYZER_VERSION};min_text_chars={min_text_chars}"


def _is_unchanged(
    existing: Any,
    size_bytes: int,
    modified_time_ns: int,
    full_rescan: bool,
    analysis_key: str,
) -> bool:
    return bool(
        not full_rescan
        and existing is not None
        and existing["processing_state"] == "complete"
        and existing["content_id"] is not None
        and existing["analyzer_version"] == analysis_key
        and int(existing["size_bytes"]) == size_bytes
        and int(existing["modified_time_ns"]) == modified_time_ns
    )


def _find_move_candidate(
    catalog: Catalog,
    library_id: int,
    content_id: int,
    library_root: Path,
    new_relative_path: str,
) -> Any | None:
    for candidate in catalog.find_move_candidates(library_id, content_id):
        if candidate["relative_path"] == new_relative_path:
            continue
        old_path = library_root / candidate["relative_path"]
        if not candidate["is_present"] or not old_path.is_file():
            return candidate
    return None


def _process_file(
    catalog: Catalog,
    library_id: int,
    run_id: int,
    config: AuditConfig,
    path: Path,
    analyzed_hashes: set[str],
) -> str:
    relative_path = path.relative_to(config.library_root).as_posix()
    stat = path.stat()
    size_bytes = stat.st_size
    modified_time_ns = stat.st_mtime_ns
    existing = catalog.get_document(library_id, relative_path)
    analysis_key = _analysis_key(config.min_text_chars)

    if _is_unchanged(
        existing,
        size_bytes=size_bytes,
        modified_time_ns=modified_time_ns,
        full_rescan=config.full_rescan,
        analysis_key=analysis_key,
    ):
        catalog.mark_seen_unchanged(
            document_id=int(existing["id"]),
            run_id=run_id,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
        )
        return "reused_unchanged"

    document_id: int | None = None
    if existing is not None:
        document_id = catalog.mark_processing(
            library_id=library_id,
            relative_path=relative_path,
            file_name=path.name,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
            run_id=run_id,
        )

    try:
        file_hash = sha256_file(path)
    except Exception as error:
        catalog.mark_file_error(
            library_id=library_id,
            relative_path=relative_path,
            file_name=path.name,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
            run_id=run_id,
            error=_error_text(error),
        )
        return "error"

    content = catalog.get_content_by_sha(file_hash)
    moved = False

    if document_id is None and content is not None:
        candidate = _find_move_candidate(
            catalog=catalog,
            library_id=library_id,
            content_id=int(content["id"]),
            library_root=config.library_root,
            new_relative_path=relative_path,
        )
        if candidate is not None:
            document_id = int(candidate["id"])
            catalog.move_document(
                document_id=document_id,
                relative_path=relative_path,
                file_name=path.name,
                size_bytes=size_bytes,
                modified_time_ns=modified_time_ns,
                run_id=run_id,
            )
            moved = True

    if document_id is None:
        document_id = catalog.mark_processing(
            library_id=library_id,
            relative_path=relative_path,
            file_name=path.name,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
            run_id=run_id,
        )

    content_is_current = bool(
        content is not None and content["analyzer_version"] == analysis_key
    )
    may_reuse_content = content_is_current and (
        not config.full_rescan or file_hash in analyzed_hashes
    )

    if may_reuse_content:
        content_id = int(content["id"])
        catalog.attach_content(
            document_id=document_id,
            content_id=content_id,
            run_id=run_id,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
        )
        if moved:
            return "moved"
        if catalog.has_other_present_document(
            library_id=library_id,
            content_id=content_id,
            document_id=document_id,
            library_root=config.library_root,
        ):
            return "duplicate"
        return "reused_content"

    result = inspect_pdf(
        path=path,
        library_root=config.library_root,
        file_hash=file_hash,
        min_text_chars=config.min_text_chars,
    )
    final_stat = path.stat()
    if (
        final_stat.st_size != size_bytes
        or final_stat.st_mtime_ns != modified_time_ns
    ):
        catalog.mark_file_error(
            library_id=library_id,
            relative_path=relative_path,
            file_name=path.name,
            size_bytes=final_stat.st_size,
            modified_time_ns=final_stat.st_mtime_ns,
            run_id=run_id,
            error=(
                "Файл изменился во время анализа; "
                "он будет проверен повторно"
            ),
        )
        return "changed_during_scan"

    content_id = catalog.store_content(
        file_hash=file_hash,
        analyzer_version=analysis_key,
        result=result,
    )
    catalog.attach_content(
        document_id=document_id,
        content_id=content_id,
        run_id=run_id,
        size_bytes=size_bytes,
        modified_time_ns=modified_time_ns,
    )
    analyzed_hashes.add(file_hash)
    return "moved_and_analyzed" if moved else "analyzed"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(payload, target, ensure_ascii=False, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp_path, path)


def _atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as target:
            for record in records:
                target.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                )
                target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _build_summary(
    config: AuditConfig,
    records: list[dict[str, Any]],
    counters: Counter[str],
    run_id: int,
    total_bytes: int,
    missing_count: int,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    document_kind_counts: Counter[str] = Counter()
    page_kind_counts: Counter[str] = Counter()
    unique_hashes: set[str] = set()
    classified_hashes: set[str] = set()
    pages_declared = 0
    pages_classified = 0

    for record in records:
        status_counts[str(record.get("status", "unknown"))] += 1
        document_kind_counts[str(record.get("document_kind", "unknown"))] += 1
        pages_declared += int(record.get("page_count", 0))
        file_hash = record.get("sha256")
        if file_hash:
            unique_hashes.add(str(file_hash))
        if (
            file_hash
            and record.get("status") != "duplicate"
            and file_hash not in classified_hashes
        ):
            counts = record.get("page_kind_counts", {})
            page_kind_counts.update(counts)
            pages_classified += sum(int(value) for value in counts.values())
            classified_hashes.add(str(file_hash))

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "library_root": str(config.library_root),
        "output_dir": str(config.output_dir),
        "settings": {
            "min_text_chars": config.min_text_chars,
            "full_rescan": config.full_rescan,
            "analyzer_version": ANALYZER_VERSION,
        },
        "run": {
            "id": run_id,
            "counters": dict(sorted(counters.items())),
        },
        "files": {
            "discovered": len(records),
            "unique_by_sha256": len(unique_hashes),
            "total_bytes": total_bytes,
            "missing_from_library": missing_count,
            "by_status": dict(sorted(status_counts.items())),
            "by_document_kind": dict(sorted(document_kind_counts.items())),
        },
        "pages": {
            "declared_including_duplicates": pages_declared,
            "classified_unique_contents": pages_classified,
            "by_kind_unique_contents": dict(sorted(page_kind_counts.items())),
        },
        "catalog": {"database": "catalog.sqlite"},
        "reports": {
            "documents": "documents.jsonl",
            "summary": "summary.json",
        },
    }


def audit_library(
    config: AuditConfig,
    progress: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
) -> dict[str, Any]:
    config = config.validated()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    pdf_files = list(iter_pdf_files(config.library_root))
    catalog_path = config.output_dir / "catalog.sqlite"
    counters: Counter[str] = Counter()
    total_bytes = 0
    analyzed_hashes: set[str] = set()

    with Catalog(catalog_path) as catalog:
        library_id = catalog.get_or_create_library(config.library_root)
        run_id = catalog.start_run(library_id, config.full_rescan)

        try:
            for index, path in enumerate(pdf_files, start=1):
                if cancel_requested is not None and cancel_requested():
                    raise AuditCancelled(
                        "Аудит остановлен после завершения предыдущего файла"
                    )
                relative_path = path.relative_to(config.library_root).as_posix()
                try:
                    total_bytes += path.stat().st_size
                    outcome = _process_file(
                        catalog=catalog,
                        library_id=library_id,
                        run_id=run_id,
                        config=config,
                        path=path,
                        analyzed_hashes=analyzed_hashes,
                    )
                except Exception as error:
                    try:
                        stat = path.stat()
                        size_bytes = stat.st_size
                        modified_time_ns = stat.st_mtime_ns
                    except OSError:
                        size_bytes = 0
                        modified_time_ns = 0
                    catalog.mark_file_error(
                        library_id=library_id,
                        relative_path=relative_path,
                        file_name=path.name,
                        size_bytes=size_bytes,
                        modified_time_ns=modified_time_ns,
                        run_id=run_id,
                        error=_error_text(error),
                    )
                    outcome = "error"

                counters[outcome] += 1
                if progress is not None:
                    progress(index, len(pdf_files), relative_path, outcome)

            newly_missing = catalog.finalize_presence(library_id, run_id)
            counters["newly_missing"] += newly_missing
            records = catalog.snapshot(library_id)
            summary = _build_summary(
                config=config,
                records=records,
                counters=counters,
                run_id=run_id,
                total_bytes=total_bytes,
                missing_count=catalog.missing_count(library_id),
            )
            _atomic_write_jsonl(config.output_dir / "documents.jsonl", records)
            _atomic_write_json(config.output_dir / "summary.json", summary)
            catalog.finish_run(run_id, dict(counters))
            return summary
        except AuditCancelled as error:
            catalog.finish_run(
                run_id,
                dict(counters),
                status="cancelled",
                error=str(error),
            )
            raise
        except KeyboardInterrupt as error:
            catalog.finish_run(
                run_id,
                dict(counters),
                status="interrupted",
                error=_error_text(error),
            )
            raise
        except BaseException as error:
            catalog.finish_run(
                run_id,
                dict(counters),
                status="failed",
                error=_error_text(error),
            )
            raise
