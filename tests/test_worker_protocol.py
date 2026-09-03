import json
from pathlib import Path

import pytest

from workcall_transcriber.worker_protocol import (
    WorkerEvent,
    WorkerProtocolError,
    WorkerSpec,
    parse_worker_event,
    read_worker_spec,
    write_worker_spec,
)


def test_worker_events_are_structured_json_lines() -> None:
    event = parse_worker_event(
        json.dumps(
            {
                "type": "progress",
                "stage": "transcribing",
                "message": "Transcribing…",
                "progress": 34.5,
                "payload": {"detail": "real progress"},
            }
        )
    )

    assert event == WorkerEvent(
        type="progress",
        stage="transcribing",
        message="Transcribing…",
        progress=34.5,
        payload={"detail": "real progress"},
    )


def test_invalid_worker_line_is_reported_as_a_protocol_error() -> None:
    with pytest.raises(WorkerProtocolError):
        parse_worker_event("not json")


def test_worker_spec_round_trips_without_a_token_field(tmp_path: Path) -> None:
    spec = WorkerSpec(
        job_id="job-1",
        media_path=tmp_path / "original.mkv",
        archive_dir=tmp_path / "archive",
        temp_root=tmp_path / "temp",
        model="large-v3",
        batch_size=8,
        language="auto",
        speaker_count=None,
        keep_temporary_audio=False,
        started_at="2026-09-02T10:15:00+00:00",
    )

    path = write_worker_spec(tmp_path / "job.json", spec)

    assert read_worker_spec(path) == spec
    assert "token" not in path.read_text(encoding="utf-8").lower()
