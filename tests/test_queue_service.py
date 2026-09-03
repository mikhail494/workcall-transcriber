import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from workcall_transcriber.archive import ArchiveManager
from workcall_transcriber.config import SettingsStore
from workcall_transcriber.database import JobRepository
from workcall_transcriber.models import JobOrigin, JobStatus, Settings
from workcall_transcriber.paths import RuntimePaths
from workcall_transcriber.queue_service import DuplicateSourceError, QueueService
from workcall_transcriber.stability import FileStabilityChecker


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 2, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


def _service(
    tmp_path: Path,
    enabled: bool,
    clock: Clock,
    *,
    settings_value: Settings | None = None,
) -> tuple[QueueService, JobRepository, RuntimePaths]:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    settings = SettingsStore(paths)
    settings.save(settings_value or Settings(automatic_enabled=enabled, stability_seconds=15))
    repository = JobRepository(paths.database)
    repository.initialize()
    service = QueueService(
        paths=paths,
        settings_store=settings,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=clock.now,
    )
    return service, repository, paths


def test_disabled_watcher_never_enqueues_inbox_file(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=False, clock=clock)
    source = paths.inbox / "call.mkv"
    source.write_bytes(b"media")

    assert service.observe_inbox(source) is None
    assert service.reconcile().detected == 0
    assert repository.list_recent() == []


def test_enabled_periodic_reconciliation_imports_once_after_stability(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=True, clock=clock)
    source = paths.inbox / "call.mkv"
    source.write_bytes(b"media")

    first = service.reconcile()
    clock.value += timedelta(seconds=16)
    second = service.reconcile()
    third = service.reconcile()

    jobs = repository.list_recent()
    assert first.detected == 1
    assert second.queued == 1
    assert third.detected == 0
    assert len(jobs) == 1
    assert jobs[0].status is JobStatus.QUEUED
    assert jobs[0].archived_media_path is not None and jobs[0].archived_media_path.exists()
    assert jobs[0].archive_dir is not None and (jobs[0].archive_dir / "manifest.json").exists()
    assert source.exists() is False


def test_changing_inbox_file_waits_for_a_full_final_stability_window(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=True, clock=clock)
    source = paths.inbox / "call.mkv"
    source.write_bytes(b"first bytes")

    first = service.reconcile()
    clock.value += timedelta(seconds=10)
    source.write_bytes(b"final complete recording")
    changed = service.reconcile()
    clock.value += timedelta(seconds=14)
    still_waiting = service.reconcile()
    clock.value += timedelta(seconds=1)
    stable = service.reconcile()

    job = repository.list_recent()[0]
    assert first.detected == 1
    assert changed.queued == 0
    assert still_waiting.queued == 0
    assert stable.queued == 1
    assert job.status is JobStatus.QUEUED
    assert source.exists() is False


def test_manual_file_is_copied_even_when_automatic_processing_is_disabled(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=False, clock=clock)
    source = tmp_path / "external.mp4"
    source.write_bytes(b"external media")

    service.enqueue_manual(source)
    clock.value += timedelta(seconds=16)
    service.reconcile()

    job = repository.list_recent()[0]
    assert job.origin is JobOrigin.MANUAL
    assert job.status is JobStatus.QUEUED
    assert source.exists()
    assert job.archived_media_path is not None and job.archived_media_path.read_bytes() == b"external media"
    assert paths.inbox.exists()


def test_queue_uses_the_configured_archive_path_inside_workcalls(tmp_path: Path) -> None:
    clock = Clock()
    custom_archive = tmp_path / "WorkCalls" / "CallArchive"
    settings = Settings(
        automatic_enabled=True,
        stability_seconds=15,
        archive_path=custom_archive,
    )
    service, repository, paths = _service(
        tmp_path,
        enabled=True,
        clock=clock,
        settings_value=settings,
    )
    source = paths.inbox / "call.mkv"
    source.write_bytes(b"media")

    service.reconcile()
    clock.value += timedelta(seconds=16)
    service.reconcile()

    job = repository.list_recent()[0]
    assert job.archive_dir is not None
    assert job.archive_dir.is_relative_to(custom_archive)


