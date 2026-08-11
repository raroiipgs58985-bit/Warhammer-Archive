from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from warhammer_archive.gui_model import (
    default_output_directory,
    outcome_label,
    summary_cards,
)


class GuiModelTests(unittest.TestCase):
    def test_summary_cards_extract_user_facing_counts(self) -> None:
        summary = {
            "files": {
                "discovered": 1234,
                "unique_by_sha256": 1200,
                "missing_from_library": 7,
                "by_status": {
                    "duplicate": 34,
                    "error": 2,
                    "encrypted": 1,
                },
            },
            "pages": {
                "by_kind_unique_contents": {
                    "scan_candidate": 90,
                    "image_only": 10,
                }
            },
        }

        cards = dict(summary_cards(summary))

        self.assertEqual(cards["PDF"], "1 234")
        self.assertEqual(cards["Уникальные"], "1 200")
        self.assertEqual(cards["Скан-страницы"], "100")
        self.assertEqual(cards["Дубликаты"], "34")
        self.assertEqual(cards["Проблемы"], "3")
        self.assertEqual(cards["Отсутствуют"], "7")

    def test_default_output_is_outside_library(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            library = Path(temp_dir) / "Warhammer Library"
            library.mkdir()

            output = default_output_directory(library)

            self.assertEqual(output.parent, library.parent)
            self.assertEqual(output.name, "Warhammer Archive Data")
            self.assertNotEqual(output, library)

    def test_unknown_outcome_remains_readable(self) -> None:
        self.assertEqual(outcome_label("future_status"), "future status")


if __name__ == "__main__":
    unittest.main()

