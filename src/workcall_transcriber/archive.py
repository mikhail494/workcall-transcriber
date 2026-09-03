"""Safe recording import and deterministic per-call archive layout."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class InsufficientDiskSpace(RuntimeError):
    """Raised before a large processing operation can fail part way through."""


@dataclass(frozen=True)
class ImportedMedia:
    archive_dir: Path
    media_path: Path
    size: int
    sha256: str


AtomicMove = Callable[[str, str], None]


class ArchiveManager:
    """Own import semantics: Inbox files move; manual sources are copied.

    The interface never deletes media unless a verified cross-volume fallback has
    already made an identical archival copy of an Inbox source.
    """

    def __init__(self, archive_root: Path, atomic_move: AtomicMove = os.replace) -> None:
        self._archive_root = archive_root
        self._atomic_move = atomic_move

    def for_archive_root(self, archive_root: Path) -> ArchiveManager:
        """Create an equivalent importer for one validated runtime archive root."""
        return ArchiveManager(archive_root, atomic_move=self._atomic_move)

    def create_job_directory(self, job_id: str, source_name: str, created_at: datetime) -> Path:
        """Allocate a readable job folder without deriving paths from untrusted names."""
        day_directory = self._archive_root / created_at.strftime("%Y-%m-%d")
        day_directory.mkdir(parents=True, exist_ok=True)
        safe_stem = _safe_stem(source_name)
        short_id = re.sub(r"[^a-zA-Z0-9]", "", job_id)[:8] or "job"
        base_name = f"{created_at.strftime('%Y-%m-%d_%H%M%S')}_{safe_stem}_{short_id}"
        candidate = day_directory / base_name
        suffix = 2
        while candidate.exists():
            candidate = day_directory / f"{base_name}_{suffix}"
            suffix += 1
        candidate.mkdir()
        return candidate

    def planned_target(self, archive_dir: Path, source_name: str) -> Path:
        """Return the one collision-safe target that must be recorded before import."""
        return _target_path(archive_dir, source_name)

    def import_inbox(self, source: Path, archive_dir: Path) -> ImportedMedia:
        """Move an Inbox recording while retaining a verified fallback on move errors."""
        source = source.resolve(strict=True)
        target = _target_path(archive_dir, source.name)
        source_size = source.stat().st_size
        self.ensure_working_space(source_size)
        try:
            self._atomic_move(str(source), str(target))
        except OSError as move_error:
            # A move can fail if the user chooses a different archive volume. Copy
            # first, verify byte identity, and only then remove the Inbox original.
            if not source.exists() and target.exists():
                return _imported(archive_dir, target)
            source_digest = sha256_file(source)
            _copy_verified(
                source,
                target,
                source_digest,
                "Archive copy did not match the Inbox recording; source was left intact.",
                cause=move_error,
            )
            source.unlink()
        return _imported(archive_dir, target)

    def copy_manual(self, source: Path, archive_dir: Path) -> ImportedMedia:
        """Copy a manually selected file and leave its external source untouched."""
        source = source.resolve(strict=True)
        target = _target_path(archive_dir, source.name)
        self.ensure_working_space(source.stat().st_size)
        source_digest = sha256_file(source)
        _copy_verified(
            source,
            target,
            source_digest,
            "Archive copy did not match the manually selected recording; source was left intact.",
        )
        return _imported(archive_dir, target)

    def ensure_working_space(self, source_size: int) -> None:
        """Require conservative headroom for original media, FLAC, and output files."""
        self._archive_root.mkdir(parents=True, exist_ok=True)
        free_bytes = shutil.disk_usage(self._archive_root).free
        required = max(2 * 1024**3, source_size * 2)
        if free_bytes < required:
            raise InsufficientDiskSpace(
                "Not enough free disk space to safely import and transcribe this recording. "
                "Your source was not changed."
            )


def sha256_file(path: Path) -> str:
    """Stream a stable file once to calculate its durable identity."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(
    source: Path,
    target: Path,
    source_digest: str,
    mismatch_message: str,
    *,
    cause: OSError | None = None,
) -> None:
    """Copy into a visible evidence file, then atomically publish only a verified original."""
    staging = target.with_name(f".{target.name}.copying")
    if staging.exists():
        raise FileExistsError(f"An unfinished archive copy already exists: {staging.name}")
    shutil.copy2(source, staging)
    if sha256_file(staging) != source_digest:
        if cause is not None:
            raise OSError(mismatch_message) from cause
        raise OSError(mismatch_message)
    os.replace(staging, target)


def _imported(archive_dir: Path, target: Path) -> ImportedMedia:
    if not target.exists():
        raise OSError("The archive move reported success but the recording is not present.")
    return ImportedMedia(
        archive_dir=archive_dir,
        media_path=target,
        size=target.stat().st_size,
        sha256=sha256_file(target),
    )


def _target_path(archive_dir: Path, source_name: str) -> Path:
    archive_dir = archive_dir.resolve(strict=True)
    safe_name = _safe_filename(source_name)
    target = archive_dir / f"original_{safe_name}"
    if target.exists():
        raise FileExistsError(f"Archive target already exists: {target.name}")
    return target


def _safe_stem(source_name: str) -> str:
    safe = _safe_filename(source_name)
    stem = Path(safe).stem.strip(" .")
    return stem[:80] or "recording"


def _safe_filename(source_name: str) -> str:
    # Preserve Unicode call names but replace characters Windows cannot represent.
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(source_name).name)
    safe = safe.strip(" .")
    return safe[:160] or "recording"
