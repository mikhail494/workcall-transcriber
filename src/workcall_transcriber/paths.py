"""Runtime-data path conventions and safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    """Own the small, fixed layout used for private runtime data.

    The interface intentionally exposes only roots that callers may use.  It
    keeps source code, user recordings, and temporary processing data separate.
    """

    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "Inbox"

    @property
    def archive(self) -> Path:
        return self.root / "Archive"

    @property
    def temp(self) -> Path:
        return self.root / "Temp"

    @property
    def logs(self) -> Path:
        return self.root / "Logs"

    @property
    def state(self) -> Path:
        return self.root / "State"

    @property
    def database(self) -> Path:
        return self.state / "workcalls.db"

    @property
    def settings_file(self) -> Path:
        return self.state / "settings.json"

    @property
    def credentials_file(self) -> Path:
        return self.state / "credentials.dat"

    def ensure_exists(self) -> None:
        """Create the required folders without deleting or moving user data."""
        for directory in (self.root, self.inbox, self.archive, self.temp, self.logs, self.state):
            directory.mkdir(parents=True, exist_ok=True)

    def validate(self) -> list[str]:
        """Return human-readable configuration errors, never mutating the layout."""
        errors: list[str] = []
        root = self.root.resolve(strict=False)
        temp = self.temp.resolve(strict=False)
        archive = self.archive.resolve(strict=False)
        state = self.state.resolve(strict=False)

        if root == root.parent:
            errors.append("The WorkCalls data folder cannot be a filesystem root.")
        if _is_relative_to(archive, temp):
            errors.append("The archive folder cannot be inside the temporary folder.")
        if _is_relative_to(state, temp):
            errors.append("The state folder cannot be inside the temporary folder.")
        return errors


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
