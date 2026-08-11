from __future__ import annotations

import os
import tempfile
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    from warhammer_archive.gui import ArchiveWindow

    PYSIDE_AVAILABLE = True
except ModuleNotFoundError:
    PYSIDE_AVAILABLE = False


@unittest.skipUnless(PYSIDE_AVAILABLE, "PySide6 optional dependency is not installed")
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings_dir = tempfile.TemporaryDirectory()
        QCoreApplication.setOrganizationName("WarhammerArchiveTests")
        QCoreApplication.setApplicationName("WarhammerArchiveTests")
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            cls.settings_dir.name,
        )
        cls.application = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.settings_dir.cleanup()

    def test_main_window_can_be_constructed_without_display(self) -> None:
        window = ArchiveWindow()
        try:
            self.assertIn("Warhammer Archive", window.windowTitle())
            self.assertEqual(window.start_button.objectName(), "primary")
            self.assertEqual(len(window._card_values), 6)
            self.assertIsNone(window._thread)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()

