"""Dynamic Windows system-tray experience for the local utility."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .controller import WorkCallController
from .models import RETRYABLE_JOB_STATUSES, JobStatus
from .paths import RuntimePaths
from .ui import MainWindow, make_status_icon


class TrayManager(QObject):
    """Render compact actions around the controller's one global state."""

    def __init__(
        self,
        *,
        controller: WorkCallController,
        window: MainWindow,
        paths: RuntimePaths,
        quit_callback,
    ) -> None:
        super().__init__(window)
        self._controller = controller
        self._window = window
        self._paths = paths
        self._quit_callback = quit_callback
        self._tray = QSystemTrayIcon(self)
        self._menu = QMenu()
        self._tray.setContextMenu(self._menu)
        self._menu.aboutToShow.connect(self._rebuild_menu)
        self._tray.activated.connect(self._activated)
        self.refresh()

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def refresh(self) -> None:
        snapshot = self._controller.snapshot()
        current = snapshot.current_job
        failed = any(job.status is JobStatus.FAILED for job in snapshot.recent_jobs)
        if current is not None:
            colour, tooltip = "#3c82f6", "WorkCall Transcriber — Processing"
        elif failed:
            colour, tooltip = "#d65b63", "WorkCall Transcriber — Attention required"
        elif snapshot.settings.automatic_enabled:
            colour, tooltip = "#32a66b", "WorkCall Transcriber — Automatic processing enabled"
        else:
            colour, tooltip = "#7d8798", "WorkCall Transcriber — Automatic processing disabled"
        self._tray.setIcon(make_status_icon(colour))
        self._tray.setToolTip(tooltip)

    def notify(self, title: str, message: str) -> None:
        if self._controller.snapshot().settings.desktop_notifications:
            self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def _rebuild_menu(self) -> None:
        self._menu.clear()
        snapshot = self._controller.snapshot()
        toggle = self._menu.addAction(
            "Disable automatic processing"
            if snapshot.settings.automatic_enabled
            else "Enable automatic processing"
        )
        toggle.triggered.connect(self._window.toggle_automatic)
        self._menu.addAction("Open WorkCall Transcriber", self._show_window)
        self._menu.addAction("Process file…", self._window.process_file)
        self._menu.addSeparator()
        self._menu.addAction("Open WorkCalls folder", lambda: self._window.open_path(self._paths.root))
        completed = next(
            (
                job
                for job in snapshot.recent_jobs
                if job.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
            ),
            None,
        )
        if completed is not None:
            self._menu.addAction("Open last result", self._window.open_last_result)
        failed = next((job for job in snapshot.recent_jobs if job.status in RETRYABLE_JOB_STATUSES), None)
        if failed is not None:
            self._menu.addAction("Retry last failed job", self._window.retry_last_failed)
        if snapshot.current_job is not None:
            self._menu.addAction("Stop current processing", self._window.stop_processing)
        self._menu.addSeparator()
        self._menu.addAction("Settings", self._window.open_settings)
        self._menu.addAction("Quit", self._quit_callback)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_window()

    def _show_window(self) -> None:
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()
