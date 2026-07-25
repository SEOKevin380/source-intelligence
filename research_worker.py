"""Durable worker ownership for Source Intelligence research pipelines."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
import traceback
import uuid

from database import persist_completed_pack
from newswire_workbench.run_queue import LeaseLost, RunJob, RunJobRepository
from workflow import Job, JobStatus, JobStore, PipelineStage, StageStatus


RESEARCH_WORKFLOW_VERSION = "research-pipeline-durable-20260725-v1"


def research_queue_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "research-run-queue.db"


def _job_source_hash(job: Job) -> str:
    identity = {
        "url": job.url,
        "product_name": job.product_name,
        "quick_mode": job.quick_mode,
        "offering_id": job.offering_id,
        "metadata": job.metadata,
    }
    return hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def submit_research_job(
    job: Job,
    *,
    db_path: str | Path,
    idempotency_key: str = "",
) -> tuple[RunJob, bool]:
    """Persist one pipeline job before making it visible to a worker."""
    store = JobStore(str(db_path))
    store.save(job)
    repo = RunJobRepository(research_queue_path(db_path))
    return repo.submit(
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        project_id=job.job_id,
        source_hash=_job_source_hash(job),
        workflow_version=RESEARCH_WORKFLOW_VERSION,
        desired_action="research_pipeline",
    )


class ResearchQueueWorker:
    """Lease-fenced executor for normal and update research jobs."""

    def __init__(self, db_path: str | Path, *, lease_seconds: int = 180):
        self.db_path = str(Path(db_path).resolve())
        self.store = JobStore(self.db_path)
        self.repo = RunJobRepository(research_queue_path(self.db_path))
        self.lease_seconds = lease_seconds

    def _progress(self, queue_job: RunJob, message: str) -> None:
        job = self.store.load(queue_job.project_id)
        stages = job.get_stages() if job else PipelineStage.ordered()
        completed = (
            sum(
                1
                for stage in stages
                if job.get_stage_status(stage)
                in {StageStatus.COMPLETED, StageStatus.SKIPPED}
            )
            if job
            else 0
        )
        self.repo.heartbeat(
            queue_job.id,
            queue_job.lease_token,
            lease_seconds=self.lease_seconds,
            stage=(job.current_stage if job else "research"),
            current=completed,
            total=max(len(stages), 1),
            message=str(message or "Running durable research pipeline"),
        )

    def _pipeline(self, job: Job, callback):
        if job.metadata.get("is_update"):
            from stage_handlers import create_update_pipeline

            existing = job.metadata.get("existing_data") or {}
            if not existing:
                raise RuntimeError(
                    "Durable update job is missing its sealed existing pack."
                )
            return create_update_pipeline(
                existing,
                progress_callback=callback,
                db_path=self.db_path,
            )
        from stage_handlers import create_default_pipeline

        return create_default_pipeline(
            progress_callback=callback,
            db_path=self.db_path,
        )

    def run_once(self) -> RunJob | None:
        queue_job = self.repo.claim_next(lease_seconds=self.lease_seconds)
        if not queue_job:
            return None
        heartbeat_stop = threading.Event()

        def keep_lease_alive() -> None:
            interval = max(1.0, min(30.0, self.lease_seconds / 3))
            while not heartbeat_stop.wait(interval):
                try:
                    self.repo.heartbeat(
                        queue_job.id,
                        queue_job.lease_token,
                        lease_seconds=self.lease_seconds,
                    )
                except LeaseLost:
                    return
                except Exception:
                    # Keep retrying transient queue-store failures. If the
                    # lease truly expires, the next heartbeat is fenced.
                    continue

        heartbeat_thread = threading.Thread(
            target=keep_lease_alive,
            name=f"research-lease-{queue_job.id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        job = self.store.load(queue_job.project_id)
        try:
            if not job:
                return self.repo.finish(
                    queue_job.id,
                    queue_job.lease_token,
                    status="failed",
                    terminal_code="missing_research_job",
                    error={
                        "message": "The durable research job row is missing."
                    },
                )
            if queue_job.attempt > 1:
                # A dead worker may leave only the current stage marked
                # running. Completed checkpoints stay immutable; the
                # interrupted stage is explicitly rerun from its boundary.
                try:
                    current = PipelineStage(job.current_stage)
                except ValueError:
                    current = None
                if (
                    current is not None
                    and job.get_stage_status(current) == StageStatus.RUNNING
                ):
                    job.set_stage_status(current, StageStatus.PENDING)
                job.metadata["durable_reclaim_attempt"] = queue_job.attempt
                self.store.save(job)

            self._progress(queue_job, "Starting durable research pipeline")
            pipeline = self._pipeline(
                job,
                lambda message, level="info": self._progress(
                    queue_job, str(message)
                ),
            )
            completed = pipeline.run(job)
            result = {
                "job_id": completed.job_id,
                "status": completed.status.value,
                "stage": completed.current_stage,
                "error": completed.error,
            }
            if completed.status == JobStatus.COMPLETED:
                source_pack = completed.get_stage_result(
                    PipelineStage.SOURCE_PACK
                )
                full_data = source_pack.get("full_data") or {}
                product_key = persist_completed_pack(
                    full_data,
                    str(
                        completed.metadata.get("preferred_product_key") or ""
                    ),
                    db_path=self.db_path,
                )
                result.update({
                    "product_key": product_key,
                    "readiness": (
                        full_data.get("source_pack_contract") or {}
                    ).get("readiness", ""),
                })
                status = "completed"
                terminal_code = "research_complete"
                error = {}
            elif completed.status == JobStatus.AWAITING_REVIEW:
                # This is an expected, successful handoff—not a system
                # failure. The browser can reconstruct the review screen from
                # the persisted job without owning or replaying pipeline work.
                status = "completed"
                terminal_code = "awaiting_review"
                error = {}
            elif completed.status == JobStatus.CANCELLED:
                status = "cancelled"
                terminal_code = "cancelled_by_operator"
                error = {}
            else:
                status = "failed"
                terminal_code = (
                    "source_repair_required"
                    if completed.status == JobStatus.FAILED
                    else (
                        "research_budget_paused"
                        if completed.status == JobStatus.PAUSED
                        else "research_incomplete"
                    )
                )
                error = {
                    "message": completed.error
                    or "Research stopped before source-pack completion."
                }
            return self.repo.finish(
                queue_job.id,
                queue_job.lease_token,
                status=status,
                terminal_code=terminal_code,
                result=result,
                error=error,
            )
        except LeaseLost:
            raise
        except Exception as exc:
            job = self.store.load(queue_job.project_id) or job
            job.status = JobStatus.FAILED
            job.error = f"Durable worker failed: {exc}"
            self.store.save(job)
            return self.repo.finish(
                queue_job.id,
                queue_job.lease_token,
                status="failed",
                terminal_code="research_worker_exception",
                result={
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "stage": job.current_stage,
                },
                error={
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "traceback": traceback.format_exc()[-6000:],
                },
            )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)

    def run_forever(self, poll_seconds: float = 0.5) -> None:
        while True:
            handled = self.run_once()
            if handled is None:
                time.sleep(poll_seconds)
