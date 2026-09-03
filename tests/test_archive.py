from datetime import UTC, datetime
from pathlib import Path

import pytest

import workcall_transcriber.archive as archive_module
from workcall_transcriber.archive import ArchiveManager


def test_inbox_recording_is_moved_safely_into_its_job_archive(tmp_path: Path) -> None:
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    source = inbox / "call.mkv"
    source.write_bytes(b"original recording")
    manager = ArchiveManager(tmp_path / "Archive")
    job_dir = manager.create_job_directory("abc12345", source.name, datetime(2026, 9, 2, tzinfo=UTC))

    imported = manager.import_inbox(source, job_dir)

    assert source.exists() is False
    assert imported.media_path.read_bytes() == b"original recording"
    assert imported.sha256 == "b9847a4fdf31702fb60bd2f225b53a79a922bd6c6f9a1a5f88f21c4a12134d2d"
    assert job_dir.parent.name == "2026-09-02"


def test_manual_source_is_copied_and_external_original_is_left_intact(tmp_path: Path) -> None:
    source = tmp_path / "Outside" / "call.mp4"
    source.parent.mkdir()
    source.write_bytes(b"external original")
    manager = ArchiveManager(tmp_path / "Archive")
    job_dir = manager.create_job_directory("def67890", source.name, datetime(2026, 9, 2, tzinfo=UTC))

    imported = manager.copy_manual(source, job_dir)

    assert source.exists()
    assert source.read_bytes() == b"external original"
    assert imported.media_path.read_bytes() == b"external original"


def test_manual_copy_rejects_a_destination_that_does_not_match_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "Outside" / "call.mp4"
    source.parent.mkdir()
    source.write_bytes(b"external original")
    manager = ArchiveManager(tmp_path / "Archive")
    job_dir = manager.create_job_directory("def67890", source.name, datetime(2026, 9, 2, tzinfo=UTC))

    def corrupt_copy(_: Path, target: Path) -> str:
        target.write_bytes(b"corrupted copy")
        return str(target)

    monkeypatch.setattr(archive_module.shutil, "copy2", corrupt_copy)

    with pytest.raises(OSError, match="did not match"):
        manager.copy_manual(source, job_dir)

    assert source.read_bytes() == b"external original"
    assert not (job_dir / "original_call.mp4").exists()
    assert (job_dir / ".original_call.mp4.copying").read_bytes() == b"corrupted copy"
