"""Compact, native-feeling PySide6 desktop interface."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QDateTime, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .autostart import AutostartManager
from .controller import AppSnapshot, WorkCallController
from .database import JobRepository
from .exports import write_transcript_outputs
from .models import RETRYABLE_JOB_STATUSES, JobRecord, JobStatus
from .paths import RuntimePaths
from .queue_service import DuplicateSourceError
from .runtime import RuntimeReadiness
from .security import CredentialStore
from .worker_protocol import WorkerEvent


class ControllerBridge(QObject):
    """Thread-safe Qt signals used by controller callbacks."""

    state_changed = Signal()
    worker_event = Signal(str, object)
    notification = Signal(str, str)
    runtime_ready = Signal(object)


def make_status_icon(colour: str) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(colour))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(6, 6, 52, 52, 16, 16)
    painter.setPen(QColor("#ffffff"))
    painter.setOpacity(0.9)
    for x, height in ((22, 14), (29, 25), (36, 18), (43, 31)):
        painter.drawLine(x, 32 - height // 2, x, 32 + height // 2)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    quit_requested = Signal()

    def __init__(
        self,
        *,
        controller: WorkCallController,
        paths: RuntimePaths,
        credentials: CredentialStore,
        repository: JobRepository,
        autostart: AutostartManager,
        launch_command: str,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._paths = paths
        self._credentials = credentials
        self._repository = repository
        self._autostart = autostart
        self._launch_command = launch_command
        self._quitting = False
        self._worker_stage = ""
        self._worker_progress: float | None = None
        self._readiness: RuntimeReadiness | None = None
        self.setWindowTitle("WorkCall Transcriber")
        self.setWindowIcon(make_status_icon("#3c82f6"))
        self.setMinimumSize(830, 610)
        self.resize(920, 690)
        self._build()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(1500)
        self.refresh()

    def _build(self) -> None:
        central = QWidget(self)
        central.setObjectName("central")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("WorkCall Transcriber")
        title.setObjectName("title")
        subtitle = QLabel("Local, safe transcription for recordings placed in your WorkCalls Inbox")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status_badge = QLabel()
        self.status_badge.setObjectName("statusBadge")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_badge.setMinimumWidth(236)
        header.addWidget(self.status_badge)
        layout.addLayout(header)

        current_card = QFrame()
        current_card.setObjectName("card")
        card_layout = QVBoxLayout(current_card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_title = QLabel("Current activity")
        card_title.setObjectName("cardTitle")
        self.current_label = QLabel("Automatic processing is disabled.")
        self.current_label.setObjectName("currentActivity")
        self.current_label.setWordWrap(True)
        self.progress = QProgressBarCompat()
        card_layout.addWidget(card_title)
        card_layout.addWidget(self.current_label)
        card_layout.addWidget(self.progress)
        layout.addWidget(current_card)

        actions = QHBoxLayout()
        self.toggle_button = QPushButton()
        self.toggle_button.setObjectName("primaryButton")
        self.toggle_button.clicked.connect(self.toggle_automatic)
        self.process_button = QPushButton("Process file…")
        self.process_button.clicked.connect(self.process_file)
        self.open_result_button = QPushButton("Open result")
        self.open_result_button.clicked.connect(self.open_last_result)
        self.retry_button = QPushButton("Retry")
        self.retry_button.clicked.connect(self.retry_last_failed)
        self.stop_button = QPushButton("Stop processing")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.clicked.connect(self.stop_processing)
        actions.addWidget(self.toggle_button)
        actions.addWidget(self.process_button)
        actions.addWidget(self.open_result_button)
        actions.addWidget(self.retry_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        layout.addLayout(actions)

        section = QHBoxLayout()
        recent_label = QLabel("Recent recordings")
        recent_label.setObjectName("sectionTitle")
        section.addWidget(recent_label)
        section.addStretch()
        archive_button = QPushButton("Open archive")
        archive_button.clicked.connect(self.open_archive)
        speakers_button = QPushButton("Speakers…")
        speakers_button.clicked.connect(self.open_speakers)
        log_button = QPushButton("Open log")
        log_button.clicked.connect(self.open_log)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self.open_settings)
        section.addWidget(archive_button)
        section.addWidget(speakers_button)
        section.addWidget(log_button)
        section.addWidget(settings_button)
        layout.addLayout(section)

        self.jobs = QTableWidget(0, 6)
        self.jobs.setObjectName("jobsTable")
        self.jobs.setHorizontalHeaderLabels(
            ["Date / time", "Recording", "Status", "Duration", "Language", "Speakers"]
        )
        self.jobs.verticalHeader().setVisible(False)
        self.jobs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.jobs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.jobs.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.jobs.setAlternatingRowColors(False)
        self.jobs.doubleClicked.connect(lambda _: self.open_selected_result())
        header_view = self.jobs.horizontalHeader()
        header_view.setStretchLastSection(True)
        header_view.setSectionResizeMode(1, header_view.ResizeMode.Stretch)
        layout.addWidget(self.jobs, 1)

        footer = QLabel("Only files in the fixed WorkCalls Inbox are watched automatically. Other recordings stay untouched.")
        footer.setObjectName("footer")
        layout.addWidget(footer)
        self.setStyleSheet(DARK_STYLE)

    def refresh(self) -> None:
        try:
            snapshot = self._controller.snapshot()
        except Exception:
            return
        self._update_header(snapshot)
        self._render_jobs(snapshot.recent_jobs)

    def handle_worker_event(self, _: str, event: WorkerEvent) -> None:
        if event.type in {"stage", "progress"}:
            self._worker_stage = event.message
            self._worker_progress = event.progress
        elif event.type in {"completed", "failed", "cancelled"}:
            self._worker_stage = event.message
            self._worker_progress = None
        self.refresh()

    def update_readiness(self, readiness: RuntimeReadiness) -> None:
        self._readiness = readiness

    def _update_header(self, snapshot: AppSnapshot) -> None:
        settings = snapshot.settings
        current = snapshot.current_job
        latest_failure = next(
            (job for job in snapshot.recent_jobs if job.status is JobStatus.FAILED), None
        )
        if current is not None:
            self.status_badge.setText("PROCESSING")
            self.status_badge.setProperty("state", "processing")
            detail = self._worker_stage or f"Processing {current.source_name}…"
            self.current_label.setText(detail)
            self.progress.set_real_progress(self._worker_progress)
        elif not settings.automatic_enabled:
            self.status_badge.setText("AUTOMATIC PROCESSING DISABLED")
            self.status_badge.setProperty("state", "disabled")
            self.current_label.setText("Automatic processing is disabled. Manual processing is still available.")
            self.progress.set_idle()
        elif latest_failure is not None:
            self.status_badge.setText("ATTENTION REQUIRED")
            self.status_badge.setProperty("state", "failed")
            self.current_label.setText(latest_failure.error_message or "The last transcription needs attention.")
            self.progress.set_idle()
        else:
            waiting = sum(
                job.status in {JobStatus.WAITING_FOR_STABLE, JobStatus.QUEUED}
                for job in snapshot.recent_jobs
            )
            self.status_badge.setText("AUTOMATIC PROCESSING ENABLED")
            self.status_badge.setProperty("state", "enabled")
            self.current_label.setText(
                "Waiting for a WorkCalls recording."
                if not waiting
                else f"Waiting to process {waiting} recording{'s' if waiting != 1 else ''}."
            )
            self.progress.set_idle()
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)
        self.toggle_button.setText(
            "Disable automatic processing" if settings.automatic_enabled else "Enable automatic processing"
        )
        self.open_result_button.setEnabled(
            any(
                job.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}
                for job in snapshot.recent_jobs
            )
        )
        self.retry_button.setEnabled(any(job.status in RETRYABLE_JOB_STATUSES for job in snapshot.recent_jobs))
        self.stop_button.setEnabled(current is not None)

    def _render_jobs(self, jobs: tuple[JobRecord, ...]) -> None:
        selected = self.selected_job_id()
        self.jobs.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            values = [
                _format_datetime(job.detected_at),
                job.source_name,
                _status_text(job.status),
                _format_duration(job.duration_seconds),
                _language_text(job.detected_language, job.language_source),
                str(job.speaker_count) if job.speaker_count is not None else "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, job.id)
                if column == 2:
                    item.setForeground(QColor(_status_colour(job.status)))
                self.jobs.setItem(row, column, item)
            if job.id == selected:
                self.jobs.selectRow(row)

    def selected_job_id(self) -> str | None:
        selection = self.jobs.selectedItems()
        return str(selection[0].data(Qt.ItemDataRole.UserRole)) if selection else None

    def selected_job(self) -> JobRecord | None:
        job_id = self.selected_job_id()
        if not job_id:
            return None
        try:
            return self._repository.get(job_id)
        except KeyError:
            return None

    def toggle_automatic(self) -> None:
        try:
            current = self._controller.snapshot().settings.automatic_enabled
            self._controller.set_automatic_enabled(not current)
        except Exception as error:
            self.show_error("Could not update automatic processing", str(error))
        self.refresh()

    def process_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Process a recording",
            str(Path.home()),
            "Media files (*.mkv *.mp4 *.mov *.webm *.avi *.m4a *.mp3 *.wav *.flac)",
        )
        if not filename:
            return
        source = Path(filename)
        try:
            self._controller.process_manual_file(source)
        except DuplicateSourceError:
            choice = QMessageBox.question(
                self,
                "Already transcribed",
                "An identical completed recording already exists. Reprocess this copy anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if choice is QMessageBox.StandardButton.Yes:
                self._controller.process_manual_file(source, allow_duplicate=True)
        except Exception as error:
            self.show_error("Could not process file", str(error))
        self.refresh()

    def open_last_result(self) -> None:
        for job in self._controller.snapshot().recent_jobs:
            if job.status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS} and job.archive_dir:
                self.open_path(job.archive_dir)
                return

    def open_archive(self) -> None:
        settings = self._controller.snapshot().settings
        self.open_path(settings.effective_archive(self._paths))

    def open_selected_result(self) -> None:
        job = self.selected_job()
        if job and job.archive_dir:
            self.open_path(job.archive_dir)

    def retry_last_failed(self) -> None:
        job = next(
            (job for job in self._controller.snapshot().recent_jobs if job.status in RETRYABLE_JOB_STATUSES),
            None,
        )
        if job:
            self._controller.retry_job(job.id)
        self.refresh()

    def stop_processing(self) -> None:
        if not self._controller.stop_current_processing():
            self.show_error("Nothing to stop", "No transcription worker is currently running.")

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            controller=self._controller,
            paths=self._paths,
            credentials=self._credentials,
            autostart=self._autostart,
            launch_command=self._launch_command,
            readiness=self._readiness,
            parent=self,
        )
        dialog.exec()
        self.refresh()

    def open_speakers(self) -> None:
        job = self.selected_job()
        if job is None or job.status not in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}:
            self.show_error("Choose a completed recording", "Select a completed job to inspect its diarized speakers.")
            return
        if job.archive_dir is None:
            return
        dialog = SpeakerDialog(job.archive_dir, self._repository, self)
        dialog.exec()

    def open_log(self) -> None:
        job = self.selected_job()
        path = job.archive_dir / "processing.log" if job and job.archive_dir else self._paths.logs
        self.open_path(path)

    def open_path(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_error(self, title: str, message: str) -> None:
        QMessageBox.warning(self, title, message)

    def request_quit(self) -> None:
        self._quitting = True
        self.quit_requested.emit()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._quitting:
            event.accept()
        else:
            self.hide()
            event.ignore()


class QProgressBarCompat(QFrame):
    """Small indeterminate-or-real progress widget with no fake percentages."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("progressShell")
        self._label = QLabel("Idle")
        self._label.setObjectName("progressLabel")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)

    def set_real_progress(self, progress: float | None) -> None:
        if progress is None:
            self._label.setText("Working…")
        else:
            self._label.setText(f"Working — {progress:.0f}%")

    def set_idle(self) -> None:
        self._label.setText("Idle")


