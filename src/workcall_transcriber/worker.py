"""External WhisperX worker process for one archived transcription job."""

from __future__ import annotations

import argparse
import gc
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .exports import build_canonical_transcript, write_transcript_outputs
from .manifest import build_manifest, write_manifest
from .models import JobOrigin, JobRecord, JobStatus
from .security import SecretRedactionFilter, redact_text
from .speaker_profiles import ProfileMatch, SpeakerProfile, apply_profile_matches
from .worker_lock import WorkerLease, WorkerLeaseBusyError
from .worker_protocol import WorkerEvent, WorkerSpec, event_line, read_worker_spec

logger = logging.getLogger(__name__)


class RunStatus(StrEnum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus
    detected_language: str | None = None
    speaker_count: int | None = None
    warning_text: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class PipelineContext:
    result: dict[str, Any]
    audio: Any
    detected_language: str | None
    detected_language_probability: float | None


@dataclass(frozen=True)
class WorkerFailure(Exception):
    code: str
    message: str


def _create_worker_lease() -> WorkerLease:
    return WorkerLease()


class PipelineAdapter(Protocol):
    """The small seam that makes orchestration independently testable."""

    def verify_cuda(self) -> None: ...

    def extract_audio(self, media_path: Path, temp_directory: Path) -> Path: ...

    def duration_seconds(self, media_path: Path) -> float | None: ...

    def transcribe(
        self, audio_path: Path, spec: WorkerSpec, progress: Callable[[float], None]
    ) -> PipelineContext: ...

    def align(self, context: PipelineContext, progress: Callable[[float], None]) -> PipelineContext: ...

    def diarize(
        self,
        context: PipelineContext,
        spec: WorkerSpec,
        token: str,
        progress: Callable[[float], None],
    ) -> tuple[PipelineContext, dict[str, list[float]] | None]: ...

    def cleanup(self) -> None: ...


class WhisperXPipeline:
    """Adapter around the installed machine-specific WhisperX 3.8 runtime."""

    def __init__(self) -> None:
        self._torch: Any | None = None

    def verify_cuda(self) -> None:
        import torch

        self._torch = torch
        if not torch.cuda.is_available():
            raise WorkerFailure(
                "cuda_unavailable",
                "CUDA is unavailable. Your recording is safe in the archive. Fix the GPU runtime and click Retry.",
            )

    def extract_audio(self, media_path: Path, temp_directory: Path) -> Path:
        temp_directory.mkdir(parents=True, exist_ok=True)
        target = temp_directory / "canonical_audio.flac"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "flac",
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
            creationflags=flags,
        )
        if completed.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise WorkerFailure(
                "ffmpeg_failed",
                "FFmpeg could not extract a usable audio track. Your original recording is safe in the archive.",
            )
        return target

    def duration_seconds(self, media_path: Path) -> float | None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=flags,
            )
            return float(completed.stdout.strip()) if completed.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def transcribe(
        self,
        audio_path: Path,
        spec: WorkerSpec,
        progress: Callable[[float], None],
    ) -> PipelineContext:
        _configure_whisperx_logging()
        import whisperx

        forced_language = None if spec.language == "auto" else spec.language
        model = whisperx.load_model(
            spec.model,
            device="cuda",
            compute_type="float16",
            language=forced_language,
        )
        audio = whisperx.load_audio(str(audio_path))
        result = dict(
            model.transcribe(
                audio,
                batch_size=spec.batch_size,
                language=forced_language,
                progress_callback=progress,
            )
        )
        detected_language = str(result.get("language")) if result.get("language") else None
        probability = _as_float(result.get("language_probability"))
        del model
        return PipelineContext(result, audio, detected_language, probability)

    def align(self, context: PipelineContext, progress: Callable[[float], None]) -> PipelineContext:
        if not context.detected_language:
            raise WorkerFailure("alignment_language_missing", "WhisperX did not return a language for alignment.")
        from whisperx.alignment import align, load_align_model

        align_model, metadata = load_align_model(context.detected_language, device="cuda")
        try:
            result = dict(
                align(
                    context.result.get("segments") or [],
                    align_model,
                    metadata,
                    context.audio,
                    device="cuda",
                    return_char_alignments=False,
                    progress_callback=progress,
                )
            )
        finally:
            del align_model
        return PipelineContext(
            result,
            context.audio,
            context.detected_language,
            context.detected_language_probability,
        )

    def diarize(
        self,
        context: PipelineContext,
        spec: WorkerSpec,
        token: str,
        progress: Callable[[float], None],
    ) -> tuple[PipelineContext, dict[str, list[float]] | None]:
        from whisperx.diarize import DiarizationPipeline, assign_word_speakers

        diarizer = DiarizationPipeline(token=token, device="cuda")
        try:
            diarize_output = diarizer(
                context.audio,
                num_speakers=spec.speaker_count,
                return_embeddings=True,
                progress_callback=progress,
            )
            diarize_frame, embeddings = diarize_output
            result = dict(
                assign_word_speakers(
                    diarize_frame,
                    context.result,
                    speaker_embeddings=embeddings,
                    fill_nearest=False,
                )
            )
        finally:
            del diarizer
        return (
            PipelineContext(
                result,
                context.audio,
                context.detected_language,
                context.detected_language_probability,
            ),
            embeddings,
        )

    def cleanup(self) -> None:
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


