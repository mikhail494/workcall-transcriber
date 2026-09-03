"""Durable non-secret settings storage."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Settings
from .paths import RuntimePaths


class SettingsStore:
    """Hide JSON persistence behind a small load/save interface.

    The store never receives, writes, or logs credentials. Writes use replacement
    so an interrupted save cannot leave a partially written configuration file.
    """

    def __init__(self, paths: RuntimePaths) -> None:
        self._paths = paths

    def load(self) -> Settings:
        if not self._paths.settings_file.exists():
            return Settings()
        try:
            raw = json.loads(self._paths.settings_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return Settings()
            return Settings.from_mapping(raw)
        except (OSError, ValueError, TypeError):
            return Settings()

    def save(self, settings: Settings) -> None:
        errors = settings.validate(self._paths)
        if errors:
            raise ValueError(" ".join(errors))
        self._paths.ensure_exists()
        _atomic_json_write(self._paths.settings_file, settings.to_mapping())


def _atomic_json_write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}-",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
