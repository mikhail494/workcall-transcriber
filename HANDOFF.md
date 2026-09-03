# Handoff

## Delivered locations

- Source repository: `D:\Codex work\Projects\workcall-transcriber`
- Runtime data: `D:\WorkCalls`
- Installed executable: `%LOCALAPPDATA%\WorkCall Transcriber\WorkCallTranscriber.exe`
- External WhisperX interpreter: `D:\WhisperWork\.venv\Scripts\python.exe`

The external WhisperX environment was inspected and reused only. No package installation, configuration edit, recording deletion, or cleanup was performed in `D:\WhisperWork`.

## Validation completed

- The project suite passed with 74 tests, including all watcher scenarios A–D in test mode, queue safety, fixed-Inbox enforcement, archive-path application, staged hash-verified copies, crash-safe import intent/final-manifest recovery, conservative SQLite/PID/mutex recovery, exact recorded and PID-less worker identity, worker protocol behavior, traceback-safe secret handling, output formats, PyInstaller entry-point regression coverage, and temporary worker-spec cleanup.
- Ruff lint passed for the repository.
- The production PyInstaller bundle was built successfully and its `--version` smoke test exited with code 0.
- The installed UI was opened successfully. Its accessibility tree showed the expected main window, tray-capable controls, automatic-processing-disabled first-run badge, manual processing action, archive/settings/log actions, and completed test jobs.
- A real CUDA WhisperX end-to-end run completed against an existing external FLAC source. The source remained present, a separate archived original was created, audio extraction/transcription/alignment ran, and JSON/TXT/SRT/VTT/TSV/manifest outputs were written.
- That real run completed with warnings only because no Hugging Face diarization credential was configured. This is the intended degraded-success behavior: the transcript is retained and the missing diarization requirement is explicit.

## Operating notes

- Automatic Intake is disabled by default. Configure OBS to write completed recordings directly into `D:\WorkCalls\Inbox` only when the operator is ready to enable it.
- The Inbox is intentionally fixed to `D:\WorkCalls\Inbox`; Settings can open it but cannot turn another OBS recording directory into an automatic source. A custom Archive setting is accepted only below `D:\WorkCalls` and is applied to new jobs.
- Manual file processing is safe for recordings outside Inbox: the app copies and hash-verifies the source before starting the worker.
- A worker PID is persisted before processing. On restart the app only terminates an orphan after its executable and exact arguments identify the WorkCall worker and matching job spec. If a crash occurred before that durable PID write, it enumerates Python processes and applies the same exact identity test to the expected spec; the named mutex remains a second fail-closed guard. If Windows cannot confirm either outcome, the durable processing row blocks new GPU work until it can. Final manifests restore completed work after a GUI crash only after JSON/TXT/SRT/VTT/TSV are all present, and terminal job states clear the PID.
- A valid Hugging Face token and accepted pyannote model terms are still required to exercise and use speaker diarization. The app stores that token with Windows DPAPI for the current user.
- Do not upgrade PySide6 casually. Version 6.9.3 was locally validated in the actual frozen executable after PySide6 6.10.3 failed to import `QtWidgets` from the frozen bundle on this Windows 10 machine.
- Screen-capture automation could not capture a screenshot of the installed Qt window because of a Windows accessibility-interface error (`0x80004002`). The accessibility tree itself was available and verified the running UI; this is a test-tool limitation, not an application error.

## Safe support procedure

1. For a failed job, open its archive folder and `processing.log`; do not delete `original_<name>`.
2. Resolve the underlying CUDA, FFmpeg, or Hugging Face access issue.
3. Use **Retry** in the app. The job state is durable and the archived source is reused.
4. If the app is reinstalled, use `scripts\uninstall.ps1`. It preserves both `D:\WorkCalls\Archive` and `D:\WorkCalls\Inbox` by design.

## Recommended next checks for an operator

- Save a Hugging Face token in Settings only if speaker labels are required, then process a short non-sensitive recording to confirm diarization access.
- Confirm the desired OBS profile writes into `D:\WorkCalls\Inbox` before enabling automatic processing.
- Keep the app auto-start entry unless processing should be entirely manual; it is stored in the current-user Windows Run key.
