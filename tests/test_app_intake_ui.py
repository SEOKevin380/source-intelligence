from streamlit.testing.v1 import AppTest


def test_primary_intake_is_simple_and_advanced_schema_is_collapsed(
    monkeypatch,
):
    monkeypatch.setenv("SI_APP_PASSWORD", "ui-test")
    app = AppTest.from_file("app.py")
    app.session_state["authenticated"] = True
    app.run(timeout=30)

    assert not app.exception
    assert "Source Intelligence" in [item.value for item in app.title]
    assert (
        "Additional Sources, Contacts, Terms, or Instructions"
        in [item.label for item in app.text_area]
    )
    override = next(
        item for item in app.expander
        if item.label
        == "Optional source classification and structured overrides"
    )
    assert override.proto.expanded is False
    assert "Run Research" in [item.label for item in app.button]
