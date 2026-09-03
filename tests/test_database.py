from pathlib import Path

import pytest

from workcall_transcriber.database import InvalidJobTransition, JobRepository
from workcall_transcriber.models import JobOrigin, JobStatus


def test_repository_claims_only_one_queued_job(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()
    first = repository.detect(Path("C:/Inbox/first.mkv"), JobOrigin.INBOX, 10, 100)
    second = repository.detect(Path("C:/Inbox/second.mkv"), JobOrigin.INBOX, 20, 200)
    repository.transition(first.id, JobStatus.QUEUED)
    repository.transition(second.id, JobStatus.QUEUED)

    claimed = repository.claim_next_queued()

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status is JobStatus.PROCESSING
    assert repository.claim_next_queued() is None


def test_repository_rejects_invalid_state_transition(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()
    job = repository.detect(Path("C:/Inbox/call.mkv"), JobOrigin.INBOX, 10, 100)

    with pytest.raises(InvalidJobTransition):
        repository.transition(job.id, JobStatus.COMPLETED)


def test_restart_recovers_active_processing_as_retryable(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()
    job = repository.detect(Path("C:/Inbox/call.mkv"), JobOrigin.INBOX, 10, 100)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.set_worker_pid(processing.id, 4242)

    recovered = repository.recover_after_restart()

    assert recovered == 1
    interrupted = repository.get(job.id)
    assert interrupted.status is JobStatus.INTERRUPTED
    assert interrupted.worker_pid is None
    assert repository.retry(job.id).status is JobStatus.QUEUED


def test_restart_does_not_clear_a_processing_pid_that_is_explicitly_safety_blocked(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()
    job = repository.detect(Path("C:/Inbox/call.mkv"), JobOrigin.INBOX, 10, 100)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.set_worker_pid(processing.id, 4242)

    recovered = repository.recover_after_restart(blocked_processing_job_ids={processing.id})

    assert recovered == 0
    retained = repository.get(processing.id)
    assert retained.status is JobStatus.PROCESSING
    assert retained.worker_pid == 4242


def test_terminal_transition_clears_the_worker_pid(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()
    job = repository.detect(Path("C:/Inbox/call.mkv"), JobOrigin.INBOX, 10, 100)
    repository.transition(job.id, JobStatus.QUEUED)
    processing = repository.claim_next_queued()
    assert processing is not None
    repository.set_worker_pid(processing.id, 4242)

    completed = repository.transition(processing.id, JobStatus.FAILED)

    assert completed.worker_pid is None


def test_completed_hash_prevents_duplicate_reprocessing(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()
    job = repository.detect(Path("C:/Inbox/call.mkv"), JobOrigin.INBOX, 10, 100)
    repository.transition(job.id, JobStatus.QUEUED)
    repository.claim_next_queued()
    repository.set_source_hash(job.id, "a" * 64)
    repository.transition(job.id, JobStatus.COMPLETED)

    duplicate = repository.find_completed_by_hash("a" * 64)

    assert duplicate is not None
    assert duplicate.id == job.id


def test_speaker_profiles_are_persisted_as_centroids_not_job_transcripts(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "workcalls.db")
    repository.initialize()

    created = repository.save_speaker_profile("Mikhail", [1.0, 0.0])
    updated = repository.save_speaker_profile("Mikhail", [0.0, 1.0])

    assert created.id == updated.id
    assert updated.reference_count == 2
    assert repository.list_speaker_profiles()[0].display_name == "Mikhail"
    repository.delete_speaker_profile(updated.id)
    assert repository.list_speaker_profiles() == []
