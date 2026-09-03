"""JSON-lines contract between the lightweight controller and WhisperX worker."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class WorkerProtocolError(ValueError):
    """Raised when worker input or output does not satisfy the stable protocol."""


@dataclass(frozen=True)
class WorkerEvent:
    type: str
    stage: str
    message: str
    progress: float | None = None
    payload: dict[str, Any] | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "stage": self.stage,
            "message": self.message,
            "progress": self.progress,
            "payload": self.payload or {},
        }


@dataclass(frozen=True)
class WorkerSpec:
    """Non-secret launch input written by the controller for one job."""

    job_id: str
    media_path: Path
    archive_dir: Path
    temp_root: Path
    model: str
    batch_size: int
    language: str
    speaker_count: int | None
    keep_temporary_audio: bool
    original_path: Path | None = None
    source_origin: str = "manual"
    source_sha256: str | None = None
    source_size: int | None = None
    detected_at: str | None = None
    started_at: str | None = None
    speaker_match_threshold: float = 0.95
    speaker_profiles: list[dict[str, Any]] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "media_path": str(self.media_path),
            "archive_dir": str(self.archive_dir),
            "temp_root": str(self.temp_root),
            "model": self.model,
            "batch_size": self.batch_size,
            "language": self.language,
            "speaker_count": self.speaker_count,
            "keep_temporary_audio": self.keep_temporary_audio,
            "original_path": str(self.original_path) if self.original_path else None,
            "source_origin": self.source_origin,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
            "detected_at": self.detected_at,
            "started_at": self.started_at,
            "speaker_match_threshold": self.speaker_match_threshold,
            "speaker_profiles": self.speaker_profiles,
        }

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> WorkerSpec:
        try:
            language = str(data["language"])
            if language not in {"auto", "ru", "en"}:
                raise WorkerProtocolError("Worker language must be auto, ru, or en.")
            speaker_count = data.get("speaker_count")
            return cls(
                job_id=str(data["job_id"]),
                media_path=Path(str(data["media_path"])),
                archive_dir=Path(str(data["archive_dir"])),
                temp_root=Path(str(data["temp_root"])),
                model=str(data["model"]),
                batch_size=int(data["batch_size"]),
                language=language,
                speaker_count=int(speaker_count) if speaker_count is not None else None,
                keep_temporary_audio=bool(data["keep_temporary_audio"]),
                original_path=Path(str(data["original_path"]))
                if data.get("original_path")
                else None,
                source_origin=str(data.get("source_origin", "manual")),
                source_sha256=str(data["source_sha256"])
                if data.get("source_sha256")
                else None,
                source_size=int(data["source_size"]) if data.get("source_size") is not None else None,
                detected_at=str(data["detected_at"]) if data.get("detected_at") else None,
                started_at=str(data["started_at"]) if data.get("started_at") else None,
                speaker_match_threshold=float(data.get("speaker_match_threshold", 0.95)),
                speaker_profiles=list(data.get("speaker_profiles") or []),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, WorkerProtocolError):
                raise
            raise WorkerProtocolError("Worker specification is invalid.") from error


def parse_worker_event(line: str) -> WorkerEvent:
    """Parse one stdout line and reject prose or malformed payloads early."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise WorkerProtocolError("Worker emitted a non-JSON output line.") from error
    if not isinstance(data, dict):
        raise WorkerProtocolError("Worker event must be a JSON object.")
    event_type = data.get("type")
    stage = data.get("stage")
    message = data.get("message")
    if not all(isinstance(item, str) and item for item in (event_type, stage, message)):
        raise WorkerProtocolError("Worker event requires type, stage, and message.")
    progress = data.get("progress")
    if progress is not None:
        try:
            progress = float(progress)
        except (TypeError, ValueError) as error:
            raise WorkerProtocolError("Worker event progress must be numeric.") from error
        if not 0.0 <= progress <= 100.0:
            raise WorkerProtocolError("Worker event progress must be between 0 and 100.")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise WorkerProtocolError("Worker event payload must be an object.")
    return WorkerEvent(event_type, stage, message, progress, payload)


def event_line(event: WorkerEvent) -> str:
    return json.dumps(event.to_mapping(), ensure_ascii=False, separators=(",", ":"))


def write_worker_spec(path: Path, spec: WorkerSpec) -> Path:
    """Atomically persist a non-secret worker job specification."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}-",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(spec.to_mapping(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_worker_spec(path: Path) -> WorkerSpec:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkerProtocolError("Worker specification could not be read.") from error
    if not isinstance(data, dict):
        raise WorkerProtocolError("Worker specification must be a JSON object.")
    return WorkerSpec.from_mapping(data)
