"""
Source Intelligence — Resumable Pipeline Engine
=================================================
State-machine pipeline replacing the monolithic research_product() orchestrator.

Each pipeline stage is a registered handler function that receives a Job and
returns a result dict. The pipeline:
- Executes stages in order
- Saves checkpoints after each stage
- Resumes from the current stage on restart
- Stops on failure
- Enforces budget limits (time-based)

Jobs are persisted in SQLite and can be inspected, resumed, or cancelled.
"""

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Callable, Dict, List, Any


_workflow_lock = threading.Lock()


class ReviewBlockError(Exception):
    """Raised by a stage handler to signal the job needs human review.

    The pipeline catches this and transitions the job to AWAITING_REVIEW
    instead of FAILED. The job can be resumed after human approval.

    Handlers may attach structured data via the ``details`` dict so the
    UI can act on it without parsing prose error messages.
    """

    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.details: dict = details or {}


class PipelineStage(Enum):
    """Ordered stages of the research pipeline."""
    IDENTIFY = "identify"          # Classify entity type, create Offering
    ACQUIRE = "acquire"            # Fetch official pages, store in evidence lake
    EXTRACT = "extract"            # Extract atomic claims from artifacts
    RECONCILE = "reconcile"        # Detect conflicts, resolve disagreements
    RESEARCH = "research"          # PubMed, safety, drug interactions
    COMPLY = "comply"              # Compliance rule evaluation
    ANALYZE_SITE = "analyze_site"  # Keyword research, site context
    ANALYZE_MARKET = "analyze_market"  # Competitive landscape, reputation
    PLAN = "plan"                  # Content planning, article structure
    REVIEW = "review"              # Human review checkpoint
    SOURCE_PACK = "source_pack"    # Generate final source document

    @classmethod
    def ordered(cls) -> List["PipelineStage"]:
        """Return stages in execution order."""
        return [
            cls.IDENTIFY, cls.ACQUIRE, cls.EXTRACT, cls.RECONCILE,
            cls.RESEARCH, cls.COMPLY, cls.ANALYZE_SITE, cls.ANALYZE_MARKET,
            cls.PLAN, cls.REVIEW, cls.SOURCE_PACK,
        ]

    @classmethod
    def quick_stages(cls) -> List["PipelineStage"]:
        """Stages to run in quick mode (skips market/site analysis).
        REVIEW is never skipped — compliance gate is mandatory."""
        return [
            cls.IDENTIFY, cls.ACQUIRE, cls.EXTRACT, cls.RECONCILE,
            cls.RESEARCH, cls.COMPLY, cls.PLAN, cls.REVIEW, cls.SOURCE_PACK,
        ]


