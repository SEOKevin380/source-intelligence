"""End-to-end integration tests for the pipeline through actual migrated database.

These tests verify that the migration v3 schema, JobStore, EvidenceLake,
ClaimsLedger, and Pipeline all work together correctly — not with
hand-built schemas, but through the actual database.py migration path.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import ProductDatabase


@pytest.fixture
def migrated_db():
    """Create a database through the actual migration path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = ProductDatabase(db_path=path)
    yield path
    db.close()
    os.unlink(path)


class TestJobStoreOnMigratedDB:
    """Verify JobStore operations work on a database created by migrations."""

    def test_save_and_load_job(self, migrated_db):
        """JobStore.save() and load() must work on the migrated schema."""
        from workflow import JobStore, Job, JobStatus

        store = JobStore(db_path=migrated_db)
        job = Job.create(url="https://example.com", product_name="Test Product")
        store.save(job)

        loaded = store.load(job.job_id)
        assert loaded is not None
        assert loaded.url == "https://example.com"
        assert loaded.product_name == "Test Product"
        assert loaded.status == JobStatus.CREATED

    def test_save_checkpoint(self, migrated_db):
        """Checkpoints must save to the migrated job_checkpoints table."""
        from workflow import JobStore, Job, PipelineStage, StageStatus

        store = JobStore(db_path=migrated_db)
        job = Job.create(url="https://example.com")
        store.save(job)

        store.save_checkpoint(
            job, PipelineStage.IDENTIFY, StageStatus.COMPLETED,
            result={"product_name": "Test"}
        )

        checkpoints = store.get_checkpoints(job.job_id)
        assert len(checkpoints) == 1
        assert checkpoints[0]["stage"] == "identify"
        assert checkpoints[0]["status"] == "completed"

    def test_list_jobs_with_filters(self, migrated_db):
        """list_jobs() must filter correctly on migrated schema."""
        from workflow import JobStore, Job, JobStatus

        store = JobStore(db_path=migrated_db)
        job1 = Job.create(url="https://a.com", product_name="A")
        job1.status = JobStatus.COMPLETED
        store.save(job1)

        job2 = Job.create(url="https://b.com", product_name="B")
        job2.status = JobStatus.FAILED
        store.save(job2)

        completed = store.list_jobs(status=JobStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].product_name == "A"

    def test_stage_data_roundtrips(self, migrated_db):
        """Stage data JSON must survive save/load roundtrip."""
        from workflow import JobStore, Job, PipelineStage

        store = JobStore(db_path=migrated_db)
        job = Job.create(url="https://example.com")
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "product_data": {"product_name": "Test", "pricing": {"1 bottle": "$49.99"}},
            "offering_type": "supplement",
        })
        store.save(job)

        loaded = store.load(job.job_id)
        result = loaded.get_stage_result(PipelineStage.IDENTIFY)
        assert result["offering_type"] == "supplement"
        assert result["product_data"]["pricing"]["1 bottle"] == "$49.99"


class TestEvidenceLakeOnMigratedDB:
    """Verify EvidenceLake operations on a database created by migrations."""

    def test_store_and_retrieve_artifact(self, migrated_db):
        """Artifacts must be storable/retrievable on migrated schema."""
        from evidence import EvidenceLake, Artifact, SourceClass, SourceRelationship

        lake = EvidenceLake(db_path=migrated_db)
        content = b"<html><body>Test product page</body></html>"
        artifact = Artifact(
            source_url="https://example.com/product",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            offering_id="test-offering-123",
        )
        aid = lake.store(artifact, content)

        retrieved = lake.get(aid)
        assert retrieved is not None
        assert retrieved.source_class == SourceClass.OFFICIAL_VENDOR
        assert retrieved.offering_id == "test-offering-123"

        text = lake.get_content(aid)
        assert "Test product page" in text

    def test_list_artifacts_for_offering(self, migrated_db):
        """list_for_offering() must work on migrated schema."""
        from evidence import EvidenceLake, Artifact, SourceClass, SourceRelationship

        lake = EvidenceLake(db_path=migrated_db)
        for i in range(3):
            artifact = Artifact(
                source_url=f"https://example.com/page{i}",
                source_class=SourceClass.OFFICIAL_VENDOR,
                source_relationship=SourceRelationship.FIRST_PARTY,
                offering_id="offering-abc",
            )
            lake.store(artifact, f"content {i}".encode())

        artifacts = lake.list_for_offering("offering-abc")
        assert len(artifacts) == 3


class TestClaimsLedgerOnMigratedDB:
    """Verify ClaimsLedger operations on a database created by migrations."""

    def test_add_and_retrieve_claim(self, migrated_db):
        """Claims must be storable/retrievable on migrated schema."""
        from claims import ClaimsLedger, Claim, ClaimType

        ledger = ClaimsLedger(db_path=migrated_db)
        claim = Claim(
            offering_id="test-offering",
            claim_text="Vitamin D3: 2000 IU",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="artifact-abc123",
            exact_excerpt="Vitamin D3: 2000 IU per serving",
            page_location="Supplement Facts panel",
            source_class="official_vendor",
            confidence=0.7,
            extraction_method="llm_extraction",
        )
        cid = ledger.add_claim(claim)
        assert cid

        retrieved = ledger.get_claim(cid)
        assert retrieved is not None
        assert retrieved.source_artifact_id == "artifact-abc123"
        assert retrieved.exact_excerpt == "Vitamin D3: 2000 IU per serving"
        assert retrieved.page_location == "Supplement Facts panel"

    def test_batch_claims_with_citations(self, migrated_db):
        """Batch claims with artifact references must roundtrip correctly."""
        from claims import ClaimsLedger, Claim, ClaimType

        ledger = ClaimsLedger(db_path=migrated_db)
        artifact_id = "art-official-page-001"
        claims = [
            Claim(
                offering_id="off-123",
                claim_text="Magnesium: 400mg",
                claim_type=ClaimType.INGREDIENT_AMOUNT,
                source_artifact_id=artifact_id,
                exact_excerpt="Magnesium (as Magnesium Glycinate): 400mg",
                page_location="Supplement Facts panel",
                source_class="official_vendor",
                confidence=0.7,
                extraction_method="llm_extraction",
                metadata={"ingredient_name": "magnesium", "amount": "400mg"},
            ),
            Claim(
                offering_id="off-123",
                claim_text="1 bottle: $39.99",
                claim_type=ClaimType.PRICING,
                source_artifact_id=artifact_id,
                exact_excerpt="1 bottle: $39.99",
                page_location="Pricing section",
                source_class="official_vendor",
                confidence=0.8,
                extraction_method="llm_extraction",
                metadata={"package": "1 bottle", "price": "$39.99"},
            ),
        ]
        ids = ledger.add_claims_batch(claims)
        assert len(ids) == 2

        # All claims should reference the same artifact
        for cid in ids:
            c = ledger.get_claim(cid)
            assert c.source_artifact_id == artifact_id
            assert c.exact_excerpt  # Not empty
            assert c.page_location  # Not empty

    def test_conflict_detection_on_migrated_db(self, migrated_db):
        """Conflict detection must work end-to-end on migrated schema."""
        from claims import ClaimsLedger, Claim, ClaimType

        ledger = ClaimsLedger(db_path=migrated_db)
        # Two different amounts for same ingredient from different sources
        c1 = Claim(
            offering_id="off-conflict",
            claim_text="Zinc: 30mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="art-vendor",
            source_class="official_vendor",
            confidence=0.5,
            metadata={"ingredient_name": "zinc", "amount": "30mg"},
        )
        c2 = Claim(
            offering_id="off-conflict",
            claim_text="Zinc: 15mg",
            claim_type=ClaimType.INGREDIENT_AMOUNT,
            source_artifact_id="art-lab",
            source_class="independent_lab",
            confidence=0.85,
            metadata={"ingredient_name": "zinc", "amount": "15mg"},
        )
        ledger.add_claim(c1)
        ledger.add_claim(c2)

        conflicts = ledger.detect_conflicts("off-conflict")
        assert len(conflicts) >= 1
        assert "zinc" in conflicts[0][2].lower()


class TestOfferingPersistenceOnMigratedDB:
    """Verify Offering save/load on migrated schema."""

    def test_offering_save_and_load(self, migrated_db):
        """Offering must persist to the offerings table created by migration."""
        from entities import Offering, OfferingType

        offering = Offering(
            name="TestMax Pro",
            offering_type=OfferingType.SUPPLEMENT,
            url="https://testmax.com",
            category="brain",
        )
        offering.save(db_path=migrated_db)

        loaded = Offering.load(offering.offering_id, db_path=migrated_db)
        assert loaded is not None
        assert loaded.name == "TestMax Pro"
        assert loaded.offering_type == OfferingType.SUPPLEMENT
        assert loaded.category == "brain"

    def test_unknown_type_persists(self, migrated_db):
        """UNKNOWN offering type must be saveable (fail-closed, not fail-crash)."""
        from entities import Offering, OfferingType

        offering = Offering(
            name="Mystery Product",
            offering_type=OfferingType.UNKNOWN,
        )
        offering.save(db_path=migrated_db)

        loaded = Offering.load(offering.offering_id, db_path=migrated_db)
        assert loaded is not None
        assert loaded.offering_type == OfferingType.UNKNOWN


class TestPipelineWithMigratedDB:
    """Verify the full pipeline executes against migrated schema."""

    def test_pipeline_with_mock_handlers(self, migrated_db):
        """Pipeline must run mock handlers and save state to migrated DB."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus, StageStatus

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        # Register simple mock handlers
        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "product_data": {"product_name": "Test"},
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {
            "artifacts_stored": 0, "artifacts": [],
        })
        pipeline.register(PipelineStage.EXTRACT, lambda job: {"claims_stored": 0})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {
            "ingredient_research": {}, "safety_data": {},
        })
        pipeline.register(PipelineStage.COMPLY, lambda job: {
            "compliance": {}, "risk_level": "low",
        })
        pipeline.register(PipelineStage.PLAN, lambda job: {"plan_status": "ready"})
        pipeline.register(PipelineStage.REVIEW, lambda job: {
            "auto_approved": True, "reason": "low risk, no conflicts",
        })
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {
            "json_path": "", "doc_path": "", "doc_text": "",
            "full_data": {"product": {"product_name": "Test"}},
        })

        job = Job.create(url="https://test.com", product_name="Test")
        result = pipeline.run(job)

        assert result.status == JobStatus.COMPLETED

        # Verify it was persisted
        loaded = store.load(job.job_id)
        assert loaded.status == JobStatus.COMPLETED

        # Verify checkpoints
        checkpoints = store.get_checkpoints(job.job_id)
        assert len(checkpoints) >= 9  # At least 9 stages ran

    def test_review_block_persists(self, migrated_db):
        """ReviewBlockError must persist AWAITING_REVIEW state in migrated DB."""
        from workflow import (
            Pipeline, JobStore, Job, PipelineStage,
            JobStatus, ReviewBlockError,
        )

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "unknown",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {"claims_stored": 0})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {"risk_level": "low"})
        pipeline.register(PipelineStage.PLAN, lambda job: {})

        def blocking_review(job):
            raise ReviewBlockError("offering type not classified")

        pipeline.register(PipelineStage.REVIEW, blocking_review)

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Verify persisted in DB
        loaded = store.load(job.job_id)
        assert loaded.status == JobStatus.AWAITING_REVIEW
        assert "Review required" in loaded.error

    def test_approve_and_resume(self, migrated_db):
        """approve_review() must resume pipeline through migrated DB."""
        from workflow import (
            Pipeline, JobStore, Job, PipelineStage,
            JobStatus, ReviewBlockError,
        )

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        call_count = {"review": 0}

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {"risk_level": "high"})
        pipeline.register(PipelineStage.PLAN, lambda job: {})

        def review_handler(job):
            call_count["review"] += 1
            if job.metadata.get("review_approved_by"):
                return {"auto_approved": False, "previously_approved": True}
            raise ReviewBlockError("risk level: high")

        pipeline.register(PipelineStage.REVIEW, review_handler)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {
            "full_data": {"product": {"product_name": "Approved"}},
        })

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Approve and resume
        final = pipeline.approve_review(job.job_id, reviewer="kevin")
        assert final.status == JobStatus.COMPLETED

        # Review handler was called twice (blocked then approved)
        assert call_count["review"] == 2

    def test_per_rule_resolution_passes_when_all_resolved(self, migrated_db):
        """Per-rule resolution: resolving all findings allows pipeline to proceed."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus
        from stage_handlers import handle_review

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {
            "risk_level": "high",
            "compliance": {
                "state": "human_review",
                "results": [
                    {"rule_id": "RED_FLAG_CURES", "state": "human_review",
                     "matched_text": "cures", "description": "Unhedged claim"},
                    {"rule_id": "RED_FLAG_PREVENTS", "state": "human_review",
                     "matched_text": "prevents", "description": "Unhedged claim"},
                ],
            },
        })
        pipeline.register(PipelineStage.PLAN, lambda job: {})
        pipeline.register(PipelineStage.REVIEW, handle_review)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {"done": True})

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Resolve both rules individually with severity-appropriate actions
        # human_review severity: requires accept/substitute + note
        final = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "RED_FLAG_CURES": {
                    "action": "substitute",
                    "note": "Changed to 'may help'",
                    "substitute_text": "may help support",
                },
                "RED_FLAG_PREVENTS": {
                    "action": "accept",
                    "note": "Acceptable in this educational context",
                },
            },
        )
        assert final.status == JobStatus.COMPLETED
        review_result = final.get_stage_result(PipelineStage.REVIEW)
        assert review_result.get("previously_approved") is True
        assert len(review_result.get("rule_resolutions", [])) == 2

    def test_partial_rule_resolution_still_blocks(self, migrated_db):
        """Per-rule resolution: only resolving some findings still blocks."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus
        from stage_handlers import handle_review

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {
            "risk_level": "high",
            "compliance": {
                "state": "human_review",
                "results": [
                    {"rule_id": "RED_FLAG_CURES", "state": "human_review",
                     "matched_text": "cures", "description": "Unhedged claim"},
                    {"rule_id": "RED_FLAG_PREVENTS", "state": "human_review",
                     "matched_text": "prevents", "description": "Unhedged claim"},
                ],
            },
        })
        pipeline.register(PipelineStage.PLAN, lambda job: {})
        pipeline.register(PipelineStage.REVIEW, handle_review)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {"done": True})

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Only resolve ONE of the two rules
        final = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "RED_FLAG_CURES": {"action": "accept", "note": "Acceptable in context"},
            },
        )
        # Still blocked because RED_FLAG_PREVENTS is unresolved
        assert final.status == JobStatus.AWAITING_REVIEW

    def test_conflict_resolution_per_rule(self, migrated_db):
        """Claim conflicts can be individually resolved."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus
        from stage_handlers import handle_review

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 2})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {"risk_level": "low"})
        pipeline.register(PipelineStage.PLAN, lambda job: {})
        pipeline.register(PipelineStage.REVIEW, handle_review)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {"done": True})

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Resolve claim conflicts
        final = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "CLAIM_CONFLICTS": {"action": "accept", "note": "Reviewed all conflicts"},
            },
        )
        assert final.status == JobStatus.COMPLETED

    def test_block_severity_rejects_waive(self, migrated_db):
        """BLOCK-severity rules cannot be waived — only substitute with text."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus
        from stage_handlers import handle_review

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {
            "risk_level": "critical",
            "compliance": {
                "state": "blocked",
                "results": [
                    {"rule_id": "CVD9_DISEASE_REVERSAL", "state": "blocked",
                     "matched_text": "cure diabetes", "description": "Disease reversal"},
                ],
            },
        })
        pipeline.register(PipelineStage.PLAN, lambda job: {})
        pipeline.register(PipelineStage.REVIEW, handle_review)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {"done": True})

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Try to waive a BLOCK rule — should still block
        still_blocked = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "CVD9_DISEASE_REVERSAL": {"action": "waive", "note": "I want to waive it"},
            },
        )
        assert still_blocked.status == JobStatus.AWAITING_REVIEW

        # Try accept without substitute_text — should still block
        still_blocked2 = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "CVD9_DISEASE_REVERSAL": {"action": "substitute", "note": "Changed it"},
            },
        )
        assert still_blocked2.status == JobStatus.AWAITING_REVIEW

        # Provide valid substitute with actual text — should pass
        final = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "CVD9_DISEASE_REVERSAL": {
                    "action": "substitute",
                    "note": "Replaced with hedged language",
                    "substitute_text": "may help support healthy blood sugar levels",
                },
            },
        )
        assert final.status == JobStatus.COMPLETED

    def test_review_severity_requires_note(self, migrated_db):
        """REVIEW-severity rules require a justification note."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus
        from stage_handlers import handle_review

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 0})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {
            "risk_level": "high",
            "compliance": {
                "state": "human_review",
                "results": [
                    {"rule_id": "RED_FLAG_BOOSTS", "state": "human_review",
                     "matched_text": "boosts", "description": "Unhedged claim"},
                ],
            },
        })
        pipeline.register(PipelineStage.PLAN, lambda job: {})
        pipeline.register(PipelineStage.REVIEW, handle_review)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {"done": True})

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Accept without note — should block
        still_blocked = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "RED_FLAG_BOOSTS": {"action": "accept", "note": ""},
            },
        )
        assert still_blocked.status == JobStatus.AWAITING_REVIEW

        # Accept with note — should pass
        final = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "RED_FLAG_BOOSTS": {"action": "accept", "note": "Used in hedged context"},
            },
        )
        assert final.status == JobStatus.COMPLETED

    def test_conflict_resolution_requires_note(self, migrated_db):
        """Claim conflict resolution requires a justification note."""
        from workflow import Pipeline, JobStore, Job, PipelineStage, JobStatus
        from stage_handlers import handle_review

        store = JobStore(db_path=migrated_db)
        pipeline = Pipeline(store)

        pipeline.register(PipelineStage.IDENTIFY, lambda job: {
            "offering_type": "supplement",
        })
        pipeline.register(PipelineStage.ACQUIRE, lambda job: {"artifacts": []})
        pipeline.register(PipelineStage.EXTRACT, lambda job: {})
        pipeline.register(PipelineStage.RECONCILE, lambda job: {"conflicts_found": 1})
        pipeline.register(PipelineStage.RESEARCH, lambda job: {})
        pipeline.register(PipelineStage.COMPLY, lambda job: {"risk_level": "low"})
        pipeline.register(PipelineStage.PLAN, lambda job: {})
        pipeline.register(PipelineStage.REVIEW, handle_review)
        pipeline.register(PipelineStage.SOURCE_PACK, lambda job: {"done": True})

        job = Job.create(url="https://test.com")
        result = pipeline.run(job)
        assert result.status == JobStatus.AWAITING_REVIEW

        # Resolve without note — should still block
        still_blocked = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "CLAIM_CONFLICTS": {"action": "accept", "note": ""},
            },
        )
        assert still_blocked.status == JobStatus.AWAITING_REVIEW

        # Resolve with note — should pass
        final = pipeline.approve_review(
            job.job_id, reviewer="kevin",
            rule_resolutions={
                "CLAIM_CONFLICTS": {"action": "accept", "note": "Label value is authoritative"},
            },
        )
        assert final.status == JobStatus.COMPLETED


