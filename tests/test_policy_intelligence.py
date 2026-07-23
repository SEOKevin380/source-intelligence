from policy_intelligence import (
    applicable_sources,
    adopt_source,
    content_hash,
    policy_status,
    record_observation,
)


def test_html_hash_ignores_markup_and_whitespace():
    assert content_hash(b"<p>Rule   text</p>", "text/html") == content_hash(
        b"<div>Rule text</div>", "text/html"
    )


def test_changed_policy_requires_review_after_adoption():
    source = {
        "id": "rule", "title": "Rule", "url": "https://example.com/rule",
        "authority": "regulator", "verticals": ["all"],
    }
    snapshot = {"schema_version": 1, "sources": {}, "adoptions": {}}
    record_observation(snapshot, source, b"version one")
    adopt_source(snapshot, "rule", "rules-v1", "Editor")
    record_observation(snapshot, source, b"version two")
    assert snapshot["sources"]["rule"]["requires_review"] is True


def test_policy_status_is_fail_closed_for_missing_and_changed_sources():
    registry = {
        "sources": [{
            "id": "rule", "title": "Rule", "url": "https://example.com/rule",
            "authority": "regulator", "verticals": ["all"],
        }]
    }
    assert applicable_sources("device", registry)[0]["id"] == "rule"
    status = policy_status("device", {"sources": {}, "adoptions": {}})
    assert status["current"] is False
    assert status["missing_observations"]
