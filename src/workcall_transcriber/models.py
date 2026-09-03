"""Domain records shared by the controller, worker, and user interface."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    """Durable states for a transcription job."""

    WAITING_FOR_STABLE = "waiting_for_stable"
    IMPORTING = "importing"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    DUPLICATE = "duplicate"


class JobOrigin(StrEnum):
    """Whether the source is managed by the Inbox queue or supplied manually."""

    INBOX = "inbox"
    MANUAL = "manual"


ACTIVE_JOB_STATUSES = frozenset(
    {
        JobStatus.WAITING_FOR_STABLE,
        JobStatus.IMPORTING,
        JobStatus.QUEUED,
        JobStatus.PROCESSING,
    }
)

RETRYABLE_JOB_STATUSES = frozenset(
    {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
)

TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_WARNINGS,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.INTERRUPTED,
        JobStatus.DUPLICATE,
    }
)


@dataclass(frozen=True)
class Settings:
    """Small, user-facing settings with safe first-run defaults."""

    automatic_enabled: bool = False
    autostart_enabled: bool = True
    desktop_notifications: bool = True
    language: str = "auto"
    speaker_count: int | None = None
    batch_size: int = 8
    stability_seconds: int = 15
    reconciliation_seconds: int = 45
    keep_temporary_audio: bool = False
    speaker_match_threshold: float = 0.95
    inbox_path: Path | None = None
    archive_path: Path | None = None

    def to_mapping(self) -> dict[str, Any]:
        """Create JSON-safe settings. Credentials deliberately have no field here."""
        return {
            "automatic_enabled": self.automatic_enabled,
            "autostart_enabled": self.autostart_enabled,
            "desktop_notifications": self.desktop_notifications,
            "language": self.language,
            "speaker_count": self.speaker_count,
            "batch_size": self.batch_size,
            "stability_seconds": self.stability_seconds,
            "reconciliation_seconds": self.reconciliation_seconds,
            "keep_temporary_audio": self.keep_temporary_audio,
            "speaker_match_threshold": self.speaker_match_threshold,
            "inbox_path": str(self.inbox_path) if self.inbox_path else None,
            "archive_path": str(self.archive_path) if self.archive_path else None,
        }

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> Settings:
        """Tolerantly load known values while retaining first-run defaults."""
        defaults = cls()
        language = str(mapping.get("language", defaults.language)).lower()
        inbox = mapping.get("inbox_path")
        archive = mapping.get("archive_path")
        speaker_count = mapping.get("speaker_count", defaults.speaker_count)
        return cls(
            automatic_enabled=bool(mapping.get("automatic_enabled", defaults.automatic_enabled)),
            autostart_enabled=bool(mapping.get("autostart_enabled", defaults.autostart_enabled)),
            desktop_notifications=bool(
                mapping.get("desktop_notifications", defaults.desktop_notifications)
            ),
            language=language,
            speaker_count=int(speaker_count) if speaker_count not in (None, "") else None,
            batch_size=int(mapping.get("batch_size", defaults.batch_size)),
            stability_seconds=int(mapping.get("stability_seconds", defaults.stability_seconds)),
            reconciliation_seconds=int(
                mapping.get("reconciliation_seconds", defaults.reconciliation_seconds)
            ),
            keep_temporary_audio=bool(
                mapping.get("keep_temporary_audio", defaults.keep_temporary_audio)
            ),
            speaker_match_threshold=float(
                mapping.get("speaker_match_threshold", defaults.speaker_match_threshold)
            ),
            inbox_path=Path(inbox) if inbox else None,
            archive_path=Path(archive) if archive else None,
        )

    def effective_inbox(self, runtime_paths: Any) -> Path:
        """Return the only automatic intake path allowed by the product contract."""
        return runtime_paths.inbox

    def effective_archive(self, runtime_paths: Any) -> Path:
        candidate = self.archive_path or runtime_paths.archive
        return candidate if not _archive_path_errors(candidate, runtime_paths) else runtime_paths.archive

    def validate(self, runtime_paths: Any) -> list[str]:
        """Return user-facing validation errors without modifying paths."""
        errors: list[str] = []
        if self.language not in {"auto", "ru", "en"}:
            errors.append("Language must be Auto, Russian, or English.")
        if not 1 <= self.batch_size <= 32:
            errors.append("Whisper batch size must be between 1 and 32.")
        if not 5 <= self.stability_seconds <= 900:
            errors.append("File stability wait must be between 5 seconds and 15 minutes.")
        if not 15 <= self.reconciliation_seconds <= 600:
            errors.append("Reconciliation interval must be between 15 seconds and 10 minutes.")
        if self.speaker_count is not None and not 1 <= self.speaker_count <= 20:
            errors.append("Speaker count must be Auto or between 1 and 20.")
        if not 0.0 < self.speaker_match_threshold <= 1.0:
            errors.append("Speaker matching threshold must be between 0 and 1.")

        fixed_inbox = runtime_paths.inbox.resolve(strict=False)
        if self.inbox_path is not None and self.inbox_path.resolve(strict=False) != fixed_inbox:
            errors.append("Automatic processing only watches the fixed WorkCalls Inbox.")
        errors.extend(_archive_path_errors(self.archive_path or runtime_paths.archive, runtime_paths))
        return errors


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _archive_path_errors(candidate: Path, runtime_paths: Any) -> list[str]:
    """Keep archive writes inside the owned data root and away from other roles."""
    archive = candidate.resolve(strict=False)
    root = runtime_paths.root.resolve(strict=False)
    inbox = runtime_paths.inbox.resolve(strict=False)
    temp = runtime_paths.temp.resolve(strict=False)
    state = runtime_paths.state.resolve(strict=False)
    errors: list[str] = []
    if archive == root or not _is_relative_to(archive, root):
        errors.append("The archive folder must be inside the WorkCalls data folder.")
    if _is_relative_to(archive, temp) or _is_relative_to(temp, archive):
        errors.append("The archive folder cannot overlap the temporary folder.")
    if _is_relative_to(archive, state) or _is_relative_to(state, archive):
        errors.append("The archive folder cannot overlap the state folder.")
    if _is_relative_to(archive, inbox) or _is_relative_to(inbox, archive):
        errors.append("Inbox and archive folders must be different.")
    return errors


@dataclass(frozen=True)
class JobRecord:
    """A durable job snapshot returned by the queue repository."""

    id: str
    source_path: Path
    source_name: str
    origin: JobOrigin
    status: JobStatus
    detected_at: str
    source_size: int | None = None
    source_mtime_ns: int | None = None
    last_observed_size: int | None = None
    last_observed_mtime_ns: int | None = None
    stable_since: str | None = None
    archive_dir: Path | None = None
    archived_media_path: Path | None = None
    source_sha256: str | None = None
    queued_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    detected_language: str | None = None
    detected_language_probability: float | None = None
    language_source: str | None = None
    speaker_count: int | None = None
    warning_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    worker_pid: int | None = None
    metadata_json: str = "{}"
