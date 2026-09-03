"""Safe reproducibility manifest generation for each archived call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .exports import _atomic_json_write
from .models import JobRecord

MANIFEST_SCHEMA_VERSION = 1


def build_manifest(
    job: JobRecord,
    *,
    app_version: str,
    transcription: dict[str, Any] | None = None,
    diarization: dict[str, Any] | None = None,
    media: dict[str, Any] | None = None,
    outputs: dict[str, str] | None = None,
    timings: dict[str, float] | None = None,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a schema that records observed facts without credentials or guesses."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "app_version": app_version,
        "job_id": job.id,
        "status": job.status.value,
        "created_at": job.detected_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "source": {
            "original_filename": job.source_name,
            "original_path": str(job.source_path),
            "origin": job.origin.value,
            "archived_path": str(job.archived_media_path) if job.archived_media_path else None,
            "size": job.source_size,
            "sha256": job.source_sha256,
        },
        "media": media or {},
        "transcription": transcription or {},
        "diarization": diarization or {},
        "outputs": outputs or {},
        "timings": timings or {},
        "errors": errors or {},
    }


def write_manifest(job_directory: Path, manifest: dict[str, Any]) -> Path:
    """Atomically update the one stable manifest file for a job."""
    path = job_directory / "manifest.json"
    _atomic_json_write(path, manifest)
    return path
