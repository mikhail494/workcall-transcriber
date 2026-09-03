import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from workcall_transcriber.archive import ArchiveManager
from workcall_transcriber.config import SettingsStore
from workcall_transcriber.controller import WorkCallController
from workcall_transcriber.database import JobRepository
from workcall_transcriber.models import JobOrigin, JobStatus, Settings
from workcall_transcriber.paths import RuntimePaths
from workcall_transcriber.queue_service import QueueService
from workcall_transcriber.security import CredentialStore
from workcall_transcriber.stability import FileStabilityChecker
from workcall_transcriber.worker_lock import WorkerLeaseState
from workcall_transcriber.worker_protocol import WorkerEvent
from workcall_transcriber.worker_runner import OrphanWorkerStatus, WorkerFinished


class InlineRunner:
    def __init__(self) -> None:
        self.finished = threading.Event()

    def start(self, spec, token, on_event, on_finished, on_started=None) -> bool:
        if on_started is not None:
            on_started(1234)
        on_event(
            WorkerEvent(
                "completed",
                "finalizing",
                "Completed.",
                payload={
                    "status": "completed",
                    "detected_language": "ru",
                    "language_source": "auto",
                    "speaker_count": 2,
                    "duration_seconds": 10.0,
                },
            )
        )
        on_finished(WorkerFinished(spec.job_id, 0, False, ""))
        self.finished.set()
        return True

    def stop_current(self) -> bool:
        return False

    def stop_orphaned_worker(self, process_id: int, job_id: str, temp_root: Path) -> bool:
        return OrphanWorkerStatus.UNCONFIRMED

    def stop_unrecorded_worker(self, job_id: str, temp_root: Path) -> OrphanWorkerStatus:
        return OrphanWorkerStatus.ABSENT

    def worker_lease_state(self) -> WorkerLeaseState:
        return WorkerLeaseState.INACTIVE


class RecoveryRunner:
    def __init__(self) -> None:
        self.orphaned: list[tuple[int, str]] = []

    def stop_orphaned_worker(
        self, process_id: int, job_id: str, temp_root: Path
    ) -> OrphanWorkerStatus:
        self.orphaned.append((process_id, job_id))
        return OrphanWorkerStatus.STOPPED

    def stop_unrecorded_worker(self, job_id: str, temp_root: Path) -> OrphanWorkerStatus:
        return OrphanWorkerStatus.ABSENT

    def worker_lease_state(self) -> WorkerLeaseState:
        return WorkerLeaseState.INACTIVE

    def stop_current(self) -> bool:
        return False


class RecordingObserver:
    def __init__(self) -> None:
        self.scheduled: tuple[object, str, bool] | None = None
        self.started = False

    def schedule(self, handler: object, path: str, recursive: bool) -> None:
        self.scheduled = (handler, path, recursive)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None


def test_controller_finalizes_a_completed_worker_job(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=False))
    repository = JobRepository(paths.database)
    repository.initialize()
    media = paths.archive / "original.mkv"
    media.write_bytes(b"media")
    job = repository.detect(media, JobOrigin.MANUAL, media.stat().st_size, media.stat().st_mtime_ns)
    repository.transition(job.id, JobStatus.QUEUED)
    queued = repository.get(job.id)
    repository.set_archive(queued.id, paths.archive, media, media.stat().st_size)
    repository.set_source_hash(queued.id, "a" * 64)
    runner = InlineRunner()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )

    controller = WorkCallController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=runner,
    )

    controller.start()
    assert runner.finished.wait(5)

    completed = repository.get(job.id)
    controller.shutdown()
    assert completed.status is JobStatus.COMPLETED
    assert completed.detected_language == "ru"
    assert completed.speaker_count == 2


def test_startup_terminates_a_recorded_orphan_before_marking_the_job_retryable(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=False))
    repository = JobRepository(paths.database)
    repository.initialize()
    media = paths.archive / "original.mkv"
    media.write_bytes(b"media")
    job = repository.detect(media, JobOrigin.MANUAL, media.stat().st_size, media.stat().st_mtime_ns)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.set_worker_pid(processing.id, 4242)
    runner = RecoveryRunner()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )
    controller = WorkCallController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=runner,
    )

    controller.start()
    controller.shutdown()

    recovered = repository.get(processing.id)
    assert runner.orphaned and runner.orphaned[0] == (4242, processing.id)
    assert recovered.status is JobStatus.INTERRUPTED
    assert recovered.worker_pid is None


def test_startup_keeps_the_queue_blocked_when_a_prior_worker_cannot_be_verified_stopped(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=False))
    repository = JobRepository(paths.database)
    repository.initialize()
    first_media = paths.archive / "first.mkv"
    second_media = paths.archive / "second.mkv"
    first_media.write_bytes(b"first")
    second_media.write_bytes(b"second")
    first = repository.detect(first_media, JobOrigin.MANUAL, 5, 10)
    second = repository.detect(second_media, JobOrigin.MANUAL, 6, 11)
    repository.transition(first.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None and processing.id == first.id
    repository.set_worker_pid(processing.id, 4242)
    repository.transition(second.id, JobStatus.QUEUED)

    class UnconfirmedRunner(RecoveryRunner):
        def stop_orphaned_worker(
            self, process_id: int, job_id: str, temp_root: Path
        ) -> OrphanWorkerStatus:
            self.orphaned.append((process_id, job_id))
            return OrphanWorkerStatus.UNCONFIRMED

    runner = UnconfirmedRunner()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )
    controller = WorkCallController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=runner,
    )

    controller.start()
    controller.shutdown()

    blocked = repository.get(processing.id)
    still_queued = repository.get(second.id)
    assert runner.orphaned and runner.orphaned[0] == (4242, processing.id)
    assert blocked.status is JobStatus.PROCESSING
    assert blocked.worker_pid == 4242
    assert blocked.error_code == "worker_recovery_unconfirmed"
    assert still_queued.status is JobStatus.QUEUED


