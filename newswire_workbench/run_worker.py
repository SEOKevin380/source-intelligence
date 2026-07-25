"""Durable worker that owns provider execution outside browser sessions."""

from __future__ import annotations

from pathlib import Path
import threading
import time
import traceback
from typing import Callable
import uuid

from .engine import (
    WORKBENCH_SOURCE_CONTEXT_VERSION,
    WorkbenchEngine,
)
from .run_queue import LeaseLost, RunJob, RunJobRepository


_STAGE_PROGRESS = {
    "source_ready": (0, 5, "Preparing source-grounded draft"),
    "drafted": (1, 5, "Running independent compliance review"),
    "compliance_reviewed": (2, 5, "Applying bounded compliance repair"),
    "revised": (3, 5, "Running exact-hash final sign-off"),
    "signed_off": (4, 5, "Building immutable submission package"),
    "package_ready": (5, 5, "Submission package complete"),
    "admin_review": (5, 5, "Stopped at a typed review boundary"),
}


def queue_path(root: str | Path) -> Path:
    return Path(root) / "run-queue.db"


def submit_project_run(
    engine: WorkbenchEngine,
    project_id: str,
    *,
    idempotency_key: str = "",
) -> tuple[RunJob, bool]:
    project = engine.get(project_id)
    repo = RunJobRepository(queue_path(engine.root))
    return repo.submit(
        idempotency_key=idempotency_key or uuid.uuid4().hex,
        project_id=project_id,
        source_hash=str(project.get("source_hash") or ""),
        workflow_version=WORKBENCH_SOURCE_CONTEXT_VERSION,
        desired_action="run_to_completion",
    )


class RunQueueWorker:
    """Lease-fenced worker for one shared workbench queue."""

    def __init__(
        self,
        root: str | Path,
        master_instructions: str,
        *,
        engine_factory: Callable[..., WorkbenchEngine] = WorkbenchEngine,
        lease_seconds: int = 180,
    ):
        self.root = Path(root)
        self.master_instructions = master_instructions
        self.engine_factory = engine_factory
        self.lease_seconds = lease_seconds
        self.repo = RunJobRepository(queue_path(self.root))

    def _progress(self, job: RunJob, message: str) -> None:
        engine = self.engine_factory(self.root)
        project = engine.get(job.project_id)
        current, total, fallback = _STAGE_PROGRESS.get(
            str(project.get("stage") or ""),
            (0, 5, "Running owned workflow stage"),
        )
        self.repo.heartbeat(
            job.id,
            job.lease_token,
            lease_seconds=self.lease_seconds,
            stage=str(project.get("stage") or "running"),
            current=current,
            total=total,
            message=str(message or fallback),
        )

    def run_once(self) -> RunJob | None:
        job = self.repo.claim_next(lease_seconds=self.lease_seconds)
        if not job:
            return None
        heartbeat_stop = threading.Event()

        def keep_lease_alive() -> None:
            interval = max(1.0, min(30.0, self.lease_seconds / 3))
            while not heartbeat_stop.wait(interval):
                try:
                    self.repo.heartbeat(
                        job.id,
                        job.lease_token,
                        lease_seconds=self.lease_seconds,
                    )
                except LeaseLost:
                    return
                except Exception:
                    # A transient SQLite lock must not permanently kill the
                    # lease keeper. The next interval retries; lease fencing
                    # still prevents a stale worker from committing.
                    continue

        heartbeat_thread = threading.Thread(
            target=keep_lease_alive,
            name=f"newswire-lease-{job.id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        engine = self.engine_factory(self.root)
        try:
            if not engine.prepare_queue_execution(
                job.project_id,
                queue_job_id=job.id,
                reclaim_attempt=job.attempt,
            ):
                project = engine.get(job.project_id)
                return self.repo.finish(
                    job.id,
                    job.lease_token,
                    status="completed",
                    terminal_code=str(project.get("stage") or "admin_review"),
                    result={
                        "project_id": job.project_id,
                        "stage": project.get("stage"),
                        "paid_calls": engine.usage_summary(
                            job.project_id
                        )["calls"],
                    },
                )
            self._progress(job, "Starting durable newswire workflow")
            result = engine.run_to_completion(
                job.project_id,
                self.master_instructions,
                progress_callback=lambda message: self._progress(
                    job, str(message)
                ),
            )
            delivery_result = {}
            if (
                result.get("stage") == "package_ready"
                and engine.capabilities().get("wordpress")
            ):
                try:
                    saved = engine.send_to_wordpress_draft(job.project_id)
                    delivery_result["wordpress_edit_url"] = str(
                        (saved or {}).get("edit_url") or ""
                    )
                except Exception as exc:
                    # Delivery is downstream of editorial approval. Preserve
                    # the approved package and expose transport failure for a
                    # safe zero-provider-call retry.
                    delivery_result["wordpress_delivery_error"] = str(exc)[
                        :2000
                    ]
            return self.repo.finish(
                job.id,
                job.lease_token,
                status="completed",
                terminal_code=str(result.get("stage") or "completed"),
                result={
                    "project_id": job.project_id,
                    "stage": result.get("stage"),
                    "article_hash": result.get("article_hash"),
                    "paid_calls": engine.usage_summary(
                        job.project_id
                    )["calls"],
                    **delivery_result,
                },
            )
        except LeaseLost:
            raise
        except Exception as exc:
            engine.quarantine_queue_failure(
                job.project_id,
                queue_job_id=job.id,
                error=exc,
            )
            return self.repo.finish(
                job.id,
                job.lease_token,
                status="failed",
                terminal_code="workflow_exception",
                error={
                    "type": type(exc).__name__,
                    "message": str(exc)[:2000],
                    "traceback": traceback.format_exc()[-6000:],
                },
                result={
                    "project_id": job.project_id,
                    "stage": engine.get(job.project_id).get("stage"),
                    "paid_calls": engine.usage_summary(
                        job.project_id
                    )["calls"],
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


def worker_health(root: str | Path) -> dict:
    repo = RunJobRepository(queue_path(root))
    return {
        "queue_path": str(repo.db_path),
        "status": "ready",
        "schema": "lease-fenced-v1",
    }