class SettingsDialog(QDialog):
    def __init__(
        self,
        *,
        controller: WorkCallController,
        paths: RuntimePaths,
        credentials: CredentialStore,
        autostart: AutostartManager,
        launch_command: str,
        readiness: RuntimeReadiness | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._paths = paths
        self._credentials = credentials
        self._autostart = autostart
        self._launch_command = launch_command
        self._readiness = readiness
        self.setWindowTitle("WorkCall Transcriber settings")
        self.setMinimumWidth(560)
        self._build()

    def _build(self) -> None:
        settings = self._controller.snapshot().settings
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.inbox = QLineEdit(str(settings.effective_inbox(self._paths)))
        self.inbox.setReadOnly(True)
        self.archive = QLineEdit(str(settings.effective_archive(self._paths)))
        inbox_row = self._fixed_inbox_row()
        archive_row = self._path_row(self.archive, "Choose Archive folder")
        self.autostart = QCheckBox("Start WorkCall Transcriber when I sign in to Windows")
        self.autostart.setChecked(settings.autostart_enabled)
        self.notifications = QCheckBox("Show Windows notifications")
        self.notifications.setChecked(settings.desktop_notifications)
        self.language = QComboBox()
        self.language.addItem("Auto", "auto")
        self.language.addItem("Russian", "ru")
        self.language.addItem("English", "en")
        self.language.setCurrentIndex(max(0, self.language.findData(settings.language)))
        self.speakers = QComboBox()
        self.speakers.addItem("Auto", None)
        for count in range(1, 13):
            self.speakers.addItem(str(count), count)
        self.speakers.setCurrentIndex(max(0, self.speakers.findData(settings.speaker_count)))
        form.addRow("Inbox", inbox_row)
        form.addRow("Archive", archive_row)
        form.addRow("Language", self.language)
        form.addRow("Speaker count", self.speakers)
        form.addRow("", self.autostart)
        form.addRow("", self.notifications)
        layout.addLayout(form)

        advanced = QGroupBox("Advanced")
        advanced_form = QFormLayout(advanced)
        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 32)
        self.batch_size.setValue(settings.batch_size)
        self.stability = QSpinBox()
        self.stability.setRange(5, 900)
        self.stability.setSuffix(" seconds")
        self.stability.setValue(settings.stability_seconds)
        self.keep_audio = QCheckBox("Keep canonical FLAC audio after success")
        self.keep_audio.setChecked(settings.keep_temporary_audio)
        self.match_threshold = QSpinBox()
        self.match_threshold.setRange(50, 100)
        self.match_threshold.setSuffix("%")
        self.match_threshold.setValue(round(settings.speaker_match_threshold * 100))
        advanced_form.addRow("Whisper batch size", self.batch_size)
        advanced_form.addRow("File stability wait", self.stability)
        advanced_form.addRow("Speaker match threshold", self.match_threshold)
        advanced_form.addRow("", self.keep_audio)
        layout.addWidget(advanced)

        credentials = QGroupBox("Diarization credentials")
        credentials_layout = QHBoxLayout(credentials)
        self.credential_status = QLabel()
        set_token = QPushButton("Replace token" if self._credentials.is_configured() else "Set token")
        remove_token = QPushButton("Remove token")
        set_token.clicked.connect(self.set_token)
        remove_token.clicked.connect(self.remove_token)
        credentials_layout.addWidget(self.credential_status)
        credentials_layout.addStretch()
        credentials_layout.addWidget(set_token)
        credentials_layout.addWidget(remove_token)
        layout.addWidget(credentials)
        self._refresh_credential_status()

        readiness = QGroupBox("Runtime readiness")
        readiness_form = QFormLayout(readiness)
        data = self._readiness
        readiness_form.addRow("WhisperX runtime", QLabel(_ready_text(data.whisper_runtime) if data else "Checking…"))
        readiness_form.addRow("CUDA", QLabel(_ready_text(data.cuda) if data else "Checking…"))
        readiness_form.addRow("FFmpeg", QLabel(_ready_text(data.ffmpeg) if data else "Checking…"))
        readiness_form.addRow("Diarization", QLabel(_ready_text(data.diarization_available) if data else "Checking…"))
        layout.addWidget(readiness)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setStyleSheet(DARK_STYLE)

    def _path_row(self, field: QLineEdit, caption: str) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self._choose_directory(field, caption))
        row.addWidget(field)
        row.addWidget(browse)
        return holder

    def _fixed_inbox_row(self) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        open_inbox = QPushButton("Open Inbox")
        open_inbox.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._paths.inbox))))
        row.addWidget(self.inbox)
        row.addWidget(open_inbox)
        return holder

    def _choose_directory(self, field: QLineEdit, caption: str) -> None:
        chosen = QFileDialog.getExistingDirectory(self, caption, field.text())
        if chosen:
            field.setText(chosen)

    def _refresh_credential_status(self) -> None:
        self.credential_status.setText(
            "Status: Configured" if self._credentials.is_configured() else "Status: Not configured"
        )

    def set_token(self) -> None:
        token, accepted = QInputDialog.getText(
            self,
            "Diarization token",
            "Hugging Face token:",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not token:
            return
        try:
            self._credentials.save_token(token)
        except Exception as error:
            QMessageBox.warning(self, "Could not save credential", str(error))
            return
        self._refresh_credential_status()

    def remove_token(self) -> None:
        if QMessageBox.question(
            self,
            "Remove token",
            "Remove the saved diarization token from Windows secure storage?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) is QMessageBox.StandardButton.Yes:
            self._credentials.remove_token()
            self._refresh_credential_status()

    def save(self) -> None:
        old = self._controller.snapshot().settings
        archive = Path(self.archive.text().strip())
        settings = replace(
            old,
            autostart_enabled=self.autostart.isChecked(),
            desktop_notifications=self.notifications.isChecked(),
            language=str(self.language.currentData()),
            speaker_count=self.speakers.currentData(),
            batch_size=self.batch_size.value(),
            stability_seconds=self.stability.value(),
            keep_temporary_audio=self.keep_audio.isChecked(),
            speaker_match_threshold=self.match_threshold.value() / 100,
            inbox_path=None,
            archive_path=None if archive == self._paths.archive else archive,
        )
        errors = settings.validate(self._paths)
        if errors:
            QMessageBox.warning(self, "Could not save settings", " ".join(errors))
            return
        autostart_attempted = False
        controller_attempted = False
        try:
            autostart_attempted = True
            self._autostart.set_enabled(settings.autostart_enabled)
            controller_attempted = True
            self._controller.update_settings(settings)
        except Exception as error:
            rollback_errors: list[str] = []
            if controller_attempted:
                try:
                    self._controller.update_settings(old)
                except Exception as rollback_error:
                    rollback_errors.append(f"settings rollback failed: {rollback_error}")
            if autostart_attempted:
                try:
                    self._autostart.set_enabled(old.autostart_enabled)
                except Exception as rollback_error:
                    rollback_errors.append(f"autostart rollback failed: {rollback_error}")
            detail = " ".join([str(error), *rollback_errors])
            QMessageBox.warning(self, "Could not save settings", detail)
            return
        self.accept()


class SpeakerDialog(QDialog):
    """Inspect a completed job's speakers and save/remove opted-in profiles."""

    def __init__(self, archive_dir: Path, repository: JobRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._archive_dir = archive_dir
        self._repository = repository
        self._transcript_path = archive_dir / "transcript.json"
        self._transcript: dict = {}
        self.setWindowTitle("Speakers")
        self.setMinimumWidth(560)
        self._build()
        self._load()

    def _build(self) -> None:
        layout = QGridLayout(self)
        layout.addWidget(QLabel("Speakers in this recording"), 0, 0)
        layout.addWidget(QLabel("Saved local profiles"), 0, 1)
        self.recording_speakers = QListWidget()
        self.saved_profiles = QListWidget()
        layout.addWidget(self.recording_speakers, 1, 0)
        layout.addWidget(self.saved_profiles, 1, 1)
        recording_actions = QHBoxLayout()
        rename = QPushButton("Rename in transcript…")
        remember = QPushButton("Remember voice…")
        rename.clicked.connect(self.rename_selected)
        remember.clicked.connect(self.remember_selected)
        recording_actions.addWidget(rename)
        recording_actions.addWidget(remember)
        layout.addLayout(recording_actions, 2, 0)
        remove = QPushButton("Remove selected profile")
        remove.clicked.connect(self.remove_selected_profile)
        layout.addWidget(remove, 2, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons, 3, 0, 1, 2)
        self.setStyleSheet(DARK_STYLE)

    def _load(self) -> None:
        try:
            self._transcript = json.loads(self._transcript_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._transcript = {}
        self.recording_speakers.clear()
        for speaker in self._transcript.get("speakers") or []:
            label = speaker.get("raw_label")
            if not label:
                continue
            name = speaker.get("identity")
            item = QListWidgetItem(f"{label}" + (f" — {name}" if name else ""))
            item.setData(Qt.ItemDataRole.UserRole, str(label))
            self.recording_speakers.addItem(item)
        self.saved_profiles.clear()
        for profile in self._repository.list_speaker_profiles():
            item = QListWidgetItem(f"{profile.display_name} ({profile.reference_count} reference(s))")
            item.setData(Qt.ItemDataRole.UserRole, profile.id)
            self.saved_profiles.addItem(item)

    def _selected_raw_label(self) -> str | None:
        item = self.recording_speakers.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def rename_selected(self) -> None:
        label = self._selected_raw_label()
        if not label:
            return
        name, accepted = QInputDialog.getText(self, "Rename speaker", f"Name for {label}:")
        if not accepted or not name.strip():
            return
        for segment in self._transcript.get("segments") or []:
            if segment.get("speaker") == label:
                segment["speaker_identity"] = name.strip()
                segment["speaker_identity_confidence"] = None
        for speaker in self._transcript.get("speakers") or []:
            if speaker.get("raw_label") == label:
                speaker["identity"] = name.strip()
                speaker["identity_confidence"] = None
        write_transcript_outputs(self._archive_dir, self._transcript)
        self._load()

    def remember_selected(self) -> None:
        label = self._selected_raw_label()
        if not label:
            return
        speaker = next(
            (item for item in self._transcript.get("speakers") or [] if item.get("raw_label") == label),
            None,
        )
        embedding = speaker.get("embedding") if speaker else None
        if not isinstance(embedding, list) or not embedding:
            QMessageBox.information(
                self,
                "Embedding unavailable",
                "This recording does not contain a speaker embedding to remember.",
            )
            return
        name, accepted = QInputDialog.getText(self, "Remember voice", f"Display name for {label}:")
        if not accepted or not name.strip():
            return
        try:
            self._repository.save_speaker_profile(name.strip(), [float(value) for value in embedding])
        except Exception as error:
            QMessageBox.warning(self, "Could not save profile", str(error))
            return
        self._load()

    def remove_selected_profile(self) -> None:
        item = self.saved_profiles.currentItem()
        if item:
            self._repository.delete_speaker_profile(str(item.data(Qt.ItemDataRole.UserRole)))
            self._load()


def _format_datetime(value: str) -> str:
    parsed = QDateTime.fromString(value, Qt.DateFormat.ISODate)
    return parsed.toLocalTime().toString("dd MMM yyyy  HH:mm") if parsed.isValid() else value


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, remainder = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{remainder:02d}" if hours else f"{minutes}:{remainder:02d}"


def _language_text(language: str | None, source: str | None) -> str:
    if not language:
        return "—"
    return language.upper() + (" (forced)" if source == "forced" else "")


def _status_text(status: JobStatus) -> str:
    return {
        JobStatus.WAITING_FOR_STABLE: "Waiting for stable file",
        JobStatus.IMPORTING: "Importing",
        JobStatus.QUEUED: "Queued",
        JobStatus.PROCESSING: "Processing",
        JobStatus.COMPLETED: "Completed",
        JobStatus.COMPLETED_WITH_WARNINGS: "Completed with warnings",
        JobStatus.FAILED: "Failed",
        JobStatus.CANCELLED: "Cancelled",
        JobStatus.INTERRUPTED: "Interrupted",
        JobStatus.DUPLICATE: "Already processed",
    }[status]


def _status_colour(status: JobStatus) -> str:
    if status in {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}:
        return "#70d6a0"
    if status is JobStatus.FAILED:
        return "#ff8e8e"
    if status in {JobStatus.PROCESSING, JobStatus.QUEUED}:
        return "#93b7ff"
    return "#b6c0d4"


def _ready_text(ready: bool) -> str:
    return "Ready" if ready else "Not ready"


DARK_STYLE = """
QMainWindow, QDialog, QWidget#central { background: #151922; color: #edf1f8; font-family: Segoe UI, Arial, sans-serif; }
QLabel#title { font-size: 24px; font-weight: 700; color: #f7f9fc; }
QLabel#subtitle, QLabel#footer { color: #9eaac0; }
QLabel#statusBadge { border-radius: 13px; padding: 9px 13px; font-size: 11px; font-weight: 700; }
QLabel#statusBadge[state="enabled"] { background: #173d2d; color: #87e3ad; }
QLabel#statusBadge[state="disabled"] { background: #303846; color: #c2c9d6; }
QLabel#statusBadge[state="processing"] { background: #17375e; color: #a9c9ff; }
QLabel#statusBadge[state="failed"] { background: #4b252b; color: #ffafb5; }
QFrame#card { background: #1d2330; border: 1px solid #2a3343; border-radius: 12px; }
QLabel#cardTitle, QLabel#sectionTitle { color: #aebbd0; font-size: 12px; font-weight: 700; text-transform: uppercase; }
QLabel#currentActivity { color: #f3f5f9; font-size: 16px; padding-top: 2px; }
QFrame#progressShell { background: transparent; min-height: 22px; }
QLabel#progressLabel { color: #8fa0bb; }
QPushButton { background: #273041; border: 1px solid #354155; border-radius: 7px; color: #e9eef7; padding: 7px 12px; }
QPushButton:hover { background: #323e53; }
QPushButton:disabled { color: #6c778b; background: #202633; border-color: #283040; }
QPushButton#primaryButton { background: #2b63ca; border-color: #4279dd; color: white; font-weight: 600; }
QPushButton#primaryButton:hover { background: #3671dd; }
QPushButton#dangerButton { color: #ffb1b8; }
QTableWidget { background: #1b202b; border: 1px solid #2a3343; border-radius: 9px; gridline-color: #293244; selection-background-color: #2a4771; selection-color: #ffffff; }
QHeaderView::section { background: #202735; color: #aebbd0; border: none; border-bottom: 1px solid #2c3647; padding: 8px; font-weight: 600; }
QTableWidget::item { padding: 7px; border: none; }
QLineEdit, QComboBox, QSpinBox, QListWidget { background: #1c2230; border: 1px solid #354155; border-radius: 6px; padding: 6px; color: #edf1f8; }
QGroupBox { border: 1px solid #354155; border-radius: 8px; margin-top: 10px; padding: 12px; color: #dbe5f5; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QCheckBox { spacing: 7px; }
QMessageBox { background: #151922; }
"""
