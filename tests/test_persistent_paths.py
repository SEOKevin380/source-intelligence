import importlib
import os


def test_explicit_data_root_owns_crm_and_workbench(monkeypatch, tmp_path):
    data_root = tmp_path / "durable-data"
    monkeypatch.setenv("SOURCE_INTELLIGENCE_DATA_DIR", str(data_root))
    monkeypatch.delenv("NEWSWIRE_WORKBENCH_HOME", raising=False)

    import config
    importlib.reload(config)

    assert config.DB_PATH == str(data_root / "source_intelligence.db")
    assert config.NEWSWIRE_WORKBENCH_PATH == str(
        data_root / "newswire-workbench"
    )


def test_workbench_override_remains_supported(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    workbench_root = tmp_path / "separate-workbench"
    monkeypatch.setenv("SOURCE_INTELLIGENCE_DATA_DIR", str(data_root))
    monkeypatch.setenv("NEWSWIRE_WORKBENCH_HOME", str(workbench_root))

    import config
    importlib.reload(config)

    assert config.NEWSWIRE_WORKBENCH_PATH == str(workbench_root)
