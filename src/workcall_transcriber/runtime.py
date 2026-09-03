"""Non-invasive checks for the separately managed WhisperX runtime."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .security import CredentialStore


@dataclass(frozen=True)
class RuntimeReadiness:
    whisper_runtime: bool
    cuda: bool
    ffmpeg: bool
    diarization_available: bool
    credentials_configured: bool
    detail: str = ""


class RuntimeInspector:
    """Inspect, never modify, the proven D:\\WhisperWork environment."""

    def __init__(
        self,
        credential_store: CredentialStore,
        runtime_python: Path = Path(r"D:\WhisperWork\.venv\Scripts\python.exe"),
    ) -> None:
        self._credential_store = credential_store
        self._runtime_python = runtime_python

    def inspect(self) -> RuntimeReadiness:
        ffmpeg = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
        credentials = self._credential_store.is_configured()
        if not self._runtime_python.is_file():
            return RuntimeReadiness(False, False, ffmpeg, False, credentials, "WhisperX runtime was not found.")
        script = (
            "import importlib.util,json,torch; "
            "print(json.dumps({'cuda':torch.cuda.is_available(),"
            "'whisperx':bool(importlib.util.find_spec('whisperx')),"
            "'diarization':bool(importlib.util.find_spec('pyannote.audio'))}))"
        )
        try:
            completed = subprocess.run(
                [str(self._runtime_python), "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data = json.loads(completed.stdout) if completed.returncode == 0 else {}
            return RuntimeReadiness(
                bool(data.get("whisperx")),
                bool(data.get("cuda")),
                ffmpeg,
                bool(data.get("diarization")),
                credentials,
                "" if completed.returncode == 0 else "WhisperX runtime check failed.",
            )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return RuntimeReadiness(False, False, ffmpeg, False, credentials, "WhisperX runtime check failed.")