def run_job(
    spec: WorkerSpec,
    pipeline: PipelineAdapter,
    emit: Callable[[WorkerEvent], None],
    *,
    token: str | None,
) -> RunOutcome:
    """Run one job and always leave the archived source plus a clear result state."""
    warnings: list[dict[str, str]] = []
    temporary = spec.temp_root / f"job_{_safe_job_component(spec.job_id)}"
    started = time.monotonic()
    context: PipelineContext | None = None
    duration: float | None = None
    lease: WorkerLease | None = None
    try:
        lease = _create_worker_lease()
        lease.acquire()
        _emit(emit, "stage", "preparing", "Preparing transcription runtime…")
        pipeline.verify_cuda()
        duration = pipeline.duration_seconds(spec.media_path)

        _emit(emit, "stage", "extracting_audio", "Extracting faithful 16 kHz FLAC audio…")
        audio_path = pipeline.extract_audio(spec.media_path, temporary)

        _emit(emit, "stage", "loading_model", "Loading transcription model…")
        _emit(emit, "stage", "transcribing", "Transcribing…")
        context = pipeline.transcribe(
            audio_path,
            spec,
            lambda progress: _emit(
                emit, "progress", "transcribing", "Transcribing…", progress=progress
            ),
        )
        language_source = "forced" if spec.language != "auto" else "auto"

        _emit(emit, "stage", "aligning", "Aligning timestamps…")
        try:
            context = pipeline.align(
                context,
                lambda progress: _emit(
                    emit, "progress", "aligning", "Aligning timestamps…", progress=progress
                ),
            )
        except Exception as error:
            warnings.append(
                {
                    "code": "alignment_failed",
                    "message": "Alignment failed; the unaligned transcript was preserved.",
                }
            )
            logger.exception("Alignment stage failed: %s", error)

        embeddings: dict[str, list[float]] | None = None
        profile_matches: dict[str, ProfileMatch] = {}
        diarization_metadata: dict[str, Any] = {"enabled": bool(token), "speaker_count": None}
        if token:
            _emit(emit, "stage", "diarizing", "Diarizing speakers…")
            try:
                context, embeddings = pipeline.diarize(
                    context,
                    spec,
                    token,
                    lambda progress: _emit(
                        emit,
                        "progress",
                        "diarizing",
                        "Diarizing speakers…",
                        progress=progress,
                    ),
                )
                profile_matches = apply_profile_matches(
                    context.result,
                    embeddings,
                    _profiles_from_spec(spec),
                    threshold=spec.speaker_match_threshold,
                )
                diarization_metadata["speaker_count"] = len(embeddings or _speaker_labels(context.result))
                diarization_metadata["speaker_profile_matches"] = {
                    label: {
                        "profile_id": match.profile_id,
                        "display_name": match.display_name,
                        "similarity": match.similarity,
                        "threshold": match.threshold,
                        "method": match.method,
                    }
                    for label, match in profile_matches.items()
                }
            except Exception as error:
                warnings.append(
                    {
                        "code": _diarization_error_code(error),
                        "message": _diarization_error_message(error, token),
                    }
                )
                logger.exception("Diarization stage failed: %s", error)
        else:
            warnings.append(
                {
                    "code": "diarization_token_missing",
                    "message": "Diarization credentials are missing; the transcript was saved without speaker labels.",
                }
            )

        _emit(emit, "stage", "exporting", "Writing transcript files…")
        completed_status = (
            RunStatus.COMPLETED_WITH_WARNINGS if warnings else RunStatus.COMPLETED
        )
        job = _manifest_job(spec, completed_status)
        transcript = build_canonical_transcript(
            job=job,
            result=context.result,
            detected_language=context.detected_language,
            detected_language_probability=context.detected_language_probability,
            language_source=language_source,
            speaker_embeddings=embeddings,
        )
        outputs = write_transcript_outputs(spec.archive_dir, transcript)
        manifest = build_manifest(
            job,
            app_version=__version__,
            media={"duration_seconds": duration},
            transcription={
                "engine": "WhisperX",
                "model": spec.model,
                "device": "cuda",
                "compute_type": "float16",
                "batch_size": spec.batch_size,
                "detected_language": context.detected_language,
                "detected_language_probability": context.detected_language_probability,
                "language_source": language_source,
            },
            diarization=diarization_metadata,
            outputs={kind: path.name for kind, path in outputs.items()},
            timings={"total_seconds": round(time.monotonic() - started, 3)},
            errors={warning["code"]: warning["message"] for warning in warnings},
        )
        write_manifest(spec.archive_dir, manifest)
        speaker_count = diarization_metadata.get("speaker_count")
        warning_text = " ".join(warning["message"] for warning in warnings) or None
        _emit(
            emit,
            "completed",
            "finalizing",
            "Completed with warnings." if warnings else "Completed.",
            payload={
                "status": completed_status.value,
                "detected_language": context.detected_language,
                "detected_language_probability": context.detected_language_probability,
                "language_source": language_source,
                "speaker_count": speaker_count,
                "duration_seconds": duration,
                "warning_text": warning_text,
                "archive_dir": str(spec.archive_dir),
            },
        )
        return RunOutcome(
            completed_status,
            context.detected_language,
            speaker_count,
            warning_text,
        )
    except KeyboardInterrupt:
        outcome = RunOutcome(
            RunStatus.CANCELLED,
            error_code="cancelled",
            error_message="Processing was stopped. Your recording is safe in the archive and can be retried.",
        )
        _emit(emit, "cancelled", "finalizing", outcome.error_message or "Processing stopped.")
        return outcome
    except Exception as error:
        failure = classify_worker_error(error, token)
        _write_failure_manifest(spec, failure, duration, started)
        _emit(
            emit,
            "failed",
            "failed",
            failure.message,
            payload={"code": failure.code, "archive_dir": str(spec.archive_dir)},
        )
        return RunOutcome(RunStatus.FAILED, error_code=failure.code, error_message=failure.message)
    finally:
        if lease is not None:
            try:
                lease.close()
            except Exception:
                logger.exception("Could not release the worker lease")
        try:
            pipeline.cleanup()
        except Exception:
            logger.exception("Pipeline cleanup failed")
        if not spec.keep_temporary_audio:
            _remove_job_temp(temporary, spec.temp_root)


