import json

import pytest
from click.testing import CliRunner

from hivemind_worker import cli as cli_mod
from hivemind_worker.cli import main


@pytest.fixture
def started(monkeypatch):
    """Capture uvicorn.run instead of serving."""
    calls = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(cli_mod.uvicorn, "run", fake_run)
    return calls


def config_file(tmp_path, **fields):
    path = tmp_path / "worker.json"
    path.write_text(json.dumps({"workspace_root": str(tmp_path / "ws"), **fields}), encoding="utf-8")
    return path


def test_start_key_flag_warns_but_still_works(tmp_path, started):
    path = config_file(tmp_path)

    result = CliRunner().invoke(main, ["--config", str(path), "start", "--key", "hive-key-abc"])

    assert result.exit_code == 0, result.output
    assert "visible in the process list" in result.output
    assert started["app"].state.config.fleet_key == "hive-key-abc"


def test_start_key_flag_is_hidden_from_help():
    result = CliRunner().invoke(main, ["start", "--help"])
    assert "--key" not in result.output


def test_start_with_saved_key_does_not_warn(tmp_path, started):
    path = config_file(tmp_path, fleet_key="hive-key-saved")
    result = CliRunner().invoke(main, ["--config", str(path), "start"])
    assert result.exit_code == 0, result.output
    assert "process list" not in result.output
