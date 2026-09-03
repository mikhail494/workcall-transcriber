"""Safe eligibility and stability checks for files arriving in the Inbox."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".mov", ".webm", ".avi", ".m4a", ".mp3", ".wav", ".flac"}
)


class StabilityState(StrEnum):
    """The outcome of one non-destructive candidate inspection."""

    INELIGIBLE = "ineligible"
    MISSING = "missing"
    CHANGED = "changed"
    WAITING = "waiting"
    UNREADABLE = "unreadable"
    UNPROBEABLE = "unprobeable"
    STABLE = "stable"


@dataclass(frozen=True)
class FileObservation:
    """The state persisted between reconciliation passes."""

    size: int
    mtime_ns: int
    stable_since: datetime


@dataclass(frozen=True)
class StabilityAssessment:
    state: StabilityState
    observation: FileObservation | None
    message: str = ""


MediaProbe = Callable[[Path], bool]


class FileStabilityChecker:
    """Hide multi-signal stability detection behind one inexpensive interface.

    It never moves, hashes, or opens a recording for more than a one-byte read;
    archive import happens only after this module reports ``STABLE``.
    """

    def __init__(self, stability_seconds: int, probe: MediaProbe | None = None) -> None:
        if stability_seconds < 1:
            raise ValueError("Stability wait must be positive.")
        self._stability_seconds = stability_seconds
        self._probe = probe or ffprobe_readable

    def for_window(self, stability_seconds: int) -> FileStabilityChecker:
        """Reuse the same probe adapter with a newly validated user setting."""
        return FileStabilityChecker(stability_seconds, self._probe)

    def assess(
        self,
        path: Path,
        previous: FileObservation | None,
        now: datetime,
    ) -> StabilityAssessment:
        """Assess a candidate using unchanged size, mtime, readability, and FFprobe."""
        if not is_supported_media(path):
            return StabilityAssessment(StabilityState.INELIGIBLE, previous, "Unsupported or temporary file.")
        try:
            stat = path.stat()
        except FileNotFoundError:
            return StabilityAssessment(StabilityState.MISSING, previous, "The source file disappeared.")
        except OSError as error:
            return StabilityAssessment(StabilityState.UNREADABLE, previous, str(error))

        if stat.st_size <= 0:
            return StabilityAssessment(StabilityState.INELIGIBLE, previous, "Zero-byte files are ignored.")
        changed = (
            previous is None
            or stat.st_size != previous.size
            or stat.st_mtime_ns != previous.mtime_ns
        )
        if changed:
            observation = FileObservation(stat.st_size, stat.st_mtime_ns, now)
            return StabilityAssessment(StabilityState.CHANGED, observation, "The file is still changing.")

        elapsed = (now - previous.stable_since).total_seconds()
        if elapsed < self._stability_seconds:
            return StabilityAssessment(
                StabilityState.WAITING,
                previous,
                "Waiting for the configured stability window.",
            )
        if not _can_open_for_read(path):
            return StabilityAssessment(
                StabilityState.UNREADABLE,
                previous,
                "The recording cannot be opened for reading yet.",
            )
        if not self._probe(path):
            return StabilityAssessment(
                StabilityState.UNPROBEABLE,
                previous,
                "FFprobe cannot inspect the recording yet.",
            )
        return StabilityAssessment(StabilityState.STABLE, previous, "The recording is stable.")


def is_supported_media(path: Path) -> bool:
    """Return whether a path belongs to the explicit media contract."""
    name = path.name
    if not name or name.startswith(".") or name.startswith("~"):
        return False
    if name.lower().endswith((".part", ".partial", ".tmp", ".temp")):
        return False
    return path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS


def ffprobe_readable(path: Path) -> bool:
    """Ask FFprobe for basic format data without decoding or modifying media."""
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
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _can_open_for_read(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True
