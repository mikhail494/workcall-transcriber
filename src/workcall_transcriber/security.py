"""Windows DPAPI credential storage and defensive secret redaction."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from ctypes import wintypes
from pathlib import Path


class CredentialStorageError(RuntimeError):
    """A safe, non-secret error for credential storage failures."""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_HF_TOKEN_PATTERN = re.compile(r"\bhf_[A-Za-z0-9_-]{8,}\b")
_GENERIC_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[opsu]_|github_pat_|sk-|Bearer\s+)[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE
)


class CredentialStore:
    """Store the diarization token encrypted for the current Windows user."""

    def __init__(self, credential_file: Path) -> None:
        self._credential_file = credential_file

    def is_configured(self) -> bool:
        try:
            return self._credential_file.is_file() and self._credential_file.stat().st_size > 0
        except OSError:
            return False

    def save_token(self, token: str) -> None:
        value = token.strip()
        if not value:
            raise ValueError("The diarization token cannot be empty.")
        protected = _protect(value.encode("utf-8"))
        self._credential_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._credential_file.parent,
            prefix=".credentials-",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._credential_file)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def load_token(self) -> str | None:
        if not self._credential_file.exists():
            return None
        try:
            protected = self._credential_file.read_bytes()
            return _unprotect(protected).decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise CredentialStorageError("Saved diarization credentials could not be read.") from error

    def remove_token(self) -> None:
        self._credential_file.unlink(missing_ok=True)


def redact_text(text: str, known_secrets: Iterable[str] = ()) -> str:
    """Remove recognizable tokens and any caller-provided credential values."""
    redacted = str(text)
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _HF_TOKEN_PATTERN.sub("[REDACTED_HF_TOKEN]", redacted)
    return _GENERIC_TOKEN_PATTERN.sub("[REDACTED_TOKEN]", redacted)


class SecretRedactionFilter(logging.Filter):
    """A logging filter that strips known credentials before handlers see a record."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_info:
            exception_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact_text(exception_text, self._secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, self._secrets)
        return True


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStorageError("Windows credential protection is unavailable on this platform.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, keepalive = _as_blob(data)
    output_blob = _DataBlob()
    success = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "WorkCall Transcriber",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = keepalive
    if not success:
        raise CredentialStorageError("Windows could not securely save the diarization credential.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise CredentialStorageError("Windows credential protection is unavailable on this platform.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, keepalive = _as_blob(data)
    output_blob = _DataBlob()
    success = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = keepalive
    if not success:
        raise CredentialStorageError("Saved diarization credentials could not be decrypted for this user.")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _as_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer
