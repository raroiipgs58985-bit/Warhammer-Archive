from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from warhammer_archive.audit import (
    AuditCancelled,
    AuditConfig,
    audit_library,
    inspect_pdf,
    iter_pdf_files,
)
from warhammer_archive.catalog import Catalog


def make_text_pdf(
    path: Path,
    text: str = "Imperial archive record. " * 20,
) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def make_scan_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=300, height=400)
    pixmap = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 10, 10),
        False,
    )
    pixmap.clear_with(220)
    page.insert_image(page.rect, stream=pixmap.tobytes("png"))
    document.save(path)
    document.close()


class LibraryAuditTests(unittest.TestCase):
    def test_audit_classifies_pages_and_detects_exact_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()

            text_pdf = library / "a_text.pdf"
            make_text_pdf(text_pdf)
            shutil.copy2(text_pdf, library / "b_duplicate.PDF")
            make_scan_pdf(library / "c_scan.pdf")

            summary = audit_library(AuditConfig(library, output))

            self.assertEqual(summary["files"]["discovered"], 3)
            self.assertEqual(summary["files"]["unique_by_sha256"], 2)
            self.assertEqual(summary["files"]["by_status"]["duplicate"], 1)
            self.assertEqual(summary["pages"]["classified_unique_contents"], 2)
            self.assertEqual(summary["run"]["counters"]["analyzed"], 2)
            self.assertEqual(summary["run"]["counters"]["duplicate"], 1)
            self.assertTrue((output / "catalog.sqlite").is_file())

            records = [
                json.loads(line)
                for line in (output / "documents.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(len(records), 3)

            text_record = next(
                record for record in records if record["relative_path"] == "a_text.pdf"
            )
            duplicate_record = next(
                record
                for record in records
                if record["relative_path"] == "b_duplicate.PDF"
            )
            scan_record = next(
                record for record in records if record["relative_path"] == "c_scan.pdf"
            )

            self.assertEqual(text_record["document_kind"], "text")
            self.assertEqual(text_record["pages"][0]["kind"], "text")
            self.assertEqual(duplicate_record["status"], "duplicate")
            self.assertEqual(duplicate_record["duplicate_of"], "a_text.pdf")
            self.assertEqual(scan_record["document_kind"], "scanned")
            self.assertEqual(scan_record["pages"][0]["kind"], "scan_candidate")

    def test_second_run_reuses_unchanged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            make_text_pdf(library / "one.pdf")
            make_scan_pdf(library / "two.pdf")

            first = audit_library(AuditConfig(library, output))
            second = audit_library(AuditConfig(library, output))

            self.assertEqual(first["run"]["counters"]["analyzed"], 2)
            self.assertEqual(
                second["run"]["counters"]["reused_unchanged"], 2
            )
            self.assertNotIn("analyzed", second["run"]["counters"])

    def test_remaining_duplicate_becomes_canonical_after_original_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            original = library / "a_original.pdf"
            duplicate = library / "b_duplicate.pdf"
            make_text_pdf(original)
            shutil.copy2(original, duplicate)
            audit_library(AuditConfig(library, output))

            original.unlink()
            summary = audit_library(AuditConfig(library, output))
            records = [
                json.loads(line)
                for line in (output / "documents.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertEqual(summary["files"]["missing_from_library"], 1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["relative_path"], "b_duplicate.pdf")
            self.assertEqual(records[0]["status"], "ok")
            self.assertEqual(records[0]["pages"][0]["kind"], "text")

    def test_changed_file_is_analyzed_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            pdf = library / "changing.pdf"
            make_text_pdf(pdf)
            audit_library(AuditConfig(library, output))

            pdf.unlink()
            make_text_pdf(pdf, "A completely different archive entry. " * 40)
            summary = audit_library(AuditConfig(library, output))

            self.assertEqual(summary["run"]["counters"]["analyzed"], 1)
            self.assertEqual(summary["files"]["unique_by_sha256"], 1)

    def test_full_rescan_reanalyzes_unchanged_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            make_text_pdf(library / "unchanged.pdf")
            audit_library(AuditConfig(library, output))

            summary = audit_library(
                AuditConfig(library, output, full_rescan=True)
            )

            self.assertEqual(summary["run"]["counters"]["analyzed"], 1)
            self.assertNotIn(
                "reused_unchanged", summary["run"]["counters"]
            )

    def test_changed_analysis_settings_invalidate_cached_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            make_text_pdf(library / "settings.pdf")
            audit_library(AuditConfig(library, output, min_text_chars=80))

            summary = audit_library(
                AuditConfig(library, output, min_text_chars=10_000)
            )

            self.assertEqual(summary["run"]["counters"]["analyzed"], 1)
            self.assertEqual(
                summary["files"]["by_document_kind"]["text"], 1
            )

    def test_moved_file_keeps_analysis_without_missing_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            original = library / "original.pdf"
            make_text_pdf(original)
            audit_library(AuditConfig(library, output))

            destination_dir = library / "renamed"
            destination_dir.mkdir()
            original.rename(destination_dir / "moved.pdf")
            summary = audit_library(AuditConfig(library, output))

            self.assertEqual(summary["run"]["counters"]["moved"], 1)
            self.assertEqual(summary["files"]["missing_from_library"], 0)
            records = [
                json.loads(line)
                for line in (output / "documents.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                [record["relative_path"] for record in records],
                ["renamed/moved.pdf"],
            )

    def test_deleted_file_is_marked_missing_without_history_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            pdf = library / "deleted.pdf"
            make_text_pdf(pdf)
            audit_library(AuditConfig(library, output))

            pdf.unlink()
            summary = audit_library(AuditConfig(library, output))

            self.assertEqual(summary["files"]["discovered"], 0)
            self.assertEqual(summary["files"]["missing_from_library"], 1)
            self.assertEqual(summary["run"]["counters"]["newly_missing"], 1)
            self.assertEqual(
                (output / "documents.jsonl").read_text(encoding="utf-8"), ""
            )

    def test_new_run_marks_abandoned_run_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            library.mkdir()
            database = root / "catalog.sqlite"

            with Catalog(database) as catalog:
                library_id = catalog.get_or_create_library(library)
                abandoned_run = catalog.start_run(library_id, full_rescan=False)

            with Catalog(database) as catalog:
                library_id = catalog.get_or_create_library(library)
                active_run = catalog.start_run(library_id, full_rescan=False)
                history = catalog.run_history(library_id)
                catalog.finish_run(active_run, {})

            abandoned = next(
                run for run in history if run["id"] == abandoned_run
            )
            self.assertEqual(abandoned["status"], "interrupted")

    def test_interrupted_audit_resumes_from_completed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            make_text_pdf(library / "a_done.pdf")
            make_text_pdf(library / "b_interrupted.pdf", "Second document. " * 30)

            def interrupt_second(*args, **kwargs):
                path = kwargs.get("path", args[0] if args else None)
                if path.name == "b_interrupted.pdf":
                    raise KeyboardInterrupt()
                return inspect_pdf(*args, **kwargs)

            with patch(
                "warhammer_archive.audit.inspect_pdf",
                side_effect=interrupt_second,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    audit_library(AuditConfig(library, output))

            resumed = audit_library(AuditConfig(library, output))

            self.assertEqual(
                resumed["run"]["counters"]["reused_unchanged"], 1
            )
            self.assertEqual(resumed["run"]["counters"]["analyzed"], 1)
            with Catalog(output / "catalog.sqlite") as catalog:
                library_id = catalog.get_or_create_library(library)
                statuses = [run["status"] for run in catalog.run_history(library_id)]
            self.assertIn("interrupted", statuses)

    def test_cooperative_cancellation_stops_between_files_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            output = root / "report"
            library.mkdir()
            make_text_pdf(library / "a_done.pdf")
            make_text_pdf(library / "b_waiting.pdf", "Waiting record. " * 30)
            cancel = False

            def progress(*_args) -> None:
                nonlocal cancel
                cancel = True

            with self.assertRaises(AuditCancelled):
                audit_library(
                    AuditConfig(library, output),
                    progress=progress,
                    cancel_requested=lambda: cancel,
                )

            resumed = audit_library(AuditConfig(library, output))
            self.assertEqual(
                resumed["run"]["counters"]["reused_unchanged"], 1
            )
            self.assertEqual(resumed["run"]["counters"]["analyzed"], 1)
            with Catalog(output / "catalog.sqlite") as catalog:
                library_id = catalog.get_or_create_library(library)
                statuses = [run["status"] for run in catalog.run_history(library_id)]
            self.assertIn("cancelled", statuses)

    def test_iter_pdf_files_is_recursive_and_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            (root / "one.pdf").touch()
            (nested / "two.PDF").touch()
            (nested / "ignored.txt").touch()

            paths = [path.relative_to(root).as_posix() for path in iter_pdf_files(root)]
            self.assertEqual(paths, ["nested/two.PDF", "one.pdf"])

    def test_missing_library_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "не найдена"):
                audit_library(
                    AuditConfig(root / "missing", root / "report")
                )


if __name__ == "__main__":
    unittest.main()
