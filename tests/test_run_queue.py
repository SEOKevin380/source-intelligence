from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from newswire_workbench.run_queue import LeaseLost, QueueConflict, RunJobRepository


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 23, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def _submit(repo, key="request-1"):
    return repo.submit(
        idempotency_key=key,
        project_id="project-1",
        source_hash="source-1",
        workflow_version="workflow-v1",
    )


def test_submit_is_idempotent(tmp_path):
    repo = RunJobRepository(tmp_path / "queue.db")
    first, created = _submit(repo)
    repeated, repeated_created = _submit(repo)
    assert created is True
    assert repeated_created is False
    assert repeated.id == first.id


def test_idempotency_key_cannot_change_identity(tmp_path):
    repo = RunJobRepository(tmp_path / "queue.db")
    _submit(repo)
    with pytest.raises(QueueConflict):
        repo.submit(
            idempotency_key="request-1",
            project_id="different",
            source_hash="source-1",
            workflow_version="workflow-v1",
        )


def test_concurrent_submissions_converge_on_one_active_job(tmp_path):
    path = tmp_path / "queue.db"

    def submit(number):
        repo = RunJobRepository(path)
        job, _ = repo.submit(
            idempotency_key=f"tab-{number}",
            project_id="project-1",
            source_hash="source-1",
            workflow_version="workflow-v1",
        )
        return job.id

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(submit, range(24)))
    assert len(set(ids)) == 1


def test_only_one_worker_claims_pending_job(tmp_path):
    path = tmp_path / "queue.db"
    _submit(RunJobRepository(path))

    def claim(_):
        job = RunJobRepository(path).claim_next()
        return job.id if job else None

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    assert len([claim for claim in claims if claim]) == 1


def test_expired_lease_is_reclaimed_and_old_worker_is_fenced(tmp_path):
    clock = Clock()
    repo = RunJobRepository(tmp_path / "queue.db", clock=clock)
    _submit(repo)
    old = repo.claim_next(lease_seconds=10)
    clock.advance(11)
    new = repo.claim_next(lease_seconds=10)
    assert new.id == old.id
    assert new.lease_token != old.lease_token
    assert new.attempt == 2
    with pytest.raises(LeaseLost):
        repo.heartbeat(old.id, old.lease_token)
    with pytest.raises(LeaseLost):
        repo.finish(
            old.id,
            old.lease_token,
            status="completed",
            terminal_code="done",
        )


def test_heartbeat_updates_progress_and_extends_lease(tmp_path):
    clock = Clock()
    repo = RunJobRepository(tmp_path / "queue.db", clock=clock)
    _submit(repo)
    job = repo.claim_next(lease_seconds=10)
    original_expiry = job.lease_expires_at
    clock.advance(5)
    updated = repo.heartbeat(
        job.id,
        job.lease_token,
        lease_seconds=20,
        stage="compliance",
        current=2,
        total=4,
        message="Reviewing exact hash",
    )
    assert updated.lease_expires_at > original_expiry
    assert (updated.stage, updated.progress_current, updated.progress_total) == (
        "compliance",
        2,
        4,
    )


def test_pending_cancel_is_terminal_and_unclaimable(tmp_path):
    repo = RunJobRepository(tmp_path / "queue.db")
    job, _ = _submit(repo)
    cancelled = repo.request_cancel(job.id)
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is True
    assert repo.claim_next() is None


def test_running_cancel_is_cooperative_and_lease_holder_finishes(tmp_path):
    repo = RunJobRepository(tmp_path / "queue.db")
    _submit(repo)
    job = repo.claim_next()
    requested = repo.request_cancel(job.id)
    assert requested.status == "running"
    assert repo.cancellation_requested(job.id, job.lease_token) is True
    finished = repo.finish(
        job.id,
        job.lease_token,
        status="cancelled",
        terminal_code="cancelled_by_operator",
    )
    assert finished.status == "cancelled"


def test_finish_persists_structured_terminal_state(tmp_path):
    repo = RunJobRepository(tmp_path / "queue.db")
    _submit(repo)
    job = repo.claim_next()
    finished = repo.finish(
        job.id,
        job.lease_token,
        status="failed",
        terminal_code="policy_drift",
        error={"family": "policy", "retryable": False},
        result={"paid_calls": 1},
    )
    assert finished.terminal_code == "policy_drift"
    assert finished.error["retryable"] is False
    assert finished.result["paid_calls"] == 1
    with pytest.raises(LeaseLost):
        repo.finish(
            job.id,
            job.lease_token,
            status="completed",
            terminal_code="done",
        )