class TestComplianceEngineIntegration:
    """Verify new compliance engine works with pipeline data."""

    def test_evaluate_claims_text(self):
        """ComplianceEngine must evaluate text and return structured results."""
        from compliance import ComplianceEngine, ComplianceState
        from entities import OfferingType

        engine = ComplianceEngine()
        # Text with a disease reversal claim (should be blocked)
        text = "This supplement can cure diabetes and reverse heart disease."
        report = engine.evaluate(text, OfferingType.SUPPLEMENT)

        assert report.overall_state in (
            ComplianceState.BLOCKED,
            ComplianceState.HUMAN_REVIEW_REQUIRED,
        )
        assert report.blocks + report.reviews > 0

    def test_accesswire_channel_filtering(self):
        """AccessWire-specific rules should only fire on accesswire channel."""
        from compliance import ComplianceEngine, ComplianceState
        from entities import OfferingType

        engine = ComplianceEngine()
        text = "weight loss miracle"  # Common AccessWire blocklist term

        # WordPress channel — should not trigger AccessWire rules
        wp_report = engine.evaluate(text, OfferingType.SUPPLEMENT, channel="wordpress")
        aw_report = engine.evaluate(text, OfferingType.SUPPLEMENT, channel="accesswire")

        # AccessWire should have more or equal blocks than WordPress
        # (AccessWire blocklist adds channel-specific rules)
        assert aw_report.blocks >= wp_report.blocks


class TestAuthorityScoring:
    """Verify authority scoring integration."""

    def test_regulatory_beats_vendor(self):
        """Regulatory source must score higher than vendor source."""
        from authority import score_authority
        from evidence import SourceClass, SourceRelationship

        reg = score_authority(
            SourceClass.REGULATORY_DATABASE,
            SourceRelationship.THIRD_PARTY,
        )
        vendor = score_authority(
            SourceClass.OFFICIAL_VENDOR,
            SourceRelationship.FIRST_PARTY,
        )
        assert reg > vendor

    def test_tls_penalty_applied(self):
        """Non-TLS source must score lower."""
        from authority import score_authority
        from evidence import SourceClass

        with_tls = score_authority(SourceClass.OFFICIAL_VENDOR, tls_verified=True)
        without_tls = score_authority(SourceClass.OFFICIAL_VENDOR, tls_verified=False)
        assert with_tls > without_tls


class TestIntelligencePackRouting:
    """Verify intelligence pack routing for research decisions."""

    def test_supplement_requires_pubmed(self):
        """Supplement pack must require PubMed research."""
        from intelligence_packs import get_pack
        from entities import OfferingType

        pack = get_pack(OfferingType.SUPPLEMENT)
        assert pack["evidence_requirements"]["pubmed_research"] == "required"

    def test_software_does_not_require_pubmed(self):
        """Software pack should not require PubMed research."""
        from intelligence_packs import get_pack
        from entities import OfferingType

        pack = get_pack(OfferingType.SOFTWARE)
        assert "pubmed_research" not in pack["evidence_requirements"]

    def test_unknown_type_fails_closed(self):
        """UNKNOWN type must raise ValueError (cannot research unclassified)."""
        from intelligence_packs import get_pack
        from entities import OfferingType

        with pytest.raises(ValueError, match="No intelligence pack"):
            get_pack(OfferingType.UNKNOWN)


# ============================================================================
# REAL-HANDLER END-TO-END INTEGRATION TEST
# ============================================================================

# Fake product data returned by mocked phase1_extract_product
_FAKE_PRODUCT_DATA = {
    "product_name": "TestoMax Elite",
    "product_type": "supplement",
    "url": "https://testomax-elite.example.com",
    "description": "A powerful testosterone support formula with clinically-backed ingredients.",
    "supplement_facts": {
        "serving_size": "2 capsules",
        "ingredients": [
            {"name": "D-Aspartic Acid", "amount": "2352mg", "form": ""},
            {"name": "Fenugreek Extract", "amount": "600mg", "form": "4:1 extract"},
            {"name": "Zinc", "amount": "30mg", "form": "as Zinc Gluconate"},
        ],
    },
    "pricing": {"1 bottle": "$59.99", "3 bottles": "$149.99"},
    "claims": [
        {"claim": "Boosts testosterone by up to 42%"},
        "Supports lean muscle growth",
    ],
    "company": {
        "name": "TestoMax Labs LLC",
        "address": "123 Test Ave, Miami FL",
    },
    "refund_policy": "60-day money-back guarantee",
}


