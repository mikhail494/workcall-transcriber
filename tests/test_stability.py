from datetime import UTC, datetime, timedelta
from pathlib import Path

from workcall_transcriber.stability import FileStabilityChecker, StabilityState, is_supported_media


def test_unsupported_and_hidden_files_are_never_eligible(tmp_path: Path) -> None:
    assert is_supported_media(tmp_path / "call.mkv") is True
    assert is_supported_media(tmp_path / ".call.mkv") is False
    assert is_supported_media(tmp_path / "call.mkv.part") is False
    assert is_supported_media(tmp_path / "notes.txt") is False


def test_file_becomes_stable_only_after_unchanged_window(tmp_path: Path) -> None:
    source = tmp_path / "call.mkv"
    source.write_bytes(b"media")
    checker = FileStabilityChecker(15, probe=lambda _: True)
    start = datetime(2026, 9, 2, tzinfo=UTC)

    first = checker.assess(source, None, start)
    waiting = checker.assess(source, first.observation, start + timedelta(seconds=14))
    stable = checker.assess(source, waiting.observation, start + timedelta(seconds=15))

    assert first.state is StabilityState.CHANGED
    assert waiting.state is StabilityState.WAITING
    assert stable.state is StabilityState.STABLE


def test_file_that_changes_resets_its_stability_window(tmp_path: Path) -> None:
    source = tmp_path / "call.mkv"
    source.write_bytes(b"media")
    checker = FileStabilityChecker(15, probe=lambda _: True)
    start = datetime(2026, 9, 2, tzinfo=UTC)
    first = checker.assess(source, None, start)
    source.write_bytes(b"media plus more")

    changed = checker.assess(source, first.observation, start + timedelta(seconds=20))

    assert changed.state is StabilityState.CHANGED
    assert changed.observation.stable_since == start + timedelta(seconds=20)
