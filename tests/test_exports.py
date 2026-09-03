import json
from pathlib import Path

from workcall_transcriber.exports import build_canonical_transcript, write_transcript_outputs
from workcall_transcriber.manifest import build_manifest, write_manifest
from workcall_transcriber.models import JobOrigin, JobRecord, JobStatus


def _job(tmp_path: Path) -> JobRecord:
    return JobRecord(
        id="job-1",
        source_path=tmp_path / "source.mkv",
        source_name="source.mkv",
        origin=JobOrigin.INBOX,
        status=JobStatus.PROCESSING,
        detected_at="2026-09-02T00:00:00+00:00",
        archive_dir=tmp_path,
        archived_media_path=tmp_path / "original_source.mkv",
        source_size=42,
        source_sha256="a" * 64,
    )


def test_canonical_transcript_keeps_transcription_stage_language(tmp_path: Path) -> None:
    transcript = build_canonical_transcript(
        job=_job(tmp_path),
        result={
            "language": "en",  # Simulates a downstream stage overwriting this field.
            "segments": [
                {
                    "start": 1.0,
                    "end": 3.0,
                    "text": "Привет, команда",
                    "speaker": "SPEAKER_00",
                    "words": [{"start": 1.0, "end": 1.5, "word": "Привет"}],
                }
            ],
        },
        detected_language="ru",
        detected_language_probability=0.99,
        language_source="auto",
        speaker_embeddings={"SPEAKER_00": [0.1, 0.2]},
    )

    assert transcript["transcription"]["detected_language"] == "ru"
    assert transcript["transcription"]["language_source"] == "auto"
    assert transcript["segments"][0]["speaker"] == "SPEAKER_00"


def test_output_exporters_write_utf8_txt_srt_vtt_tsv_and_json(tmp_path: Path) -> None:
    transcript = build_canonical_transcript(
        job=_job(tmp_path),
        result={
            "segments": [
                {"start": 1.0, "end": 3.0, "text": "Привет", "speaker": "SPEAKER_00"}
            ]
        },
        detected_language="ru",
        detected_language_probability=None,
        language_source="auto",
        speaker_embeddings=None,
    )

    outputs = write_transcript_outputs(tmp_path, transcript)

    assert set(outputs) == {"json", "txt", "srt", "vtt", "tsv"}
    assert "Привет" in outputs["txt"].read_text(encoding="utf-8")
    assert "SPEAKER_00: Привет" in outputs["srt"].read_text(encoding="utf-8")
    assert json.loads(outputs["json"].read_text(encoding="utf-8"))["schema_version"] == 1


def test_manifest_contains_hash_but_never_credentials(tmp_path: Path) -> None:
    job = _job(tmp_path)
    manifest = build_manifest(
        job,
        app_version="0.1.0",
        transcription={"model": "large-v3", "detected_language": "ru"},
        outputs={"json": "transcript.json"},
    )

    path = write_manifest(tmp_path, manifest)

    saved = path.read_text(encoding="utf-8")
    assert "a" * 64 in saved
    assert "token" not in saved.lower()
