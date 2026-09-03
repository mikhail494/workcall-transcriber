"""SQLite-backed durable queue and speaker-profile state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    ACTIVE_JOB_STATUSES,
    RETRYABLE_JOB_STATUSES,
    JobOrigin,
    JobRecord,
    JobStatus,
)
from .speaker_profiles import SpeakerProfile, normalize, updated_centroid


class InvalidJobTransition(ValueError):
    """Raised when a caller attempts to violate the durable job state machine."""


_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.WAITING_FOR_STABLE: frozenset(
        {
            JobStatus.IMPORTING,
            JobStatus.QUEUED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.DUPLICATE,
        }
    ),
    JobStatus.IMPORTING: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
            JobStatus.DUPLICATE,
        }
    ),
    JobStatus.QUEUED: frozenset(
        {JobStatus.PROCESSING, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
    ),
    JobStatus.PROCESSING: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.COMPLETED_WITH_WARNINGS,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.COMPLETED_WITH_WARNINGS: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.INTERRUPTED: frozenset(),
    JobStatus.DUPLICATE: frozenset(),
}

_MUTABLE_COLUMNS = frozenset(
    {
        "source_path",
        "source_name",
        "source_size",
        "source_mtime_ns",
        "last_observed_size",
        "last_observed_mtime_ns",
        "stable_since",
        "archive_dir",
        "archived_media_path",
        "source_sha256",
        "duration_seconds",
        "detected_language",
        "detected_language_probability",
        "language_source",
        "speaker_count",
        "warning_text",
        "error_code",
        "error_message",
        "worker_pid",
        "metadata_json",
    }
)


class JobRepository:
    """A deep module that owns transactions, state transitions, and recovery.

    Callers use domain states and receive immutable records; they do not need to
    know the SQL schema or transaction details.
    """

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    source_size INTEGER,
                    source_mtime_ns INTEGER,
                    last_observed_size INTEGER,
                    last_observed_mtime_ns INTEGER,
                    stable_since TEXT,
                    archive_dir TEXT,
                    archived_media_path TEXT,
                    source_sha256 TEXT,
                    queued_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_seconds REAL,
                    detected_language TEXT,
                    detected_language_probability REAL,
                    language_source TEXT,
                    speaker_count INTEGER,
                    warning_text TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    worker_pid INTEGER,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_detected
                    ON jobs(status, detected_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_hash
                    ON jobs(source_sha256);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_path_active
                    ON jobs(source_path, status);

                CREATE TABLE IF NOT EXISTS speaker_profiles (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    centroid_json TEXT NOT NULL,
                    reference_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def detect(
        self,
        source_path: Path,
        origin: JobOrigin,
        source_size: int | None,
        source_mtime_ns: int | None,
    ) -> JobRecord:
        """Register one candidate, reusing an existing active candidate for its path."""
        path_text = str(source_path)
        with self._transaction() as connection:
            active_values = tuple(status.value for status in ACTIVE_JOB_STATUSES)
            placeholders = ",".join("?" for _ in active_values)
            existing = connection.execute(
                f"SELECT * FROM jobs WHERE source_path = ? AND status IN ({placeholders}) "
                "ORDER BY detected_at DESC LIMIT 1",
                (path_text, *active_values),
            ).fetchone()
            if existing is not None:
                return _row_to_job(existing)

            now = _now()
            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source_path, source_name, origin, status, detected_at,
                    source_size, source_mtime_ns, last_observed_size,
                    last_observed_mtime_ns, stable_since
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    path_text,
                    source_path.name,
                    origin.value,
                    JobStatus.WAITING_FOR_STABLE.value,
                    now,
                    source_size,
                    source_mtime_ns,
                    source_size,
                    source_mtime_ns,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            return _row_to_job(row)

    def get(self, job_id: str) -> JobRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"No job with id {job_id}")
        return _row_to_job(row)

    def list_recent(self, limit: int = 30) -> list[JobRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY detected_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_waiting_for_stability(self) -> list[JobRecord]:
        return self._list_by_status(JobStatus.WAITING_FOR_STABLE)

    def current_processing(self) -> JobRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY started_at LIMIT 1",
                (JobStatus.PROCESSING.value,),
            ).fetchone()
        return _row_to_job(row) if row else None

    def list_processing(self) -> list[JobRecord]:
        """Return durable processing records so startup can stop verified orphans first."""
        return self._list_by_status(JobStatus.PROCESSING)

    def list_importing(self) -> list[JobRecord]:
        """Return import records whose archive intent may need crash recovery."""
        return self._list_by_status(JobStatus.IMPORTING)

    def find_active_by_source(self, source_path: Path) -> JobRecord | None:
        """Find an uncompleted candidate for a path without exposing SQL to callers."""
        active_values = tuple(status.value for status in ACTIVE_JOB_STATUSES)
        placeholders = ",".join("?" for _ in active_values)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM jobs WHERE source_path = ? AND status IN ({placeholders}) "
                "ORDER BY detected_at DESC LIMIT 1",
                (str(source_path), *active_values),
            ).fetchone()
        return _row_to_job(row) if row else None

    def transition(self, job_id: str, target: JobStatus, **updates: Any) -> JobRecord:
        """Atomically move a job to an allowed target state and apply metadata."""
        _validate_update_columns(updates)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"No job with id {job_id}")
            current = JobStatus(row["status"])
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidJobTransition(f"Cannot move {current.value} to {target.value}")
            self._update_row(connection, job_id, target=target, updates=updates)
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
            return _row_to_job(updated)

    def update_observation(
        self,
        job_id: str,
        *,
        size: int,
        mtime_ns: int,
        stable_since: str,
    ) -> JobRecord:
        with self._transaction() as connection:
            self._update_row(
                connection,
                job_id,
                updates={
                    "last_observed_size": size,
                    "last_observed_mtime_ns": mtime_ns,
                    "stable_since": stable_since,
                },
            )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert row is not None
            return _row_to_job(row)

    def set_archive(
        self,
        job_id: str,
        archive_dir: Path,
        archived_media_path: Path,
        source_size: int,
    ) -> JobRecord:
        return self._update(
            job_id,
            archive_dir=str(archive_dir),
            archived_media_path=str(archived_media_path),
            source_size=source_size,
        )

    def set_archive_intent(
        self,
        job_id: str,
        archive_dir: Path,
        archived_media_path: Path,
        source_size: int | None,
    ) -> JobRecord:
        """Persist where an import will land before any Inbox source can move."""
        return self._update(
            job_id,
            archive_dir=str(archive_dir),
            archived_media_path=str(archived_media_path),
            source_size=source_size,
        )

    def set_source_hash(self, job_id: str, source_sha256: str) -> JobRecord:
        if len(source_sha256) != 64:
            raise ValueError("Source SHA256 must be a 64-character hexadecimal digest.")
        return self._update(job_id, source_sha256=source_sha256.lower())

    def set_worker_pid(self, job_id: str, worker_pid: int | None) -> JobRecord:
        return self._update(job_id, worker_pid=worker_pid)

    def set_metadata(self, job_id: str, metadata: dict[str, Any]) -> JobRecord:
        return self._update(
            job_id,
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )

    def set_result_summary(
        self,
        job_id: str,
        *,
        duration_seconds: float | None = None,
        detected_language: str | None = None,
        detected_language_probability: float | None = None,
        language_source: str | None = None,
        speaker_count: int | None = None,
        warning_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> JobRecord:
        return self._update(
            job_id,
            duration_seconds=duration_seconds,
            detected_language=detected_language,
            detected_language_probability=detected_language_probability,
            language_source=language_source,
            speaker_count=speaker_count,
            warning_text=warning_text,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        )

    def claim_next_queued(self) -> JobRecord | None:
        """Claim at most one job, enforcing the single-GPU-worker invariant."""
        with self._transaction() as connection:
            active = connection.execute(
                "SELECT id FROM jobs WHERE status = ? LIMIT 1", (JobStatus.PROCESSING.value,)
            ).fetchone()
            if active is not None:
                return None
            queued = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY queued_at, detected_at, id LIMIT 1",
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if queued is None:
                return None
            job_id = queued["id"]
            self._update_row(connection, job_id, target=JobStatus.PROCESSING, updates={})
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert claimed is not None
            return _row_to_job(claimed)

    def restore_terminal_from_manifest(
        self,
        job_id: str,
        *,
        status: JobStatus,
        completed_at: str | None,
        duration_seconds: float | None,
        detected_language: str | None,
        detected_language_probability: float | None,
        language_source: str | None,
        speaker_count: int | None,
        warning_text: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> JobRecord:
        """Make an atomically written worker manifest authoritative after a GUI crash."""
        allowed = {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS, JobStatus.FAILED}
        if status not in allowed:
            raise ValueError("Only final worker manifest statuses can be restored.")
        updates = {
            "duration_seconds": duration_seconds,
            "detected_language": detected_language,
            "detected_language_probability": detected_language_probability,
            "language_source": language_source,
            "speaker_count": speaker_count,
            "warning_text": warning_text,
            "error_code": error_code,
            "error_message": error_message,
        }
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"No job with id {job_id}")
            if JobStatus(row["status"]) is not JobStatus.PROCESSING:
                return _row_to_job(row)
            self._update_row(connection, job_id, target=status, updates=updates)
            if completed_at:
                connection.execute("UPDATE jobs SET completed_at = ? WHERE id = ?", (completed_at, job_id))
            restored = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert restored is not None
            return _row_to_job(restored)

    def mark_worker_recovery_unconfirmed(self, job_id: str) -> JobRecord:
        """Keep a processing row as a durable launch block until an orphan is resolved."""
        return self._update(
            job_id,
            error_code="worker_recovery_unconfirmed",
            error_message=(
                "A previous transcription worker could not be confirmed stopped. "
                "The queue is paused to protect the GPU until it can be verified."
            ),
        )

    def recover_after_restart(
        self, *, blocked_processing_job_ids: Collection[str] = ()
    ) -> int:
        """Mark work that died with the app as retryable, retaining all evidence."""
        active = (JobStatus.IMPORTING, JobStatus.PROCESSING)
        blocked = tuple(sorted({job_id for job_id in blocked_processing_job_ids if job_id}))
        with self._transaction() as connection:
            placeholders = ",".join("?" for _ in active)
            where = f"status IN ({placeholders})"
            values: list[Any] = [
                JobStatus.INTERRUPTED.value,
                "app_restarted",
                "Processing was interrupted by an application restart. Your recording is safe and can be retried.",
                *(status.value for status in active),
            ]
            if blocked:
                blocked_placeholders = ",".join("?" for _ in blocked)
                where += f" AND (status != ? OR id NOT IN ({blocked_placeholders}))"
                values.extend((JobStatus.PROCESSING.value, *blocked))
            cursor = connection.execute(
                f"UPDATE jobs SET status = ?, worker_pid = NULL, error_code = ?, error_message = ? "
                f"WHERE {where}",
                values,
            )
            return cursor.rowcount

    def retry(self, job_id: str, *, needs_import: bool = False) -> JobRecord:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"No job with id {job_id}")
            current = JobStatus(row["status"])
            if current not in RETRYABLE_JOB_STATUSES:
                raise InvalidJobTransition("Only failed, cancelled, or interrupted jobs can be retried.")
            if needs_import:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, queued_at = NULL, started_at = NULL, completed_at = NULL,
                        worker_pid = NULL, error_code = NULL, error_message = NULL,
                        last_observed_size = NULL, last_observed_mtime_ns = NULL,
                        stable_since = NULL, retry_count = retry_count + 1
                    WHERE id = ?
                    """,
                    (JobStatus.WAITING_FOR_STABLE.value, job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = ?, queued_at = ?, started_at = NULL, completed_at = NULL,
                        worker_pid = NULL, error_code = NULL, error_message = NULL,
                        retry_count = retry_count + 1
                    WHERE id = ?
                    """,
                    (JobStatus.QUEUED.value, _now(), job_id),
                )
            updated = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            assert updated is not None
            return _row_to_job(updated)

    def find_completed_by_hash(self, source_sha256: str) -> JobRecord | None:
        completed = (JobStatus.COMPLETED.value, JobStatus.COMPLETED_WITH_WARNINGS.value)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE source_sha256 = ? AND status IN (?, ?) "
                "ORDER BY completed_at DESC LIMIT 1",
                (source_sha256.lower(), *completed),
            ).fetchone()
        return _row_to_job(row) if row else None

    def save_speaker_profile(self, display_name: str, embedding: list[float]) -> SpeakerProfile:
        """Create or update an explicitly user-named local speaker profile."""
        name = display_name.strip()
        if not name:
            raise ValueError("Speaker profile name cannot be empty.")
        normalized = normalize(embedding)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM speaker_profiles WHERE display_name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            now = _now()
            if row is None:
                profile_id = str(uuid.uuid4())
                centroid = normalized
                reference_count = 1
                connection.execute(
                    """
                    INSERT INTO speaker_profiles (
                        id, display_name, centroid_json, reference_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (profile_id, name, json.dumps(centroid), reference_count, now, now),
                )
            else:
                profile_id = row["id"]
                current = tuple(json.loads(row["centroid_json"]))
                reference_count = int(row["reference_count"]) + 1
                centroid = updated_centroid(current, reference_count - 1, normalized)
                connection.execute(
                    """
                    UPDATE speaker_profiles
                    SET centroid_json = ?, reference_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json.dumps(centroid), reference_count, now, profile_id),
                )
            return SpeakerProfile(profile_id, name, tuple(centroid), reference_count)

    def list_speaker_profiles(self) -> list[SpeakerProfile]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM speaker_profiles ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
        return [
            SpeakerProfile(
                row["id"],
                row["display_name"],
                tuple(json.loads(row["centroid_json"])),
                int(row["reference_count"]),
            )
            for row in rows
        ]

    def delete_speaker_profile(self, profile_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM speaker_profiles WHERE id = ?", (profile_id,))

    def _update(self, job_id: str, **updates: Any) -> JobRecord:
        _validate_update_columns(updates)
        with self._transaction() as connection:
            self._update_row(connection, job_id, updates=updates)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"No job with id {job_id}")
            return _row_to_job(row)

    def _list_by_status(self, status: JobStatus) -> list[JobRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY detected_at, id", (status.value,)
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def _update_row(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        target: JobStatus | None = None,
        updates: dict[str, Any],
    ) -> None:
        _validate_update_columns(updates)
        assignments: list[str] = []
        values: list[Any] = []
        if target is not None:
            assignments.append("status = ?")
            values.append(target.value)
            now = _now()
            if target is JobStatus.QUEUED:
                assignments.append("queued_at = ?")
                values.append(now)
            elif target is JobStatus.PROCESSING:
                assignments.append("started_at = ?")
                values.append(now)
            elif target not in ACTIVE_JOB_STATUSES:
                assignments.append("completed_at = ?")
                values.append(now)
                if "worker_pid" not in updates:
                    assignments.append("worker_pid = NULL")
        for column, value in updates.items():
            assignments.append(f"{column} = ?")
            values.append(value)
        if not assignments:
            return
        values.append(job_id)
        cursor = connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", values)
        if cursor.rowcount != 1:
            raise KeyError(f"No job with id {job_id}")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        source_path=Path(row["source_path"]),
        source_name=row["source_name"],
        origin=JobOrigin(row["origin"]),
        status=JobStatus(row["status"]),
        detected_at=row["detected_at"],
        source_size=row["source_size"],
        source_mtime_ns=row["source_mtime_ns"],
        last_observed_size=row["last_observed_size"],
        last_observed_mtime_ns=row["last_observed_mtime_ns"],
        stable_since=row["stable_since"],
        archive_dir=Path(row["archive_dir"]) if row["archive_dir"] else None,
        archived_media_path=Path(row["archived_media_path"]) if row["archived_media_path"] else None,
        source_sha256=row["source_sha256"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        duration_seconds=row["duration_seconds"],
        detected_language=row["detected_language"],
        detected_language_probability=row["detected_language_probability"],
        language_source=row["language_source"],
        speaker_count=row["speaker_count"],
        warning_text=row["warning_text"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        retry_count=row["retry_count"],
        worker_pid=row["worker_pid"],
        metadata_json=row["metadata_json"],
    )


def _validate_update_columns(updates: dict[str, Any]) -> None:
    unknown = set(updates) - _MUTABLE_COLUMNS
    if unknown:
        joined = ", ".join(sorted(unknown))
        raise ValueError(f"Unsupported job field update: {joined}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
