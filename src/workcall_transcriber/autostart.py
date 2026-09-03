"""Normal per-user Windows autostart integration."""

from __future__ import annotations

import os
from typing import Protocol


class RunRegistry(Protocol):
    def set_value(self, name: str, value: str) -> None: ...

    def remove_value(self, name: str) -> None: ...

    def get_value(self, name: str) -> str | None: ...


class AutostartManager:
    """Manage one HKCU Run entry; no service or administrator rights required."""

    def __init__(self, launch_command: str, registry: RunRegistry | None = None) -> None:
        self._launch_command = launch_command
        self._registry = registry or _WindowsRunRegistry()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._registry.set_value("WorkCallTranscriber", self._launch_command)
        else:
            self._registry.remove_value("WorkCallTranscriber")

    def is_enabled(self) -> bool:
        return self._registry.get_value("WorkCallTranscriber") is not None


class _WindowsRunRegistry:
    _RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows autostart is unavailable on this platform.")
        import winreg

        self._winreg = winreg

    def set_value(self, name: str, value: str) -> None:
        with self._winreg.CreateKey(self._winreg.HKEY_CURRENT_USER, self._RUN_KEY) as key:
            self._winreg.SetValueEx(key, name, 0, self._winreg.REG_SZ, value)

    def remove_value(self, name: str) -> None:
        try:
            with self._winreg.OpenKey(
                self._winreg.HKEY_CURRENT_USER,
                self._RUN_KEY,
                0,
                self._winreg.KEY_SET_VALUE,
            ) as key:
                self._winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return

    def get_value(self, name: str) -> str | None:
        try:
            with self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, self._RUN_KEY) as key:
                value, _ = self._winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None
