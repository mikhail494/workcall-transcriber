"""The controller-facing queue interface for safe discovery and import."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .archive import ArchiveManager, ImportedMedia, InsufficientDiskSpace, sha256_file
from .config import SettingsStore
from .database import JobRepository
from .manifest import build_manifest, write_manifest
from .models import JobOrigin, JobRecord, JobStatus
from .paths import RuntimePaths
from .stability import FileObservation, FileStabilityChecker, StabilityState, is_supported_media


class DuplicateSourceError(ValueError):
    """Raised for a manual source that has already completed successfully."""

    def __init__(self, completed_job: JobRecord) -> None:
        super().__init__("This file has already been transcribed. Choose reprocess to create another job.")
        self.completed_job = completed_job


@dataclass(frozen=True)
class ReconciliationReport:
    detected: int = 0
    queued: int = 0
    duplicates: int = 0
    failed: int = 0


class QueueService:
    """Coordinate settings, durable state, stability, and archive import.

    This is the public seam used by both the filesystem watcher and periodic
    reconciliation. It contains the whole Inbox contract so callers cannot
    accidentally process unrelated files or bypass safe import semantics.
    """

    def __init__(
        self,
        *,
        paths: RuntimePaths,
        settings_store: SettingsStore,
        repository: JobRepository,
        archive_manager: ArchiveManager,
        stability_checker: FileStabilityChecker,
        clock: Callable[[], datetime],
    ) -> None:
        self._paths = paths
        self._settings_store = settings_store
        self._repository = repository
        self._archive_manager = archive_manager
        self._stability_checker = stability_checker
        self._clock = clock

    def observe_inbox(self, source: Path) -> JobRecord | None:
        """Accept a watcher candidate only while automatic processing is enabled."""
        settings = self._settings_store.load()
        inbox = settings.effective_inbox(self._paths)
        if not settings.automatic_enabled or not _is_direct_child(source, inbox):
            return None
        return self._detect_if_new(source, JobOrigin.INBOX)

    def enqueue_manual(self, source: Path, *, allow_duplicate: bool = False) -> JobRecord:
        """Queue a manually selected file even when automatic mode is disabled."""
        if not is_supported_media(source):
            raise ValueError("Choose a supported audio or video file.")
        source = source.resolve(strict=True)
        if not allow_duplicate:
            prior = self._repository.find_completed_by_hash(sha256_file(source))
            if prior is not None:
                raise DuplicateSourceError(prior)
        job = self._repository.find_active_by_source(source)
        if job is None:
            stat = source.stat()
            job = self._repository.detect(source, JobOrigin.MANUAL, stat.st_size, stat.st_mtime_ns)
            job = self._repository.update_observation(
                job.id,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                stable_since=self._clock().isoformat(),
            )
        if allow_duplicate:
            metadata = _metadata(job)
            metadata["allow_duplicate_reprocess"] = True
            job = self._repository.set_metadata(job.id, metadata)
        return job

    def reconcile(self) -> ReconciliationReport:
        """Rescan Inbox and advance stable candidates without launching the worker."""
        settings = self._settings_store.load()
        detected = 0
        if settings.automatic_enabled:
            inbox = settings.effective_inbox(self._paths)
            try:
                candidates = list(inbox.iterdir()) if inbox.is_dir() else []
            except OSError:
                candidates = []
            for candidate in candidates:
                if candidate.is_file() and self._detect_if_new(candidate, JobOrigin.INBOX) is not None:
                    detected += 1

        queued = duplicates = failed = 0
        checker = self._stability_checker.for_window(settings.stability_seconds)
        for job in self._repository.list_waiting_for_stability():
            # A disable is immediate for automatic work: leave any unaccepted
            # Inbox recording untouched until the user enables it again.
            if job.origin is JobOrigin.INBOX and not settings.automatic_enabled:
                continue
            outcome = self._advance_waiting_job(job, checker)
            queued += outcome.queued
            duplicates += outcome.duplicates
            failed += outcome.failed
        return ReconciliationReport(detected, queued, duplicates, failed)

    def claim_next_job(self) -> JobRecord | None:
        """Claim exactly one queued job for the external GPU worker."""
        return self._repository.claim_next_queued()

    def retry(self, job_id: str) -> JobRecord:
        job = self._repository.get(job_id)
        needs_import = _verified_archived_media(job, self._paths.root) is None
        return self._repository.retry(job_id, needs_import=needs_import)

    def recover_completed_manifests(
        self,
        *,
        blocked_processing_job_ids: Collection[str] = (),
        job_ids: Collection[str] | None = None,
    ) -> int:
        """Restore a final worker result if the GUI died after its atomic manifest write."""
        blocked = set(blocked_processing_job_ids)
        requested = set(job_ids) if job_ids is not None else None
        restored = 0
        for job in self._repository.list_processing():
            if job.id in blocked or (requested is not None and job.id not in requested):
                continue
            final = _read_final_manifest(job)
            if final is None:
                continue
            try:
                self._repository.restore_terminal_from_manifest(job.id, **final)
            except (KeyError, ValueError):
                continue
            restored += 1
        return restored

    def recover_after_restart(
        self, *, blocked_processing_job_ids: Collection[str] = ()
    ) -> int:
        """Recover durable import intent before marking only safe active rows retryable."""
        recovered_imports = self._recover_importing_jobs()
        return recovered_imports + self._repository.recover_after_restart(
            blocked_processing_job_ids=blocked_processing_job_ids
        )

    def _detect_if_new(self, source: Path, origin: JobOrigin) -> JobRecord | None:
        if not is_supported_media(source) or not source.exists():
            return None
        try:
            source = source.resolve(strict=True)
            if self._repository.find_active_by_source(source) is not None:
                return None
            stat = source.stat()
        except OSError:
            return None
        job = self._repository.detect(source, origin, stat.st_size, stat.st_mtime_ns)
        return self._repository.update_observation(
            job.id,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            stable_since=self._clock().isoformat(),
        )

    def _advance_waiting_job(
        self,
        job: JobRecord,
        checker: FileStabilityChecker,
    ) -> ReconciliationReport:
        observation = _observation(job)
        assessment = checker.assess(job.source_path, observation, self._clock())
        if assessment.observation is not None:
            self._repository.update_observation(
                job.id,
                size=assessment.observation.size,
                mtime_ns=assessment.observation.mtime_ns,
                stable_since=assessment.observation.stable_since.isoformat(),
            )
        if assessment.state is not StabilityState.STABLE:
            if assessment.state is StabilityState.MISSING:
                self._repository.transition(
                    job.id,
                    JobStatus.FAILED,
                    error_code="source_missing",
                    error_message="The recording disappeared before it could be imported. No archive copy was made.",
                )
                return ReconciliationReport(failed=1)
            if assessment.state is StabilityState.INELIGIBLE:
                self._repository.transition(
                    job.id,
                    JobStatus.CANCELLED,
                    error_code="source_ineligible",
                    error_message="The source is no longer an eligible media recording.",
                )
                return ReconciliationReport(failed=1)
            return ReconciliationReport()

        importing = self._repository.transition(job.id, JobStatus.IMPORTING)
        try:
            archive_manager = self._archive_manager.for_archive_root(
                self._settings_store.load().effective_archive(self._paths)
            )
            archive_dir = archive_manager.create_job_directory(
                importing.id, importing.source_name, self._clock()
            )
            target = archive_manager.planned_target(archive_dir, importing.source_name)
            final_source_size = importing.source_path.stat().st_size
            importing = self._repository.set_archive_intent(
                importing.id,
                archive_dir,
                target,
                final_source_size,
            )
            imported = (
                archive_manager.import_inbox(importing.source_path, archive_dir)
                if importing.origin is JobOrigin.INBOX
                else archive_manager.copy_manual(importing.source_path, archive_dir)
            )
            return self._finish_import(importing, imported)
        except (OSError, InsufficientDiskSpace) as error:
            self._repository.transition(
                importing.id,
                JobStatus.FAILED,
                error_code="archive_import_failed",
                error_message=(
                    "The recording could not be safely imported. The original source was left intact. "
                    f"{error}"
                ),
            )
            return ReconciliationReport(failed=1)

    def _recover_importing_jobs(self) -> int:
        """Complete an import whose source moved after its destination was committed to SQLite."""
        recovered = 0
        for job in self._repository.list_importing():
            imported = _verified_archived_media(job, self._paths.root)
            if imported is not None:
                try:
                    self._finish_import(job, imported)
                    _remove_verified_inbox_source(job, imported.sha256, self._paths.inbox)
                    recovered += 1
                    continue
                except (OSError, InsufficientDiskSpace):
                    pass
            self._repository.transition(
                job.id,
                JobStatus.INTERRUPTED,
                error_code="archive_import_interrupted",
                error_message=(
                    "The app restarted while this recording was being archived. "
                    "The source was not deleted automatically; verify the archive and click Retry."
                ),
            )
            recovered += 1
        return recovered

    def _finish_import(self, importing: JobRecord, imported: ImportedMedia) -> ReconciliationReport:
        """Persist verified archive metadata and make the imported job ready exactly once."""
        updated = self._repository.set_archive(
            importing.id,
            imported.archive_dir,
            imported.media_path,
            imported.size,
        )
        updated = self._repository.set_source_hash(updated.id, imported.sha256)
        existing = self._repository.find_completed_by_hash(imported.sha256)
        allow_manual_duplicate = bool(_metadata(updated).get("allow_duplicate_reprocess"))
        if existing is not None and (updated.origin is JobOrigin.INBOX or not allow_manual_duplicate):
            duplicate_job = self._repository.transition(
                updated.id,
                JobStatus.DUPLICATE,
                warning_text=(
                    "An identical completed recording already exists in the archive. "
                    "This copy was preserved but was not transcribed again."
                ),
            )
            _write_initial_manifest(duplicate_job)
            return ReconciliationReport(duplicates=1)
        queued_job = self._repository.transition(updated.id, JobStatus.QUEUED)
        _write_initial_manifest(queued_job)
        return ReconciliationReport(queued=1)


def _is_direct_child(path: Path, parent: Path) -> bool:
    try:
        return path.resolve(strict=False).parent == parent.resolve(strict=False)
    except OSError:
        return False


def _observation(job: JobRecord) -> FileObservation | None:
    if job.last_observed_size is None or job.last_observed_mtime_ns is None or not job.stable_since:
        return None
    try:
        stable_since = datetime.fromisoformat(job.stable_since)
    except ValueError:
        return None
    return FileObservation(job.last_observed_size, job.last_observed_mtime_ns, stable_since)


def _metadata(job: JobRecord) -> dict[str, object]:
    try:
        parsed = json.loads(job.metadata_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_initial_manifest(job: JobRecord) -> None:
    if job.archive_dir is None:
        return
    from . import __version__

    try:
        write_manifest(
            job.archive_dir,
            build_manifest(job, app_version=__version__, transcription={}, diarization={}, outputs={}),
        )
    except OSError:
        # The job can still be safely processed; the worker writes the authoritative
        # final manifest atomically after all transcript outputs exist.
        return


def _verified_archived_media(job: JobRecord, runtime_root: Path) -> ImportedMedia | None:
    """Return an archived original only when its durable or live identity is proven."""
    if job.archive_dir is None or job.archived_media_path is None:
        return None
    try:
        root = runtime_root.resolve(strict=False)
        archive_dir = job.archive_dir.resolve(strict=False)
        media = job.archived_media_path.resolve(strict=True)
        archive_dir.relative_to(root)
        media.relative_to(archive_dir)
        if not media.is_file():
            return None
        media_digest = sha256_file(media)
    except (OSError, ValueError):
        return None
    imported = ImportedMedia(archive_dir, media, media.stat().st_size, media_digest)
    if job.source_sha256:
        return imported if media_digest == job.source_sha256.lower() else None
    try:
        if job.source_path.is_file() and sha256_file(job.source_path) == media_digest:
            return imported
    except OSError:
        return None
    # An Inbox source that is gone can only have reached the published target by
    # an atomic move or a previously verified fallback. Its final stable size is
    # recorded with the archive intent before the move starts.
    if job.origin is JobOrigin.INBOX and job.source_size == imported.size:
        return imported
    return None


def _remove_verified_inbox_source(job: JobRecord, digest: str, inbox: Path) -> None:
    """Finish a crash-interrupted copy fallback only after proving byte identity again."""
    if job.origin is not JobOrigin.INBOX or not _is_direct_child(job.source_path, inbox):
        return
    try:
        if job.source_path.is_file() and sha256_file(job.source_path) == digest:
            job.source_path.unlink()
    except OSError:
        return


def _read_final_manifest(job: JobRecord) -> dict[str, Any] | None:
    """Read only a valid final manifest in the job's own durable archive directory."""
    if job.archive_dir is None:
        return None
    path = job.archive_dir / "manifest.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("job_id") != job.id:
        return None
    try:
        status = JobStatus(str(raw.get("status")))
    except ValueError:
        return None
    if status not in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.FAILED}:
        return None
    outputs = _mapping(raw.get("outputs"))
    expected_outputs = {
        "json": "transcript.json",
        "txt": "transcript.txt",
        "srt": "transcript.srt",
        "vtt": "transcript.vtt",
        "tsv": "transcript.tsv",
    }
    if status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS} and any(
        outputs.get(kind) != filename or not (job.archive_dir / filename).is_file()
        for kind, filename in expected_outputs.items()
    ):
        return None
    transcription = _mapping(raw.get("transcription"))
    diarization = _mapping(raw.get("diarization"))
    media = _mapping(raw.get("media"))
    errors = _mapping(raw.get("errors"))
    error_pairs = [
        (str(code), message)
        for code, message in errors.items()
        if isinstance(message, str) and message.strip()
    ]
    error_code, error_message = error_pairs[0] if error_pairs else (None, None)
    warning_text = " ".join(message for _, message in error_pairs) or None
    return {
        "status": status,
        "completed_at": _optional_string(raw.get("completed_at")),
        "duration_seconds": _optional_float(media.get("duration_seconds")),
        "detected_language": _optional_string(transcription.get("detected_language")),
        "detected_language_probability": _optional_float(
            transcription.get("detected_language_probability")
        ),
        "language_source": _optional_string(transcription.get("language_source")),
        "speaker_count": _optional_int(diarization.get("speaker_count")),
        "warning_text": warning_text if status is JobStatus.COMPLETED_WITH_WARNINGS else None,
        "error_code": error_code if status is JobStatus.FAILED else None,
        "error_message": error_message if status is JobStatus.FAILED else None,
    }


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
