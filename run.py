"""Standalone launcher used by PyInstaller for the packaged desktop application."""

from workcall_transcriber.main import main

if __name__ == "__main__":
    raise SystemExit(main())
