from database import ProductDatabase
from research_worker import (
    ResearchQueueWorker,
    submit_research_job,
)
from workflow import Job, JobStatus, PipelineStage, StageStatus


class CompletingPipeline:
    def __init__(self, store):
        self.store = store

    def run(self, job):
        full_data = {
            "product": {
                "product_name": "Durable Product",
                "official_url": "https://example.com/product",
                "product_type": "device",
                "category": "device",
            },
            "all_artifacts": [{"artifact_id": "artifact-1"}],
            "source_manifest": [{
                "artifact_id": "artifact-1",
                "status": "captured",
            }],
            "publication_claims": {},
            "required_facts": {"missing": []},
        }
        job.set_stage_status(
            PipelineStage.SOURCE_PACK,
            # The worker only needs the persisted completed boundary.
            StageStatus.COMPLETED,
        )
        job.set_stage_result(PipelineStage.SOURCE_PACK, {
            "full_data": full_data,
            "doc_text": "Durable report",
        })
        job.status = JobStatus.COMPLETED
        job.current_stage = PipelineStage.SOURCE_PACK.value
        self.store.save(job)
        return job


class AwaitingReviewPipeline:
    def __init__(self, store):
        self.store = store

    def run(self, job):
        job.status = JobStatus.AWAITING_REVIEW
        job.current_stage = PipelineStage.REVIEW.value
        job.error = "Review required: one source conflict"
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "product_name": "Durable Product",
            "offering_type": "device",
        })
        self.store.save(job)
        return job


def test_research_worker_persists_completed_pack_without_browser(tmp_path):
    db_path = tmp_path / "source.db"
    db = ProductDatabase(str(db_path))
    db.conn.close()
    job = Job.create(
        url="https://example.com/product",
        product_name="Durable Product",
        unattended=True,
    )
    queued, created = submit_research_job(job, db_path=db_path)
    assert created is True

    worker = ResearchQueueWorker(db_path)
    worker._pipeline = lambda job, callback: CompletingPipeline(worker.store)
    finished = worker.run_once()

    assert finished.id == queued.id
    assert finished.status == "completed"
    assert finished.terminal_code == "research_complete"
    assert finished.result["product_key"] == "durable-product"
    stored = ProductDatabase(str(db_path)).get_product("durable-product")
    assert stored["research_data"]["product"]["product_name"] == (
        "Durable Product"
    )


def test_research_worker_preserves_human_review_as_typed_handoff(tmp_path):
    db_path = tmp_path / "source.db"
    db = ProductDatabase(str(db_path))
    db.conn.close()
    job = Job.create(
        url="https://example.com/product",
        product_name="Durable Product",
        unattended=True,
    )
    queued, _ = submit_research_job(job, db_path=db_path)

    worker = ResearchQueueWorker(db_path)
    worker._pipeline = lambda job, callback: AwaitingReviewPipeline(
        worker.store
    )
    finished = worker.run_once()

    assert finished.id == queued.id
    assert finished.status == "completed"
    assert finished.terminal_code == "awaiting_review"
    persisted = worker.store.load(job.job_id)
    assert persisted.status == JobStatus.AWAITING_REVIEW
    assert persisted.current_stage == PipelineStage.REVIEW.value


def test_research_submission_is_idempotent_while_active(tmp_path):
    db_path = tmp_path / "source.db"
    db = ProductDatabase(str(db_path))
    db.conn.close()
    job = Job.create(
        url="https://example.com/product",
        product_name="Durable Product",
        unattended=True,
    )
    first, first_created = submit_research_job(
        job, db_path=db_path, idempotency_key="same-submit"
    )
    second, second_created = submit_research_job(
        job, db_path=db_path, idempotency_key="same-submit"
    )
    assert first_created is True
    assert second_created is False
    assert first.id == second.id
