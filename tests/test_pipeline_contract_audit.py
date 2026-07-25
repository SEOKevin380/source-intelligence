from scripts.audit_pipeline_contract import audit_pipeline_contract


def test_every_intake_type_owns_a_complete_zero_cost_pipeline_contract():
    result = audit_pipeline_contract()
    assert result["passed"] is True, result["errors"]
    assert result["product_types_checked"] == 16
    assert result["platform_contracts_checked"] == 32
    assert result["model_calls"] == 0
