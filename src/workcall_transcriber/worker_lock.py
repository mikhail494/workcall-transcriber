"""One Windows process-lifetime mutex shared by every transcription worker."""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import StrEnum

WORKER_MUTEX_NAME = r"Local\WorkCallTranscriberGpuWorker"
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_NOT_FOUND = 2
_SYNCHRONIZE = 0x00100000


class WorkerLeaseState(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNCONFIRMED = "unconfirmed"


class WorkerLease:
    """Hold the mutex until process cleanup; Windows releases it if the worker crashes."""

    def __init__(self) -> None:
        self._close_handle: Callable[[int], object] | None = None
        self._handle: int | None = None

    def acquire(self) -> None:
        if os.name != "nt":
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, WORKER_MUTEX_NAME)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create the WorkCall worker mutex.")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            close_handle(handle)
            raise WorkerLeaseBusyError
        self._handle = int(handle)
        self._close_handle = close_handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._close_handle is not None:
            self._close_handle(handle)


class WorkerLeaseBusyError(RuntimeError):
    """Raised before a second worker can load CUDA or begin processing."""


def worker_lease_state() -> WorkerLeaseState:
    """Check mutex visibility without creating or acquiring a new mutex."""
    if os.name != "nt":
        return WorkerLeaseState.UNCONFIRMED
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_mutex = kernel32.OpenMutexW
        open_mutex.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
        open_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        handle = open_mutex(_SYNCHRONIZE, False, WORKER_MUTEX_NAME)
    except (AttributeError, OSError):
        return WorkerLeaseState.UNCONFIRMED
    if handle:
        close_handle(handle)
        return WorkerLeaseState.ACTIVE
    return (
        WorkerLeaseState.INACTIVE
        if ctypes.get_last_error() == _ERROR_FILE_NOT_FOUND
        else WorkerLeaseState.UNCONFIRMED
    )
