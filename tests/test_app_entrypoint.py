import ast
from pathlib import Path


def test_streamlit_entrypoint_declares_regex_dependency():
    """Form submission and advertorial rendering both execute regex calls."""
    tree = ast.parse(
        Path("app.py").read_text(encoding="utf-8"),
        filename="app.py",
    )
    imported = {
        alias.asname or alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    regex_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
    ]
    assert regex_calls, "The entrypoint no longer uses regex; remove this guard."
    assert "re" in imported