def classify_worker_error(error: Exception, token: str | None) -> WorkerFailure:
    """Map raw dependency errors to concise, safe wording for the controller."""
    if isinstance(error, WorkerFailure):
        return WorkerFailure(error.code, redact_text(error.message, {token or ""}))
    if isinstance(error, WorkerLeaseBusyError):
        return WorkerFailure(
            "worker_busy", "Another WorkCall transcription worker is already active."
        )
    raw = redact_text(str(error), {token or ""}).lower()
    if "out of memory" in raw or "cuda oom" in raw:
        return WorkerFailure(
            "gpu_out_of_memory",
            "The GPU ran out of memory. Your recording is safe in the archive. Close GPU-heavy apps and click Retry.",
        )
    if "cuda" in raw and ("unavailable" in raw or "not available" in raw or "not compiled" in raw):
        return WorkerFailure(
            "cuda_unavailable",
            "CUDA is unavailable. Your recording is safe in the archive. Fix the GPU runtime and click Retry.",
        )
    if any(marker in raw for marker in ("gated", "403", "401", "license", "access denied")):
        return WorkerFailure(
            "diarization_access",
            "Diarization could not access its Hugging Face model. Check your token and accept the model license, then Retry.",
        )
    if "ffmpeg" in raw:
        return WorkerFailure(
            "ffmpeg_failed",
            "FFmpeg could not process this recording. Your original recording is safe in the archive.",
        )
    return WorkerFailure(
        "worker_failed",
        "Transcription failed. Your recording is safe in the archive. Open the job log for details and click Retry.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WorkCall Transcriber WhisperX worker")
    parser.add_argument("--spec", required=True, type=Path)
    arguments = parser.parse_args(argv)
    spec = read_worker_spec(arguments.spec)
    token = os.environ.get("WORKCALL_HF_TOKEN")
    _configure_worker_logging(spec.archive_dir, token)
    outcome = run_job(spec, WhisperXPipeline(), _emit_stdout, token=token)
    return 0 if outcome.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_WARNINGS} else 1