def test_startup_blocks_a_pidless_launch_while_the_worker_mutex_is_active(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=False))
    repository = JobRepository(paths.database)
    repository.initialize()
    first_media = paths.archive / "first.mkv"
    second_media = paths.archive / "second.mkv"
    first_media.write_bytes(b"first")
    second_media.write_bytes(b"second")
    first = repository.detect(first_media, JobOrigin.MANUAL, 5, 10)
    second = repository.detect(second_media, JobOrigin.MANUAL, 6, 11)
    repository.transition(first.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.transition(second.id, JobStatus.QUEUED)

    class LaunchPendingRunner(RecoveryRunner):
        def worker_lease_state(self) -> WorkerLeaseState:
            return WorkerLeaseState.ACTIVE

    runner = LaunchPendingRunner()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )

    controller = WorkCallController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=runner,
    )

    controller.start()
    controller.shutdown()

    assert repository.get(processing.id).status is JobStatus.PROCESSING
    assert repository.get(processing.id).error_code == "worker_recovery_unconfirmed"
    assert repository.get(second.id).status is JobStatus.QUEUED


def test_startup_blocks_a_pidless_launch_when_process_identity_cannot_be_confirmed(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=False))
    repository = JobRepository(paths.database)
    repository.initialize()
    media = paths.archive / "first.mkv"
    media.write_bytes(b"first")
    job = repository.detect(media, JobOrigin.MANUAL, 5, 10)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None

    class UnconfirmedLaunchRunner(RecoveryRunner):
        def stop_unrecorded_worker(self, job_id: str, temp_root: Path) -> OrphanWorkerStatus:
            return OrphanWorkerStatus.UNCONFIRMED

    runner = UnconfirmedLaunchRunner()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )
    controller = WorkCallController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=runner,
    )

    controller.start()
    controller.shutdown()

    blocked = repository.get(processing.id)
    assert blocked.status is JobStatus.PROCESSING
    assert blocked.error_code == "worker_recovery_unconfirmed"


def test_pidless_launch_restores_its_final_manifest_after_the_worker_mutex_releases(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=False))
    repository = JobRepository(paths.database)
    repository.initialize()
    source = tmp_path / "external.mkv"
    source.write_bytes(b"media")
    archive_dir = paths.archive / "launch-gap"
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
                "outputs": {
                    "json": "transcript.json",
                    "txt": "transcript.txt",
                    "srt": "transcript.srt",
                    "vtt": "transcript.vtt",
                    "tsv": "transcript.tsv",
                },
            }
        ),
        encoding="utf-8",
    )

    class LaunchPendingRunner(RecoveryRunner):
        state = WorkerLeaseState.ACTIVE

        def worker_lease_state(self) -> WorkerLeaseState:
            return self.state

    runner = LaunchPendingRunner()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )

    class ManualRecoveryController(WorkCallController):
        def request_reconciliation(self) -> bool:
            return False

    controller = ManualRecoveryController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=runner,
    )

    controller.start()
    assert processing.id in controller._orphan_blocked_job_ids
    runner.state = WorkerLeaseState.INACTIVE
    assert runner.worker_lease_state() is WorkerLeaseState.INACTIVE
    assert repository.get(processing.id).status is JobStatus.PROCESSING
    assert controller._recover_unresolved_orphaned_workers() == 1
    controller.shutdown()

    assert repository.get(processing.id).status is JobStatus.COMPLETED


def test_enabled_controller_watches_only_the_fixed_workcalls_inbox(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "WorkCalls")
    paths.ensure_exists()
    store = SettingsStore(paths)
    store.save(Settings(automatic_enabled=True))
    repository = JobRepository(paths.database)
    repository.initialize()
    service = QueueService(
        paths=paths,
        settings_store=store,
        repository=repository,
        archive_manager=ArchiveManager(paths.archive),
        stability_checker=FileStabilityChecker(15, probe=lambda _: True),
        clock=lambda: datetime.now(UTC),
    )
    observer = RecordingObserver()
    controller = WorkCallController(
        paths=paths,
        settings_store=store,
        repository=repository,
        queue_service=service,
        credential_store=CredentialStore(paths.credentials_file),
        worker_runner=RecoveryRunner(),
        observer_factory=lambda: observer,
    )

    controller.start()
    controller.shutdown()

    assert observer.started is True
    assert observer.scheduled is not None
    _, watched_path, recursive = observer.scheduled
    assert watched_path == str(paths.inbox)
    assert recursive is False
