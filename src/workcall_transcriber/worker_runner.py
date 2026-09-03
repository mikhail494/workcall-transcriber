"""Asynchronous external-worker launch, event streaming, and safe cancellation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .security import redact_text
from .worker_lock import WorkerLeaseState, worker_lease_state
from .worker_protocol import (
    WorkerEvent,
    WorkerProtocolError,
    WorkerSpec,
    parse_worker_event,
    write_worker_spec,
)


@dataclass(frozen=True)
class WorkerFinished:
    job_id: str
    return_code: int
    cancelled_by_user: bool
    stderr: str


@dataclass(frozen=True)
class ProcessInspection:
    """The result of a deliberate, fail-closed inspection of one persisted PID."""

    exists: bool | None
    executable_path: str | None = None
    command: str | None = None


class OrphanWorkerStatus(StrEnum):
    """Whether startup can safely clear a durable worker launch block."""

    STOPPED = "stopped"
    ABSENT = "absent"
    PID_REUSED = "pid_reused"
    UNCONFIRMED = "unconfirmed"

    @property
    def permits_recovery(self) -> bool:
        return self in {
            OrphanWorkerStatus.STOPPED,
            OrphanWorkerStatus.ABSENT,
            OrphanWorkerStatus.PID_REUSED,
        }


@dataclass
class _CurrentWorker:
    job_id: str
    process: subprocess.Popen[str]
    token: str | None
    spec_path: Path
    temp_root: Path
    cancelled_by_user: bool = False


CommandBuilder = Callable[[Path], list[str]]
EventCallback = Callable[[WorkerEvent], None]
FinishCallback = Callable[[WorkerFinished], None]
ProcessInspector = Callable[[int], ProcessInspection]
ProcessLister = Callable[[], list[tuple[int, ProcessInspection]] | None]
ProcessTerminator = Callable[[int], bool]


class WorkerRunner:
    """Own exactly one child worker and its process tree at a time."""

    def __init__(
        self,
        *,
        worker_python: Path | None = None,
        worker_package_root: Path | None = None,
        command_builder: CommandBuilder | None = None,
        process_inspector: ProcessInspector | None = None,
        process_lister: ProcessLister | None = None,
        process_terminator: ProcessTerminator | None = None,
    ) -> None:
        self._worker_python = worker_python or Path(r"D:\WhisperWork\.venv\Scripts\python.exe")
        self._worker_package_root = worker_package_root or _default_worker_package_root()
        self._command_builder = command_builder
        self._process_inspector = process_inspector or _inspect_process_command
        self._process_lister = process_lister or _list_python_processes
        self._process_terminator = process_terminator or _terminate_process_tree
        self._lock = threading.Lock()
        self._current: _CurrentWorker | None = None

    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.process.poll() is None

    def start(
        self,
        spec: WorkerSpec,
        token: str | None,
        on_event: EventCallback,
        on_finished: FinishCallback,
        on_started: Callable[[int], None] | None = None,
    ) -> bool:
        """Launch one worker and return False if another one is still alive."""
        with self._lock:
            if self._current is not None and self._current.process.poll() is None:
                return False
            if self._command_builder is None and not self._worker_python.is_file():
                raise FileNotFoundError(
                    "WhisperX runtime was not found at D:\\WhisperWork\\.venv\\Scripts\\python.exe"
                )
            spec_path = _worker_spec_path(spec.temp_root, spec.job_id)
            write_worker_spec(spec_path, spec)
            command = (
                self._command_builder(spec_path)
                if self._command_builder is not None
                else [
                    str(self._worker_python),
                    "-m",
                    "workcall_transcriber.worker",
                    "--spec",
                    str(spec_path),
                ]
            )
            environment = os.environ.copy()
            environment.pop("WORKCALL_HF_TOKEN", None)
            if token:
                environment["WORKCALL_HF_TOKEN"] = token
            if self._command_builder is None:
                previous_python_path = environment.get("PYTHONPATH")
                environment["PYTHONPATH"] = (
                    str(self._worker_package_root)
                    if not previous_python_path
                    else f"{self._worker_package_root}{os.pathsep}{previous_python_path}"
                )
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except BaseException:
                _remove_worker_spec(spec_path, spec.temp_root)
                raise
            current = _CurrentWorker(spec.job_id, process, token, spec_path, spec.temp_root)
            self._current = current
            try:
                if on_started is not None:
                    on_started(process.pid)
            except BaseException:
                self._current = None
                self._process_terminator(process.pid)
                _remove_worker_spec(spec_path, spec.temp_root)
                raise
        watcher = threading.Thread(
            target=self._watch,
            args=(current, on_event, on_finished),
            name=f"WorkCallWorker-{spec.job_id[:8]}",
            daemon=True,
        )
        watcher.start()
        return True

    def stop_current(self) -> bool:
        """Terminate only the tracked worker tree, preserving archived media and logs."""
        with self._lock:
            current = self._current
            if current is None or current.process.poll() is not None:
                return False
            current.cancelled_by_user = True
            process_id = current.process.pid
        return self._process_terminator(process_id)

    def worker_lease_state(self) -> WorkerLeaseState:
        """Expose the worker-held named mutex to startup crash recovery."""
        return worker_lease_state()

    def stop_orphaned_worker(
        self, process_id: int, job_id: str, temp_root: Path
    ) -> OrphanWorkerStatus:
        """Stop a prior worker or deliberately keep the queue blocked when uncertain."""
        if process_id <= 0:
            return OrphanWorkerStatus.UNCONFIRMED
        try:
            inspection = self._process_inspector(process_id)
        except Exception:
            return OrphanWorkerStatus.UNCONFIRMED
        if inspection.exists is False:
            return OrphanWorkerStatus.ABSENT
        if inspection.exists is not True:
            return OrphanWorkerStatus.UNCONFIRMED
        if not inspection.command or not inspection.executable_path:
            return OrphanWorkerStatus.UNCONFIRMED
        expected_spec = _worker_spec_path(temp_root, job_id)
        if not _is_exact_workcall_worker_process(
            inspection,
            worker_python=self._worker_python,
            expected_spec=expected_spec,
        ):
            # The persisted PID now belongs to another process, so the original
            # worker has exited. Never terminate an unrelated process.
            return OrphanWorkerStatus.PID_REUSED
        return (
            OrphanWorkerStatus.STOPPED
            if self._process_terminator(process_id)
            else OrphanWorkerStatus.UNCONFIRMED
        )

    def stop_unrecorded_worker(self, job_id: str, temp_root: Path) -> OrphanWorkerStatus:
        """Resolve a worker launched just before a GUI crash persisted its PID.

        A named mutex is acquired early by the child, but startup must also inspect
        the exact command line: another GUI can restart while that child is still
        in Python startup, before it has created the mutex.
        """
        try:
            observed = self._process_lister()
        except Exception:
            return OrphanWorkerStatus.UNCONFIRMED
        if observed is None:
            return OrphanWorkerStatus.UNCONFIRMED
        expected_spec = _worker_spec_path(temp_root, job_id)
        matches = [
            process_id
            for process_id, inspection in observed
            if process_id > 0
            and inspection.exists is True
            and _is_exact_workcall_worker_process(
                inspection,
                worker_python=self._worker_python,
                expected_spec=expected_spec,
            )
        ]
        if not matches:
            return OrphanWorkerStatus.ABSENT
        if len(matches) != 1:
            return OrphanWorkerStatus.UNCONFIRMED
        return (
            OrphanWorkerStatus.STOPPED
            if self._process_terminator(matches[0])
            else OrphanWorkerStatus.UNCONFIRMED
        )

    def _watch(
        self,
        current: _CurrentWorker,
        on_event: EventCallback,
        on_finished: FinishCallback,
    ) -> None:
        stderr_chunks: list[str] = []

        def drain_stderr() -> None:
            assert current.process.stderr is not None
            for line in current.process.stderr:
                # Cap retained diagnostics; the per-job log has the full safe output.
                if sum(len(chunk) for chunk in stderr_chunks) < 16_000:
                    stderr_chunks.append(line)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        assert current.process.stdout is not None
        for line in current.process.stdout:
            try:
                event = parse_worker_event(line)
            except WorkerProtocolError:
                event = WorkerEvent(
                    "protocol_error",
                    "worker_protocol",
                    "The transcription worker emitted an invalid progress event. See the job log.",
                    payload={},
                )
            try:
                on_event(event)
            except Exception:
                # A UI callback must not strand the independently running worker.
                continue
        return_code = current.process.wait()
        stderr_thread.join(timeout=2)
        safe_stderr = redact_text("".join(stderr_chunks), {current.token or ""})
        with self._lock:
            if self._current is current:
                self._current = None
        _remove_worker_spec(current.spec_path, current.temp_root)
        on_finished(
            WorkerFinished(
                current.job_id,
                return_code,
                current.cancelled_by_user,
                safe_stderr,
            )
        )


def _default_worker_package_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "worker_lib"
    return Path(__file__).resolve().parents[1]


def _inspect_process_command(process_id: int) -> ProcessInspection:
    """Read one Windows process command line without trusting a persisted PID alone."""
    if os.name != "nt" or process_id <= 0:
        return ProcessInspection(exists=None)
    command = (
        "$ErrorActionPreference = 'Stop'; try { "
        f"Get-CimInstance Win32_Process -Filter 'ProcessId = {int(process_id)}' "
        "| Select-Object ProcessId, ExecutablePath, CommandLine | ConvertTo-Json -Compress "
        "} catch { exit 1 }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ProcessInspection(exists=None)
    if result.returncode != 0:
        return ProcessInspection(exists=None)
    output = result.stdout.strip().lstrip("\ufeff")
    if not output:
        return ProcessInspection(exists=False)
    try:
        payload = json.loads(output)
        observed_pid = int(payload["ProcessId"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ProcessInspection(exists=None)
    if observed_pid != process_id:
        return ProcessInspection(exists=None)
    command_line = payload.get("CommandLine")
    executable_path = payload.get("ExecutablePath")
    return ProcessInspection(
        exists=True,
        executable_path=str(executable_path) if isinstance(executable_path, str) else None,
        command=str(command_line) if isinstance(command_line, str) else None,
    )


def _list_python_processes() -> list[tuple[int, ProcessInspection]] | None:
    """List Python command lines or return ``None`` when Windows cannot prove it."""
    if os.name != "nt":
        return None
    command = (
        "$ErrorActionPreference = 'Stop'; try { "
        "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" "
        "| Select-Object ProcessId, ExecutablePath, CommandLine | ConvertTo-Json -Compress "
        "} catch { exit 1 }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip().lstrip("\ufeff")
    if not output:
        return []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    records = payload if isinstance(payload, list) else [payload]
    observed: list[tuple[int, ProcessInspection]] = []
    for record in records:
        if not isinstance(record, dict):
            return None
        try:
            process_id = int(record["ProcessId"])
        except (KeyError, TypeError, ValueError):
            return None
        executable_path = record.get("ExecutablePath")
        command_line = record.get("CommandLine")
        observed.append(
            (
                process_id,
                ProcessInspection(
                    exists=True,
                    executable_path=(
                        str(executable_path) if isinstance(executable_path, str) else None
                    ),
                    command=str(command_line) if isinstance(command_line, str) else None,
                ),
            )
        )
    return observed


def _is_exact_workcall_worker_process(
    inspection: ProcessInspection,
    *,
    worker_python: Path,
    expected_spec: Path,
) -> bool:
    if not inspection.command or not inspection.executable_path:
        return False
    arguments = _split_windows_command_line(inspection.command)
    if arguments is None or len(arguments) != 5:
        return False
    return (
        _same_path(inspection.executable_path, worker_python)
        and _same_path(arguments[0], worker_python)
        and arguments[1:4] == ["-m", "workcall_transcriber.worker", "--spec"]
        and _same_path(arguments[4], expected_spec)
    )


def _split_windows_command_line(command: str) -> list[str] | None:
    if os.name != "nt" or not command:
        return None
    try:
        import ctypes

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        command_line_to_argv = shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int))
        command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
        count = ctypes.c_int()
        arguments_pointer = command_line_to_argv(command, ctypes.byref(count))
        if not arguments_pointer:
            return None
        try:
            return [arguments_pointer[index] for index in range(count.value)]
        finally:
            local_free(arguments_pointer)
    except (AttributeError, OSError):
        return None


def _same_path(actual: str | Path, expected: str | Path) -> bool:
    try:
        return str(Path(actual).resolve(strict=False)).casefold() == str(
            Path(expected).resolve(strict=False)
        ).casefold()
    except OSError:
        return False


def _terminate_process_tree(process_id: int) -> bool:
    if process_id <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process_id), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return False
        return result.returncode == 0
    try:
        os.kill(process_id, 15)
    except OSError:
        return False
    return True


def _worker_spec_path(temp_root: Path, job_id: str) -> Path:
    safe_job_id = "".join(character for character in job_id if character.isalnum() or character in {"-", "_"})
    if not safe_job_id:
        raise ValueError("Worker job id must contain a letter or number.")
    return temp_root / f"worker_{safe_job_id}.json"


def _remove_worker_spec(spec_path: Path, temp_root: Path) -> None:
    """Delete only the non-secret spec file that this runner itself created."""
    try:
        resolved_root = temp_root.resolve(strict=False)
        resolved_path = spec_path.resolve(strict=False)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return
    if resolved_path.parent == resolved_root and resolved_path.name.startswith("worker_"):
        resolved_path.unlink(missing_ok=True)