def _emit(
    emit: Callable[[WorkerEvent], None],
    event_type: str,
    stage: str,
    message: str,
    *,
    progress: float | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    emit(WorkerEvent(event_type, stage, message, progress, payload or {}))


def _emit_stdout(event: WorkerEvent) -> None:
    print(event_line(event), flush=True)


def _manifest_job(spec: WorkerSpec, status: RunStatus) -> JobRecord:
    try:
        origin = JobOrigin(spec.source_origin)
    except ValueError:
        origin = JobOrigin.MANUAL
    finished = datetime.now(UTC).isoformat()
    return JobRecord(
        id=spec.job_id,
        source_path=spec.original_path or spec.media_path,
        source_name=(spec.original_path or spec.media_path).name,
        origin=origin,
        status=(
            JobStatus.COMPLETED_WITH_WARNINGS
            if status is RunStatus.COMPLETED_WITH_WARNINGS
            else JobStatus.COMPLETED
        ),
        detected_at=spec.detected_at or finished,
        source_size=spec.source_size or (spec.media_path.stat().st_size if spec.media_path.exists() else None),
        archive_dir=spec.archive_dir,
        archived_media_path=spec.media_path,
        source_sha256=spec.source_sha256,
        started_at=spec.started_at or finished,
        completed_at=finished,
    )


def _write_failure_manifest(
    spec: WorkerSpec,
    failure: WorkerFailure,
    duration: float | None,
    started: float,
) -> None:
    try:
        job = _manifest_job(spec, RunStatus.COMPLETED)
        failure_job = JobRecord(**{**job.__dict__, "status": JobStatus.FAILED})
        write_manifest(
            spec.archive_dir,
            build_manifest(
                failure_job,
                app_version=__version__,
                media={"duration_seconds": duration},
                timings={"total_seconds": round(time.monotonic() - started, 3)},
                errors={failure.code: failure.message},
            ),
        )
    except Exception:
        logger.exception("Could not write failure manifest")


def _speaker_labels(result: dict[str, Any]) -> set[str]:
    return {
        str(segment["speaker"])
        for segment in result.get("segments") or []
        if isinstance(segment, dict) and segment.get("speaker")
    }


def _diarization_error_code(error: Exception) -> str:
    return "diarization_access" if classify_worker_error(error, None).code == "diarization_access" else "diarization_failed"


def _diarization_error_message(error: Exception, token: str) -> str:
    failure = classify_worker_error(error, token)
    if failure.code == "diarization_access":
        return failure.message
    return "Diarization failed; the transcript was saved without speaker labels."


def _remove_job_temp(temporary: Path, temp_root: Path) -> None:
    try:
        resolved_root = temp_root.resolve(strict=False)
        resolved_temp = temporary.resolve(strict=False)
        resolved_temp.relative_to(resolved_root)
    except (OSError, ValueError):
        return
    if resolved_temp.name.startswith("job_") and resolved_temp.exists():
        shutil.rmtree(resolved_temp)


def _configure_worker_logging(archive_dir: Path, token: str | None) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(archive_dir / "processing.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactionFilter({token or ""}))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _safe_job_component(job_id: str) -> str:
    return "".join(character for character in job_id if character.isalnum() or character in {"-", "_"})


def _profiles_from_spec(spec: WorkerSpec) -> list[SpeakerProfile]:
    profiles: list[SpeakerProfile] = []
    for item in spec.speaker_profiles:
        try:
            profiles.append(
                SpeakerProfile(
                    str(item["id"]),
                    str(item["display_name"]),
                    tuple(float(value) for value in item["centroid"]),
                    int(item["reference_count"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Ignoring malformed speaker profile data for this job.")
    return profiles


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _configure_whisperx_logging() -> None:
    """Keep the worker's stdout exclusively for its JSON-lines protocol.

    WhisperX configures its first logger handler against ``sys.stdout`` during
    import. Its ordinary informational messages would then be mistaken for
    malformed progress events by the controller. Configure that isolated logger
    before importing WhisperX and send dependency diagnostics to stderr instead.
    """
    whisperx_logger = logging.getLogger("whisperx")
    for handler in tuple(whisperx_logger.handlers):
        whisperx_logger.removeHandler(handler)
        handler.close()
    whisperx_logger.setLevel(logging.WARNING)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    whisperx_logger.addHandler(stderr_handler)
    whisperx_logger.propagate = False


if __name__ == "__main__":
    raise SystemExit(main())
