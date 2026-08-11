from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    QSettings,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .audit import AuditCancelled, AuditConfig, audit_library
from .gui_model import default_output_directory, outcome_label, summary_cards


APP_STYLESHEET = """
QWidget {
    background: #17181b;
    color: #e6e0d6;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QMainWindow {
    background: #121315;
}
QLabel#title {
    color: #f0e8dc;
    font-size: 22pt;
    font-weight: 700;
    letter-spacing: 2px;
}
QLabel#subtitle, QLabel#muted {
    color: #aaa297;
}
QFrame#panel, QFrame#card {
    background: #202226;
    border: 1px solid #35383d;
    border-radius: 8px;
}
QLabel#cardTitle {
    color: #aaa297;
    font-size: 9pt;
}
QLabel#cardValue {
    color: #f0e8dc;
    font-size: 18pt;
    font-weight: 700;
}
QLineEdit, QPlainTextEdit {
    background: #111214;
    border: 1px solid #3b3e44;
    border-radius: 5px;
    padding: 7px;
    selection-background-color: #7c293c;
}
QLineEdit:focus, QPlainTextEdit:focus {
    border-color: #873047;
}
QPushButton {
    background: #31343a;
    border: 1px solid #474b52;
    border-radius: 5px;
    padding: 8px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background: #3a3e45;
}
QPushButton#primary {
    background: #72273a;
    border-color: #9a3a54;
}
QPushButton#primary:hover {
    background: #873047;
}
QPushButton#danger {
    background: #40242b;
    border-color: #6f3542;
}
QPushButton:disabled {
    background: #24262a;
    border-color: #303238;
    color: #6f706f;
}
QProgressBar {
    background: #111214;
    border: 1px solid #3b3e44;
    border-radius: 5px;
    min-height: 22px;
    text-align: center;
}
QProgressBar::chunk {
    background: #7c293c;
    border-radius: 4px;
}
QCheckBox {
    spacing: 8px;
}
"""


class AuditWorker(QObject):
    progress = Signal(int, int, str, str)
    completed = Signal(object)
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, config: AuditConfig, cancel_event: threading.Event) -> None:
        super().__init__()
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            summary = audit_library(
                self.config,
                progress=self.progress.emit,
                cancel_requested=self.cancel_event.is_set,
            )
        except AuditCancelled:
            self.cancelled.emit()
        except Exception:
            self.failed.emit(traceback.format_exc())
        else:
            self.completed.emit(summary)
        finally:
            self.finished.emit()


class ArchiveWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"Warhammer Archive {__version__}")
        self.setMinimumSize(880, 680)
        self.resize(980, 760)

        self.settings = QSettings()
        self._thread: QThread | None = None
        self._worker: AuditWorker | None = None
        self._cancel_event: threading.Event | None = None
        self._close_when_finished = False
        self._card_values: dict[str, QLabel] = {}

        self._build_ui()
        self._restore_settings()

    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(14)

        title = QLabel("WARHAMMER ARCHIVE")
        title.setObjectName("title")
        subtitle = QLabel(
            "Локальный аудит PDF-библиотеки — без отправки книг в интернет"
        )
        subtitle.setObjectName("subtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        paths_panel = QFrame()
        paths_panel.setObjectName("panel")
        paths_layout = QGridLayout(paths_panel)
        paths_layout.setContentsMargins(16, 16, 16, 16)
        paths_layout.setHorizontalSpacing(10)
        paths_layout.setVerticalSpacing(10)

        paths_layout.addWidget(QLabel("Библиотека PDF"), 0, 0)
        self.library_edit = QLineEdit()
        self.library_edit.setPlaceholderText(r"Например: D:\Warhammer Library")
        self.library_button = QPushButton("Выбрать…")
        self.library_button.clicked.connect(self._choose_library)
        paths_layout.addWidget(self.library_edit, 0, 1)
        paths_layout.addWidget(self.library_button, 0, 2)

        paths_layout.addWidget(QLabel("Данные приложения"), 1, 0)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(r"Например: D:\Warhammer Archive Data")
        self.output_button = QPushButton("Выбрать…")
        self.output_button.clicked.connect(self._choose_output)
        paths_layout.addWidget(self.output_edit, 1, 1)
        paths_layout.addWidget(self.output_button, 1, 2)

        self.full_rescan = QCheckBox(
            "Полная перепроверка — игнорировать сохранённый результат"
        )
        paths_layout.addWidget(self.full_rescan, 2, 1, 1, 2)
        paths_layout.setColumnStretch(1, 1)
        root.addWidget(paths_panel)

        actions = QHBoxLayout()
        self.start_button = QPushButton("Начать аудит")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start_audit)
        self.cancel_button = QPushButton("Остановить после текущего PDF")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._request_cancel)
        self.open_output_button = QPushButton("Открыть папку отчёта")
        self.open_output_button.setEnabled(False)
        self.open_output_button.clicked.connect(self._open_output)
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        actions.addWidget(self.open_output_button)
        root.addLayout(actions)

        self.status_label = QLabel("Готов к запуску")
        self.status_label.setObjectName("muted")
        self.current_file_label = QLabel("—")
        self.current_file_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        root.addWidget(self.status_label)
        root.addWidget(self.current_file_label)
        root.addWidget(self.progress_bar)

        cards = QGridLayout()
        for column, label in enumerate(
            ("PDF", "Уникальные", "Скан-страницы", "Дубликаты", "Проблемы", "Отсутствуют")
        ):
            card = QFrame()
            card.setObjectName("card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            card_title = QLabel(label)
            card_title.setObjectName("cardTitle")
            card_value = QLabel("—")
            card_value.setObjectName("cardValue")
            card_layout.addWidget(card_title)
            card_layout.addWidget(card_value)
            cards.addWidget(card, 0, column)
            self._card_values[label] = card_value
        root.addLayout(cards)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        self.log.setPlaceholderText("Здесь появится ход обработки файлов")
        root.addWidget(self.log, 1)

        self.setCentralWidget(central)

    def _restore_settings(self) -> None:
        self.library_edit.setText(str(self.settings.value("libraryPath", "")))
        self.output_edit.setText(str(self.settings.value("outputPath", "")))
        self.full_rescan.setChecked(
            self.settings.value("fullRescan", False, type=bool)
        )
        if self.output_edit.text() and Path(self.output_edit.text()).exists():
            self.open_output_button.setEnabled(True)

    @Slot()
    def _choose_library(self) -> None:
        initial = self.library_edit.text() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку с PDF",
            initial,
        )
        if not selected:
            return
        self.library_edit.setText(selected)
        if not self.output_edit.text().strip():
            self.output_edit.setText(
                str(default_output_directory(Path(selected)))
            )

    @Slot()
    def _choose_output(self) -> None:
        initial = self.output_edit.text() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Выберите папку данных приложения",
            initial,
        )
        if selected:
            self.output_edit.setText(selected)

    def _validated_config(self) -> AuditConfig | None:
        library_text = self.library_edit.text().strip()
        output_text = self.output_edit.text().strip()
        if not library_text:
            QMessageBox.warning(self, "Не выбрана библиотека", "Укажите папку с PDF.")
            return None
        library = Path(library_text).expanduser()
        if not library.is_dir():
            QMessageBox.warning(
                self,
                "Папка не найдена",
                f"Библиотека не найдена:\n{library}",
            )
            return None
        if not output_text:
            output_text = str(default_output_directory(library))
            self.output_edit.setText(output_text)

        return AuditConfig(
            library_root=library,
            output_dir=Path(output_text).expanduser(),
            full_rescan=self.full_rescan.isChecked(),
        )

    @Slot()
    def _start_audit(self) -> None:
        if self._thread is not None:
            return
        config = self._validated_config()
        if config is None:
            return

        self.settings.setValue("libraryPath", str(config.library_root))
        self.settings.setValue("outputPath", str(config.output_dir))
        self.settings.setValue("fullRescan", config.full_rescan)

        self._cancel_event = threading.Event()
        thread = QThread(self)
        worker = AuditWorker(config, self._cancel_event)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_completed)
        worker.cancelled.connect(self._on_cancelled)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        self._set_running(True)
        self.log.clear()
        self.status_label.setText("Подготовка каталога…")
        self.current_file_label.setText("Поиск PDF")
        self.progress_bar.setRange(0, 0)
        thread.start()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.library_button.setEnabled(not running)
        self.output_button.setEnabled(not running)
        self.library_edit.setEnabled(not running)
        self.output_edit.setEnabled(not running)
        self.full_rescan.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    @Slot(int, int, str, str)
    def _on_progress(self, index: int, total: int, path: str, outcome: str) -> None:
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(index)
        self.progress_bar.setFormat(f"{index} / {total}")
        self.status_label.setText(f"Обработано {index} из {total}")
        self.current_file_label.setText(path)
        self.log.appendPlainText(f"{outcome_label(outcome)} — {path}")

    @Slot(object)
    def _on_completed(self, summary: dict[str, Any]) -> None:
        self.status_label.setText("Аудит завершён")
        self.current_file_label.setText(
            str(Path(summary["output_dir"]) / "summary.json")
        )
        self.open_output_button.setEnabled(True)
        for label, value in summary_cards(summary):
            self._card_values[label].setText(value)
        self.log.appendPlainText("Готово. Отчёты и каталог сохранены.")

    @Slot()
    def _on_cancelled(self) -> None:
        self.status_label.setText("Аудит остановлен")
        self.current_file_label.setText(
            "Готовые файлы сохранены. Следующий запуск продолжит обработку."
        )
        self.log.appendPlainText("Остановлено по запросу пользователя.")

    @Slot(str)
    def _on_failed(self, details: str) -> None:
        self.status_label.setText("Ошибка аудита")
        self.log.appendPlainText(details)
        QMessageBox.critical(
            self,
            "Ошибка",
            "Аудит завершился с ошибкой. Подробности сохранены в журнале окна.",
        )

    @Slot()
    def _request_cancel(self) -> None:
        if self._cancel_event is None:
            return
        self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Остановка после текущего PDF…")

    @Slot()
    def _open_output(self) -> None:
        output = Path(self.output_edit.text()).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output.resolve())))

    @Slot()
    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._cancel_event = None
        self._set_running(False)
        if self._close_when_finished:
            self._close_when_finished = False
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None:
            self._close_when_finished = True
            self._request_cancel()
            event.ignore()
            return
        event.accept()


def main(argv: list[str] | None = None) -> int:
    application = QApplication(argv if argv is not None else sys.argv)
    application.setOrganizationName("WarhammerArchive")
    application.setApplicationName("WarhammerArchive")
    application.setApplicationVersion(__version__)
    application.setFont(QFont("Segoe UI", 10))
    application.setStyleSheet(APP_STYLESHEET)

    window = ArchiveWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

