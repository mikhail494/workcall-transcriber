# Architecture

## Scope and trust boundaries

The application is a thin local Qt interface over a durable queue. It owns only the project folder, its installed copy under `%LOCALAPPDATA%`, and `D:\WorkCalls`. Automatic intake is permanently scoped to `D:\WorkCalls\Inbox`; a validated Archive override must remain inside `D:\WorkCalls`. `D:\WhisperWork` is an external runtime dependency: the controller launches its existing Python executable but does not install packages, alter configuration, or delete files there.

```text
Qt main window + tray
          │ commands / snapshots
          ▼
WorkCallController ── Inbox watcher (enabled only) ──► QueueService
          │                                              │
          │                                  stability + ffprobe + SHA-256
          │                                              │
          ▼                                              ▼
WorkerRunner ◄─────────────────────────────── ArchiveManager + SQLite state
          │
          │ external process, JSONL stdout only
          ▼
D:\WhisperWork\.venv\Scripts\python.exe -m workcall_transcriber.worker
          │
          ├── FFmpeg audio extraction
          ├── WhisperX large-v3 CUDA float16 transcription
          ├── language-aware alignment
          ├── optional pyannote diarization
          └── JSON / TXT / SRT / VTT / TSV / manifest outputs
```

## Main modules

| Area | Responsibility |
| --- | --- |
| `main.py`, `ui/`, `tray.py` | Creates the Qt application, single-instance behavior, main window, notifications, and tray controls. |
| `controller.py` | Coordinates the watcher, reconciliation loop, job lifecycle, UI callbacks, and the one-worker schedule. |
| `queue_service.py`, `stability.py` | Accepts only the enabled fixed Inbox or an explicit manual source, waits for stable media, calls `ffprobe`, detects duplicates, and queues work. |
| `archive.py` | Creates deterministic per-job archive folders under the validated runtime archive; move-or-copy imports Inbox sources, while manual sources are copy-only. Source and copy SHA-256 values are compared for every non-atomic import. |
| `database.py`, `models.py`, `config.py` | Stores settings and validated job state in SQLite/JSON, including crash recovery and legal state transitions. |
| `worker_runner.py`, `worker_protocol.py` | Starts one external interpreter, passes a non-secret JSON spec through a temporary file, parses JSON-line events, and cleans the spec after use. |
| `worker.py`, `exports.py` | Runs the actual WhisperX pipeline and writes the application-owned transcript schema and output formats. |
| `security.py`, `speaker_profiles.py` | Encrypts the optional Hugging Face token with DPAPI, redacts it from diagnostics, and manages local speaker profiles. |

## Job lifecycle

`JobRepository` is the source of truth. A candidate progresses through:

```text
WaitingForStable → Importing → Queued → Processing
                                      ├→ Completed
                                      ├→ CompletedWithWarnings
                                      ├→ Failed / Cancelled
                                      └→ Duplicate
```

Only `claim_next_queued()` may claim a job for processing, which prevents multiple GPU workers even if the UI and watcher race. The controller persists the worker PID immediately after launch. On restart it verifies the recorded executable and exact Windows argument vector (`python -m workcall_transcriber.worker --spec <expected path>`) before attempting termination; terminal transitions clear the PID. If that PID was not yet persisted, startup enumerates Python processes and accepts only the same exact executable/argument identity for the expected spec, so it also closes the pre-mutex Python-startup gap. The worker holds a Windows named mutex for its full lifetime. If either process verification or mutex state is inconclusive, its processing row remains a durable queue block rather than allowing another GPU worker. Once absence or termination is confirmed and the mutex is gone, the job becomes retryable. A final, atomically written worker manifest restores a completed/failed DB state after a GUI crash, so completed hashes still prevent duplicates. The controller will not start a worker until the archive directory and archived media path are present.

Automatic intake has two gates: the setting must be enabled, and the source must be a direct child of the fixed WorkCalls Inbox. Manual selection bypasses the automatic-setting gate but never bypasses archive safety. Disabling automatic processing stops accepting new Inbox candidates immediately; it does not delete already archived work. A retry after an archive-import failure returns to the stability/import stage rather than starting a worker without an archived original.

## Storage and output contract

Every accepted source gets its own archive directory. Before an Inbox source moves, the intended archive directory and `original_<name>` target are committed to SQLite; startup can therefore complete a move that survived a crash between filesystem and DB operations. Copy fallbacks write to a same-directory `.copying` evidence file and atomically publish `original_<name>` only after SHA-256 verification, so a partial copy is never considered an archive original. The worker writes:

```text
transcript.json  canonical application schema with detected language metadata
transcript.txt   readable timestamped transcript
transcript.srt   subtitle format
transcript.vtt   WebVTT subtitle format
transcript.tsv   tabular segment export
manifest.json    job, pipeline, output, and warning metadata
processing.log   worker diagnostics with secret redaction
```

The canonical transcript schema is owned by this app rather than being an opaque WhisperX dump. It records detected language directly after transcription, so later alignment or diarization cannot overwrite that fact. `started_at` is passed from the durable claim record rather than synthesized at export time. JSON writes use a same-directory temporary file followed by an atomic replace.

## Worker boundary

The GUI process never imports WhisperX. `WorkerRunner` resolves `D:\WhisperWork\.venv\Scripts\python.exe`, sets a package path for the source or bundled worker library, and launches `-m workcall_transcriber.worker`.

The worker receives a JSON spec that contains paths and job configuration but no diarization token. The controller passes the DPAPI-decrypted token only through the child process environment. Worker stdout is a strict JSON-lines protocol for stage, progress, completion, and failure events; third-party WhisperX logging is redirected away from stdout so it cannot corrupt that protocol. The token is redacted in the archive log and never serialized into the manifest.

The processing order is CUDA validation, FFmpeg extraction to 16 kHz mono FLAC, WhisperX `large-v3` transcription on CUDA with `float16`, alignment using the detected language, optional diarization, then export. Failure and cancellation handlers preserve the archived source and write a clear retryable result.

## Packaging boundary

`run.py` is the top-level PyInstaller entry point; it imports the package entry point rather than executing a package module as a loose script. `scripts/build.ps1` bundles the Qt UI and copies `src/workcall_transcriber` as `worker_lib/workcall_transcriber` for the external interpreter. `scripts/install.ps1` copies that bundle to the per-user installation directory, creates the Start Menu shortcut and HKCU autostart value, and leaves runtime recordings alone.

PySide6 is intentionally pinned to 6.9.3 because the fully frozen target executable was smoke-tested with that version on the target Windows build. Any change to PySide6, PyInstaller, or the target Qt runtime must include a fresh packaged `--version` smoke test and a GUI launch check.
