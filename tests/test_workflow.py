"""Tests for workflow.py — Resumable pipeline engine."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workflow import (
    Job, JobStore, Pipeline, PipelineStage, StageStatus, JobStatus,
    ReviewBlockError,
)


@pytest.fixture
def store():
    """Create a temporary job store."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Create jobs and job_checkpoints tables
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            offering_id TEXT,
            url TEXT,
            product_name TEXT,
            status TEXT NOT NULL DEFAULT 'created',
            current_stage TEXT DEFAULT '',
            quick_mode INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT DEFAULT '',
            error TEXT DEFAULT '',
            budget_seconds INTEGER DEFAULT 600,
            elapsed_seconds REAL DEFAULT 0.0,
            stage_data_json TEXT DEFAULT '{}',
            stage_status_json TEXT DEFAULT '{}',
            metadata_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS job_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result_json TEXT DEFAULT '{}',
            error TEXT DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()
    s = JobStore(db_path=path)
    yield s
    if s._conn:
        s._conn.close()
    os.unlink(path)


class TestJob:
    def test_create(self):
        job = Job.create(url="https://test.com", product_name="TestProd")
        assert job.job_id  # Auto-generated
        assert job.url == "https://test.com"
        assert job.status == JobStatus.CREATED

    def test_quick_mode_stages(self):
        job = Job.create(quick=True)
        stages = job.get_stages()
        assert PipelineStage.ANALYZE_SITE not in stages
        assert PipelineStage.ANALYZE_MARKET not in stages
        assert PipelineStage.IDENTIFY in stages

    def test_full_mode_stages(self):
        job = Job.create(quick=False)
        stages = job.get_stages()
        assert PipelineStage.ANALYZE_SITE in stages
        assert PipelineStage.ANALYZE_MARKET in stages

    def test_stage_status_tracking(self):
        job = Job.create()
        job.set_stage_status(PipelineStage.IDENTIFY, StageStatus.COMPLETED)
        assert job.get_stage_status(PipelineStage.IDENTIFY) == StageStatus.COMPLETED
        assert job.get_stage_status(PipelineStage.ACQUIRE) == StageStatus.PENDING

    def test_stage_result_storage(self):
        job = Job.create()
        job.set_stage_result(PipelineStage.IDENTIFY, {"product_name": "Test"})
        result = job.get_stage_result(PipelineStage.IDENTIFY)
        assert result["product_name"] == "Test"

    def test_budget_check(self):
        job = Job.create(budget_seconds=10)
        job.elapsed_seconds = 5
        assert not job.is_budget_exceeded()
        job.elapsed_seconds = 15
        assert job.is_budget_exceeded()


class TestJobStore:
    def test_save_and_load(self, store):
        job = Job.create(url="https://test.com", product_name="Test")
        store.save(job)

        loaded = store.load(job.job_id)
        assert loaded is not None
        assert loaded.url == "https://test.com"
        assert loaded.product_name == "Test"

    def test_load_nonexistent(self, store):
        assert store.load("nonexistent") is None

    def test_save_checkpoint(self, store):
        job = Job.create()
        store.save(job)
        store.save_checkpoint(
            job, PipelineStage.IDENTIFY, StageStatus.COMPLETED,
            result={"test": True}
        )
        checkpoints = store.get_checkpoints(job.job_id)
        assert len(checkpoints) == 1
        assert checkpoints[0]["stage"] == "identify"

    def test_list_jobs(self, store):
        for i in range(3):
            job = Job.create(url=f"https://test{i}.com")
            store.save(job)

        jobs = store.list_jobs()
        assert len(jobs) == 3

    def test_list_jobs_filtered(self, store):
        job1 = Job.create(url="https://test1.com")
        job1.status = JobStatus.COMPLETED
        store.save(job1)

        job2 = Job.create(url="https://test2.com")
        job2.status = JobStatus.RUNNING
        store.save(job2)

        completed = store.list_jobs(status=JobStatus.COMPLETED)
        assert len(completed) == 1


