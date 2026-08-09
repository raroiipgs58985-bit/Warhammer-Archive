from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

from warhammer_archive.audit import AuditConfig, audit_library, iter_pdf_files


def make_text_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Imperial archive record. " * 20)
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
            self.assertEqual(summary["pages"]["analyzed_unique_files"], 2)

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
