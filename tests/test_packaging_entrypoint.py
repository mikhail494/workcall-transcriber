from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_standalone_packaging_launcher_imports_the_package() -> None:
    """A PyInstaller entrypoint is executed as a script, not as a package module."""
    launcher = PROJECT_ROOT / "run.py"

    completed = subprocess.run(
        [sys.executable, str(launcher), "--version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0.1.0"


def test_build_script_targets_the_standalone_packaging_launcher() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")

    assert "$entryPoint = Join-Path $projectRoot 'run.py'" in build_script


def test_pyside6_is_pinned_to_the_verified_windows_packaging_version() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "PySide6==6.9.3" in project["project"]["dependencies"]
