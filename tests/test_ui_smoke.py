from pathlib import Path

from workcall_transcriber.autostart import AutostartManager
from workcall_transcriber.controller import AppSnapshot
from workcall_transcriber.database import JobRepository
from workcall_transcriber.models import Settings
from workcall_transcriber.paths import RuntimePaths
from workcall_transcriber.security import CredentialStore
from workcall_transcriber.ui import MainWindow, QMessageBox, SettingsDialog


class FakeRegistry:
    def set_value(self, *_):
        return None

    def remove_value(self, *_):
        return None

    def get_value(self, *_):
        return None


class RecordingAutostart:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def set_enabled(self, enabled: bool) -> None:
        self.calls.append(enabled)


class FakeController:
    def __init__(self) -> None:
        self.settings = Settings()

    def snapshot(self) -> AppSnapshot:
        return AppSnapshot(self.settings, None, ())

    def set_automatic_enabled(self, enabled: bool):
        self.settings = Settings(automatic_enabled=enabled)


def test_main_window_renders_disabled_state_and_toggle(qtbot, tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    repository = JobRepository(paths.database)
    repository.initialize()
    controller = FakeController()
    window = MainWindow(
        controller=controller,
        paths=paths,
        credentials=CredentialStore(paths.credentials_file),
        repository=repository,
        autostart=AutostartManager("test", FakeRegistry()),
        launch_command="test",
    )
    qtbot.addWidget(window)

    window.show()
    assert "DISABLED" in window.status_badge.text()
    window.toggle_automatic()
    assert "ENABLED" in window.status_badge.text()
    window.request_quit()
    assert window.close() is True


def test_settings_exposes_the_fixed_inbox_without_allowing_another_watched_folder(
    qtbot, tmp_path: Path
) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    dialog = SettingsDialog(
        controller=FakeController(),
        paths=paths,
        credentials=CredentialStore(paths.credentials_file),
        autostart=AutostartManager("test", FakeRegistry()),
        launch_command="test",
        readiness=None,
    )
    qtbot.addWidget(dialog)

    assert dialog.inbox.isReadOnly() is True
    assert dialog.inbox.text() == str(paths.inbox)


def test_main_window_opens_the_effective_configured_archive(qtbot, tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    repository = JobRepository(paths.database)
    repository.initialize()
    controller = FakeController()
    custom_archive = paths.root / "CallArchive"
    controller.settings = Settings(archive_path=custom_archive)
    window = MainWindow(
        controller=controller,
        paths=paths,
        credentials=CredentialStore(paths.credentials_file),
        repository=repository,
        autostart=AutostartManager("test", FakeRegistry()),
        launch_command="test",
    )
    qtbot.addWidget(window)
    opened: list[Path] = []
    window.open_path = opened.append  # type: ignore[method-assign]

    window.open_archive()

    assert opened == [custom_archive]


def test_invalid_settings_do_not_change_autostart_before_rejection(qtbot, tmp_path: Path, monkeypatch) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    autostart = RecordingAutostart()
    dialog = SettingsDialog(
        controller=FakeController(),
        paths=paths,
        credentials=CredentialStore(paths.credentials_file),
        autostart=autostart,  # type: ignore[arg-type]
        launch_command="test",
        readiness=None,
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_: None)
    dialog.archive.setText(str(tmp_path / "outside-workcalls"))
    dialog.autostart.setChecked(False)

    dialog.save()

    assert autostart.calls == []
