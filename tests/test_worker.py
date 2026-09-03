import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import workcall_transcriber.worker as worker_module
from workcall_transcriber.worker import (
    PipelineContext,
    RunStatus,
    WhisperXPipeline,
    classify_worker_error,
    run_job,
)
from workcall_transcriber.worker_protocol import WorkerEvent, WorkerSpec


class FakePipeline:
    def verify_cuda(self) -> None:
        return None

    def extract_audio(self, media_path: Path, temp_directory: Path) -> Path:
        temp_directory.mkdir(parents=True, exist_ok=True)
        canonical = temp_directory / "canonical.flac"
        canonical.write_bytes(b"audio")
        return canonical

    def duration_seconds(self, media_path: Path) -> float:
        return 12.0

    def transcribe(self, audio_path: Path, spec: WorkerSpec, progress) -> PipelineContext:
        progress(50.0)
        return PipelineContext(
            result={
                "language": "ru",
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "Привет", "speaker": "SPEAKER_00"}
                ],
            },
            audio=b"audio",
            detected_language="ru",
            detected_language_probability=None,
        )

    def align(self, context: PipelineContext, progress) -> PipelineContext:
        return context

    def diarize(self, context: PipelineContext, spec: WorkerSpec, token: str, progress):
        return context, {"SPEAKER_00": [1.0, 0.0]}

    def cleanup(self) -> None:
        return None


class CudaFailurePipeline(FakePipeline):
    def verify_cuda(self) -> None:
        raise RuntimeError("CUDA is not available")


def _spec(tmp_path: Path) -> WorkerSpec:
    media = tmp_path / "original.mkv"
    media.write_bytes(b"media")
    archive = tmp_path / "archive"
    archive.mkdir()
    return WorkerSpec(
        job_id="job-1",
        media_path=media,
        archive_dir=archive,
        temp_root=tmp_path / "temp",
        model="large-v3",
        batch_size=8,
        language="auto",
        speaker_count=None,
        keep_temporary_audio=False,
        speaker_match_threshold=0.95,
        speaker_profiles=[
            {
                "id": "profile-1",
                "display_name": "Mikhail",
                "centroid": [1.0, 0.0],
                "reference_count": 1,
            }
        ],
    )


def test_worker_writes_outputs_and_preserves_language_metadata(tmp_path: Path) -> None:
    events: list[WorkerEvent] = []

    outcome = run_job(_spec(tmp_path), FakePipeline(), events.append, token="hf_test_token")

    assert outcome.status is RunStatus.COMPLETED
    assert (tmp_path / "archive" / "transcript.json").exists()
    assert (tmp_path / "archive" / "manifest.json").exists()
    assert events[-1].type == "completed"
    assert events[-1].payload["detected_language"] == "ru"
    transcript = json.loads((tmp_path / "archive" / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["segments"][0]["speaker_identity"] == "Mikhail"


def test_worker_turns_cuda_failure_into_a_safe_actionable_event(tmp_path: Path) -> None:
    events: list[WorkerEvent] = []

    outcome = run_job(_spec(tmp_path), CudaFailurePipeline(), events.append, token="hf_actual_secret")

    assert outcome.status is RunStatus.FAILED
    assert events[-1].payload["code"] == "cuda_unavailable"
    assert "hf_actual_secret" not in events[-1].message


def test_worker_refuses_a_second_gpu_lease_before_loading_the_pipeline(tmp_path: Path, monkeypatch) -> None:
    class BusyLease:
        def acquire(self) -> None:
            raise worker_module.WorkerFailure(
                "worker_busy", "Another WorkCall transcription worker is already active."
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(worker_module, "_create_worker_lease", lambda: BusyLease(), raising=False)
    events = []
    spec = _spec(tmp_path)

    outcome = worker_module.run_job(spec, FakePipeline(), events.append, token=None)

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code == "worker_busy"
    assert events[-1].type == "failed"
    assert events[-1].payload["code"] == "worker_busy"
    assert spec.media_path.exists()
    assert (spec.archive_dir / "manifest.json").exists()


def test_worker_manifest_preserves_the_durable_job_start_time(tmp_path: Path) -> None:
    started_at = "2026-09-02T10:15:00+00:00"
    spec = replace(_spec(tmp_path), started_at=started_at)

    outcome = run_job(spec, FakePipeline(), lambda _: None, token=None)

    manifest = json.loads((tmp_path / "archive" / "manifest.json").read_text(encoding="utf-8"))
    assert outcome.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert manifest["started_at"] == started_at
    assert manifest["completed_at"] != started_at


def test_worker_error_classification_handles_gpu_memory_errors() -> None:
    error = classify_worker_error(RuntimeError("CUDA out of memory"), token=None)

    assert error.code == "gpu_out_of_memory"


def test_transcribe_reserves_stdout_for_json_protocol(monkeypatch, tmp_path: Path) -> None:
    """WhisperX must not retain its default stdout log handler in the worker."""

    class FakeModel:
        def transcribe(self, audio, batch_size, language, progress_callback):
            return {"language": "en", "segments": []}

    logger = logging.getLogger("whisperx")
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate
    logger.handlers[:] = [logging.StreamHandler(sys.stdout)]

    def load_model(*args, **kwargs):
        assert logger.handlers
        assert all(getattr(handler, "stream", None) is not sys.stdout for handler in logger.handlers)
        return FakeModel()

    fake_whisperx = SimpleNamespace(load_model=load_model, load_audio=lambda _: b"audio")
    monkeypatch.setitem(sys.modules, "whisperx", fake_whisperx)
    try:
        context = WhisperXPipeline().transcribe(
            tmp_path / "audio.flac", _spec(tmp_path), lambda _: None
        )
    finally:
        logger.handlers[:] = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    assert context.detected_language == "en"
