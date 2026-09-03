"""Application-owned transcript schema and human-readable export formats."""

from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import JobRecord

TRANSCRIPT_SCHEMA_VERSION = 1


def build_canonical_transcript(
    *,
    job: JobRecord,
    result: dict[str, Any],
    detected_language: str | None,
    detected_language_probability: float | None,
    language_source: str,
    speaker_embeddings: dict[str, list[float]] | None,
) -> dict[str, Any]:
    """Convert model output into the stable schema owned by this application.

    ``detected_language`` is intentionally an explicit argument captured directly
    after transcription. Later alignment or diarization data cannot overwrite it.
    """
    raw_segments = result.get("segments") or []
    segments = [_normalise_segment(segment) for segment in raw_segments]
    speaker_labels = sorted(
        {
            str(segment["speaker"])
            for segment in segments
            if segment.get("speaker")
        }
        | set((speaker_embeddings or {}).keys())
    )
    speakers = []
    for label in speaker_labels:
        matched_segment = next(
            (segment for segment in segments if segment.get("speaker") == label), {}
        )
        speakers.append(
            {
                "raw_label": label,
                "embedding": _json_safe((speaker_embeddings or {}).get(label)),
                "identity": matched_segment.get("speaker_identity"),
                "identity_confidence": matched_segment.get("speaker_identity_confidence"),
                "match_threshold": matched_segment.get("speaker_match_threshold"),
                "matching_method": matched_segment.get("speaker_matching_method"),
            }
        )
    return {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "job": {"id": job.id, "source_filename": job.source_name},
        "transcription": {
            "engine": "WhisperX",
            "detected_language": detected_language,
            "detected_language_probability": detected_language_probability,
            "language_source": language_source,
        },
        "speakers": speakers,
        "segments": segments,
    }


def write_transcript_outputs(output_directory: Path, transcript: dict[str, Any]) -> dict[str, Path]:
    """Write the five supported UTF-8 formats with atomic JSON persistence."""
    output_directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "json": output_directory / "transcript.json",
        "txt": output_directory / "transcript.txt",
        "srt": output_directory / "transcript.srt",
        "vtt": output_directory / "transcript.vtt",
        "tsv": output_directory / "transcript.tsv",
    }
    _atomic_json_write(outputs["json"], transcript)
    segments = transcript.get("segments") or []
    outputs["txt"].write_text(_render_txt(segments), encoding="utf-8", newline="\n")
    outputs["srt"].write_text(_render_srt(segments), encoding="utf-8", newline="\n")
    outputs["vtt"].write_text(_render_vtt(segments), encoding="utf-8", newline="\n")
    with outputs["tsv"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["start", "end", "speaker", "speaker_alias", "text"])
        for segment in segments:
            writer.writerow(
                [
                    _seconds_text(segment.get("start")),
                    _seconds_text(segment.get("end")),
                    segment.get("speaker") or "",
                    segment.get("speaker_identity") or "",
                    segment.get("text") or "",
                ]
            )
    return outputs


def _normalise_segment(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else dict(raw)
    words = [_normalise_word(word) for word in source.get("words") or []]
    return {
        "start": _finite_number(source.get("start")),
        "end": _finite_number(source.get("end")),
        "text": str(source.get("text") or "").strip(),
        "speaker": source.get("speaker"),
        "speaker_identity": source.get("speaker_identity"),
        "speaker_identity_confidence": _finite_number(source.get("speaker_identity_confidence")),
        "speaker_match_threshold": _finite_number(source.get("speaker_match_threshold")),
        "speaker_matching_method": source.get("speaker_matching_method"),
        "words": words,
    }


def _normalise_word(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else dict(raw)
    return {
        "start": _finite_number(source.get("start")),
        "end": _finite_number(source.get("end")),
        "word": str(source.get("word") or ""),
        "score": _finite_number(source.get("score")),
        "speaker": source.get("speaker"),
    }


def _render_txt(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{_timestamp(segment.get('start'), separator=':')}] {_speaker_name(segment)}: "
        f"{segment.get('text') or ''}".rstrip()
        for segment in segments
    ) + ("\n" if segments else "")


def _render_srt(segments: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            f"{index}\n{_timestamp(segment.get('start'), separator=',')} --> "
            f"{_timestamp(segment.get('end'), separator=',')}\n"
            f"{_speaker_name(segment)}: {segment.get('text') or ''}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _render_vtt(segments: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines.extend(
            [
                f"{_timestamp(segment.get('start'), separator='.')} --> "
                f"{_timestamp(segment.get('end'), separator='.')}",
                f"{_speaker_name(segment)}: {segment.get('text') or ''}",
                "",
            ]
        )
    return "\n".join(lines)


def _speaker_name(segment: dict[str, Any]) -> str:
    return str(segment.get("speaker_identity") or segment.get("speaker") or "SPEAKER_UNKNOWN")


def _timestamp(value: Any, separator: str) -> str:
    seconds = max(0.0, float(value or 0.0))
    total_milliseconds = int(round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{separator}{milliseconds:03d}"


def _seconds_text(value: Any) -> str:
    number = _finite_number(value)
    return "" if number is None else f"{number:.3f}"


def _finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    return str(value)


def _atomic_json_write(path: Path, data: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(_json_safe(data), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
