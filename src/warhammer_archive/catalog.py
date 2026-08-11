from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class Catalog:
    """Persistent local catalog used by incremental library audits."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._migrate()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def _migrate(self) -> None:
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > CATALOG_SCHEMA_VERSION:
            raise RuntimeError(
                "Каталог создан более новой версией приложения: "
                f"schema {current}, поддерживается {CATALOG_SCHEMA_VERSION}"
            )

        if current == 0:
            self.connection.executescript(
                """
                CREATE TABLE libraries (
                    id INTEGER PRIMARY KEY,
                    root_key TEXT NOT NULL UNIQUE,
                    root_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE audit_runs (
                    id INTEGER PRIMARY KEY,
                    library_id INTEGER NOT NULL REFERENCES libraries(id),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    full_rescan INTEGER NOT NULL DEFAULT 0,
                    counters_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );

                CREATE TABLE contents (
                    id INTEGER PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    analyzer_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    document_kind TEXT NOT NULL,
                    pdf_metadata_json TEXT NOT NULL DEFAULT '{}',
                    page_kind_counts_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    analyzed_at TEXT NOT NULL
                );

                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY,
                    library_id INTEGER NOT NULL REFERENCES libraries(id),
                    relative_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    modified_time_ns INTEGER NOT NULL DEFAULT 0,
                    content_id INTEGER REFERENCES contents(id),
                    processing_state TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    is_present INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_seen_run_id INTEGER REFERENCES audit_runs(id),
                    UNIQUE(library_id, relative_path)
                );

                CREATE INDEX documents_library_content_idx
                    ON documents(library_id, content_id);
                CREATE INDEX documents_library_present_idx
                    ON documents(library_id, is_present);

                CREATE TABLE pages (
                    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                    pdf_page INTEGER NOT NULL,
                    printed_label TEXT NOT NULL,
                    width REAL,
                    height REAL,
                    rotation INTEGER,
                    text_chars INTEGER,
                    word_count INTEGER,
                    image_count INTEGER,
                    image_coverage REAL,
                    kind TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY(content_id, pdf_page)
                );
                """
            )
            self.connection.execute(
                f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}"
            )
            self.connection.commit()

    def get_or_create_library(self, root: Path) -> int:
        root_path = str(root.resolve())
        root_key = os.path.normcase(root_path)
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO libraries(root_key, root_path, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(root_key) DO UPDATE SET
                root_path = excluded.root_path,
                updated_at = excluded.updated_at
            """,
            (root_key, root_path, now, now),
        )
        row = self.connection.execute(
            "SELECT id FROM libraries WHERE root_key = ?", (root_key,)
        ).fetchone()
        self.connection.commit()
        if row is None:
            raise RuntimeError(
                "Не удалось создать запись библиотеки"
            )
        return int(row["id"])

    def start_run(self, library_id: int, full_rescan: bool) -> int:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE audit_runs
            SET status = 'interrupted', completed_at = ?
            WHERE library_id = ? AND status = 'running'
            """,
            (now, library_id),
        )
        cursor = self.connection.execute(
            """
            INSERT INTO audit_runs(
                library_id, started_at, status, full_rescan, counters_json
            ) VALUES (?, ?, 'running', ?, '{}')
            """,
            (library_id, now, int(full_rescan)),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        counters: dict[str, int],
        status: str = "completed",
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE audit_runs
            SET status = ?, completed_at = ?, counters_json = ?, error = ?
            WHERE id = ?
            """,
            (status, utc_now(), _json_dump(counters), error, run_id),
        )
        self.connection.commit()

    def get_document(self, library_id: int, relative_path: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT
                d.*,
                c.sha256,
                c.analyzer_version,
                c.status AS content_status
            FROM documents AS d
            LEFT JOIN contents AS c ON c.id = d.content_id
            WHERE d.library_id = ? AND d.relative_path = ?
            """,
            (library_id, relative_path),
        ).fetchone()

    def mark_seen_unchanged(
        self,
        document_id: int,
        run_id: int,
        size_bytes: int,
        modified_time_ns: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE documents
            SET size_bytes = ?, modified_time_ns = ?, processing_state = 'complete',
                error = NULL, is_present = 1, last_seen_at = ?, last_seen_run_id = ?
            WHERE id = ?
            """,
            (size_bytes, modified_time_ns, utc_now(), run_id, document_id),
        )
        self.connection.commit()

    def mark_processing(
        self,
        library_id: int,
        relative_path: str,
        file_name: str,
        size_bytes: int,
        modified_time_ns: int,
        run_id: int,
    ) -> int:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO documents(
                library_id, relative_path, file_name, size_bytes,
                modified_time_ns, content_id, processing_state, error,
                is_present, first_seen_at, last_seen_at, last_seen_run_id
            ) VALUES (?, ?, ?, ?, ?, NULL, 'processing', NULL, 1, ?, ?, ?)
            ON CONFLICT(library_id, relative_path) DO UPDATE SET
                file_name = excluded.file_name,
                size_bytes = excluded.size_bytes,
                modified_time_ns = excluded.modified_time_ns,
                content_id = NULL,
                processing_state = 'processing',
                error = NULL,
                is_present = 1,
                last_seen_at = excluded.last_seen_at,
                last_seen_run_id = excluded.last_seen_run_id
            """,
            (
                library_id,
                relative_path,
                file_name,
                size_bytes,
                modified_time_ns,
                now,
                now,
                run_id,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM documents
            WHERE library_id = ? AND relative_path = ?
            """,
            (library_id, relative_path),
        ).fetchone()
        self.connection.commit()
        if row is None:
            raise RuntimeError(
                f"Не удалось зарегистрировать файл: {relative_path}"
            )
        return int(row["id"])

    def mark_file_error(
        self,
        library_id: int,
        relative_path: str,
        file_name: str,
        size_bytes: int,
        modified_time_ns: int,
        run_id: int,
        error: str,
    ) -> int:
        document_id = self.mark_processing(
            library_id=library_id,
            relative_path=relative_path,
            file_name=file_name,
            size_bytes=size_bytes,
            modified_time_ns=modified_time_ns,
            run_id=run_id,
        )
        self.connection.execute(
            """
            UPDATE documents
            SET processing_state = 'error', error = ?, content_id = NULL
            WHERE id = ?
            """,
            (error, document_id),
        )
        self.connection.commit()
        return document_id

    def get_content_by_sha(self, file_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM contents WHERE sha256 = ?", (file_hash,)
        ).fetchone()

    def store_content(
        self,
        file_hash: str,
        analyzer_version: str,
        result: dict[str, Any],
    ) -> int:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO contents(
                sha256, analyzer_version, status, page_count, document_kind,
                pdf_metadata_json, page_kind_counts_json, error, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                analyzer_version = excluded.analyzer_version,
                status = excluded.status,
                page_count = excluded.page_count,
                document_kind = excluded.document_kind,
                pdf_metadata_json = excluded.pdf_metadata_json,
                page_kind_counts_json = excluded.page_kind_counts_json,
                error = excluded.error,
                analyzed_at = excluded.analyzed_at
            """,
            (
                file_hash,
                analyzer_version,
                result.get("status", "error"),
                int(result.get("page_count", 0)),
                result.get("document_kind", "unreadable"),
                _json_dump(result.get("pdf_metadata", {})),
                _json_dump(result.get("page_kind_counts", {})),
                result.get("error"),
                now,
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM contents WHERE sha256 = ?", (file_hash,)
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "Не удалось сохранить анализ содержимого PDF"
            )
        content_id = int(row["id"])

        self.connection.execute("DELETE FROM pages WHERE content_id = ?", (content_id,))
        page_rows = []
        for page in result.get("pages", []):
            page_rows.append(
                (
                    content_id,
                    int(page["pdf_page"]),
                    str(page.get("printed_label", page["pdf_page"])),
                    page.get("width"),
                    page.get("height"),
                    page.get("rotation"),
                    page.get("text_chars"),
                    page.get("word_count"),
                    page.get("image_count"),
                    page.get("image_coverage"),
                    page.get("kind", "page_error"),
                    page.get("error"),
                )
            )
        self.connection.executemany(
            """
            INSERT INTO pages(
                content_id, pdf_page, printed_label, width, height, rotation,
                text_chars, word_count, image_count, image_coverage, kind, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            page_rows,
        )
        return content_id

    def attach_content(
        self,
        document_id: int,
        content_id: int,
        run_id: int,
        size_bytes: int,
        modified_time_ns: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE documents
            SET content_id = ?, size_bytes = ?, modified_time_ns = ?,
                processing_state = 'complete', error = NULL, is_present = 1,
                last_seen_at = ?, last_seen_run_id = ?
            WHERE id = ?
            """,
            (
                content_id,
                size_bytes,
                modified_time_ns,
                utc_now(),
                run_id,
                document_id,
            ),
        )
        self.connection.commit()

    def find_move_candidates(
        self,
        library_id: int,
        content_id: int,
    ) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM documents
                WHERE library_id = ? AND content_id = ?
                ORDER BY is_present ASC, relative_path COLLATE NOCASE
                """,
                (library_id, content_id),
            ).fetchall()
        )

    def move_document(
        self,
        document_id: int,
        relative_path: str,
        file_name: str,
        size_bytes: int,
        modified_time_ns: int,
        run_id: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE documents
            SET relative_path = ?, file_name = ?, size_bytes = ?,
                modified_time_ns = ?, processing_state = 'complete', error = NULL,
                is_present = 1, last_seen_at = ?, last_seen_run_id = ?
            WHERE id = ?
            """,
            (
                relative_path,
                file_name,
                size_bytes,
                modified_time_ns,
                utc_now(),
                run_id,
                document_id,
            ),
        )
        self.connection.commit()

    def has_other_present_document(
        self,
        library_id: int,
        content_id: int,
        document_id: int,
        library_root: Path,
    ) -> bool:
        rows = self.connection.execute(
            """
            SELECT relative_path FROM documents
            WHERE library_id = ? AND content_id = ? AND id != ?
            """,
            (library_id, content_id, document_id),
        ).fetchall()
        return any((library_root / row["relative_path"]).is_file() for row in rows)

    def finalize_presence(self, library_id: int, run_id: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE documents
            SET is_present = 0
            WHERE library_id = ?
              AND (last_seen_run_id IS NULL OR last_seen_run_id != ?)
              AND is_present = 1
            """,
            (library_id, run_id),
        )
        self.connection.commit()
        return max(int(cursor.rowcount), 0)

    def missing_count(self, library_id: int) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM documents
            WHERE library_id = ? AND is_present = 0
            """,
            (library_id,),
        ).fetchone()
        return int(row["count"] if row else 0)

    def snapshot(self, library_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT
                d.*,
                c.sha256,
                c.analyzer_version,
                c.status AS content_status,
                c.page_count,
                c.document_kind,
                c.pdf_metadata_json,
                c.page_kind_counts_json,
                c.error AS content_error
            FROM documents AS d
            LEFT JOIN contents AS c ON c.id = d.content_id
            WHERE d.library_id = ? AND d.is_present = 1
            ORDER BY d.relative_path COLLATE NOCASE
            """,
            (library_id,),
        ).fetchall()

        pages_by_content: dict[int, list[dict[str, Any]]] = {}
        if any(row["content_id"] for row in rows):
            page_rows = self.connection.execute(
                """
                SELECT p.* FROM pages AS p
                WHERE EXISTS (
                    SELECT 1 FROM documents AS d
                    WHERE d.content_id = p.content_id
                      AND d.library_id = ?
                      AND d.is_present = 1
                )
                ORDER BY p.content_id, p.pdf_page
                """,
                (library_id,),
            ).fetchall()
            for page in page_rows:
                record = {
                    "pdf_page": page["pdf_page"],
                    "printed_label": page["printed_label"],
                    "kind": page["kind"],
                }
                for key in (
                    "width",
                    "height",
                    "rotation",
                    "text_chars",
                    "word_count",
                    "image_count",
                    "image_coverage",
                    "error",
                ):
                    if page[key] is not None:
                        record[key] = page[key]
                pages_by_content.setdefault(int(page["content_id"]), []).append(record)

        canonical_path: dict[int, str] = {}
        for row in rows:
            if row["content_id"] is not None:
                canonical_path.setdefault(int(row["content_id"]), row["relative_path"])

        records: list[dict[str, Any]] = []
        for row in rows:
            content_id = int(row["content_id"]) if row["content_id"] else None
            is_duplicate = bool(
                content_id is not None
                and canonical_path.get(content_id) != row["relative_path"]
            )
            if row["processing_state"] == "error" or content_id is None:
                status = "error"
                document_kind = "unreadable"
                duplicate_of = None
                pages: list[dict[str, Any]] = []
            else:
                status = "duplicate" if is_duplicate else row["content_status"]
                document_kind = row["document_kind"]
                duplicate_of = canonical_path[content_id] if is_duplicate else None
                pages = [] if is_duplicate else pages_by_content.get(content_id, [])

            record = {
                "relative_path": row["relative_path"],
                "file_name": row["file_name"],
                "size_bytes": row["size_bytes"],
                "modified_time_ns": row["modified_time_ns"],
                "sha256": row["sha256"],
                "status": status,
                "duplicate_of": duplicate_of,
                "page_count": int(row["page_count"] or 0),
                "document_kind": document_kind,
                "pdf_metadata": _json_load(row["pdf_metadata_json"], {}),
                "page_kind_counts": _json_load(row["page_kind_counts_json"], {}),
                "pages": pages,
            }
            error = row["error"] or row["content_error"]
            if error:
                record["error"] = error
            if is_duplicate:
                record["analysis_reused"] = True
            records.append(record)
        return records

    def run_history(self, library_id: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM audit_runs
            WHERE library_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (library_id, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "status": row["status"],
                "full_rescan": bool(row["full_rescan"]),
                "counters": _json_load(row["counters_json"], {}),
                "error": row["error"],
            }
            for row in rows
        ]