def test_manual_duplicate_requires_an_explicit_reprocess_choice(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, _ = _service(tmp_path, enabled=False, clock=clock)
    first = tmp_path / "first.mp4"
    first.write_bytes(b"same media")
    job = repository.detect(first, JobOrigin.MANUAL, first.stat().st_size, first.stat().st_mtime_ns)
    repository.transition(job.id, JobStatus.QUEUED)
    repository.claim_next_queued()
    from workcall_transcriber.archive import sha256_file

    repository.set_source_hash(job.id, sha256_file(first))
    repository.transition(job.id, JobStatus.COMPLETED)
    second = tmp_path / "second.mp4"
    second.write_bytes(b"same media")

    with pytest.raises(DuplicateSourceError):
        service.enqueue_manual(second)

    allowed = service.enqueue_manual(second, allow_duplicate=True)
    assert allowed.status is JobStatus.WAITING_FOR_STABLE


def test_retry_after_an_import_failure_returns_to_stability_before_queueing(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, _ = _service(tmp_path, enabled=False, clock=clock)
    source = tmp_path / "external.mp4"
    source.write_bytes(b"external media")
    job = repository.detect(source, JobOrigin.MANUAL, source.stat().st_size, source.stat().st_mtime_ns)
    repository.transition(job.id, JobStatus.FAILED, error_code="archive_import_failed")

    retried = service.retry(job.id)

    assert retried.status is JobStatus.WAITING_FOR_STABLE
    assert retried.queued_at is None
    assert retried.stable_since is None


def test_restart_recovers_an_inbox_import_after_the_move_was_durably_intended(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=True, clock=clock)
    source = paths.inbox / "call.mkv"
    source.write_bytes(b"media")
    job = repository.detect(source, JobOrigin.INBOX, source.stat().st_size, source.stat().st_mtime_ns)
    importing = repository.transition(job.id, JobStatus.IMPORTING)
    manager = ArchiveManager(paths.archive)
    archive_dir = manager.create_job_directory(importing.id, importing.source_name, clock.now())
    target = archive_dir / "original_call.mkv"
    repository.set_archive_intent(importing.id, archive_dir, target, importing.source_size)
    source.replace(target)

    recovered = service.recover_after_restart()

    restored = repository.get(job.id)
    assert recovered == 1
    assert restored.status is JobStatus.QUEUED
    assert restored.archived_media_path == target
    assert restored.source_sha256 is not None
    assert target.exists()
    assert source.exists() is False
    assert (archive_dir / "manifest.json").exists()


def test_restart_restores_a_completed_job_from_its_final_manifest(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=False, clock=clock)
    source = tmp_path / "external.mkv"
    source.write_bytes(b"media")
    archive_dir = paths.archive / "completed-job"
    archive_dir.mkdir()
    archived = archive_dir / "original_external.mkv"
    archived.write_bytes(source.read_bytes())
    job = repository.detect(source, JobOrigin.MANUAL, source.stat().st_size, source.stat().st_mtime_ns)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.set_archive(processing.id, archive_dir, archived, archived.stat().st_size)
    repository.set_source_hash(processing.id, "a" * 64)
    for name in ("transcript.json", "transcript.txt", "transcript.srt", "transcript.vtt", "transcript.tsv"):
        (archive_dir / name).write_text("{}", encoding="utf-8")
    (archive_dir / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job.id,
                "status": "completed",
                "completed_at": "2026-09-02T11:00:00+00:00",
                "media": {"duration_seconds": 12.5},
                "transcription": {
                    "detected_language": "ru",
                    "detected_language_probability": 0.99,
                    "language_source": "auto",
                },
                "diarization": {"speaker_count": 2},
                "outputs": {
                    "json": "transcript.json",
                    "txt": "transcript.txt",
                    "srt": "transcript.srt",
                    "vtt": "transcript.vtt",
                    "tsv": "transcript.tsv",
                },
                "errors": {},
            }
        ),
        encoding="utf-8",
    )

    recovered = service.recover_completed_manifests()

    restored = repository.get(job.id)
    assert recovered == 1
    assert restored.status is JobStatus.COMPLETED
    assert restored.completed_at == "2026-09-02T11:00:00+00:00"
    assert restored.detected_language == "ru"
    assert restored.speaker_count == 2
    assert repository.find_completed_by_hash("a" * 64).id == job.id


def test_restart_does_not_queue_a_partial_manual_copy_left_by_a_crash(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=False, clock=clock)
    source = tmp_path / "external.mkv"
    source.write_bytes(b"complete recording")
    job = repository.detect(source, JobOrigin.MANUAL, source.stat().st_size, source.stat().st_mtime_ns)
    importing = repository.transition(job.id, JobStatus.IMPORTING)
    manager = ArchiveManager(paths.archive)
    archive_dir = manager.create_job_directory(importing.id, importing.source_name, clock.now())
    target = archive_dir / "original_external.mkv"
    repository.set_archive_intent(importing.id, archive_dir, target, source.stat().st_size)
    target.write_bytes(b"partial")

    recovered = service.recover_after_restart()

    restored = repository.get(job.id)
    assert recovered == 1
    assert restored.status is JobStatus.INTERRUPTED
    assert restored.source_sha256 is None
    assert source.exists()
    assert target.exists()


def test_retry_reimports_when_an_existing_archive_target_fails_hash_verification(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=False, clock=clock)
    source = tmp_path / "external.mkv"
    source.write_bytes(b"complete recording")
    job = repository.detect(source, JobOrigin.MANUAL, source.stat().st_size, source.stat().st_mtime_ns)
    importing = repository.transition(job.id, JobStatus.IMPORTING)
    manager = ArchiveManager(paths.archive)
    archive_dir = manager.create_job_directory(importing.id, importing.source_name, clock.now())
    target = archive_dir / "original_external.mkv"
    repository.set_archive_intent(importing.id, archive_dir, target, source.stat().st_size)
    target.write_bytes(b"partial")
    repository.set_source_hash(importing.id, "a" * 64)
    repository.transition(importing.id, JobStatus.FAILED, error_code="archive_import_failed")

    retried = service.retry(job.id)

    assert retried.status is JobStatus.WAITING_FOR_STABLE


def test_manifest_recovery_requires_every_required_export(tmp_path: Path) -> None:
    clock = Clock()
    service, repository, paths = _service(tmp_path, enabled=False, clock=clock)
    source = tmp_path / "external.mkv"
    source.write_bytes(b"media")
    archive_dir = paths.archive / "incomplete-job"
    archive_dir.mkdir()
    archived = archive_dir / "original_external.mkv"
    archived.write_bytes(source.read_bytes())
    job = repository.detect(source, JobOrigin.MANUAL, source.stat().st_size, source.stat().st_mtime_ns)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.set_archive(processing.id, archive_dir, archived, archived.stat().st_size)
    (archive_dir / "transcript.json").write_text("{}", encoding="utf-8")
    (archive_dir / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": job.id,
                "status": "completed",
                "outputs": {"json": "transcript.json"},
            }
        ),
        encoding="utf-8",
    )

    assert service.recover_completed_manifests() == 0
    assert repository.get(job.id).status is JobStatus.PROCESSING
