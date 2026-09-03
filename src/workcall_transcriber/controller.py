"""Responsive controller coordinating tray UI, watcher, queue, and worker."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import SettingsStore
from .database import JobRepository
from .models import JobRecord, JobStatus, Settings
from .paths import RuntimePaths
from .queue_service import DuplicateSourceError, QueueService
from .security import CredentialStorageError, CredentialStore
from .worker_lock import WorkerLeaseState
from .worker_protocol import WorkerEvent, WorkerSpec
from .worker_runner import WorkerFinished, WorkerRunner

logger = logging.getLogger("workcall_transcriber.controller")


def _noop() -> None:
    return None


def _noop_event(_: str, __: WorkerEvent) -> None:
    return None


def _noop_notification(_: str, __: str) -> None:
    return None


@dataclass
class ControllerCallbacks:
    state_changed: Callable[[], None] = _noop
    worker_event: Callable[[str, WorkerEvent], None] = _noop_event
    notification: Callable[[str, str], None] = _noop_notification


@dataclass(frozen=True)
class AppSnapshot:
    settings: Settings
    current_job: JobRecord | None
    recent_jobs: tuple[JobRecord, ...]


class WorkCallController:
    """Deep application controller with a small UI-facing interface.

    Reconciliation, file probing, and subprocess monitoring live off the UI
    thread. The only automatic source accepted by this module is the configured
    direct child of the explicit WorkCalls Inbox.
    """

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        settings_store: SettingsStore,
        repository: JobRepository,
        queue_service: QueueService,
        credential_store: CredentialStore,
        worker_runner: WorkerRunner,
        callbacks: ControllerCallbacks | None = None,
        observer_factory: Callable[[], Observer] = Observer,
    ) -> None:
        self._paths = paths
        self._settings_store = settings_store
        self._repository = repository
        self._queue_service = queue_service
        self._credential_store = credential_store
        self._worker_runner = worker_runner
        self._callbacks = callbacks or ControllerCallbacks()
        self._observer_factory = observer_factory
        self._observer: Observer | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="WorkCallQueue")
        self._reconcile_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._terminal_events: dict[str, WorkerEvent] = {}
        self._orphan_blocked_job_ids: set[str] = set()

    def start(self) -> None:
        """Initialize durable state, recover crashes, and start lightweight monitoring."""
        self._paths.ensure_exists()
        self._repository.initialize()
        self._orphan_blocked_job_ids = self._stop_recorded_orphaned_workers()
        manifest_recovered = self._queue_service.recover_completed_manifests(
            blocked_processing_job_ids=self._orphan_blocked_job_ids
        )
        recovered = self._queue_service.recover_after_restart(
            blocked_processing_job_ids=self._orphan_blocked_job_ids
        )
        if manifest_recovered:
            logger.info("Restored %s final worker result(s) from durable manifests.", manifest_recovered)
        if recovered:
            logger.info("Recovered %s interrupted job(s) after restart.", recovered)
        self._refresh_watcher()
        with self._lifecycle_lock:
            if self._scheduler is None or not self._scheduler.is_alive():
                self._stop_event.clear()
                self._scheduler = threading.Thread(
                    target=self._scheduler_loop,
                    name="WorkCallReconciler",
                    daemon=True,
                )
                self._scheduler.start()
        self.request_reconciliation()
        self._callbacks.state_changed()

    def shutdown(self) -> None:
        """Stop watcher and known worker tree so Quit actually terminates the utility."""
        self._stop_event.set()
        self._stop_watcher()
        self._worker_runner.stop_current()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def snapshot(self) -> AppSnapshot:
        return AppSnapshot(
            self._settings_store.load(),
            self._repository.current_processing(),
            tuple(self._repository.list_recent()),
        )

    def set_automatic_enabled(self, enabled: bool) -> Settings:
        settings = replace(self._settings_store.load(), automatic_enabled=enabled)
        self.update_settings(settings)
        return settings

    def update_settings(self, settings: Settings) -> None:
        errors = settings.validate(self._paths)
        if errors:
            raise ValueError(" ".join(errors))
        settings.effective_inbox(self._paths).mkdir(parents=True, exist_ok=True)
        settings.effective_archive(self._paths).mkdir(parents=True, exist_ok=True)
        self._settings_store.save(settings)
        self._refresh_watcher()
        self._callbacks.state_changed()
        if settings.automatic_enabled:
            self.request_reconciliation()

    def process_manual_file(self, source: Path, *, allow_duplicate: bool = False) -> JobRecord:
        """Register a manual source; duplicate confirmation remains a UI decision."""
        job = self._queue_service.enqueue_manual(source, allow_duplicate=allow_duplicate)
        self.request_reconciliation()
        self._callbacks.state_changed()
        return job

    def retry_job(self, job_id: str) -> JobRecord:
        job = self._queue_service.retry(job_id)
        self.request_reconciliation()
        self._callbacks.state_changed()
        return job

    def stop_current_processing(self) -> bool:
        if self._worker_runner.stop_current():
            return True
        return self._recover_unresolved_orphaned_workers() > 0

    def request_reconciliation(self) -> bool:
        """Schedule a non-blocking reconciliation unless one is already running."""
        if not self._reconcile_lock.acquire(blocking=False):
            return False
        self._executor.submit(self._run_reconciliation)
        return True

    def _run_reconciliation(self) -> None:
        try:
            self._recover_unresolved_orphaned_workers()
            report = self._queue_service.reconcile()
            if report.detected or report.queued or report.duplicates or report.failed:
                logger.info(
                    "Reconciliation: detected=%s queued=%s duplicates=%s failed=%s",
                    report.detected,
                    report.queued,
                    report.duplicates,
                    report.failed,
                )
            self._start_next_job()
        except Exception:
            logger.exception("Inbox reconciliation failed")
        finally:
            self._reconcile_lock.release()
            self._callbacks.state_changed()

    def _start_next_job(self) -> None:
        job = self._queue_service.claim_next_job()
        if job is None:
            return
        if job.archived_media_path is None or job.archive_dir is None:
            self._repository.transition(
                job.id,
                JobStatus.FAILED,
                error_code="archive_missing",
                error_message="The archived recording is missing. The source was not deleted automatically.",
            )
            self._callbacks.state_changed()
            return
        try:
            token = self._credential_store.load_token()
        except CredentialStorageError:
            token = None
            logger.exception("Saved diarization credential could not be loaded")
        settings = self._settings_store.load()
        spec = WorkerSpec(
            job_id=job.id,
            media_path=job.archived_media_path,
            archive_dir=job.archive_dir,
            temp_root=self._paths.temp,
            model="large-v3",
            batch_size=settings.batch_size,
            language=settings.language,
            speaker_count=settings.speaker_count,
            keep_temporary_audio=settings.keep_temporary_audio,
            original_path=job.source_path,
            source_origin=job.origin.value,
            source_sha256=job.source_sha256,
            source_size=job.source_size,
            detected_at=job.detected_at,
            started_at=job.started_at,
            speaker_match_threshold=settings.speaker_match_threshold,
            speaker_profiles=[
                {
                    "id": profile.id,
                    "display_name": profile.display_name,
                    "centroid": list(profile.centroid),
                    "reference_count": profile.reference_count,
                }
                for profile in self._repository.list_speaker_profiles()
            ],
        )
        self._terminal_events.pop(job.id, None)
        try:
            started = self._worker_runner.start(
                spec,
                token,
                lambda event: self._on_worker_event(job.id, event),
                self._on_worker_finished,
                on_started=lambda worker_pid: self._repository.set_worker_pid(job.id, worker_pid),
            )
        except Exception as error:
            self._repository.transition(
                job.id,
                JobStatus.FAILED,
                error_code="worker_start_failed",
                error_message="The transcription worker could not start. Your recording is safe in the archive.",
            )
            logger.exception("Worker launch failed: %s", error)
            self._callbacks.notification("Transcription failed", "The worker could not start. Your recording is safe.")
            return
        if not started:
            self._repository.transition(
                job.id,
                JobStatus.INTERRUPTED,
                error_code="worker_busy",
                error_message="Another worker was already running. This job can be retried.",
            )

    def _on_worker_event(self, job_id: str, event: WorkerEvent) -> None:
        if event.type in {"completed", "failed", "cancelled"}:
            self._terminal_events[job_id] = event
        self._callbacks.worker_event(job_id, event)
        self._callbacks.state_changed()

    def _stop_recorded_orphaned_workers(self) -> set[str]:
        """Avoid a second GPU job after an unclean GUI-process exit."""
        blocked: set[str] = set()
        for job in self._repository.list_processing():
            if job.worker_pid is None:
                if not self._pidless_worker_permits_recovery(job):
                    self._repository.mark_worker_recovery_unconfirmed(job.id)
                    blocked.add(job.id)
                    logger.warning(
                        "Queue remains blocked while launch outcome is unknown for job %s", job.id
                    )
                continue
            try:
                outcome = self._worker_runner.stop_orphaned_worker(
                    job.worker_pid, job.id, self._paths.temp
                )
            except Exception:
                logger.exception("Could not inspect prior worker PID for job %s", job.id)
                self._repository.mark_worker_recovery_unconfirmed(job.id)
                blocked.add(job.id)
                continue
            if outcome.permits_recovery:
                logger.info("Resolved prior worker for job %s: %s", job.id, outcome.value)
                continue
            self._repository.mark_worker_recovery_unconfirmed(job.id)
            blocked.add(job.id)
            logger.warning("Queue remains blocked until prior worker is resolved for job %s", job.id)
        return blocked

    def _recover_unresolved_orphaned_workers(self) -> int:
        """Recheck only startup-blocked workers; never inspect the live runner's own job."""
        released = 0
        for job in self._repository.list_processing():
            if job.id not in self._orphan_blocked_job_ids:
                continue
            if job.worker_pid is None:
                if not self._pidless_worker_permits_recovery(job):
                    continue
                released += self._release_orphan_block(job)
                continue
            try:
                outcome = self._worker_runner.stop_orphaned_worker(
                    job.worker_pid, job.id, self._paths.temp
                )
            except Exception:
                logger.exception("Could not recheck prior worker PID for job %s", job.id)
                continue
            if not outcome.permits_recovery:
                continue
            released += self._release_orphan_block(job)
        return released

    def _pidless_worker_permits_recovery(self, job: JobRecord) -> bool:
        """Fail closed until both the exact launch command and worker mutex are clear."""
        try:
            outcome = self._worker_runner.stop_unrecorded_worker(job.id, self._paths.temp)
        except Exception:
            logger.exception("Could not inspect the PID-less launch for job %s", job.id)
            return False
        if not outcome.permits_recovery:
            return False
        try:
            lease_state = self._worker_runner.worker_lease_state()
        except Exception:
            logger.exception("Could not inspect the worker lease for job %s", job.id)
            return False
        return lease_state is WorkerLeaseState.INACTIVE

    def _release_orphan_block(self, job: JobRecord) -> int:
        """Restore a late final manifest before making a safely-resolved job retryable."""
        self._orphan_blocked_job_ids.discard(job.id)
        self._queue_service.recover_completed_manifests(job_ids={job.id})
        current = self._repository.get(job.id)
        if current.status is JobStatus.PROCESSING:
            self._repository.transition(
                job.id,
                JobStatus.INTERRUPTED,
                error_code="app_restarted",
                error_message=(
                    "The prior worker is no longer running. This recording is safe and can be retried."
                ),
            )
        return 1

    def _on_worker_finished(self, finished: WorkerFinished) -> None:
        try:
            job = self._repository.get(finished.job_id)
            if job.status is not JobStatus.PROCESSING:
                return
            terminal = self._terminal_events.pop(finished.job_id, None)
            if finished.cancelled_by_user or (terminal and terminal.type == "cancelled"):
                self._repository.transition(
                    job.id,
                    JobStatus.CANCELLED,
                    error_code="cancelled",
                    error_message="Processing was stopped. Your recording is safe in the archive and can be retried.",
                )
                self._callbacks.notification("Processing stopped", "Your recording is safe and can be retried.")
            elif terminal and terminal.type == "completed":
                payload = terminal.payload or {}
                self._repository.set_result_summary(
                    job.id,
                    duration_seconds=_as_optional_float(payload.get("duration_seconds")),
                    detected_language=_as_optional_string(payload.get("detected_language")),
                    detected_language_probability=_as_optional_float(
                        payload.get("detected_language_probability")
                    ),
                    language_source=_as_optional_string(payload.get("language_source")),
                    speaker_count=_as_optional_int(payload.get("speaker_count")),
                    warning_text=_as_optional_string(payload.get("warning_text")),
                )
                status = (
                    JobStatus.COMPLETED_WITH_WARNINGS
                    if payload.get("status") == JobStatus.COMPLETED_WITH_WARNINGS.value
                    else JobStatus.COMPLETED
                )
                self._repository.transition(job.id, status)
                title = "Transcription complete"
                message = "Completed with warnings. Open the result for details." if status is JobStatus.COMPLETED_WITH_WARNINGS else "Your WorkCall transcript is ready."
                self._callbacks.notification(title, message)
            else:
                payload = terminal.payload if terminal and terminal.type == "failed" else {}
                self._repository.transition(
                    job.id,
                    JobStatus.FAILED,
                    error_code=_as_optional_string((payload or {}).get("code")) or "worker_crashed",
                    error_message="Transcription failed. Your recording is safe in the archive. Open the job log and click Retry.",
                )
                self._callbacks.notification(
                    "Transcription failed",
                    "Your recording is safe in the archive. Open the job and click Retry.",
                )
        except Exception:
            logger.exception("Could not finalize worker result")
        finally:
            self._callbacks.state_changed()
            self.request_reconciliation()

    def _scheduler_loop(self) -> None:
        next_run = 0.0
        while not self._stop_event.wait(1.0):
            settings = self._settings_store.load()
            now = time.monotonic()
            if now >= next_run:
                self.request_reconciliation()
                next_run = now + settings.reconciliation_seconds

    def _refresh_watcher(self) -> None:
        self._stop_watcher()
        settings = self._settings_store.load()
        if not settings.automatic_enabled:
            return
        inbox = settings.effective_inbox(self._paths)
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            observer = self._observer_factory()
            observer.schedule(_InboxHandler(self._on_watcher_candidate), str(inbox), recursive=False)
            observer.start()
            self._observer = observer
        except Exception:
            logger.exception("Inbox watcher could not start")
            self._observer = None

    def _stop_watcher(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=5)

    def _on_watcher_candidate(self, path: Path) -> None:
        try:
            if self._queue_service.observe_inbox(path) is not None:
                self.request_reconciliation()
                self._callbacks.state_changed()
        except Exception:
            logger.exception("Inbox watcher event failed")


class _InboxHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[Path], None]) -> None:
        super().__init__()
        self._callback = callback

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._callback(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            destination = getattr(event, "dest_path", event.src_path)
            self._callback(Path(destination))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._callback(Path(event.src_path))


def _as_optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = ["AppSnapshot", "ControllerCallbacks", "DuplicateSourceError", "WorkCallController"]