class StageStatus(Enum):
    """Status of a pipeline stage."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class JobStatus(Enum):
    """Overall job status."""
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    AWAITING_REVIEW = "awaiting_review"  # Blocked until human approves


@dataclass
class Job:
    """A research pipeline job with full state tracking.

    Jobs are persisted in SQLite and can be resumed after interruption.
    """
    job_id: str = ""
    offering_id: str = ""
    url: str = ""
    product_name: str = ""
    status: JobStatus = JobStatus.CREATED
    current_stage: str = ""
    quick_mode: bool = False
    created_at: str = ""
    updated_at: str = ""
    completed_at: str = ""
    error: str = ""
    budget_seconds: int = 600      # 10 minute default budget
    elapsed_seconds: float = 0.0
    stage_data: dict = field(default_factory=dict)   # Per-stage results
    stage_status: dict = field(default_factory=dict)  # Per-stage status
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.job_id:
            ts = datetime.now(timezone.utc).isoformat()
            raw = f"{self.url}:{self.product_name}:{ts}"
            self.job_id = hashlib.sha256(raw.encode()).hexdigest()[:24]
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if not self.updated_at:
            self.updated_at = self.created_at

    @classmethod
    def create(cls, url: str = "", product_name: str = "",
               quick: bool = False, budget_seconds: int = 600,
               offering_id: str = "", **kwargs) -> "Job":
        """Create a new job."""
        return cls(
            url=url,
            product_name=product_name,
            quick_mode=quick,
            budget_seconds=budget_seconds,
            offering_id=offering_id,
            metadata=kwargs,
        )

    def get_stages(self) -> List[PipelineStage]:
        """Get the ordered list of stages for this job."""
        if self.quick_mode:
            return PipelineStage.quick_stages()
        return PipelineStage.ordered()

    def set_stage_status(self, stage: PipelineStage, status: StageStatus):
        """Update status for a specific stage."""
        self.stage_status[stage.value] = status.value
        self.current_stage = stage.value
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def set_stage_result(self, stage: PipelineStage, result: dict):
        """Store the result data from a completed stage."""
        self.stage_data[stage.value] = result

    def get_stage_result(self, stage: PipelineStage) -> dict:
        """Get stored result from a previously completed stage."""
        return self.stage_data.get(stage.value, {})

    def get_stage_status(self, stage: PipelineStage) -> StageStatus:
        """Get status of a specific stage."""
        raw = self.stage_status.get(stage.value, StageStatus.PENDING.value)
        return StageStatus(raw)

    def is_budget_exceeded(self) -> bool:
        """Check if job has exceeded its time budget."""
        return self.elapsed_seconds >= self.budget_seconds


class JobStore:
    """Persistence layer for jobs using the shared SQLite database.

    Uses the jobs and job_checkpoints tables created by database.py migration v3.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from config import DB_PATH
            db_path = DB_PATH
        self.db_path = db_path
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def save(self, job: Job):
        """Persist job state to database."""
        with _workflow_lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO jobs (
                    job_id, offering_id, url, product_name, status,
                    current_stage, quick_mode, created_at, updated_at,
                    completed_at, error, budget_seconds, elapsed_seconds,
                    stage_data_json, stage_status_json, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                job.job_id, job.offering_id, job.url, job.product_name,
                job.status.value, job.current_stage, int(job.quick_mode),
                job.created_at, job.updated_at, job.completed_at,
                job.error, job.budget_seconds, job.elapsed_seconds,
                json.dumps(job.stage_data), json.dumps(job.stage_status),
                json.dumps(job.metadata),
            ))
            self.conn.commit()

    def load(self, job_id: str) -> Optional[Job]:
        """Load a job from the database."""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_job(dict(row))

    def save_checkpoint(self, job: Job, stage: PipelineStage,
                        status: StageStatus, result: dict = None,
                        error: str = ""):
        """Save a stage checkpoint for audit trail."""
        now = datetime.now(timezone.utc).isoformat()
        with _workflow_lock:
            self.conn.execute("""
                INSERT INTO job_checkpoints (
                    job_id, stage, status, started_at, completed_at,
                    result_json, error
                ) VALUES (?,?,?,?,?,?,?)
            """, (
                job.job_id, stage.value, status.value,
                now, now if status in (StageStatus.COMPLETED, StageStatus.FAILED) else "",
                json.dumps(result or {}), error,
            ))
            self.conn.commit()

    def list_jobs(self, status: Optional[JobStatus] = None,
                  offering_id: Optional[str] = None,
                  limit: int = 50) -> List[Job]:
        """List jobs with optional filters."""
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if offering_id:
            query += " AND offering_id = ?"
            params.append(offering_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_job(dict(r)) for r in rows]

    def get_checkpoints(self, job_id: str) -> List[dict]:
        """Get all checkpoints for a job."""
        rows = self.conn.execute(
            "SELECT * FROM job_checkpoints WHERE job_id = ? ORDER BY id ASC",
            (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_job(d: dict) -> Job:
        """Convert a database row to a Job instance."""
        return Job(
            job_id=d["job_id"],
            offering_id=d.get("offering_id", ""),
            url=d.get("url", ""),
            product_name=d.get("product_name", ""),
            status=JobStatus(d.get("status", "created")),
            current_stage=d.get("current_stage", ""),
            quick_mode=bool(d.get("quick_mode", 0)),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            completed_at=d.get("completed_at", ""),
            error=d.get("error", ""),
            budget_seconds=d.get("budget_seconds", 600),
            elapsed_seconds=d.get("elapsed_seconds", 0.0),
            stage_data=json.loads(d.get("stage_data_json", "{}")),
            stage_status=json.loads(d.get("stage_status_json", "{}")),
            metadata=json.loads(d.get("metadata_json", "{}")),
        )


# Type alias for stage handler functions
StageHandler = Callable[[Job], dict]


class Pipeline:
    """Executes a sequence of stages with checkpointing and budget enforcement.

    Usage:
        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, handle_identify)
        pipeline.register(PipelineStage.ACQUIRE, handle_acquire)
        ...
        pipeline.run(job)
    """

    def __init__(self, store: JobStore, progress_callback: Callable = None):
        self._store = store
        self._handlers: Dict[PipelineStage, StageHandler] = {}
        self._progress = progress_callback

    def register(self, stage: PipelineStage, handler: StageHandler):
        """Register a handler function for a pipeline stage."""
        self._handlers[stage] = handler

    def _emit(self, message: str, level: str = "info"):
        """Emit a progress message."""
        if self._progress:
            self._progress(message, level)

    def run(self, job: Job) -> Job:
        """Execute the pipeline for a job.

        - Skips already-completed stages (enables resume)
        - Saves checkpoints after each stage
        - Stops on failure or budget exceeded
        - Returns the updated job
        """
        stages = job.get_stages()
        job.status = JobStatus.RUNNING
        self._store.save(job)

        start_time = time.time()
        # Preserve elapsed time from earlier runs exactly once.  The previous
        # implementation added the full current-run duration to the already
        # cumulative value before every stage, causing triangular/double
        # counting and premature budget pauses.
        elapsed_before_run = job.elapsed_seconds
        resumed = False

        for stage in stages:
            # Skip already-completed stages (resume support)
            current_status = job.get_stage_status(stage)
            if current_status == StageStatus.COMPLETED:
                if not resumed:
                    self._emit(f"  Skipping {stage.value} (already completed)")
                continue
            if current_status == StageStatus.SKIPPED:
                continue

            resumed = True

            # Check handler exists
            handler = self._handlers.get(stage)
            if not handler:
                self._emit(f"  Skipping {stage.value} (no handler registered)")
                job.set_stage_status(stage, StageStatus.SKIPPED)
                continue

            # Budget check
            job.elapsed_seconds = elapsed_before_run + (time.time() - start_time)
            if (job.is_budget_exceeded()
                    and not job.metadata.get("unattended", False)):
                self._emit(f"  Budget exceeded at {stage.value} "
                          f"({job.elapsed_seconds:.0f}s / {job.budget_seconds}s)")
                job.status = JobStatus.PAUSED
                job.error = f"Budget exceeded at stage {stage.value}"
                self._store.save(job)
                return job

            # Execute stage
            self._emit(f"\n  Stage: {stage.value.upper()}")
            job.set_stage_status(stage, StageStatus.RUNNING)
            self._store.save(job)

            stage_start = time.time()
            try:
                result = handler(job)
                stage_elapsed = time.time() - stage_start
                job.elapsed_seconds = elapsed_before_run + (time.time() - start_time)

                job.set_stage_status(stage, StageStatus.COMPLETED)
                job.set_stage_result(stage, result or {})
                self._store.save_checkpoint(
                    job, stage, StageStatus.COMPLETED,
                    result={"elapsed_ms": stage_elapsed * 1000, **(result or {})}
                )
                self._store.save(job)
                self._emit(f"  {stage.value} completed ({stage_elapsed:.1f}s)")

            except ReviewBlockError as e:
                # Production VA runs never open an editorial questionnaire.
                # Any ReviewBlockError that survives the unattended handlers is
                # a genuine source/system repair condition, not a human choice.
                if job.metadata.get("unattended", False):
                    stage_elapsed = time.time() - stage_start
                    job.elapsed_seconds = elapsed_before_run + (time.time() - start_time)
                    failure_result = {
                        "blocked": True,
                        "repair_required": True,
                        "reason": str(e),
                        **e.details,
                    }
                    job.set_stage_status(stage, StageStatus.FAILED)
                    job.set_stage_result(stage, failure_result)
                    job.status = JobStatus.FAILED
                    job.error = f"Source repair required: {e}"
                    self._store.save_checkpoint(
                        job, stage, StageStatus.FAILED,
                        result={**failure_result,
                                "elapsed_ms": stage_elapsed * 1000},
                    )
                    self._store.save(job)
                    self._emit(
                        f"  {stage.value} requires source/system repair: {e}",
                        "error",
                    )
                    return job
                # Human review required — pause, don't fail.
                # Stage stays PENDING so it re-runs after approval.
                stage_elapsed = time.time() - stage_start
                job.elapsed_seconds = elapsed_before_run + (time.time() - start_time)
                job.set_stage_status(stage, StageStatus.PENDING)
                block_result = {"blocked": True, "reason": str(e),
                                **e.details}
                job.set_stage_result(stage, block_result)
                job.status = JobStatus.AWAITING_REVIEW
                job.error = f"Review required: {e}"
                self._store.save_checkpoint(
                    job, stage, StageStatus.PENDING,
                    result={**block_result,
                            "elapsed_ms": stage_elapsed * 1000}
                )
                self._store.save(job)
                self._emit(f"  {stage.value} BLOCKED — awaiting human review: {e}", "warning")
                return job

            except Exception as e:
                stage_elapsed = time.time() - stage_start
                job.elapsed_seconds = elapsed_before_run + (time.time() - start_time)
                error_msg = f"{stage.value} failed: {e}"
                job.set_stage_status(stage, StageStatus.FAILED)
                job.status = JobStatus.FAILED
                job.error = error_msg
                self._store.save_checkpoint(
                    job, stage, StageStatus.FAILED,
                    error=str(e),
                    result={"elapsed_ms": stage_elapsed * 1000}
                )
                self._store.save(job)
                self._emit(f"  {stage.value} FAILED: {e}", "error")
                return job

        # All stages completed
        job.elapsed_seconds = elapsed_before_run + (time.time() - start_time)
        job.status = JobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc).isoformat()
        self._store.save(job)
        self._emit(f"\n  Pipeline completed ({job.elapsed_seconds:.1f}s total)")
        return job

    def cancel(self, job: Job) -> Job:
        """Cancel a running or paused job."""
        job.status = JobStatus.CANCELLED
        job.updated_at = datetime.now(timezone.utc).isoformat()
        self._store.save(job)
        return job

    def resume(self, job_id: str) -> Optional[Job]:
        """Resume a paused, failed, or review-approved job."""
        job = self._store.load(job_id)
        if not job:
            return None
        if job.status not in (JobStatus.PAUSED, JobStatus.FAILED,
                              JobStatus.AWAITING_REVIEW):
            return job  # Nothing to resume
        return self.run(job)

    def approve_review(self, job_id: str, reviewer: str = "human",
                       rule_resolutions: dict = None) -> Optional[Job]:
        """Approve a job that is awaiting review, then resume pipeline.

        Args:
            job_id: The job to approve.
            reviewer: Who approved it.
            rule_resolutions: Per-rule resolution decisions. Dict mapping
                rule_id → {"action": "accept"|"waive"|"substitute",
                           "note": "reviewer explanation",
                           "substitute_text": "replacement text (for substitute)"}
                If None, blanket-approves all findings.
        """
        job = self._store.load(job_id)
        if not job:
            return None
        if job.status != JobStatus.AWAITING_REVIEW:
            return job
        job.metadata["review_approved_by"] = reviewer
        job.metadata["review_approved_at"] = datetime.now(timezone.utc).isoformat()
        if rule_resolutions:
            existing = job.metadata.get("rule_resolutions", {})
            existing.update(rule_resolutions)
            job.metadata["rule_resolutions"] = existing
        else:
            # Blanket approval — mark all findings as accepted
            job.metadata["review_blanket_approved"] = True
        self._store.save(job)
        return self.run(job)
