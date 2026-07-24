"""Durable, fenced run queue primitives for the newswire workbench.

This module deliberately contains no worker thread and no provider logic.  A
deployment can have one or more workers polling ``claim_next``; SQLite
transactions and lease tokens ensure that only the current lease holder can
advance a job.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"pending", "running"})


class QueueConflict(RuntimeError):
    """The requested operation conflicts with durable queue state."""


class LeaseLost(QueueConflict):
    """The caller no longer owns the job lease."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class RunJob:
    id: str
    idempotency_key: str
    project_id: str
    source_hash: str
    workflow_version: str
    desired_action: str
    status: str
    stage: str
    progress_current: int
    progress_total: int
    progress_message: str
    lease_token: str
    lease_expires_at: str
    heartbeat_at: str
    attempt: int
    cancel_requested: bool
    terminal_code: str
    error: dict[str, Any]
    result: dict[str, Any]
    created_at: str
    started_at: str
    completed_at: str
    updated_at: str


class RunJobRepository:
    """SQLite repository for idempotent, lease-fenced workbench runs."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        # Multiple web processes may construct the repository at the same
        # instant during a deployment. SQLite does not consistently honor the
        # connection timeout while changing journal mode, so retry this one
        # idempotent schema transaction instead of failing app startup.
        for attempt in range(20):
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    workflow_version TEXT NOT NULL,
                    desired_action TEXT NOT NULL DEFAULT 'run_to_completion',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','completed','failed','cancelled')),
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 1,
                    progress_message TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                        CHECK(cancel_requested IN (0,1)),
                    terminal_code TEXT NOT NULL DEFAULT '',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_run_jobs_claim
                    ON run_jobs(status, lease_expires_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_run_jobs_project
                    ON run_jobs(project_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_jobs_one_active
                    ON run_jobs(project_id, source_hash, workflow_version, desired_action)
                    WHERE status IN ('pending','running');
                """
                    )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).casefold() or attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> RunJob | None:
        if row is None:
            return None
        values = dict(row)
        values["cancel_requested"] = bool(values["cancel_requested"])
        values["error"] = json.loads(values.pop("error_json") or "{}")
        values["result"] = json.loads(values.pop("result_json") or "{}")
        return RunJob(**values)

    def get(self, job_id: str) -> RunJob | None:
        with self._connect() as conn:
            return self._decode(
                conn.execute("SELECT * FROM run_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def submit(
        self,
        *,
        idempotency_key: str,
        project_id: str,
        source_hash: str,
        workflow_version: str,
        desired_action: str = "run_to_completion",
    ) -> tuple[RunJob, bool]:
        """Create a job or return the identical job for a repeated request."""
        required = {
            "idempotency_key": idempotency_key,
            "project_id": project_id,
            "source_hash": source_hash,
            "workflow_version": workflow_version,
            "desired_action": desired_action,
        }
        if any(not str(value).strip() for value in required.values()):
            raise ValueError("All run identity fields must be non-empty")

        now = _timestamp(self.clock())
        job_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM run_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                expected = (
                    project_id,
                    source_hash,
                    workflow_version,
                    desired_action,
                )
                actual = tuple(
                    existing[key]
                    for key in (
                        "project_id",
                        "source_hash",
                        "workflow_version",
                        "desired_action",
                    )
                )
                if actual != expected:
                    raise QueueConflict(
                        "Idempotency key was already used for a different run"
                    )
                return self._decode(existing), False

            active = conn.execute(
                """SELECT * FROM run_jobs
                   WHERE project_id=? AND source_hash=? AND workflow_version=?
                     AND desired_action=? AND status IN ('pending','running')""",
                (project_id, source_hash, workflow_version, desired_action),
            ).fetchone()
            if active:
                return self._decode(active), False

            conn.execute(
                """INSERT INTO run_jobs
                   (id,idempotency_key,project_id,source_hash,workflow_version,
                    desired_action,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    idempotency_key,
                    project_id,
                    source_hash,
                    workflow_version,
                    desired_action,
                    now,
                    now,
                ),
            )
            created = conn.execute(
                "SELECT * FROM run_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._decode(created), True

    def claim_next(self, *, lease_seconds: int = 120) -> RunJob | None:
        """Claim pending work or reclaim a job whose lease has expired."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_dt = self.clock()
        now = _timestamp(now_dt)
        expiry = _timestamp(now_dt + timedelta(seconds=lease_seconds))
        token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM run_jobs
                   WHERE cancel_requested=0
                     AND (status='pending'
                          OR (status='running' AND lease_expires_at < ?))
                   ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,
                            created_at
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                """UPDATE run_jobs
                   SET status='running', lease_token=?, lease_expires_at=?,
                       heartbeat_at=?, attempt=attempt+1,
                       started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,
                       updated_at=?
                   WHERE id=? AND cancel_requested=0
                     AND (status='pending'
                          OR (status='running' AND lease_expires_at < ?))""",
                (token, expiry, now, now, now, row["id"], now),
            )
            if changed.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM run_jobs WHERE id=?", (row["id"],)
            ).fetchone()
            return self._decode(claimed)

    def heartbeat(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 120,
        stage: str | None = None,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
    ) -> RunJob:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if current is not None and current < 0:
            raise ValueError("progress current cannot be negative")
        if total is not None and total < 1:
            raise ValueError("progress total must be positive")
        now_dt = self.clock()
        now = _timestamp(now_dt)
        expiry = _timestamp(now_dt + timedelta(seconds=lease_seconds))
        assignments = [
            "lease_expires_at=?",
            "heartbeat_at=?",
            "updated_at=?",
        ]
        params: list[Any] = [expiry, now, now]
        for column, value in (
            ("stage", stage),
            ("progress_current", current),
            ("progress_total", total),
            ("progress_message", message),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                params.append(value)
        params.extend([job_id, lease_token, now])
        with self._connect() as conn:
            changed = conn.execute(
                f"""UPDATE run_jobs SET {', '.join(assignments)}
                    WHERE id=? AND lease_token=? AND status='running'
                      AND lease_expires_at >= ?""",
                params,
            )
            if changed.rowcount != 1:
                raise LeaseLost("Job lease is missing, expired, or fenced")
        return self.get(job_id)

    def cancellation_requested(self, job_id: str, lease_token: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT cancel_requested FROM run_jobs
                   WHERE id=? AND lease_token=? AND status='running'""",
                (job_id, lease_token),
            ).fetchone()
        if not row:
            raise LeaseLost("Job lease is missing or fenced")
        return bool(row["cancel_requested"])

    def request_cancel(self, job_id: str) -> RunJob:
        now = _timestamp(self.clock())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM run_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["status"] == "pending":
                conn.execute(
                    """UPDATE run_jobs SET status='cancelled',
                       cancel_requested=1, terminal_code='cancelled_by_operator',
                       completed_at=?, updated_at=? WHERE id=?""",
                    (now, now, job_id),
                )
            elif row["status"] == "running":
                conn.execute(
                    """UPDATE run_jobs SET cancel_requested=1, updated_at=?
                       WHERE id=?""",
                    (now, job_id),
                )
        return self.get(job_id)

    def finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        status: str,
        terminal_code: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> RunJob:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal status: {status}")
        now = _timestamp(self.clock())
        with self._connect() as conn:
            changed = conn.execute(
                """UPDATE run_jobs
                   SET status=?, terminal_code=?, result_json=?, error_json=?,
                       lease_token='', lease_expires_at='', completed_at=?,
                       updated_at=?
                   WHERE id=? AND lease_token=? AND status='running'""",
                (
                    status,
                    terminal_code,
                    json.dumps(result or {}, sort_keys=True),
                    json.dumps(error or {}, sort_keys=True),
                    now,
                    now,
                    job_id,
                    lease_token,
                ),
            )
            if changed.rowcount != 1:
                raise LeaseLost("Job lease is missing or fenced")
        return self.get(job_id)

    def as_dict(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        return asdict(job) if job else None
