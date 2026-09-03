"""Windows GUI entry point for WorkCall Transcriber."""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication

from . import __version__
from .archive import ArchiveManager
from .autostart import AutostartManager
from .config import SettingsStore
from .controller import ControllerCallbacks, WorkCallController
from .database import JobRepository
from .logging_config import configure_application_logging
from .paths import RuntimePaths
from .queue_service import QueueService
from .runtime import RuntimeInspector
from .security import CredentialStore
from .single_instance import SingleInstance
from .stability import FileStabilityChecker
from .tray import TrayManager
from .ui import ControllerBridge, MainWindow
from .worker_runner import WorkerRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="WorkCall Transcriber")
    parser.add_argument("--minimized", action="store_true", help="Start in the system tray")
    parser.add_argument("--version", action="version", version=__version__)
    arguments = parser.parse_args(argv)

    application = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    application.setApplicationName("WorkCall Transcriber")
    application.setOrganizationName("WorkCall Transcriber")
    application.setQuitOnLastWindowClosed(False)
    application.setStyle("Fusion")

    instance = SingleInstance()
    window_holder: list[MainWindow] = []

    def activate_existing() -> None:
        if window_holder:
            window = window_holder[0]
            window.showNormal()
            window.raise_()
            window.activateWindow()

    if not instance.acquire(activate_existing):
        return 0

    paths = RuntimePaths(Path(r"D:\WorkCalls"))
    credentials = CredentialStore(paths.credentials_file)
    logger = configure_application_logging(paths.logs)
    settings_store = SettingsStore(paths)
    repository = JobRepository(paths.database)
    queue_service = QueueService(
        paths=paths,
        settings_store=settings_store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15),
        clock=lambda: datetime.now(UTC),
    )
    bridge = ControllerBridge()
    controller = WorkCallController(
        paths=paths,
        settings_store=settings_store,
        repository=repository,
        queue_service=queue_service,
        credential_store=credentials,
        worker_runner=WorkerRunner(),
        callbacks=ControllerCallbacks(
            state_changed=bridge.state_changed.emit,
            worker_event=bridge.worker_event.emit,
            notification=bridge.notification.emit,
        ),
    )
    launch_command = _launch_command()
    autostart = AutostartManager(launch_command)
    window = MainWindow(
        controller=controller,
        paths=paths,
        credentials=credentials,
        repository=repository,
        autostart=autostart,
        launch_command=launch_command,
    )
    window_holder.append(window)
    tray: TrayManager | None = None

    def quit_application() -> None:
        window._quitting = True  # The close policy is intentionally controlled here.
        controller.shutdown()
        if tray is not None:
            tray.hide()
        instance.close()
        application.quit()

    tray = TrayManager(
        controller=controller,
        window=window,
        paths=paths,
        quit_callback=window.request_quit,
    )
    bridge.state_changed.connect(window.refresh)
    bridge.state_changed.connect(tray.refresh)
    bridge.worker_event.connect(window.handle_worker_event)
    bridge.notification.connect(tray.notify)
    bridge.runtime_ready.connect(window.update_readiness)
    window.quit_requested.connect(quit_application)
    tray.show()
    controller.start()

    def inspect_runtime() -> None:
        try:
            bridge.runtime_ready.emit(RuntimeInspector(credentials).inspect())
        except Exception:
            logger.exception("Runtime readiness inspection failed")

    threading.Thread(target=inspect_runtime, name="WorkCallRuntimeCheck", daemon=True).start()
    if not arguments.minimized:
        window.show()
    try:
        return application.exec()
    finally:
        controller.shutdown()
        tray.hide()
        instance.close()


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" -m workcall_transcriber --minimized'


if __name__ == "__main__":
    raise SystemExit(main())
