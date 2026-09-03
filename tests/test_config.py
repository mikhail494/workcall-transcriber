from pathlib import Path

from workcall_transcriber.config import SettingsStore
from workcall_transcriber.models import Settings
from workcall_transcriber.paths import RuntimePaths


def test_first_run_defaults_to_disabled_automatic_processing(tmp_path: Path) -> None:
    store = SettingsStore(RuntimePaths(tmp_path / "WorkCalls"))

    settings = store.load()

    assert settings.automatic_enabled is False
    assert settings.language == "auto"
    assert settings.batch_size == 8


def test_settings_round_trip_without_storing_secrets(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    store = SettingsStore(paths)
    expected = Settings(automatic_enabled=True, language="ru", batch_size=6)

    store.save(expected)

    assert store.load() == expected
    assert "token" not in paths.settings_file.read_text(encoding="utf-8").lower()


def test_settings_reject_archive_inside_temp_directory(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    settings = Settings(archive_path=paths.temp / "archive")

    errors = settings.validate(paths)

    assert "archive" in " ".join(errors).lower()


def test_settings_reject_a_custom_inbox_and_keeps_the_fixed_workcalls_inbox(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    settings = Settings(inbox_path=tmp_path / "another-recording-folder")

    errors = settings.validate(paths)

    assert settings.effective_inbox(paths) == paths.inbox
    assert "only watches" in " ".join(errors).lower()


def test_settings_reject_archive_outside_the_workcalls_data_root(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    settings = Settings(archive_path=tmp_path / "another-project-archive")

    errors = settings.validate(paths)

    assert settings.effective_archive(paths) == paths.archive
    assert "workcalls data folder" in " ".join(errors).lower()
