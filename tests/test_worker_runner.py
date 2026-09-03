import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import workcall_transcriber.worker_runner as runner_module
from workcall_transcriber.worker_protocol import WorkerSpec
from workcall_transcriber.worker_runner import (
    OrphanWorkerStatus,
    ProcessInspection,
    WorkerRunner,
)


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
    )


def test_runner_forwards_json_events_and_completion(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        "import json\nprint(json.dumps({'type':'completed','stage':'finalizing','message':'Completed.','payload':{}}), flush=True)\n",
        encoding="utf-8",
    )
    events = []
    completed = []
    started_pids = []
    done = threading.Event()
    runner = WorkerRunner(command_builder=lambda _: [sys.executable, str(script)])

    assert runner.start(
        _spec(tmp_path),
        None,
        events.append,
        lambda result: (completed.append(result), done.set()),
        on_started=started_pids.append,
    )
    assert len(started_pids) == 1
    assert started_pids[0] > 0
    assert done.wait(5)
    assert events[0].type == "completed"
    assert completed[0].return_code == 0
    assert completed[0].cancelled_by_user is False
    assert not (tmp_path / "temp" / "worker_job-1.json").exists()


def test_runner_stop_marks_the_known_worker_as_cancelled(tmp_path: Path) -> None:
    script = tmp_path / "slow_worker.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    done = threading.Event()
    completed = []
    runner = WorkerRunner(command_builder=lambda _: [sys.executable, str(script)])

    assert runner.start(_spec(tmp_path), None, lambda _: None, lambda result: (completed.append(result), done.set()))
    assert runner.stop_current() is True
    assert done.wait(10)
    assert completed[0].cancelled_by_user is True


def test_runner_terminates_only_an_identified_orphaned_worker() -> None:
    terminated: list[int] = []
    worker_python = Path(r"D:\WhisperWork\.venv\Scripts\python.exe")
    temp_root = Path(r"D:\WorkCalls\Temp")
    expected_command = (
        'D:\\WhisperWork\\.venv\\Scripts\\python.exe -m workcall_transcriber.worker '
        '--spec D:\\WorkCalls\\Temp\\worker_job-1.json'
    )
    runner = WorkerRunner(
        process_inspector=lambda _: ProcessInspection(
            exists=True,
            executable_path=str(worker_python),
            command=expected_command,
        ),
        process_terminator=lambda process_id: terminated.append(process_id) or True,
    )

    assert runner.stop_orphaned_worker(4242, "job-1", temp_root) is OrphanWorkerStatus.STOPPED
    assert terminated == [4242]

    unrelated = WorkerRunner(
        process_inspector=lambda _: ProcessInspection(
            exists=True,
            executable_path="C:\\Python\\python.exe",
            command="python unrelated_script.py",
        ),
        process_terminator=lambda process_id: terminated.append(process_id) or True,
    )
    assert (
        unrelated.stop_orphaned_worker(4343, "job-1", temp_root)
        is OrphanWorkerStatus.PID_REUSED
    )
    assert terminated == [4242]


def test_runner_never_kills_a_substring_only_pid_reuse() -> None:
    terminated: list[int] = []
    worker_python = Path(r"D:\WhisperWork\.venv\Scripts\python.exe")
    temp_root = Path(r"D:\WorkCalls\Temp")
    runner = WorkerRunner(
        process_inspector=lambda _: ProcessInspection(
            exists=True,
            executable_path=str(worker_python),
            command=(
                '"D:\\WhisperWork\\.venv\\Scripts\\python.exe" helper.py '
                '"workcall_transcriber.worker --spec D:\\WorkCalls\\Temp\\worker_job-1.json"'
            ),
        ),
        process_terminator=lambda process_id: terminated.append(process_id) or True,
    )

    outcome = runner.stop_orphaned_worker(4242, "job-1", temp_root)

    assert outcome is OrphanWorkerStatus.PID_REUSED
    assert terminated == []


def test_runner_terminates_an_exact_pidless_worker_before_queue_recovery() -> None:
    terminated: list[int] = []
    worker_python = Path(r"D:\WhisperWork\.venv\Scripts\python.exe")
    temp_root = Path(r"D:\WorkCalls\Temp")
    exact = ProcessInspection(
        exists=True,
        executable_path=str(worker_python),
        command=(
            '"D:\\WhisperWork\\.venv\\Scripts\\python.exe" -m workcall_transcriber.worker '
            '--spec "D:\\WorkCalls\\Temp\\worker_job-1.json"'
        ),
    )
    unrelated = ProcessInspection(
        exists=True,
        executable_path=r"C:\Python\python.exe",
        command=r"C:\Python\python.exe unrelated.py",
    )
    runner = WorkerRunner(
        process_lister=lambda: [(4242, exact), (4343, unrelated)],
        process_terminator=lambda process_id: terminated.append(process_id) or True,
    )

    outcome = runner.stop_unrecorded_worker("job-1", temp_root)

    assert outcome is OrphanWorkerStatus.STOPPED
    assert terminated == [4242]


def test_runner_keeps_pidless_recovery_blocked_for_an_unconfirmed_or_ambiguous_scan() -> None:
    worker_python = Path(r"D:\WhisperWork\.venv\Scripts\python.exe")
    temp_root = Path(r"D:\WorkCalls\Temp")
    exact = ProcessInspection(
        exists=True,
        executable_path=str(worker_python),
        command=(
            'D:\\WhisperWork\\.venv\\Scripts\\python.exe -m workcall_transcriber.worker '
            '--spec D:\\WorkCalls\\Temp\\worker_job-1.json'
        ),
    )
    unavailable = WorkerRunner(process_lister=lambda: None)
    ambiguous = WorkerRunner(process_lister=lambda: [(4242, exact), (4343, exact)])

    assert (
        unavailable.stop_unrecorded_worker("job-1", temp_root)
        is OrphanWorkerStatus.UNCONFIRMED
    )
    assert (
        ambiguous.stop_unrecorded_worker("job-1", temp_root)
        is OrphanWorkerStatus.UNCONFIRMED
    )


def test_runner_keeps_the_queue_blocked_when_orphan_inspection_is_inconclusive() -> None:
    runner = WorkerRunner(
        process_inspector=lambda _: ProcessInspection(exists=None),
        process_terminator=lambda _: True,
    )

    assert runner.stop_orphaned_worker(4242, "job-1", Path(r"D:\WorkCalls\Temp")) is OrphanWorkerStatus.UNCONFIRMED


def test_runner_does_not_treat_an_unreadable_existing_process_as_a_reused_pid() -> None:
    for command in (None, ""):
        runner = WorkerRunner(
            process_inspector=lambda _, value=command: ProcessInspection(exists=True, command=value),
            process_terminator=lambda _: True,
        )

        assert (
            runner.stop_orphaned_worker(4242, "job-1", Path(r"D:\WorkCalls\Temp"))
            is OrphanWorkerStatus.UNCONFIRMED
        )


def test_windows_process_inspection_is_unconfirmed_when_cim_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(returncode=1, stdout=""),
    )

    inspection = runner_module._inspect_process_command(4242)

    assert inspection.exists is None
