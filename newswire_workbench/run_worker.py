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

_AUTOMATIC_CORRECTED_TRANSACTION_LIMIT = 1


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


def _automatic_correction_generation(
    engine: WorkbenchEngine,
    project_id: str,
) -> int:
    generation = 0
    for event in engine.events(project_id):
        if event.get("event_type") != "automatic_corrected_transaction_started":
            continue
        payload = event.get("payload") or {}
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        generation = max(generation, int(payload.get("generation") or 0))
    return generation


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
            if result.get("stage") == "admin_review":
                action = engine.run_action(
                    job.project_id,
                    WORKBENCH_SOURCE_CONTEXT_VERSION,
                )
                if (
                    action.get("action")
                    == "apply_complete_exact_reviewer_patch"
                ):
                    if not engine.apply_complete_exact_reviewer_patch(
                        job.project_id
                    ):
                        raise RuntimeError(
                            "The complete exact reviewer patch could not be "
                            "applied atomically."
                        )
                    action = engine.run_action(
                        job.project_id,
                        WORKBENCH_SOURCE_CONTEXT_VERSION,
                    )
                if (
                    action.get("action")
                    == "resume_terminal_identity_signoff"
                ):
                    if engine.run_terminal_identity_recovery_signoff(
                        job.project_id
                    ):
                        result = engine.run_to_completion(
                            job.project_id,
                            self.master_instructions,
                            progress_callback=lambda message: self._progress(
                                job, str(message)
                            ),
                        )
                    else:
                        result = engine.get(job.project_id)
                    action = engine.run_action(
                        job.project_id,
                        WORKBENCH_SOURCE_CONTEXT_VERSION,
                    )
                if (
                    action.get("action")
                    == "handoff_corrected_final_candidate"
                ):
                    failed = engine.get(job.project_id)
                    replacement_id = (
                        engine.create_corrected_final_candidate_transaction(
                            job.project_id
                        )
                    )
                    replacement = engine.get(replacement_id)
                    replacement_job, _ = submit_project_run(
                        engine,
                        replacement_id,
                        idempotency_key=(
                            "corrected-final-candidate:" + job.id
                        ),
                    )
                    return self.repo.finish(
                        job.id,
                        job.lease_token,
                        status="completed",
                        terminal_code="corrected_final_candidate_queued",
                        result={
                            "project_id": replacement_id,
                            "replaces_project_id": job.project_id,
                            "replacement_queue_job_id": replacement_job.id,
                            "stage": replacement["stage"],
                            "paid_calls": engine.usage_summary(
                                job.project_id
                            )["calls"],
                            "replacement_paid_calls": 0,
                            "source_article_hash": failed["article_hash"],
                            "replacement_article_hash": replacement[
                                "article_hash"
                            ],
                        },
                    )
                generation = _automatic_correction_generation(
                    engine, job.project_id
                )
                if (
                    action.get("action") in {
                        "rebuild_corrected_transaction",
                        "rebuild_obsolete_workflow",
                    }
                    and generation
                    < _AUTOMATIC_CORRECTED_TRANSACTION_LIMIT
                ):
                    from article_provenance import extract_sealed_pack

                    failed = engine.get(job.project_id)
                    pack = extract_sealed_pack(failed.get("source_text") or "")
                    if pack:
                        replacement_id = engine.create_project_from_pack(
                            pack,
                            failed["platform"],
                            vertical=failed["vertical"],
                            force_new=True,
                        )
                        if replacement_id == job.project_id:
                            raise RuntimeError(
                                "Automatic corrected transaction reused the "
                                "failed project instead of creating a new owner."
                            )
                        replacement = engine.get(replacement_id)
                        next_generation = generation + 1
                        engine._event(
                            job.project_id,
                            "automatic_corrected_transaction_queued",
                            failed["stage"],
                            failed["article_hash"],
                            {
                                "replacement_project_id": replacement_id,
                                "generation": next_generation,
                                "queue_job_id": job.id,
                                "paid_calls_added_to_failed_project": 0,
                                "operator_click_required": False,
                                "trigger_action": action.get("action"),
                            },
                        )
                        if not any(
                            event.get("event_type")
                            == "automatic_corrected_transaction_started"
                            for event in engine.events(replacement_id)
                        ):
                            engine._event(
                                replacement_id,
                                "automatic_corrected_transaction_started",
                                replacement["stage"],
                                replacement["article_hash"],
                                {
                                    "replaces_project_id": job.project_id,
                                    "generation": next_generation,
                                    "queue_job_id": job.id,
                                    "operator_click_required": False,
                                    "prior_terminal_action": action.get(
                                        "action"
                                    ),
                                },
                            )
                        replacement_job, _ = submit_project_run(
                            engine,
                            replacement_id,
                            idempotency_key=(
                                "automatic-corrected:"
                                f"{job.id}:{next_generation}"
                            ),
                        )
                        return self.repo.finish(
                            job.id,
                            job.lease_token,
                            status="completed",
                            terminal_code=(
                                "automatic_corrected_transaction_queued"
                            ),
                            result={
                                "project_id": replacement_id,
                                "replaces_project_id": job.project_id,
                                "replacement_queue_job_id": (
                                    replacement_job.id
                                ),
                                "stage": replacement["stage"],
                                "paid_calls": engine.usage_summary(
                                    job.project_id
                                )["calls"],
                                "replacement_paid_calls": 0,
                                "automatic_correction_generation": (
                                    next_generation
                                ),
                            },
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