class TestPipeline:
    def test_simple_pipeline(self, store):
        """Pipeline should execute stages in order."""
        executed = []

        def handler_a(job):
            executed.append("a")
            return {"stage": "a"}

        def handler_b(job):
            executed.append("b")
            return {"stage": "b"}

        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, handler_a)
        pipeline.register(PipelineStage.ACQUIRE, handler_b)

        job = Job.create(quick=True)
        result = pipeline.run(job)

        assert "a" in executed
        assert "b" in executed
        assert executed.index("a") < executed.index("b")

    def test_failure_stops_pipeline(self, store):
        """Pipeline should stop on stage failure."""
        executed = []

        def handler_ok(job):
            executed.append("ok")
            return {}

        def handler_fail(job):
            raise ValueError("Test failure")

        def handler_after(job):
            executed.append("after")
            return {}

        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, handler_ok)
        pipeline.register(PipelineStage.ACQUIRE, handler_fail)
        pipeline.register(PipelineStage.EXTRACT, handler_after)

        job = Job.create(quick=True)
        result = pipeline.run(job)

        assert result.status == JobStatus.FAILED
        assert "ok" in executed
        assert "after" not in executed  # Should not have run

    def test_resume_skips_completed(self, store):
        """Resuming should skip already-completed stages."""
        call_count = {"identify": 0, "acquire": 0}

        def handler_identify(job):
            call_count["identify"] += 1
            return {}

        def handler_acquire(job):
            call_count["acquire"] += 1
            return {}

        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, handler_identify)
        pipeline.register(PipelineStage.ACQUIRE, handler_acquire)

        # Pre-mark IDENTIFY as completed
        job = Job.create(quick=True)
        job.set_stage_status(PipelineStage.IDENTIFY, StageStatus.COMPLETED)
        store.save(job)

        pipeline.run(job)

        assert call_count["identify"] == 0  # Skipped
        assert call_count["acquire"] == 1   # Ran

    def test_budget_enforcement(self, store):
        """Pipeline should pause when budget exceeded."""
        import time

        def slow_handler(job):
            time.sleep(0.1)
            return {}

        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, slow_handler)
        pipeline.register(PipelineStage.ACQUIRE, slow_handler)

        # Tiny budget
        job = Job.create(quick=True, budget_seconds=0)
        job.elapsed_seconds = 999  # Already exceeded
        result = pipeline.run(job)

        assert result.status == JobStatus.PAUSED

    def test_unattended_review_condition_becomes_repair_failure(self, store):
        """VA jobs never open the human-review workflow."""
        def repair_required(job):
            raise ReviewBlockError(
                "source validation dependency unavailable",
                details={"validation_error": "dependency unavailable"},
            )

        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, repair_required)
        job = Job.create(quick=True)
        job.metadata["unattended"] = True

        result = pipeline.run(job)

        assert result.status == JobStatus.FAILED
        assert result.status != JobStatus.AWAITING_REVIEW
        assert result.error.startswith("Source repair required:")
        stage_result = result.get_stage_result(PipelineStage.IDENTIFY)
        assert stage_result["repair_required"] is True

    def test_elapsed_budget_is_not_double_counted(self, store, monkeypatch):
        """Cumulative elapsed time is added once per run, not once per stage."""
        clock = {"now": 0.0}
        monkeypatch.setattr("workflow.time.time", lambda: clock["now"])

        def ten_second_handler(job):
            clock["now"] += 10.0
            return {}

        pipeline = Pipeline(store)
        pipeline.register(PipelineStage.IDENTIFY, ten_second_handler)
        pipeline.register(PipelineStage.ACQUIRE, ten_second_handler)
        pipeline.register(PipelineStage.EXTRACT, ten_second_handler)

        # Three 10-second stages should reach completion. The old triangular
        # accounting paused before stage three despite only 20 seconds passing.
        job = Job.create(quick=True, budget_seconds=25)
        result = pipeline.run(job)

        assert result.status == JobStatus.COMPLETED
        assert result.elapsed_seconds == 30.0

    def test_cancel(self, store):
        pipeline = Pipeline(store)
        job = Job.create()
        job.status = JobStatus.RUNNING
        store.save(job)

        result = pipeline.cancel(job)
        assert result.status == JobStatus.CANCELLED
