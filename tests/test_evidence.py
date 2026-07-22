"""Tests for evidence.py — Immutable evidence lake."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evidence import (
    EvidenceLake, Artifact, SourceClass, SourceRelationship, ArtifactType,
)


@pytest.fixture
def lake():
    """Create a temporary evidence lake for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    artifacts_dir = tempfile.mkdtemp()
    lk = EvidenceLake(db_path=path, artifacts_dir=artifacts_dir)
    yield lk
    if lk._conn:
        lk._conn.close()
    os.unlink(path)
    # Clean up artifacts dir
    import shutil
    shutil.rmtree(artifacts_dir, ignore_errors=True)


class TestArtifact:
    def test_creation(self):
        a = Artifact(
            artifact_id="abc123",
            source_url="https://example.com",
            source_class=SourceClass.OFFICIAL_VENDOR,
        )
        assert a.artifact_id == "abc123"
        assert a.source_class == SourceClass.OFFICIAL_VENDOR

    def test_source_class_enum(self):
        assert SourceClass.REGULATORY_DATABASE.value == "regulatory_database"
        assert SourceClass.PEER_REVIEWED.value == "peer_reviewed"

    def test_relationship_enum(self):
        assert SourceRelationship.FIRST_PARTY.value == "first_party"


class TestEvidenceLake:
    def test_store_small_artifact_inline(self, lake):
        content = b"<html><body>Test page content</body></html>"
        artifact = Artifact(
            artifact_type=ArtifactType.HTML_SNAPSHOT,
            source_url="https://test.com",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
        )
        aid = lake.store(artifact, content)
        assert aid  # Non-empty ID
        assert len(aid) == 64  # SHA-256 hex

        # Retrieve
        retrieved = lake.get(aid)
        assert retrieved is not None
        assert retrieved.source_url == "https://test.com"
        assert retrieved.source_class == SourceClass.OFFICIAL_VENDOR

    def test_store_retrieves_content(self, lake):
        content = b"Test content for retrieval"
        artifact = Artifact(
            source_url="https://test.com/page",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
        )
        aid = lake.store(artifact, content)
        text = lake.get_content(aid)
        assert "Test content for retrieval" in text

    def test_immutability_dedup(self, lake):
        """Same content stored twice should not create duplicate."""
        content = b"Identical content"
        a1 = Artifact(
            source_url="https://test.com/a",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
        )
        a2 = Artifact(
            source_url="https://test.com/b",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
        )
        id1 = lake.store(a1, content)
        id2 = lake.store(a2, content)
        assert id1 == id2  # Same content hash = same ID

    def test_store_large_artifact_on_disk(self, lake):
        """Content > 100KB should be stored on disk."""
        content = b"x" * 150_000
        artifact = Artifact(
            source_url="https://test.com/large",
            source_class=SourceClass.REGULATORY_DATABASE,
            source_relationship=SourceRelationship.THIRD_PARTY,
        )
        aid = lake.store(artifact, content)
        retrieved = lake.get(aid)
        assert retrieved is not None
        assert retrieved.content_path  # Should have disk path
        assert not retrieved.content_inline  # Should NOT be inline

        # Content should still be retrievable
        text = lake.get_content(aid)
        assert len(text) == 150_000

    def test_list_for_offering(self, lake):
        for i in range(3):
            artifact = Artifact(
                source_url=f"https://test.com/{i}",
                source_class=SourceClass.OFFICIAL_VENDOR,
                source_relationship=SourceRelationship.FIRST_PARTY,
                offering_id="offering-123",
            )
            lake.store(artifact, f"content-{i}".encode())

        # Different offering
        artifact = Artifact(
            source_url="https://other.com",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
            offering_id="offering-456",
        )
        lake.store(artifact, b"other content")

        results = lake.list_for_offering("offering-123")
        assert len(results) == 3

    def test_count(self, lake):
        assert lake.count() == 0
        artifact = Artifact(
            source_url="https://test.com",
            source_class=SourceClass.OFFICIAL_VENDOR,
            source_relationship=SourceRelationship.FIRST_PARTY,
        )
        lake.store(artifact, b"content")
        assert lake.count() == 1

    def test_get_nonexistent(self, lake):
        assert lake.get("nonexistent_id") is None
        assert lake.get_content("nonexistent_id") == ""