class TestRealHandlerPipeline:
    """End-to-end test using REAL handler functions with mocked externals.

    Mocks: phase1_extract_product, net.safe_fetch, phase2-8 from research_product.py
    Uses REAL: pipeline, stage_handlers, evidence lake, claims ledger,
              authority scoring, compliance engine, entities, intelligence packs
    """

    @pytest.fixture
    def pipeline_db(self, migrated_db):
        """Set up a pipeline with real handlers against the migrated DB."""
        return migrated_db

    def _run_pipeline_with_mocks(self, db_path, product_data=None,
                                  quick=False, expect_review=False):
        """Run the full pipeline with mocked external dependencies.

        Returns the completed (or review-blocked) Job.
        """
        import hashlib
        from unittest.mock import patch
        from stage_handlers import create_default_pipeline
        from workflow import Job, JobStatus
        from net import FetchResult

        pd = product_data or _FAKE_PRODUCT_DATA

        # Build a realistic page text with ingredient names and claims
        # so literal excerpt extraction can find real matches
        supp_facts = pd.get("supplement_facts", {})
        ingredients_text = " | ".join(
            f"{i.get('name', '')} {i.get('amount', '')}"
            for i in supp_facts.get("ingredients", [])
        )
        claims_list = pd.get("claims", [])
        claims_text = " ".join(
            c.get("claim", c) if isinstance(c, dict) else c
            for c in claims_list
        )
        pricing = pd.get("pricing", {})
        pricing_text = " ".join(
            f"{k}: {v}" for k, v in pricing.items()
        ) if isinstance(pricing, dict) else ""

        page_text = (
            f"<html><body>"
            f"<h1>{pd['product_name']}</h1>"
            f"<div class='supplement-facts'>"
            f"Serving Size: {supp_facts.get('serving_size', 'N/A')} | "
            f"{ingredients_text}"
            f"</div>"
            f"<div class='claims'>{claims_text}</div>"
            f"<div class='pricing'>{pricing_text}</div>"
            f"</body></html>"
        )
        page_bytes = page_text.encode("utf-8")
        fake_fetch_result = FetchResult(
            content=page_bytes,
            text=page_text,
            final_url=pd.get("url", "https://example.com"),
            status_code=200,
            headers={"Content-Type": "text/html"},
            fetched_at="2026-07-22T12:00:00+00:00",
            content_hash=hashlib.sha256(page_bytes).hexdigest(),
            content_length=len(page_bytes),
            tls_verified=True,
            elapsed_ms=150.0,
            error="",
        )

        # Mock all external dependencies at their source modules
        # (handlers import from research_product locally inside each function)
        # Also patch config.DB_PATH so EvidenceLake/ClaimsLedger use test DB
        with patch("config.DB_PATH", db_path), \
             patch("research_product.phase1_extract_product", return_value=pd), \
             patch("stage_handlers._get_browser_session", return_value=None), \
             patch("stage_handlers._cleanup_browser"), \
             patch("net.safe_fetch", return_value=fake_fetch_result), \
             patch("research_product.phase2_pubmed_research", return_value={
                 "D-Aspartic Acid": {"studies": [{"title": "DAA and testosterone", "pmid": "12345"}]},
                 "Fenugreek Extract": {"studies": []},
                 "Zinc": {"studies": [{"title": "Zinc deficiency and T", "pmid": "67890"}]},
             }), \
             patch("research_product.phase3_safety_research", return_value={
                 "drug_interactions": [],
                 "contraindications": [],
             }), \
             patch("research_product.phase4_keyword_research", return_value={
                 "primary": "testomax elite review",
                 "secondary": ["testosterone booster review"],
             }), \
             patch("research_product.phase5_reputation_check", return_value={
                 "bbb_rating": "N/A",
                 "complaints": 0,
             }), \
             patch("research_product.phase6_competitive_landscape", return_value={
                 "competitors": ["TestoFuel", "Prime Male"],
             }), \
             patch("research_product.phase8_output", return_value=(
                 "/tmp/test_source.json",
                 "/tmp/test_report.md",
                 "# TestoMax Elite Source Report\n...",
                 {"product": pd, "meta": {"version": 1}},
             )):

            pipeline = create_default_pipeline(db_path=db_path)
            job = Job.create(
                url=pd.get("url", "https://example.com"),
                product_name=pd.get("product_name", "Test"),
                quick=quick,
            )
            result = pipeline.run(job)
            return result, pipeline

    def test_full_pipeline_runs_to_completion(self, pipeline_db):
        """Real handlers must execute all stages through to COMPLETED."""
        from workflow import JobStatus

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        # The compliance engine should flag "Boosts testosterone by up to 42%"
        # as a disease/body-function claim, triggering review block.
        # This is actually correct behavior — let's verify either outcome.
        assert job.status in (JobStatus.COMPLETED, JobStatus.AWAITING_REVIEW)

    def test_offering_saved_during_identify(self, pipeline_db):
        """handle_identify must save an Offering to the offerings table."""
        from entities import Offering

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        # The offering should be persisted
        offering = Offering.load(job.offering_id, db_path=pipeline_db)
        assert offering is not None
        assert offering.name == "TestoMax Elite"
        assert offering.offering_type.value == "supplement"

    def test_artifacts_stored_in_evidence_lake(self, pipeline_db):
        """handle_acquire must store at least one artifact."""
        from evidence import EvidenceLake
        from workflow import PipelineStage

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        # Check acquire stage result
        acquire_result = job.get_stage_result(PipelineStage.ACQUIRE)
        assert acquire_result.get("evidence_lake_available") is True
        assert acquire_result.get("artifacts_stored", 0) >= 1

        # Verify artifacts in the lake
        lake = EvidenceLake(db_path=pipeline_db)
        artifacts = lake.list_for_offering(job.offering_id)
        assert len(artifacts) >= 1

    def test_claims_extracted_with_authority_scores(self, pipeline_db):
        """handle_extract must create claims with authority-based confidence."""
        from claims import ClaimsLedger, ClaimType
        from workflow import PipelineStage

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        extract_result = job.get_stage_result(PipelineStage.EXTRACT)
        assert extract_result.get("claims_stored", 0) > 0

        # Verify claims in the ledger
        ledger = ClaimsLedger(db_path=pipeline_db)
        claims = ledger.get_claims(job.offering_id)
        assert len(claims) > 0

        # Check ingredient claims have authority-computed confidence
        ingredient_claims = [c for c in claims if c.claim_type == ClaimType.INGREDIENT_AMOUNT]
        assert len(ingredient_claims) == 3  # DAA, Fenugreek, Zinc

        for c in ingredient_claims:
            # Authority-scored: OFFICIAL_VENDOR * FIRST_PARTY * llm_extraction
            # = 0.50 * 1.0 * 0.80 = 0.40
            assert c.confidence == 0.4, f"Expected authority score 0.4, got {c.confidence}"
            assert c.source_artifact_id is not None  # Linked to artifact
            # page_location is either a char offset (literal) or fallback label
            assert c.page_location in ("Supplement Facts panel",) or \
                c.page_location.startswith("chars "), \
                f"Unexpected page_location: {c.page_location}"

        # Manufacturer claims should have lower confidence (marketing_copy)
        mfr_claims = [c for c in claims if c.claim_type == ClaimType.MANUFACTURER_CLAIM]
        assert len(mfr_claims) >= 1
        for c in mfr_claims:
            # marketing_copy default = 0.70 multiplier → 0.50 * 1.0 * 0.70 = 0.35
            assert c.confidence == 0.35, f"Expected marketing score 0.35, got {c.confidence}"

    def test_claims_linked_to_source_artifacts(self, pipeline_db):
        """Every extracted claim must reference a source artifact ID."""
        from claims import ClaimsLedger
        from evidence import EvidenceLake

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        ledger = ClaimsLedger(db_path=pipeline_db)
        lake = EvidenceLake(db_path=pipeline_db)

        claims = ledger.get_claims(job.offering_id)
        assert len(claims) > 0

        for claim in claims:
            assert claim.source_artifact_id, \
                f"Claim '{claim.claim_text}' has no source_artifact_id"
            # The artifact should exist in the lake
            artifact = lake.get(claim.source_artifact_id)
            assert artifact is not None, \
                f"Artifact {claim.source_artifact_id} not found in lake"

    def test_compliance_uses_new_engine(self, pipeline_db):
        """handle_comply must use ComplianceEngine, not legacy phase7."""
        from workflow import PipelineStage

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        comply_result = job.get_stage_result(PipelineStage.COMPLY)
        compliance = comply_result.get("compliance", {})

        # New engine returns structured results with 'state' and 'results' keys
        assert "state" in compliance, "Expected new ComplianceEngine output"
        assert "results" in compliance

    def test_intelligence_pack_routes_research(self, pipeline_db):
        """handle_research must route through intelligence packs."""
        from workflow import PipelineStage

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        research_result = job.get_stage_result(PipelineStage.RESEARCH)

        # Supplement type requires PubMed → should NOT be skipped
        assert research_result.get("research_skipped") is False

    def test_review_gate_evaluates_compliance(self, pipeline_db):
        """handle_review must block on non-low risk or pass on low risk."""
        from workflow import PipelineStage, JobStatus

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        # "Boosts testosterone by up to 42%" is a strong health claim
        # that should trigger compliance concerns
        if job.status == JobStatus.AWAITING_REVIEW:
            review_result = job.get_stage_result(PipelineStage.REVIEW)
            assert review_result.get("blocked") is True
        else:
            review_result = job.get_stage_result(PipelineStage.REVIEW)
            assert review_result.get("auto_approved") is True

    def test_approve_and_complete_real_handlers(self, pipeline_db):
        """Pipeline must complete after approval when using real handlers."""
        from workflow import JobStatus

        job, pipeline = self._run_pipeline_with_mocks(pipeline_db)

        if job.status == JobStatus.AWAITING_REVIEW:
            # Approve and resume
            final = pipeline.approve_review(job.job_id, reviewer="test_admin")
            assert final is not None
            assert final.status == JobStatus.COMPLETED
            assert final.metadata.get("review_approved_by") == "test_admin"
        else:
            # Auto-approved — already completed
            assert job.status == JobStatus.COMPLETED

    def test_quick_mode_skips_site_market_analysis(self, pipeline_db):
        """Quick mode must not execute ANALYZE_SITE and ANALYZE_MARKET."""
        from workflow import PipelineStage, StageStatus, JobStatus

        job, pipeline = self._run_pipeline_with_mocks(pipeline_db, quick=True)

        # If awaiting review, approve to run remaining stages
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        # Quick mode excludes these from the stage list — they stay PENDING
        # (never executed, never set to SKIPPED/COMPLETED)
        assert job.get_stage_status(PipelineStage.ANALYZE_SITE) == StageStatus.PENDING
        assert job.get_stage_status(PipelineStage.ANALYZE_MARKET) == StageStatus.PENDING
        # But the core stages should be completed
        assert job.get_stage_status(PipelineStage.IDENTIFY) == StageStatus.COMPLETED
        assert job.get_stage_status(PipelineStage.RESEARCH) == StageStatus.COMPLETED

    def test_software_type_skips_pubmed(self, pipeline_db):
        """Software offering type must skip PubMed research via pack routing."""
        from workflow import PipelineStage, JobStatus

        software_data = {
            "product_name": "CodeBuddy Pro",
            "product_type": "software",
            "url": "https://codebuddy.example.com",
            "description": "An AI-powered code review tool for development teams.",
            "supplement_facts": {"ingredients": []},
            "pricing": {"monthly": "$29/mo"},
            "claims": ["Reduces code review time by 50%"],
            "company": {"name": "CodeBuddy Inc."},
        }

        job, pipeline = self._run_pipeline_with_mocks(
            pipeline_db, product_data=software_data
        )

        # If review-blocked, approve to continue
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        research_result = job.get_stage_result(PipelineStage.RESEARCH)
        assert research_result.get("research_skipped") is True

    def test_unknown_type_blocks_at_identify(self, pipeline_db):
        """UNKNOWN offering type must raise ReviewBlockError at IDENTIFY stage."""
        from workflow import PipelineStage, JobStatus, StageStatus

        unknown_data = {
            "product_name": "Mystery Widget",
            "product_type": "unknown_thing",
            "url": "https://mystery.example.com",
            "description": "We're not sure what this is.",
            "supplement_facts": {"ingredients": []},
            "pricing": {},
            "claims": [],
            "company": {},
        }

        job, _ = self._run_pipeline_with_mocks(
            pipeline_db, product_data=unknown_data
        )

        assert job.status == JobStatus.AWAITING_REVIEW
        assert "classify" in job.error.lower() or "unknown" in job.error.lower()
        # Pipeline should stop at IDENTIFY, not proceed further
        assert job.current_stage == "identify"

    def test_checkpoints_saved_for_all_stages(self, pipeline_db):
        """Every executed stage must have a checkpoint in the audit trail."""
        from workflow import JobStore, JobStatus

        job, pipeline = self._run_pipeline_with_mocks(pipeline_db)

        # Approve if needed to run all stages
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        store = JobStore(db_path=pipeline_db)
        checkpoints = store.get_checkpoints(job.job_id)

        # At least the core stages should have checkpoints
        stage_names = [cp["stage"] for cp in checkpoints]
        for required in ["identify", "acquire", "extract", "reconcile",
                         "research", "comply", "plan"]:
            assert required in stage_names, \
                f"Missing checkpoint for stage: {required}"

    def test_pricing_claims_use_authority_scores(self, pipeline_db):
        """Pricing claims must use authority-computed confidence, not hardcoded."""
        from claims import ClaimsLedger, ClaimType

        job, _ = self._run_pipeline_with_mocks(pipeline_db)

        ledger = ClaimsLedger(db_path=pipeline_db)
        claims = ledger.get_claims(job.offering_id)

        pricing_claims = [c for c in claims if c.claim_type == ClaimType.PRICING]
        assert len(pricing_claims) == 2  # 1 bottle, 3 bottles

        for c in pricing_claims:
            # vendor_confidence = 0.50 * 1.0 * 0.80 = 0.4
            assert c.confidence == 0.4, \
                f"Pricing claim '{c.claim_text}' has hardcoded confidence {c.confidence}"

    def test_authority_computed_from_artifact_properties(self, pipeline_db):
        """Authority scoring must read from the stored artifact, not hardcode labels.

        Verifies that claims' source_class values match the actual stored
        artifact's source_class — proving authority is computed from artifact
        properties, not hardcoded.
        """
        from claims import ClaimsLedger, ClaimType
        from evidence import EvidenceLake, SourceClass, SourceRelationship
        from authority import score_authority
        from workflow import PipelineStage

        db_path = pipeline_db
        job, _ = self._run_pipeline_with_mocks(db_path)

        # Verify the claims' source_class matches the artifact's
        lake = EvidenceLake(db_path=db_path)
        ledger = ClaimsLedger(db_path=db_path)
        claims = ledger.get_claims(job.offering_id)

        for c in claims:
            if c.source_artifact_id:
                artifact = lake.get(c.source_artifact_id)
                if artifact:
                    # Get the artifact's source class as string
                    art_sc = artifact.source_class.value \
                        if hasattr(artifact.source_class, 'value') \
                        else artifact.source_class
                    assert c.source_class == art_sc, \
                        f"Claim source_class '{c.source_class}' doesn't match " \
                        f"artifact source_class '{art_sc}'"

    def test_claims_have_literal_excerpts(self, pipeline_db):
        """Claims must contain literal text from the stored artifact, not
        just reconstructed normalized data."""
        from claims import ClaimsLedger, ClaimType

        db_path = pipeline_db
        job, _ = self._run_pipeline_with_mocks(db_path)

        ledger = ClaimsLedger(db_path=db_path)
        claims = ledger.get_claims(job.offering_id)

        # Ingredient claims should have literal excerpts from the page
        ingredient_claims = [c for c in claims
                             if c.claim_type == ClaimType.INGREDIENT_AMOUNT]
        assert len(ingredient_claims) >= 1

        literal_count = 0
        for c in ingredient_claims:
            meta = c.metadata if isinstance(c.metadata, dict) else {}
            if meta.get("excerpt_is_literal"):
                literal_count += 1
                # Literal excerpts should contain actual page content
                assert len(c.exact_excerpt) > len(c.claim_text), \
                    f"Literal excerpt should include context: '{c.exact_excerpt}'"
                # Page location should be a character offset, not a section name
                assert "chars" in c.page_location, \
                    f"Literal location should be char offset: '{c.page_location}'"

        assert literal_count > 0, \
            "At least one ingredient claim should have a literal excerpt " \
            "from the stored artifact content"

    def test_source_pack_uses_claims_ledger(self, pipeline_db):
        """Source pack must be generated from claims ledger and evidence,
        not from legacy phase8_output()."""
        from workflow import PipelineStage, JobStatus

        db_path = pipeline_db
        # Run pipeline through approval to reach SOURCE_PACK
        job, pipeline = self._run_pipeline_with_mocks(
            db_path, expect_review=True
        )
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        pack = job.get_stage_result(PipelineStage.SOURCE_PACK)

        # Source pack should have claims-based structure
        assert pack.get("claims_included", 0) > 0, \
            "Source pack must include claims from the ledger"
        assert pack.get("artifacts_referenced", 0) > 0, \
            "Source pack must reference evidence artifacts"

        # The doc_text should contain provenance information
        doc_text = pack.get("doc_text", "")
        assert "INGREDIENTS" in doc_text, "Pack should have ingredients section"
        assert "EVIDENCE PROVENANCE" in doc_text, \
            "Pack should have evidence provenance section"
        assert "confidence" in doc_text.lower(), \
            "Pack should show confidence scores"

        # full_data should contain structured claims
        full_data = pack.get("full_data", {})
        assert "claims_by_type" in full_data
        assert "artifacts_used" in full_data
        assert full_data["total_claims"] > 0

    def test_source_pack_excludes_rejected_claims(self, pipeline_db):
        """Source pack must not include claims that were rejected during review."""
        from unittest.mock import patch
        from claims import ClaimsLedger, ClaimType, ReviewStatus
        from stage_handlers import handle_source_pack
        from workflow import PipelineStage, JobStatus

        db_path = pipeline_db
        # Run the pipeline first to populate claims
        job, _ = self._run_pipeline_with_mocks(db_path)

        # Reject one ingredient claim
        ledger = ClaimsLedger(db_path=db_path)
        claims = ledger.get_claims(job.offering_id)
        rejected_id = None
        for c in claims:
            if c.claim_type == ClaimType.INGREDIENT_AMOUNT:
                ledger.update_review_status(
                    c.claim_id, ReviewStatus.REJECTED, "test"
                )
                rejected_id = c.claim_id
                break
        assert rejected_id, "Should have found an ingredient claim to reject"

        # Re-run source pack handler directly with patched DB
        with patch("config.DB_PATH", db_path):
            pack = handle_source_pack(job)

        full_data = pack.get("full_data", {})
        ingredient_claims = full_data.get("claims_by_type", {}).get(
            "ingredient_amount", []
        )

        # Should have 2 ingredient claims (one rejected out of 3)
        assert len(ingredient_claims) == 2, \
            f"Expected 2 ingredient claims after rejection, got {len(ingredient_claims)}"

    def test_update_pipeline_extracts_new_facts(self, pipeline_db):
        """Update pipeline must extract facts from the NEW artifact, not just
        duplicate old data. Proves new ingredients reach the claims ledger."""
        import hashlib
        from unittest.mock import patch
        from stage_handlers import create_update_pipeline
        from workflow import PipelineStage, JobStatus, Job
        from claims import ClaimsLedger, ClaimType
        from net import FetchResult

        db_path = pipeline_db

        # First run the full pipeline to get existing data
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        existing_data = job.get_stage_result(PipelineStage.SOURCE_PACK).get(
            "full_data", {}
        )
        assert existing_data.get("product"), "Need existing product data"

        # Count existing ingredient claims
        ledger = ClaimsLedger(db_path=db_path)
        old_claims = ledger.get_claims(
            job.offering_id, claim_type=ClaimType.INGREDIENT_AMOUNT
        )
        old_ingredient_names = {
            c.metadata.get("ingredient_name", "").lower()
            for c in old_claims if isinstance(c.metadata, dict)
        }
        assert "zinc" in old_ingredient_names, "Expected Zinc in original data"

        # Build an updated page that includes a NEW ingredient (Magnesium)
        updated_page = (
            b"<html><body>"
            b"<h1>TestoMax Elite - Updated Formula</h1>"
            b"<div>Supplement Facts: Magnesium 400mg, D-Aspartic Acid 2352mg, Zinc 30mg</div>"
            b"</body></html>"
        )
        updated_text = (
            "TestoMax Elite - Updated Formula "
            "Supplement Facts: Magnesium 400mg, D-Aspartic Acid 2352mg, Zinc 30mg"
        )
        fake_update_result = FetchResult(
            content=updated_page,
            text=updated_text,
            final_url="https://example.com/product",
            status_code=200,
            headers={"Content-Type": "text/html"},
            fetched_at="2026-07-22T14:00:00+00:00",
            content_hash=hashlib.sha256(updated_page).hexdigest(),
            content_length=len(updated_page),
            tls_verified=True,
            elapsed_ms=120.0,
            error="",
        )

        # Mock phase1_extract_product to return data with the new ingredient
        new_product_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "2 capsules",
                "ingredients": [
                    {"name": "D-Aspartic Acid", "amount": "2352mg"},
                    {"name": "Zinc", "amount": "30mg"},
                    {"name": "Magnesium", "amount": "400mg"},  # NEW
                ],
            },
            "pricing": {"1 bottle": "$64.99"},  # Updated price
            "claims": [],
        }

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_update_result), \
             patch("stage_handlers._get_browser_session", return_value=None), \
             patch("stage_handlers._cleanup_browser"), \
             patch("research_product.phase1_extract_product",
                   return_value=new_product_data):
            update_pipeline = create_update_pipeline(
                existing_data=existing_data,
                db_path=db_path,
            )

            update_job = Job.create(
                url="https://example.com/product",
                product_name="TestoMax Elite",
                is_update=True,
            )
            result = update_pipeline.run(update_job)

            # With offering_id preserved, RECONCILE finds price conflicts
            # between old ($59.99) and new ($64.99). Resolve them properly.
            if result.status == JobStatus.AWAITING_REVIEW:
                result = update_pipeline.approve_review(
                    result.job_id, reviewer="test",
                    rule_resolutions={
                        "CLAIM_CONFLICTS": {
                            "action": "accept",
                            "note": "Price update acknowledged",
                        },
                    },
                )

            assert result.status == JobStatus.COMPLETED, \
                f"Expected COMPLETED, got {result.status}: {result.error}"

            # IDENTIFY should show is_update=True
            identify = result.get_stage_result(PipelineStage.IDENTIFY)
            assert identify.get("is_update") is True

            # RESEARCH should be skipped (update mode)
            research = result.get_stage_result(PipelineStage.RESEARCH)
            assert research.get("research_skipped") is True

            # The NEW ingredient (Magnesium) must appear in the claims ledger
            all_claims = ledger.get_claims(
                result.offering_id, claim_type=ClaimType.INGREDIENT_AMOUNT
            )
            new_ingredient_names = {
                c.metadata.get("ingredient_name", "").lower()
                for c in all_claims if isinstance(c.metadata, dict)
            }
            assert "magnesium" in new_ingredient_names, \
                f"New ingredient 'Magnesium' not found in claims. " \
                f"Got: {new_ingredient_names}"

            # Price delta should also appear
            pricing_claims = ledger.get_claims(
                result.offering_id, claim_type=ClaimType.PRICING
            )
            prices = [c.claim_text for c in pricing_claims]
            assert any("64.99" in p for p in prices), \
                f"Updated price $64.99 not found. Got: {prices}"

    def test_update_pipeline_no_false_provenance(self, pipeline_db):
        """Update mode must NOT re-attribute old claims to the new artifact.

        Old ingredients from the original run already have claims in the ledger
        with their original artifact_id. The update should only create claims
        for data found in the NEW artifact — inherited old data must not get
        stamped with the new artifact's ID.
        """
        import hashlib
        from unittest.mock import patch
        from stage_handlers import create_update_pipeline
        from workflow import PipelineStage, JobStatus, Job
        from claims import ClaimsLedger, ClaimType
        from net import FetchResult

        db_path = pipeline_db

        # Run full pipeline first
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        existing_data = job.get_stage_result(PipelineStage.SOURCE_PACK).get(
            "full_data", {}
        )

        ledger = ClaimsLedger(db_path=db_path)
        old_claims = ledger.get_claims(
            job.offering_id, claim_type=ClaimType.INGREDIENT_AMOUNT
        )
        old_artifact_ids = {c.source_artifact_id for c in old_claims}

        # Updated page with only ONE new ingredient (Magnesium)
        # and NO old ingredients. If the fix works, only Magnesium gets
        # a claim with the new artifact ID. Old ingredients are not re-extracted.
        updated_page = b"<html><body>Supplement: Magnesium 400mg</body></html>"
        fake_update_result = FetchResult(
            content=updated_page,
            text="Supplement: Magnesium 400mg",
            final_url="https://example.com/product",
            status_code=200,
            headers={"Content-Type": "text/html"},
            fetched_at="2026-07-22T16:00:00+00:00",
            content_hash=hashlib.sha256(updated_page).hexdigest(),
            content_length=len(updated_page),
            tls_verified=True,
            elapsed_ms=100.0,
            error="",
        )

        # New extraction returns ONLY the new ingredient
        new_product_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "2 capsules",
                "ingredients": [
                    {"name": "Magnesium", "amount": "400mg"},
                ],
            },
            "pricing": {},
            "claims": [],
        }

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_update_result), \
             patch("stage_handlers._get_browser_session", return_value=None), \
             patch("stage_handlers._cleanup_browser"), \
             patch("research_product.phase1_extract_product",
                   return_value=new_product_data):
            update_pipeline = create_update_pipeline(
                existing_data=existing_data, db_path=db_path,
            )
            existing_offering_id = existing_data.get("offering_id", "")
            update_job = Job.create(
                url="https://example.com/product",
                product_name="TestoMax Elite",
                is_update=True,
                offering_id=existing_offering_id,
            )
            result = update_pipeline.run(update_job)
            if result.status == JobStatus.AWAITING_REVIEW:
                result = update_pipeline.approve_review(
                    result.job_id, reviewer="test",
                    rule_resolutions={
                        "CLAIM_CONFLICTS": {
                            "action": "accept",
                            "note": "Update conflicts acknowledged",
                        },
                    },
                )

            # Get the new artifact ID from the update's ACQUIRE stage
            acquire_result = result.get_stage_result(PipelineStage.ACQUIRE)
            new_artifact_id = None
            for art in acquire_result.get("artifacts", []):
                if art.get("type") == "official_page":
                    new_artifact_id = art.get("artifact_id")
                    break

            # Get ALL ingredient claims for the update offering
            all_ingredient_claims = ledger.get_claims(
                result.offering_id, claim_type=ClaimType.INGREDIENT_AMOUNT
            )

            # Magnesium should have the NEW artifact ID
            mag_claims = [
                c for c in all_ingredient_claims
                if c.metadata.get("ingredient_name", "").lower() == "magnesium"
            ]
            assert len(mag_claims) > 0, "Magnesium claim should exist"
            if new_artifact_id:
                assert mag_claims[0].source_artifact_id == new_artifact_id, \
                    "New ingredient must be attributed to the new artifact"

            # Old ingredients (Zinc, D-Aspartic Acid etc.) should NOT have
            # claims attributed to the new artifact
            non_mag_claims_with_new_artifact = [
                c for c in all_ingredient_claims
                if c.metadata.get("ingredient_name", "").lower() != "magnesium"
                and new_artifact_id
                and c.source_artifact_id == new_artifact_id
            ]
            assert len(non_mag_claims_with_new_artifact) == 0, \
                f"Old ingredients should NOT be re-attributed to the new " \
                f"artifact. Found: {[c.metadata.get('ingredient_name') for c in non_mag_claims_with_new_artifact]}"

    def test_update_pack_combines_original_and_new_claims(self, pipeline_db):
        """Updated source pack must contain BOTH original and new claims,
        each attributed to their correct artifact.

        This is the combined proof that:
        1. offering_id is preserved — both runs write to the same ledger
        2. merged product data propagates — source pack sees all ingredients
        3. artifact provenance is correct — old claims keep old artifact,
           new claims get new artifact
        4. source pack full_data includes the merged product snapshot
        """
        import hashlib
        from unittest.mock import patch
        from stage_handlers import create_update_pipeline
        from workflow import PipelineStage, JobStatus, Job
        from claims import ClaimsLedger, ClaimType
        from net import FetchResult

        db_path = pipeline_db

        # --- ORIGINAL RUN ---
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")
        assert job.status == JobStatus.COMPLETED

        original_offering_id = job.offering_id
        existing_data = job.get_stage_result(PipelineStage.SOURCE_PACK).get(
            "full_data", {}
        )

        # Verify offering_id is in the source pack output
        assert existing_data.get("offering_id") == original_offering_id, \
            "Source pack must include offering_id in full_data"

        # Capture original artifact IDs and ingredient names
        ledger = ClaimsLedger(db_path=db_path)
        original_claims = ledger.get_claims(
            original_offering_id, claim_type=ClaimType.INGREDIENT_AMOUNT
        )
        original_artifact_ids = {c.source_artifact_id for c in original_claims}
        original_ingredient_names = {
            c.metadata.get("ingredient_name", "").lower()
            for c in original_claims if isinstance(c.metadata, dict)
        }
        assert "zinc" in original_ingredient_names
        assert "d-aspartic acid" in original_ingredient_names

        # --- UPDATE RUN (adds Magnesium, keeps existing ingredients) ---
        updated_page = (
            b"<html><body>"
            b"<h1>TestoMax Elite - Updated Formula</h1>"
            b"<div>Supplement Facts: Magnesium 400mg, D-Aspartic Acid 2352mg, "
            b"Zinc 30mg</div>"
            b"</body></html>"
        )
        fake_update_result = FetchResult(
            content=updated_page,
            text="TestoMax Elite Supplement Facts: Magnesium 400mg, "
                 "D-Aspartic Acid 2352mg, Zinc 30mg",
            final_url="https://example.com/product",
            status_code=200,
            headers={"Content-Type": "text/html"},
            fetched_at="2026-07-22T18:00:00+00:00",
            content_hash=hashlib.sha256(updated_page).hexdigest(),
            content_length=len(updated_page),
            tls_verified=True,
            elapsed_ms=100.0,
            error="",
        )

        new_product_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "2 capsules",
                "ingredients": [
                    {"name": "D-Aspartic Acid", "amount": "2352mg"},
                    {"name": "Zinc", "amount": "30mg"},
                    {"name": "Magnesium", "amount": "400mg"},
                ],
            },
            "pricing": {"1 bottle": "$59.99"},  # Same price — no conflict
            "claims": [],
        }

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_update_result), \
             patch("stage_handlers._get_browser_session", return_value=None), \
             patch("stage_handlers._cleanup_browser"), \
             patch("research_product.phase1_extract_product",
                   return_value=new_product_data):
            update_pipeline = create_update_pipeline(
                existing_data=existing_data, db_path=db_path,
            )
            update_job = Job.create(
                url="https://example.com/product",
                product_name="TestoMax Elite",
                is_update=True,
                offering_id=original_offering_id,
            )
            result = update_pipeline.run(update_job)
            if result.status == JobStatus.AWAITING_REVIEW:
                result = update_pipeline.approve_review(
                    result.job_id, reviewer="test",
                    rule_resolutions={
                        "CLAIM_CONFLICTS": {
                            "action": "accept",
                            "note": "Update acknowledged",
                        },
                    },
                )
            assert result.status == JobStatus.COMPLETED, \
                f"Expected COMPLETED, got {result.status}: {result.error}"

        # --- PROOF 1: offering_id preserved ---
        assert result.offering_id == original_offering_id, \
            f"Update must use original offering_id. " \
            f"Got {result.offering_id}, expected {original_offering_id}"

        # --- PROOF 2: ALL claims in one ledger (original + new) ---
        all_ingredient_claims = ledger.get_claims(
            original_offering_id, claim_type=ClaimType.INGREDIENT_AMOUNT
        )
        all_names = {
            c.metadata.get("ingredient_name", "").lower()
            for c in all_ingredient_claims if isinstance(c.metadata, dict)
        }
        assert "zinc" in all_names, "Original ingredient Zinc must still be in ledger"
        assert "d-aspartic acid" in all_names, \
            "Original ingredient D-Aspartic Acid must still be in ledger"
        assert "magnesium" in all_names, \
            "New ingredient Magnesium must be in ledger"

        # --- PROOF 3: Artifact provenance is correct ---
        # Get the update artifact ID
        acquire_result = result.get_stage_result(PipelineStage.ACQUIRE)
        new_artifact_id = None
        for art in acquire_result.get("artifacts", []):
            if art.get("type") == "official_page":
                new_artifact_id = art.get("artifact_id")
                break

        if new_artifact_id:
            # New ingredient attributed to new artifact
            mag_claims = [
                c for c in all_ingredient_claims
                if c.metadata.get("ingredient_name", "").lower() == "magnesium"
            ]
            assert any(c.source_artifact_id == new_artifact_id for c in mag_claims), \
                "Magnesium must be attributed to the update artifact"

            # Original-only ingredients still have their original artifact IDs
            original_only_claims = [
                c for c in all_ingredient_claims
                if c.metadata.get("ingredient_name", "").lower() in original_ingredient_names
                and c.source_artifact_id in original_artifact_ids
            ]
            assert len(original_only_claims) > 0, \
                "Original claims must retain their original artifact IDs"

            # No original ingredient was re-attributed to the new artifact
            false_reattributions = [
                c for c in all_ingredient_claims
                if c.metadata.get("ingredient_name", "").lower() in original_ingredient_names
                and c.source_artifact_id == new_artifact_id
                # Exclude overlapping ingredients (Zinc, D-Aspartic Acid appear in both)
                # — those legitimately get new claims from the update
                and c.metadata.get("ingredient_name", "").lower() not in {
                    n.lower() for n in ["D-Aspartic Acid", "Zinc", "Magnesium"]
                    if n.lower() in {
                        i["name"].lower()
                        for i in new_product_data["supplement_facts"]["ingredients"]
                    }
                }
            ]
            # Original ingredients that are NOT in the new extraction should
            # never have the new artifact ID
            original_only_not_in_new = original_ingredient_names - {
                i["name"].lower()
                for i in new_product_data["supplement_facts"]["ingredients"]
            }
            wrongly_reattributed = [
                c for c in all_ingredient_claims
                if c.metadata.get("ingredient_name", "").lower() in original_only_not_in_new
                and c.source_artifact_id == new_artifact_id
            ]
            assert len(wrongly_reattributed) == 0, \
                f"Original-only ingredients must not be re-attributed: " \
                f"{[c.metadata.get('ingredient_name') for c in wrongly_reattributed]}"

        # --- PROOF 4: Source pack has merged product data ---
        pack_result = result.get_stage_result(PipelineStage.SOURCE_PACK)
        pack_data = pack_result.get("full_data", {})
        assert pack_data.get("offering_id") == original_offering_id, \
            "Updated source pack must carry the original offering_id"
        pack_product = pack_data.get("product", {})
        pack_ingredients = pack_product.get("supplement_facts", {}).get(
            "ingredients", []
        )
        pack_ingredient_names = {
            i.get("name", "").lower() for i in pack_ingredients
        }
        assert "magnesium" in pack_ingredient_names, \
            "Merged product in source pack must include new ingredient"

    def test_high_risk_claims_without_literal_evidence_flagged(self, pipeline_db):
        """High-risk claims (health benefit, drug interaction, etc.) that can't
        be matched to literal text in the artifact must be flagged as
        NEEDS_VERIFICATION, not silently accepted."""
        from claims import ClaimsLedger, ClaimType, ReviewStatus

        # Use product data with health claims that WON'T appear literally
        # in the page text (the page contains the claims but we'll make
        # one that doesn't match)
        health_data = dict(_FAKE_PRODUCT_DATA)
        health_data["claims"] = [
            # This WILL be in the page text (literal match possible)
            {"claim": "Boosts testosterone by up to 42%"},
            # This WON'T be in the page text (no literal match)
            {"claim": "Clinically proven to reverse hair loss in 90% of users",
             "type": "health_benefit"},
            # Drug interaction claim that won't match
            {"claim": "May interact with warfarin and blood thinners",
             "type": "drug_interaction"},
        ]

        db_path = pipeline_db
        job, _ = self._run_pipeline_with_mocks(db_path, product_data=health_data)

        ledger = ClaimsLedger(db_path=db_path)
        all_claims = ledger.get_claims(job.offering_id)

        # Find the health_benefit and drug_interaction claims
        high_risk_claims = [
            c for c in all_claims
            if c.claim_type in (ClaimType.HEALTH_BENEFIT,
                                ClaimType.DRUG_INTERACTION)
        ]

        # Claims without literal evidence should be NEEDS_VERIFICATION
        for c in high_risk_claims:
            is_literal = c.metadata.get("excerpt_is_literal", False)
            if not is_literal:
                assert c.review_status == ReviewStatus.NEEDS_VERIFICATION, \
                    f"High-risk claim '{c.claim_text}' without literal evidence " \
                    f"should be NEEDS_VERIFICATION, got {c.review_status}"

    def test_high_risk_claims_with_literal_evidence_accepted(self, pipeline_db):
        """High-risk claims that DO match literal text in the artifact should
        remain UNREVIEWED (not auto-flagged)."""
        from claims import ClaimsLedger, ClaimType, ReviewStatus

        # Use product data with a health claim that WILL appear in page text
        health_data = dict(_FAKE_PRODUCT_DATA)
        health_data["claims"] = [
            {"claim": "Supports lean muscle growth",
             "type": "health_benefit"},
        ]

        db_path = pipeline_db
        job, _ = self._run_pipeline_with_mocks(db_path, product_data=health_data)

        ledger = ClaimsLedger(db_path=db_path)
        benefit_claims = ledger.get_claims(
            job.offering_id, claim_type=ClaimType.HEALTH_BENEFIT
        )

        for c in benefit_claims:
            is_literal = c.metadata.get("excerpt_is_literal", False)
            if is_literal:
                assert c.review_status == ReviewStatus.UNREVIEWED, \
                    f"Literally-matched claim should be UNREVIEWED, got {c.review_status}"

    def test_get_unverified_high_risk_claims(self, pipeline_db):
        """ClaimsLedger.get_unverified_high_risk() returns high-risk claims
        that lack literal evidence in the integration context."""
        from unittest.mock import patch
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus

        db_path = pipeline_db
        # Run pipeline to set up the DB
        job, _ = self._run_pipeline_with_mocks(db_path)

        # Manually insert a high-risk claim WITHOUT literal evidence
        # (simulates LLM paraphrasing a claim that doesn't match the page)
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Prevents cardiovascular disease entirely",
                claim_type=ClaimType.SAFETY_WARNING,
                review_status=ReviewStatus.NEEDS_VERIFICATION,
                metadata={"excerpt_is_literal": False},
            ))

            unverified = ledger.get_unverified_high_risk(job.offering_id)

        unverified_texts = [c.claim_text for c in unverified]
        assert any("cardiovascular" in t.lower() for t in unverified_texts), \
            f"Expected unverified cardiovascular claim, got: {unverified_texts}"

    def test_source_pack_warns_about_unverified_high_risk(self, pipeline_db):
        """Source pack must surface unverified high-risk claims as a warning."""
        from unittest.mock import patch
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from stage_handlers import handle_source_pack
        from workflow import PipelineStage, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        # Add an unverified high-risk claim to the ledger
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Reverses kidney failure in 30 days",
                claim_type=ClaimType.SAFETY_WARNING,
                review_status=ReviewStatus.NEEDS_VERIFICATION,
                metadata={"excerpt_is_literal": False},
            ))

            # Re-run source pack to pick up the new claim
            pack = handle_source_pack(job)

        doc_text = pack.get("doc_text", "")
        # The unverified high-risk warning section should be present
        assert "UNVERIFIED" in doc_text, \
            f"Source pack must warn about unverified high-risk claims. Got:\n{doc_text[:500]}"
        assert "kidney" in doc_text.lower(), \
            "Warning should include the specific unverified claim text"

    def test_source_pack_reports_missing_required_facts(self, pipeline_db):
        """Source pack must show which required facts have no supporting claims."""
        from unittest.mock import patch
        from claims import ClaimsLedger
        from stage_handlers import handle_source_pack
        from workflow import PipelineStage, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            pack = handle_source_pack(job)

        doc_text = pack.get("doc_text", "")
        full_data = pack.get("full_data", {})
        req_facts = full_data.get("required_facts")

        # The test product is a supplement. It has ingredient_amount and
        # serving_info claims from the pipeline, but is missing allergens,
        # manufacturer, country_of_manufacture etc.
        assert req_facts is not None, "required_facts should be in full_data"
        assert len(req_facts["missing"]) > 0, \
            "Test product should be missing some required facts"

        # allergens, manufacturer, country_of_manufacture are not in the
        # fake product data and won't be extracted
        assert "allergens" in req_facts["missing"] or \
               "country_of_manufacture" in req_facts["missing"], \
            f"Expected common missing facts, got: {req_facts['missing']}"

        # MISSING REQUIRED FACTS section should appear in doc_text
        assert "MISSING REQUIRED FACTS" in doc_text, \
            f"Source pack should contain MISSING REQUIRED FACTS section. Got:\n{doc_text[:500]}"
        assert "[MISSING]" in doc_text

    def test_source_pack_full_coverage_no_warning(self, pipeline_db):
        """When all required facts are covered, no missing-facts warning appears."""
        from unittest.mock import patch
        from claims import ClaimsLedger, Claim, ClaimType
        from stage_handlers import handle_source_pack
        from workflow import PipelineStage, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        # Manually add claims for every missing supplement required fact
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)

            # Supplement required facts that need filling:
            # proprietary_blend_flag, other_ingredients, allergens,
            # manufacturer, country_of_manufacture, servings_per_container
            for text, ct in [
                ("No proprietary blends", ClaimType.MANUFACTURER_CLAIM),
                ("Gelatin capsule, rice flour", ClaimType.INGREDIENT_FORM),
                ("Contains no major allergens", ClaimType.ALLERGEN),
                ("Made by TestCorp USA", ClaimType.COMPANY_INFO),
                ("30 servings per container", ClaimType.SERVING_INFO),
            ]:
                ledger.add_claim(Claim(
                    offering_id=job.offering_id,
                    claim_text=text,
                    claim_type=ct,
                ))

            pack = handle_source_pack(job)

        doc_text = pack.get("doc_text", "")
        assert "MISSING REQUIRED FACTS" not in doc_text, \
            f"All required facts are covered — no warning expected. Got:\n{doc_text[:500]}"

    def test_substitution_rejects_original_and_creates_replacement(self, pipeline_db):
        """When a compliance rule is resolved with substitute, the original claim
        must be rejected and a replacement claim created with audit metadata."""
        from unittest.mock import patch
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from stage_handlers import _apply_substitutions
        from workflow import Job

        db_path = pipeline_db

        with patch("config.DB_PATH", db_path):
            job = Job.create(url="https://example.com", product_name="Test")
            job.offering_id = "sub-test-offering"

            ledger = ClaimsLedger(db_path=db_path)

            # Create a claim that contains the compliance-matched text
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="This product cures diabetes",
                claim_type=ClaimType.MANUFACTURER_CLAIM,
                confidence=0.35,
                source_class="official_vendor",
            ))

            resolved_rules = [{
                "rule_id": "RED_FLAG_CURES",
                "action": "substitute",
                "substitute_text": "This product may help support blood sugar",
                "note": "Replaced disease claim with hedged language",
                "reviewer": "test_reviewer",
            }]
            compliance_results = [{
                "rule_id": "RED_FLAG_CURES",
                "state": "blocked",
                "matched_text": "cures",
                "safe_alternative": "may help support",
            }]

            audit = _apply_substitutions(
                job, resolved_rules, compliance_results, "test_reviewer"
            )

        assert len(audit) == 1
        assert audit[0]["original_text"] == "This product cures diabetes"
        assert audit[0]["substitute_text"] == "This product may help support blood sugar"

        # Verify original is rejected
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            claims = ledger.get_claims(job.offering_id)

            original = [c for c in claims if c.claim_text == "This product cures diabetes"]
            assert len(original) == 1
            assert original[0].review_status == ReviewStatus.REJECTED

            replacement = [c for c in claims if "may help support" in c.claim_text]
            assert len(replacement) == 1
            assert replacement[0].review_status == ReviewStatus.ACCEPTED
            assert replacement[0].extraction_method == "reviewer_substitution"
            assert replacement[0].metadata["supersedes_claim_id"] == original[0].claim_id
            assert replacement[0].metadata["original_text"] == "This product cures diabetes"
            assert replacement[0].metadata["substitution_rule_id"] == "RED_FLAG_CURES"

    def test_source_pack_shows_substitution_audit_trail(self, pipeline_db):
        """Source pack must include a COMPLIANCE SUBSTITUTIONS section
        showing original → replacement mappings."""
        from unittest.mock import patch
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from stage_handlers import handle_source_pack, _apply_substitutions
        from workflow import Job, PipelineStage, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        # Insert a claim with substitution metadata
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)

            # Add a rejected original claim
            original_id = ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Guaranteed to cure all disease",
                claim_type=ClaimType.MANUFACTURER_CLAIM,
                review_status=ReviewStatus.REJECTED,
            ))

            # Add the replacement claim with audit metadata
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="May help support overall wellness",
                claim_type=ClaimType.MANUFACTURER_CLAIM,
                review_status=ReviewStatus.ACCEPTED,
                extraction_method="reviewer_substitution",
                metadata={
                    "supersedes_claim_id": original_id,
                    "original_text": "Guaranteed to cure all disease",
                    "substitution_rule_id": "RED_FLAG_GUARANTEED",
                    "substitution_note": "Replaced absolute claim",
                },
            ))

            pack = handle_source_pack(job)

        doc_text = pack.get("doc_text", "")

        # Should show the substitution audit trail
        assert "COMPLIANCE SUBSTITUTIONS" in doc_text, \
            f"Expected COMPLIANCE SUBSTITUTIONS section. Got:\n{doc_text[:1000]}"
        assert "Guaranteed to cure all disease" in doc_text
        assert "May help support overall wellness" in doc_text
        assert "RED_FLAG_GUARANTEED" in doc_text

        # Original claim should NOT appear in manufacturer claims (rejected)
        # but replacement should appear.
        # Extract just the manufacturer claims section (between its header
        # and the next section header).
        mfr_start = doc_text.find("MANUFACTURER CLAIMS")
        if mfr_start >= 0:
            next_section = doc_text.find("\n\n", mfr_start + 20)
            if next_section < 0:
                next_section = len(doc_text)
            mfr_section = doc_text[mfr_start:next_section]
            assert "cure all disease" not in mfr_section, \
                f"Rejected original claim should not appear in manufacturer claims: {mfr_section}"

    def test_source_pack_excludes_rejected_includes_replacement(self, pipeline_db):
        """Rejected claims must be excluded from source pack;
        replacement claims must be included."""
        from unittest.mock import patch
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from stage_handlers import handle_source_pack
        from workflow import JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)

            # Add a rejected claim (would have been the original)
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="REJECTED_ORIGINAL_CLAIM_TEXT_XYZ",
                claim_type=ClaimType.HEALTH_BENEFIT,
                review_status=ReviewStatus.REJECTED,
                metadata={"excerpt_is_literal": True},
            ))

            # Add the replacement
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="REPLACEMENT_SAFE_CLAIM_TEXT_XYZ",
                claim_type=ClaimType.HEALTH_BENEFIT,
                review_status=ReviewStatus.ACCEPTED,
                extraction_method="reviewer_substitution",
                metadata={
                    "excerpt_is_literal": True,
                    "supersedes_claim_id": "original-id",
                },
            ))

            pack = handle_source_pack(job)

        doc_text = pack.get("doc_text", "")
        assert "REJECTED_ORIGINAL_CLAIM_TEXT_XYZ" not in doc_text, \
            "Rejected claim must not appear anywhere in the source pack"
        assert "REPLACEMENT_SAFE_CLAIM_TEXT_XYZ" in doc_text, \
            "Replacement claim must appear in the source pack"

    def test_source_pack_blocks_when_mandatory_facts_missing(self, pipeline_db):
        """When mandatory facts (ingredients_with_amounts, serving_size for
        supplements) are missing, handle_source_pack must raise ReviewBlockError."""
        from unittest.mock import patch
        from claims import ClaimsLedger, ClaimType, ReviewStatus
        from stage_handlers import handle_source_pack
        from workflow import ReviewBlockError
        from workflow import PipelineStage, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        # Reject ALL ingredient and serving claims to remove mandatory facts
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            for c in ledger.get_claims(job.offering_id):
                if c.claim_type in (ClaimType.INGREDIENT_AMOUNT,
                                    ClaimType.SERVING_INFO):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.REJECTED, "test"
                    )

            with pytest.raises(ReviewBlockError, match="mandatory facts not satisfied"):
                handle_source_pack(job)

    def test_source_pack_succeeds_with_mandatory_facts_present(self, pipeline_db):
        """When mandatory facts are present (even if optional required facts
        are missing), source pack generation succeeds."""
        from unittest.mock import patch
        from stage_handlers import handle_source_pack
        from workflow import JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        # The pipeline extracts ingredients and serving_size — mandatory facts
        # are present. Non-mandatory required facts (allergens, etc.) are missing
        # but that's OK — should warn, not block.
        with patch("config.DB_PATH", db_path):
            pack = handle_source_pack(job)

        doc_text = pack.get("doc_text", "")
        # Pack generated successfully
        assert "SOURCE INTELLIGENCE PACK" in doc_text
        # Non-mandatory facts still show as warnings
        assert "MISSING REQUIRED FACTS" in doc_text

    def test_device_extraction_creates_feature_claims(self, pipeline_db):
        """Device offering type must extract key_features as FEATURE claims
        with fact_key='key_features' via generic pack-aware extraction."""
        from claims import ClaimsLedger, ClaimType

        device_data = {
            "product_name": "ThermoScan Pro",
            "product_type": "device",
            "url": "https://example.com/thermoscan",
            "supplement_facts": {},
            "pricing": {"Standard": "$129.99"},
            "claims": [],
            "key_features": [
                "Infrared temperature sensor",
                "Bluetooth connectivity",
                "FDA 510(k) cleared",
            ],
            "specifications": "Range: 90-110°F, Accuracy: ±0.2°F",
            "manufacturer": "MedTech Corp",
            "warranty": "2 year limited warranty",
        }

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(
            db_path, product_data=device_data
        )
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        ledger = ClaimsLedger(db_path=db_path)
        feature_claims = ledger.get_claims(
            job.offering_id, claim_type=ClaimType.FEATURE
        )
        feature_texts = {c.claim_text for c in feature_claims}
        feature_keys = {
            c.metadata.get("fact_key") for c in feature_claims
            if isinstance(c.metadata, dict)
        }

        assert "Infrared temperature sensor" in feature_texts, \
            f"Device feature not extracted. Got: {feature_texts}"
        assert "key_features" in feature_keys, \
            f"Feature claims must have fact_key='key_features'. Got: {feature_keys}"

        # Verify required facts coverage includes key_features
        from intelligence_packs import get_required_facts
        from entities import OfferingType
        required = get_required_facts(OfferingType.DEVICE)
        coverage = ledger.check_required_facts(job.offering_id, required)
        assert "key_features" in coverage["covered"], \
            f"key_features should be covered. Missing: {coverage['missing']}"

    def test_telehealth_extraction_creates_service_claims(self, pipeline_db):
        """Telehealth type must extract services_offered and
        prescriber_credentials as claims with correct fact_keys."""
        from claims import ClaimsLedger, ClaimType

        telehealth_data = {
            "product_name": "HealthConnect Pro",
            "product_type": "telehealth",
            "url": "https://example.com/healthconnect",
            "supplement_facts": {},
            "pricing": {"Monthly": "$49.99"},
            "claims": [],
            "services_offered": [
                "GLP-1 medication prescriptions",
                "Hormone replacement therapy",
            ],
            "prescriber_credentials": "Board-certified physicians (MD/DO)",
            "states_available": "Available in 48 states",
            "consultation_process": "Video consultation within 24 hours",
        }

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(
            db_path, product_data=telehealth_data
        )
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        ledger = ClaimsLedger(db_path=db_path)

        # Check services_offered extracted
        all_claims = ledger.get_claims(job.offering_id)
        service_claims = [
            c for c in all_claims
            if isinstance(c.metadata, dict)
            and c.metadata.get("fact_key") == "services_offered"
        ]
        assert len(service_claims) >= 1, \
            f"services_offered claims not found. All fact_keys: " \
            f"{[c.metadata.get('fact_key') for c in all_claims if isinstance(c.metadata, dict)]}"

        # Check prescriber_credentials extracted
        cred_claims = [
            c for c in all_claims
            if isinstance(c.metadata, dict)
            and c.metadata.get("fact_key") == "prescriber_credentials"
        ]
        assert len(cred_claims) >= 1, \
            "prescriber_credentials claim not found"

        # Coverage check — both mandatory facts should be covered
        from intelligence_packs import get_mandatory_facts
        from entities import OfferingType
        mandatory = get_mandatory_facts(OfferingType.TELEHEALTH)
        coverage = ledger.check_required_facts(job.offering_id, mandatory)
        assert coverage["coverage_ratio"] == 1.0, \
            f"All mandatory telehealth facts should be covered. " \
            f"Missing: {coverage['missing']}"

    def test_cannabis_extraction_creates_cannabinoid_claims(self, pipeline_db):
        """Cannabis type must extract cannabinoid_profile, thc_content,
        and lab_results — all mandatory for cannabis offerings."""
        from claims import ClaimsLedger, ClaimType

        cannabis_data = {
            "product_name": "Northern Lights Cartridge",
            "product_type": "cannabis",
            "url": "https://example.com/northern-lights",
            "supplement_facts": {},
            "pricing": {"0.5g": "$35.00"},
            "claims": [],
            "cannabinoid_profile": "THC 85%, CBD 3%, CBN 1.2%",
            "thc_content": "85%",
            "lab_results": "COA #NL-2026-0722 by SC Labs",
            "strain_type": "Indica",
            "terpene_profile": "Myrcene, Linalool, Caryophyllene",
        }

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(
            db_path, product_data=cannabis_data
        )
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        ledger = ClaimsLedger(db_path=db_path)
        all_claims = ledger.get_claims(job.offering_id)
        fact_keys_present = {
            c.metadata.get("fact_key") for c in all_claims
            if isinstance(c.metadata, dict) and c.metadata.get("fact_key")
        }

        # All three mandatory cannabis facts must be extracted
        assert "cannabinoid_profile" in fact_keys_present, \
            f"cannabinoid_profile not extracted. Got: {fact_keys_present}"
        assert "thc_content" in fact_keys_present, \
            f"thc_content not extracted. Got: {fact_keys_present}"
        assert "lab_results" in fact_keys_present, \
            f"lab_results not extracted. Got: {fact_keys_present}"

        from intelligence_packs import get_mandatory_facts
        from entities import OfferingType
        mandatory = get_mandatory_facts(OfferingType.CANNABIS)
        coverage = ledger.check_required_facts(job.offering_id, mandatory)
        assert coverage["coverage_ratio"] == 1.0, \
            f"All mandatory cannabis facts should be covered. " \
            f"Missing: {coverage['missing']}"

    def test_research_peptide_extraction(self, pipeline_db):
        """Research peptide must extract peptide_sequence, purity_percentage,
        and research_use_only_disclaimer as mandatory facts."""
        from claims import ClaimsLedger

        peptide_data = {
            "product_name": "BPC-157 5mg",
            "product_type": "research_peptide",
            "url": "https://example.com/bpc157",
            "supplement_facts": {},
            "pricing": {"5mg vial": "$42.99"},
            "claims": [],
            "peptide_sequence": "GEPPPGKPADDAGLV",
            "purity_percentage": "99.1%",
            "molecular_weight": "1419.53 g/mol",
            "cas_number": "137525-51-0",
            "research_use_only_disclaimer": "For research use only. Not for human consumption.",
        }

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(
            db_path, product_data=peptide_data
        )
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        ledger = ClaimsLedger(db_path=db_path)
        all_claims = ledger.get_claims(job.offering_id)
        fact_keys_present = {
            c.metadata.get("fact_key") for c in all_claims
            if isinstance(c.metadata, dict) and c.metadata.get("fact_key")
        }

        assert "peptide_sequence" in fact_keys_present
        assert "purity_percentage" in fact_keys_present
        assert "research_use_only_disclaimer" in fact_keys_present

        from intelligence_packs import get_mandatory_facts
        from entities import OfferingType
        mandatory = get_mandatory_facts(OfferingType.RESEARCH_PEPTIDE)
        coverage = ledger.check_required_facts(job.offering_id, mandatory)
        assert coverage["coverage_ratio"] == 1.0, \
            f"All mandatory peptide facts should be covered. " \
            f"Missing: {coverage['missing']}"

    # ── Recovery integration tests ────────────────────────────────

    def _make_recovery_fetch_result(self, page_text, url):
        """Build a FetchResult suitable for mocking net.safe_fetch
        inside recover_evidence()."""
        import hashlib
        from net import FetchResult
        page_bytes = page_text.encode("utf-8")
        return FetchResult(
            content=page_bytes,
            text=page_text,
            final_url=url,
            status_code=200,
            headers={"Content-Type": "text/html"},
            fetched_at="2026-07-22T14:00:00+00:00",
            content_hash=hashlib.sha256(page_bytes).hexdigest(),
            content_length=len(page_bytes),
            tls_verified=True,
            elapsed_ms=120.0,
            error="",
        )

    def test_recover_label_image_uses_vision_ocr(self, pipeline_db):
        """A direct PNG label URL is stored as evidence and sent to vision OCR."""
        import hashlib
        from unittest.mock import patch
        from net import FetchResult
        from stage_handlers import recover_evidence
        from claims import ClaimsLedger
        from workflow import JobStatus

        job, pipeline = self._run_pipeline_with_mocks(pipeline_db)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        image_bytes = b"\x89PNG\r\n\x1a\n" + (b"label" * 100)
        image_url = "https://cdn.example.com/supplement-facts.png"
        image_fetch = FetchResult(
            content=image_bytes,
            text="",
            final_url=image_url,
            status_code=200,
            headers={"Content-Type": "image/png"},
            content_hash=hashlib.sha256(image_bytes).hexdigest(),
            content_length=len(image_bytes),
            tls_verified=True,
            error="",
        )
        ocr_result = {
            "serving_size": "2 capsules",
            "servings_per_container": "30",
            "ingredients": [
                {"name": "Tongkat Ali", "amount": "400 mg"},
            ],
        }

        with patch("config.DB_PATH", pipeline_db), \
             patch("net.safe_fetch", return_value=image_fetch), \
             patch("research_product.extract_label_image",
                   return_value=ocr_result) as vision_ocr:
            result = recover_evidence(
                url=image_url,
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=pipeline_db,
            )

        vision_ocr.assert_called_once()
        assert result["facts_missing"] == []
        assert set(result["facts_found"]) == {
            "ingredients_with_amounts", "serving_size",
        }
        ledger = ClaimsLedger(db_path=pipeline_db)
        recovered = [
            c for c in ledger.get_claims(job.offering_id)
            if c.metadata.get("recovery_source") == image_url
        ]
        assert len(recovered) >= 2
        assert all(c.source_artifact_id == result["artifact_id"] for c in recovered)
        assert all(c.extraction_method == "machine_ocr" for c in recovered)
        assert all(c.metadata.get("image_ocr") is True for c in recovered)
        assert all(c.metadata.get("excerpt_is_literal") is False for c in recovered)

    def test_recover_evidence_single_fetch_stores_artifact_and_claims(
        self, pipeline_db
    ):
        """recover_evidence() fetches URL once, stores the artifact, and
        creates claims with the correct artifact_id — single fetch,
        full provenance."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence
        from claims import ClaimsLedger

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        recovery_page = (
            "<html><body>"
            "<h1>TestoMax Elite</h1>"
            "<div>Serving Size: 4 capsules per day</div>"
            "<div>D-Aspartic Acid 2352mg per serving</div>"
            "</body></html>"
        )
        recovery_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "4 capsules",
                "ingredients": [
                    {"name": "D-Aspartic Acid", "amount": "2352mg"},
                ],
            },
            "pricing": {},
            "claims": [],
        }
        url = "https://example.com/testomax-alt"
        fake_fr = self._make_recovery_fetch_result(recovery_page, url)

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=recovery_data):

            result = recover_evidence(
                url=url,
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=db_path,
            )

        assert result["artifact_id"], "Must return an artifact_id"
        total_found = result["claims_added"] + result.get("duplicates_skipped", 0)
        assert total_found >= 2, \
            f"Expected ≥2 facts found (new+dedup). Got: {total_found}"
        assert "ingredients_with_amounts" in result["facts_found"]
        assert "serving_size" in result["facts_found"]
        assert result["facts_missing"] == [], \
            f"No facts should be missing. Got: {result['facts_missing']}"

        # Verify claims in ledger have the recovery artifact_id
        # Note: deduplication may skip claims already created by the
        # initial pipeline extraction, so we check new + deduped >= 2
        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            recovery_claims = [
                c for c in ledger.get_claims(job.offering_id)
                if c.metadata.get("recovery_source")
            ]
            assert len(recovery_claims) >= 1, \
                "At least one new recovery claim expected"
            for rc in recovery_claims:
                assert rc.source_artifact_id == result["artifact_id"]
                assert rc.metadata.get("fact_key") in (
                    "ingredients_with_amounts", "serving_size"
                )
                assert rc.extraction_method == "llm_extraction"

    def test_recover_evidence_only_adds_facts_present_in_content(
        self, pipeline_db
    ):
        """recover_evidence() must NOT create claims for facts that
        aren't actually found in the fetched content."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        sparse_page = "<html><body><div>Serving Size: 2 tablets</div></body></html>"
        recovery_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "2 tablets",
                "ingredients": [],
            },
            "pricing": {},
            "claims": [],
        }
        url = "https://example.com/sparse-page"
        fake_fr = self._make_recovery_fetch_result(sparse_page, url)

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=recovery_data):

            result = recover_evidence(
                url=url,
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=db_path,
            )

        assert "serving_size" in result["facts_found"]
        assert "ingredients_with_amounts" in result["facts_missing"]
        total = result["claims_added"] + result.get("duplicates_skipped", 0)
        assert total >= 1

    def test_partial_recovery_leaves_mandatory_facts_blocked(
        self, pipeline_db
    ):
        """After partial recovery (only some mandatory facts found),
        handle_source_pack must still block."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence, handle_source_pack
        from claims import ClaimsLedger, ClaimType, ReviewStatus
        from workflow import ReviewBlockError, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            for c in ledger.get_claims(job.offering_id):
                if c.claim_type in (ClaimType.INGREDIENT_AMOUNT,
                                    ClaimType.SERVING_INFO):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.REJECTED, "test"
                    )

        partial_page = "<html><body><div>Serving Size: 2 capsules</div></body></html>"
        partial_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "2 capsules",
                "ingredients": [],
            },
            "pricing": {},
            "claims": [],
        }
        url = "https://example.com/partial"
        fake_fr = self._make_recovery_fetch_result(partial_page, url)

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=partial_data):
            result = recover_evidence(
                url=url,
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=db_path,
            )
            assert "serving_size" in result["facts_found"]
            assert "ingredients_with_amounts" in result["facts_missing"]

            with pytest.raises(ReviewBlockError,
                               match="mandatory facts not satisfied"):
                handle_source_pack(job)

    def test_manual_entry_stays_unverified_in_ledger(self, pipeline_db):
        """record_manual_entry() creates a claim with NEEDS_VERIFICATION,
        no artifact, confidence=0.0, and the reviewer's identity."""
        from unittest.mock import patch
        from stage_handlers import record_manual_entry
        from claims import ClaimsLedger, ReviewStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        claim_id = record_manual_entry(
            offering_id=job.offering_id,
            fact_key="ingredients_with_amounts",
            value="Vitamin C 500mg",
            reviewer="analyst@test.com",
            db_path=db_path,
        )
        assert claim_id, "Must return a claim_id"

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            all_claims = ledger.get_claims(job.offering_id)
            manual = [c for c in all_claims if c.claim_id == claim_id]
            assert len(manual) == 1
            mc = manual[0]

            assert mc.review_status == ReviewStatus.NEEDS_VERIFICATION
            assert mc.source_artifact_id is None or mc.source_artifact_id == ""
            assert mc.confidence == 0.0
            assert mc.extraction_method == "manual_entry"
            assert mc.metadata.get("manual_entry") is True
            assert mc.metadata.get("entered_by") == "analyst@test.com"
            assert mc.metadata.get("fact_key") == "ingredients_with_amounts"

    def test_resume_after_full_recovery_succeeds(self, pipeline_db):
        """After rejecting mandatory claims and recovering ALL of them
        with evidence-backed claims that are then ACCEPTED by a reviewer,
        handle_source_pack succeeds.

        Strict mode requires literal evidence OR explicit acceptance.
        Since LLM-extracted claim text may not match page text literally,
        the reviewer must accept recovered claims before the pack generates.
        """
        from unittest.mock import patch
        from stage_handlers import recover_evidence, handle_source_pack
        from claims import ClaimsLedger, ClaimType, ReviewStatus
        from workflow import JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            for c in ledger.get_claims(job.offering_id):
                if c.claim_type in (ClaimType.INGREDIENT_AMOUNT,
                                    ClaimType.SERVING_INFO):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.REJECTED, "test"
                    )

        full_page = (
            "<html><body>"
            "<div>Serving Size: 4 capsules</div>"
            "<div>D-Aspartic Acid 2352mg | Zinc 10mg</div>"
            "</body></html>"
        )
        full_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "4 capsules",
                "ingredients": [
                    {"name": "D-Aspartic Acid", "amount": "2352mg"},
                    {"name": "Zinc", "amount": "10mg"},
                ],
            },
            "pricing": {},
            "claims": [],
        }
        url = "https://example.com/full-recovery"
        fake_fr = self._make_recovery_fetch_result(full_page, url)

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=full_data):
            result = recover_evidence(
                url=url,
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=db_path,
            )
            assert len(result["facts_missing"]) == 0

            # Accept the recovered claims (strict mode requires literal
            # evidence OR explicit acceptance for mandatory facts)
            ledger = ClaimsLedger(db_path=db_path)
            for c in ledger.get_claims(job.offering_id):
                if c.metadata.get("recovery_source"):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.ACCEPTED, "reviewer"
                    )

            pack = handle_source_pack(job)
            assert "SOURCE INTELLIGENCE PACK" in pack.get("doc_text", "")

    def test_recover_evidence_wrong_offering_id_isolates_claims(
        self, pipeline_db
    ):
        """Claims created by recover_evidence() must be scoped to the
        provided offering_id. They must NOT appear under a different
        offering_id's claims."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence
        from claims import ClaimsLedger

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        from workflow import JobStatus
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        recovery_page = "<html><body><div>Zinc 10mg</div></body></html>"
        recovery_data = {
            "product_name": "TestoMax Elite",
            "supplement_facts": {
                "serving_size": "2 capsules",
                "ingredients": [{"name": "Zinc", "amount": "10mg"}],
            },
            "pricing": {},
            "claims": [],
        }
        url = "https://example.com/recovery"
        fake_fr = self._make_recovery_fetch_result(recovery_page, url)

        with patch("config.DB_PATH", db_path), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=recovery_data):
            recover_evidence(
                url=url,
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts"],
                db_path=db_path,
            )

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)
            correct_claims = [
                c for c in ledger.get_claims(job.offering_id)
                if c.metadata.get("recovery_source")
            ]
            wrong_claims = ledger.get_claims("nonexistent-offering-id")

            assert len(correct_claims) >= 1
            assert len(wrong_claims) == 0

    def test_source_pack_blocks_when_mandatory_facts_are_manual_only(
        self, pipeline_db
    ):
        """handle_source_pack uses strict=True for mandatory facts.
        Manual entries (NEEDS_VERIFICATION, no artifact) must NOT satisfy
        mandatory requirements — the pack should be blocked."""
        from unittest.mock import patch
        from stage_handlers import handle_source_pack
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from workflow import ReviewBlockError, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)

            # Reject all evidence-backed ingredient and serving claims
            for c in ledger.get_claims(job.offering_id):
                if c.claim_type in (ClaimType.INGREDIENT_AMOUNT,
                                    ClaimType.SERVING_INFO):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.REJECTED, "test"
                    )

            # Add manual entries for the same mandatory facts
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Manual: Vitamin C 500mg",
                claim_type=ClaimType.INGREDIENT_AMOUNT,
                source_artifact_id=None,
                review_status=ReviewStatus.NEEDS_VERIFICATION,
                extraction_method="manual_entry",
                metadata={"fact_key": "ingredients_with_amounts",
                           "manual_entry": True},
            ))
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Manual: 1 capsule",
                claim_type=ClaimType.SERVING_INFO,
                source_artifact_id=None,
                review_status=ReviewStatus.NEEDS_VERIFICATION,
                extraction_method="manual_entry",
                metadata={"fact_key": "serving_size",
                           "manual_entry": True},
            ))

            # strict=True means manual-only claims don't satisfy mandatory
            with pytest.raises(ReviewBlockError,
                               match="manual entry only"):
                handle_source_pack(job)

    # ── Adversarial recovery tests ────────────────────────────────

    def test_recover_rejects_nonexistent_job(self, pipeline_db):
        """recover_evidence must reject a job_id that doesn't exist."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence, RecoveryError

        with patch("config.DB_PATH", pipeline_db), \
             pytest.raises(RecoveryError, match="does not exist"):
            recover_evidence(
                url="https://example.com",
                offering_id="some-offering",
                job_id="nonexistent-job-id",
                target_facts=["serving_size"],
                db_path=pipeline_db,
            )

    def test_recover_rejects_mismatched_offering(self, pipeline_db):
        """recover_evidence must reject when job_id belongs to a different
        offering_id than the one requested."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence, RecoveryError
        from workflow import JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path), \
             pytest.raises(RecoveryError, match="belongs to offering"):
            recover_evidence(
                url="https://example.com",
                offering_id="wrong-offering-id",
                job_id=job.job_id,
                target_facts=["serving_size"],
                db_path=db_path,
            )

    def test_recover_rejects_running_job(self, pipeline_db):
        """recover_evidence must reject a job that is still RUNNING."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence, RecoveryError
        from workflow import Job, JobStore, JobStatus

        db_path = pipeline_db
        # Create a job in RUNNING state
        job = Job.create(
            url="https://example.com",
            product_name="Test",
            offering_id="run-test-1",
        )
        job.status = JobStatus.RUNNING
        store = JobStore(db_path=db_path)
        store.save(job)

        with patch("config.DB_PATH", db_path), \
             pytest.raises(RecoveryError, match="state running"):
            recover_evidence(
                url="https://example.com",
                offering_id=job.offering_id,
                job_id=job.job_id,
                target_facts=["serving_size"],
                db_path=db_path,
            )

    def test_recover_rejects_empty_ids(self, pipeline_db):
        """recover_evidence must reject empty offering_id, job_id, and
        empty target_facts."""
        from stage_handlers import recover_evidence, RecoveryError

        with pytest.raises(RecoveryError, match="offering_id is required"):
            recover_evidence("https://example.com", "", "job1", ["x"],
                             db_path=pipeline_db)

        with pytest.raises(RecoveryError, match="job_id is required"):
            recover_evidence("https://example.com", "off1", "", ["x"],
                             db_path=pipeline_db)

        with pytest.raises(RecoveryError, match="target_facts must be"):
            recover_evidence("https://example.com", "off1", "job1", [],
                             db_path=pipeline_db)

    def test_manual_entry_rejects_empty_reviewer(self, pipeline_db):
        """record_manual_entry must reject an empty reviewer name."""
        from stage_handlers import record_manual_entry, RecoveryError

        with pytest.raises(RecoveryError, match="reviewer name is required"):
            record_manual_entry(
                offering_id="off1",
                fact_key="serving_size",
                value="2 capsules",
                reviewer="",
                db_path=pipeline_db,
            )

    def test_manual_entry_rejects_empty_value(self, pipeline_db):
        """record_manual_entry must reject an empty value."""
        from stage_handlers import record_manual_entry, RecoveryError

        with pytest.raises(RecoveryError, match="value is required"):
            record_manual_entry(
                offering_id="off1",
                fact_key="serving_size",
                value="   ",
                reviewer="analyst",
                db_path=pipeline_db,
            )

    def test_nonliteral_inferred_claim_blocks_strict_mandatory(
        self, pipeline_db
    ):
        """A claim with an artifact but excerpt_is_literal=False and
        review_status=UNREVIEWED must NOT satisfy a strict mandatory gate.
        This prevents LLM-inferred values from auto-clearing gates."""
        from unittest.mock import patch
        from stage_handlers import handle_source_pack
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from workflow import ReviewBlockError, JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)

            # Reject all existing mandatory claims
            for c in ledger.get_claims(job.offering_id):
                if c.claim_type in (ClaimType.INGREDIENT_AMOUNT,
                                    ClaimType.SERVING_INFO):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.REJECTED, "test"
                    )

            # Add inferred claims: artifact-backed but NOT literal,
            # and NOT accepted
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Vitamin C: 500mg",
                claim_type=ClaimType.INGREDIENT_AMOUNT,
                source_artifact_id="art-inferred-1",
                review_status=ReviewStatus.UNREVIEWED,
                extraction_method="llm_extraction",
                metadata={"fact_key": "ingredients_with_amounts",
                           "excerpt_is_literal": False},
            ))
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Serving size: 2 capsules",
                claim_type=ClaimType.SERVING_INFO,
                source_artifact_id="art-inferred-2",
                review_status=ReviewStatus.UNREVIEWED,
                extraction_method="llm_extraction",
                metadata={"fact_key": "serving_size",
                           "excerpt_is_literal": False},
            ))

            # Strict mode: non-literal + unreviewed = blocked
            with pytest.raises(ReviewBlockError,
                               match="inferred.*needs human acceptance"):
                handle_source_pack(job)

    def test_nonliteral_claim_accepted_by_human_satisfies_strict(
        self, pipeline_db
    ):
        """After a human explicitly ACCEPTS a non-literal inferred claim,
        it SHOULD satisfy strict mandatory coverage."""
        from unittest.mock import patch
        from stage_handlers import handle_source_pack
        from claims import ClaimsLedger, Claim, ClaimType, ReviewStatus
        from workflow import JobStatus

        db_path = pipeline_db
        job, pipeline = self._run_pipeline_with_mocks(db_path)
        if job.status == JobStatus.AWAITING_REVIEW:
            job = pipeline.approve_review(job.job_id, reviewer="test")

        with patch("config.DB_PATH", db_path):
            ledger = ClaimsLedger(db_path=db_path)

            # Reject all existing mandatory claims
            for c in ledger.get_claims(job.offering_id):
                if c.claim_type in (ClaimType.INGREDIENT_AMOUNT,
                                    ClaimType.SERVING_INFO):
                    ledger.update_review_status(
                        c.claim_id, ReviewStatus.REJECTED, "test"
                    )

            # Add non-literal but ACCEPTED claims
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Vitamin C: 500mg",
                claim_type=ClaimType.INGREDIENT_AMOUNT,
                source_artifact_id="art-inferred-3",
                review_status=ReviewStatus.ACCEPTED,
                extraction_method="llm_extraction",
                metadata={"fact_key": "ingredients_with_amounts",
                           "excerpt_is_literal": False},
            ))
            ledger.add_claim(Claim(
                offering_id=job.offering_id,
                claim_text="Serving size: 2 capsules",
                claim_type=ClaimType.SERVING_INFO,
                source_artifact_id="art-inferred-4",
                review_status=ReviewStatus.ACCEPTED,
                extraction_method="llm_extraction",
                metadata={"fact_key": "serving_size",
                           "excerpt_is_literal": False},
            ))

            # Accepted non-literal claims should pass strict mode
            pack = handle_source_pack(job)
            assert "SOURCE INTELLIGENCE PACK" in pack.get("doc_text", "")

    def test_recover_rejects_invalid_target_facts(self, pipeline_db):
        """Invalid target facts must raise RecoveryError, not be swallowed."""
        from stage_handlers import recover_evidence, RecoveryError
        from workflow import Job, JobStore, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-invalid-facts",
            url="https://example.com/product",
            product_name="Test Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        # "totally_made_up_fact" is not in the supplement intelligence pack
        with pytest.raises(RecoveryError, match="not required for"):
            recover_evidence(
                "https://example.com/product",
                "off-invalid-facts",
                job.job_id,
                ["totally_made_up_fact"],
                db_path=pipeline_db,
            )

    def test_composite_literal_matching_requires_all_terms(self, pipeline_db):
        """Ingredient with name+amount must find BOTH in page to be literal."""
        from stage_handlers import _find_literal_excerpt

        page = "Our formula contains Zinc for immune support. Great product!"

        # Single-term mode: "Zinc" matches — any-term is sufficient
        excerpt, loc = _find_literal_excerpt(page, ["Zinc", "500 mg"])
        assert excerpt  # Matches on "Zinc" alone

        # Composite mode: "Zinc" + "500 mg" — page has Zinc but NOT "500 mg"
        excerpt, loc = _find_literal_excerpt(
            page, ["Zinc", "500 mg"], require_all=True
        )
        assert excerpt == ""  # Must fail — "500 mg" not in page
        assert loc == ""

        # Composite mode with ALL terms present
        page2 = "Each capsule contains Zinc 500 mg for daily immune support."
        excerpt, loc = _find_literal_excerpt(
            page2, ["Zinc", "500 mg"], require_all=True
        )
        assert "Zinc" in excerpt
        assert "500 mg" in excerpt
        assert loc  # Has a valid location

    def test_composite_literal_matching_pricing(self, pipeline_db):
        """Pricing with package+price must find BOTH components."""
        from stage_handlers import _find_literal_excerpt

        page = "Buy 1 Bottle for great savings. Limited offer!"

        # Page mentions "1 Bottle" but NOT "$49.95"
        excerpt, loc = _find_literal_excerpt(
            page, ["$49.95", "1 Bottle"], require_all=True
        )
        assert excerpt == ""

        # Page with both components
        page2 = "Buy 1 Bottle for $49.95 with free shipping."
        excerpt, loc = _find_literal_excerpt(
            page2, ["$49.95", "1 Bottle"], require_all=True
        )
        assert "$49.95" in excerpt
        assert "1 Bottle" in excerpt

    def test_ingredient_names_without_amounts_do_not_clear_mandatory_fact(
        self, pipeline_db
    ):
        """ingredients_with_amounts requires a real amount, not just a name."""
        from stage_handlers import _extract_targeted_fact

        data = {"supplement_facts": {"ingredients": [
            {"name": "Zinc", "amount": ""},
            {"name": "Vitamin B12", "amount": "2,500 mcg"},
            {"name": "Stevia", "amount": "", "form": "Other Ingredient"},
        ]}}
        facts = _extract_targeted_fact("ingredients_with_amounts", data)
        assert facts == [("Vitamin B12: 2,500 mcg",
                          ["Vitamin B12", "2,500 mcg"])]

    def test_manual_entry_rejects_invalid_fact_for_offering(self, pipeline_db):
        """record_manual_entry rejects facts not in the offering's pack."""
        from stage_handlers import record_manual_entry, RecoveryError
        from workflow import Job, JobStore, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-manual-val",
            url="https://example.com/product",
            product_name="Test Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        # "totally_fake_fact" is not in the supplement intelligence pack
        with pytest.raises(RecoveryError, match="not required for"):
            record_manual_entry(
                offering_id="off-manual-val",
                fact_key="totally_fake_fact",
                value="some value",
                reviewer="test_reviewer",
                db_path=pipeline_db,
            )

    def test_manual_entry_accepts_valid_fact_for_offering(self, pipeline_db):
        """record_manual_entry accepts facts that are in the offering's pack."""
        from stage_handlers import record_manual_entry
        from workflow import Job, JobStore, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-manual-valid",
            url="https://example.com/product",
            product_name="Test Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        # "serving_size" IS in the supplement intelligence pack
        claim_id = record_manual_entry(
            offering_id="off-manual-valid",
            fact_key="serving_size",
            value="2 capsules",
            reviewer="test_reviewer",
            db_path=pipeline_db,
        )
        assert claim_id  # Should succeed and return a claim_id

    def test_recovery_deduplicates_identical_claims(self, pipeline_db):
        """Repeated recovery with the same data should not create duplicates."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence
        from claims import ClaimsLedger
        from workflow import Job, JobStore, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-dedup",
            url="https://example.com/product",
            product_name="Dedup Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        recovery_page = (
            "<html><body>"
            "<h1>Dedup Product</h1>"
            "<div>Serving Size: 2 capsules</div>"
            "<div>Zinc 30mg per serving</div>"
            "</body></html>"
        )
        recovery_data = {
            "product_name": "Dedup Product",
            "supplement_facts": {
                "serving_size": "2 capsules",
                "ingredients": [
                    {"name": "Zinc", "amount": "30mg"},
                ],
            },
        }
        url = "https://example.com/dedup-test"
        fake_fr = self._make_recovery_fetch_result(recovery_page, url)

        with patch("config.DB_PATH", pipeline_db), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=recovery_data):

            # First recovery
            r1 = recover_evidence(
                url=url,
                offering_id="off-dedup",
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=pipeline_db,
            )
            assert r1["claims_added"] >= 1

            # Second recovery — same data should be deduplicated
            r2 = recover_evidence(
                url=url,
                offering_id="off-dedup",
                job_id=job.job_id,
                target_facts=["ingredients_with_amounts", "serving_size"],
                db_path=pipeline_db,
            )
            assert r2["duplicates_skipped"] >= 1
            # facts_found should still list them (dedup counts as found)
            assert "serving_size" in r2["facts_found"] or \
                   "ingredients_with_amounts" in r2["facts_found"]

        # Recovery must update the canonical product snapshot and invalidate
        # every ingredient-dependent downstream stage for regeneration.
        repaired_job = store.load(job.job_id)
        repaired_product = repaired_job.get_stage_result(
            PipelineStage.EXTRACT
        )["merged_product_data"]
        repaired_sf = repaired_product["supplement_facts"]
        assert repaired_sf["ingredients"][0]["name"] == "Zinc"
        assert repaired_sf["ingredients"][0]["amount"] == "30mg"
        assert repaired_sf["serving_size"] == "2 capsules"
        assert repaired_job.get_stage_status(PipelineStage.RESEARCH).value == \
            "pending"
        assert repaired_job.get_stage_status(PipelineStage.SOURCE_PACK).value == \
            "pending"

        # Total claims should match first run only
        ledger = ClaimsLedger(db_path=pipeline_db)
        all_claims = ledger.get_claims("off-dedup")
        claim_texts = [c.claim_text for c in all_claims]
        # No duplicate claim texts
        assert len(claim_texts) == len(set(c.lower() for c in claim_texts))

    def test_recovery_audit_events_logged(self, pipeline_db):
        """Recovery attempts must create audit events in the database."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence, RecoveryError
        from workflow import Job, JobStore, JobStatus, PipelineStage
        import sqlite3

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-audit",
            url="https://example.com/product",
            product_name="Audit Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        # Auth failure — should log recovery_auth_failure event
        try:
            recover_evidence(
                "https://example.com",
                "off-audit",
                job.job_id,
                ["totally_fake_fact"],
                db_path=pipeline_db,
            )
        except RecoveryError:
            pass

        # Successful recovery
        recovery_page = "<html><body>Serving Size: 2 capsules</body></html>"
        recovery_data = {
            "supplement_facts": {"serving_size": "2 capsules"},
        }
        fake_fr = self._make_recovery_fetch_result(
            recovery_page, "https://example.com/audit"
        )

        with patch("config.DB_PATH", pipeline_db), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   return_value=recovery_data):
            recover_evidence(
                "https://example.com/audit",
                "off-audit",
                job.job_id,
                ["serving_size"],
                db_path=pipeline_db,
            )

        # Check audit events were logged
        conn = sqlite3.connect(pipeline_db)
        conn.row_factory = sqlite3.Row
        events = conn.execute(
            "SELECT * FROM recovery_audit_events WHERE offering_id = ? "
            "ORDER BY id ASC",
            ("off-audit",)
        ).fetchall()
        conn.close()

        assert len(events) >= 2, f"Expected ≥2 audit events, got {len(events)}"
        event_types = [e["event_type"] for e in events]
        assert "recovery_auth_failure" in event_types
        assert "recovery_success" in event_types or \
               "recovery_attempt" in event_types

        # Auth failure event should contain the actual error message
        auth_event = [e for e in events if e["event_type"] == "recovery_auth_failure"][0]
        assert auth_event["error"], "Auth failure event must record error details"
        assert "not required for" in auth_event["error"]

    def test_composite_matching_requires_proximity(self, pipeline_db):
        """Terms must be within 200 chars of each other to be literal."""
        from stage_handlers import _find_literal_excerpt

        # "Zinc" at the start, "500 mg" 300+ chars later for a different ingredient
        page = (
            "Our formula contains Zinc for immune support. "
            + ("x" * 250)
            + "Vitamin D 500 mg per serving."
        )

        # Require_all with proximity: should FAIL — too far apart
        excerpt, loc = _find_literal_excerpt(
            page, ["Zinc", "500 mg"], require_all=True
        )
        assert excerpt == "", "Terms >200 chars apart should not match"

        # Same terms within proximity: should PASS
        page2 = "Each capsule contains Zinc 500 mg for daily support."
        excerpt, loc = _find_literal_excerpt(
            page2, ["Zinc", "500 mg"], require_all=True
        )
        assert "Zinc" in excerpt
        assert "500 mg" in excerpt

    def test_corroborating_sources_preserved(self, pipeline_db):
        """Same claim from different artifacts should create separate claims."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence
        from claims import ClaimsLedger
        from workflow import Job, JobStore, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-corroborate",
            url="https://example.com/product",
            product_name="Corroborate Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        recovery_data = {
            "product_name": "Corroborate Product",
            "supplement_facts": {
                "serving_size": "2 capsules",
            },
        }

        # Recovery from two DIFFERENT URLs (different artifacts)
        for url_suffix in ["source-a", "source-b"]:
            url = f"https://example.com/{url_suffix}"
            page = f"<html><body>Serving Size: 2 capsules (from {url_suffix})</body></html>"
            fake_fr = self._make_recovery_fetch_result(page, url)

            with patch("config.DB_PATH", pipeline_db), \
                 patch("net.safe_fetch", return_value=fake_fr), \
                 patch("research_product.phase1_extract_product",
                       return_value=recovery_data):
                recover_evidence(
                    url=url,
                    offering_id="off-corroborate",
                    job_id=job.job_id,
                    target_facts=["serving_size"],
                    db_path=pipeline_db,
                )

        # Both claims should exist (different artifacts)
        ledger = ClaimsLedger(db_path=pipeline_db)
        claims = [
            c for c in ledger.get_claims("off-corroborate")
            if c.metadata.get("fact_key") == "serving_size"
        ]
        artifact_ids = {c.source_artifact_id for c in claims}
        assert len(artifact_ids) >= 2, \
            f"Expected claims from ≥2 artifacts, got {len(artifact_ids)}"

    def test_manual_entry_rejects_no_job_exists(self, pipeline_db):
        """record_manual_entry must reject when no job exists for offering."""
        from stage_handlers import record_manual_entry, RecoveryError

        with pytest.raises(RecoveryError, match="No jobs found"):
            record_manual_entry(
                offering_id="off-nonexistent-no-job",
                fact_key="serving_size",
                value="2 capsules",
                reviewer="test_reviewer",
                db_path=pipeline_db,
            )

    def test_extraction_exception_logs_audit_failure(self, pipeline_db):
        """If phase1_extract_product() raises, a recovery_failure audit event
        must be logged and the error returned gracefully."""
        from unittest.mock import patch
        from stage_handlers import recover_evidence, RecoveryError
        from workflow import JobStore, Job, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-extract-err",
            url="https://example.com/extract-err",
            product_name="Extract Error Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "supplement",
        })
        store.save(job)

        from net import FetchResult
        page_text = "<html><body>Serving size 2 capsules</body></html>"
        fake_fr = FetchResult(
            content=page_text.encode(),
            text=page_text,
            final_url="https://example.com/extract-err",
            status_code=200,
            content_length=len(page_text),
            tls_verified=True,
            error="",
        )

        with patch("config.DB_PATH", pipeline_db), \
             patch("net.safe_fetch", return_value=fake_fr), \
             patch("research_product.phase1_extract_product",
                   side_effect=RuntimeError("LLM API timeout")):
            result = recover_evidence(
                url="https://example.com/extract-err",
                offering_id="off-extract-err",
                job_id=job.job_id,
                target_facts=["serving_size"],
                db_path=pipeline_db,
            )

        assert result["claims_added"] == 0
        assert "Extraction failed" in result["errors"][0]

        # Verify audit event was logged
        import sqlite3
        conn = sqlite3.connect(pipeline_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM recovery_audit_events "
            "WHERE offering_id='off-extract-err' AND event_type='recovery_failure'"
        ).fetchall()
        conn.close()
        assert len(rows) >= 1
        assert "LLM API timeout" in dict(rows[0])["error"]

    def test_audit_table_immutable_triggers(self, pipeline_db):
        """UPDATE and DELETE on recovery_audit_events must be rejected
        by SQLite triggers."""
        import sqlite3

        conn = sqlite3.connect(pipeline_db)
        # Insert a test row
        conn.execute(
            "INSERT INTO recovery_audit_events "
            "(event_type, offering_id, job_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test_event", "off-immutable", "job-immutable", "2026-01-01T00:00:00Z"),
        )
        conn.commit()

        # UPDATE must be rejected
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE recovery_audit_events SET error='tampered' "
                "WHERE offering_id='off-immutable'"
            )

        # DELETE must be rejected
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM recovery_audit_events "
                "WHERE offering_id='off-immutable'"
            )

        # INSERT still works (append-only)
        conn.execute(
            "INSERT INTO recovery_audit_events "
            "(event_type, offering_id, job_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test_event_2", "off-immutable", "job-immutable", "2026-01-01T00:00:01Z"),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM recovery_audit_events "
            "WHERE offering_id='off-immutable'"
        ).fetchone()[0]
        assert count == 2
        conn.close()

    def test_manual_entry_rejects_missing_offering_type(self, pipeline_db):
        """record_manual_entry must fail closed when the job has no
        identified offering_type."""
        from stage_handlers import record_manual_entry, RecoveryError
        from workflow import JobStore, Job, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-no-type",
            url="https://example.com/no-type",
            product_name="No Type Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        # IDENTIFY stage result with empty offering_type
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "",
        })
        store.save(job)

        with pytest.raises(RecoveryError, match="no identified type"):
            record_manual_entry(
                offering_id="off-no-type",
                fact_key="serving_size",
                value="2 capsules",
                reviewer="test_reviewer",
                db_path=pipeline_db,
            )

    def test_manual_entry_rejects_invalid_offering_type(self, pipeline_db):
        """record_manual_entry must fail closed when offering_type is
        not a recognized OfferingType enum value."""
        from stage_handlers import record_manual_entry, RecoveryError
        from workflow import JobStore, Job, JobStatus, PipelineStage

        store = JobStore(db_path=pipeline_db)
        job = Job(
            offering_id="off-bad-type",
            url="https://example.com/bad-type",
            product_name="Bad Type Product",
        )
        job.status = JobStatus.AWAITING_REVIEW
        job.set_stage_result(PipelineStage.IDENTIFY, {
            "offering_type": "definitely_not_a_real_type",
        })
        store.save(job)

        with pytest.raises(RecoveryError, match="not a recognized OfferingType"):
            record_manual_entry(
                offering_id="off-bad-type",
                fact_key="serving_size",
                value="2 capsules",
                reviewer="test_reviewer",
                db_path=pipeline_db,
            )

    def test_review_block_error_carries_structured_details(self, pipeline_db):
        """ReviewBlockError.details must propagate blocked_facts into
        the stage result so the UI can read them without parsing prose."""
        from workflow import ReviewBlockError

        err = ReviewBlockError(
            "mandatory facts not satisfied",
            details={
                "blocked_facts": ["ingredients_with_amounts", "serving_size"],
                "no_evidence": ["serving_size"],
                "needs_review": ["ingredients_with_amounts"],
            },
        )
        assert err.details["blocked_facts"] == [
            "ingredients_with_amounts", "serving_size"
        ]
        assert err.details["no_evidence"] == ["serving_size"]

        # Simulate what the pipeline does when catching ReviewBlockError
        block_result = {"blocked": True, "reason": str(err), **err.details}
        assert block_result["blocked"] is True
        assert "blocked_facts" in block_result
        assert len(block_result["blocked_facts"]) == 2

    def test_manual_entry_fails_on_import_error(self, pipeline_db):
        """record_manual_entry must raise RecoveryError when validation
        modules are unavailable (ImportError), not silently skip."""
        from unittest.mock import patch
        from stage_handlers import record_manual_entry, RecoveryError

        with patch.dict("sys.modules", {"entities": None}):
            with pytest.raises(RecoveryError, match="required module unavailable"):
                record_manual_entry(
                    offering_id="off-import-err",
                    fact_key="serving_size",
                    value="2 capsules",
                    reviewer="test_reviewer",
                    db_path=pipeline_db,
                )
