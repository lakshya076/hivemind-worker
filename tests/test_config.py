import json
import os
import stat
import sys

import pytest
from pydantic import ValidationError

from hivemind_worker.config import (
    ConfigError,
    WorkerConfig,
    generate_fleet_key,
    get_default_worker_config_path,
)


def test_defaults_without_config_file():
    config = WorkerConfig.load()
    assert config.fleet_key is None
    assert (config.host, config.port) == ("0.0.0.0", 7422)
    assert (config.agent_timeout, config.test_timeout) == (1800.0, 300.0)
    assert "100.64.0.0/10" in config.allowed_networks


def test_env_key_and_port_without_config_file(monkeypatch):
    monkeypatch.setenv("HIVEMIND_FLEET_KEY", "env-key")
    monkeypatch.setenv("HIVEMIND_WORKER_PORT", "7500")
    config = WorkerConfig.load()
    assert (config.fleet_key, config.port) == ("env-key", 7500)


def test_env_var_selects_config_path(tmp_path, monkeypatch):
    target = tmp_path / "custom.json"
    monkeypatch.setenv("HIVEMIND_WORKER_CONFIG", str(target))
    assert get_default_worker_config_path() == target


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "worker.json"
    config = WorkerConfig(fleet_key=generate_fleet_key(), agent_timeout=60, max_concurrent_runs=4)
    config.save(path)
    assert WorkerConfig.load(path) == config


def test_env_key_fills_saved_config_without_key(tmp_path, monkeypatch):
    path = tmp_path / "worker.json"
    WorkerConfig().save(path)
    monkeypatch.setenv("HIVEMIND_FLEET_KEY", "env-key")
    assert WorkerConfig.load(path).fleet_key == "env-key"


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"allowed_networks": ["not-a-cidr"]}), json.dumps({"agent_timeout": 0})],
)
def test_unreadable_config_fails_closed(tmp_path, content):
    path = tmp_path / "worker.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        WorkerConfig.load(path)


@pytest.mark.parametrize(
    "field, value",
    [("agent_timeout", 0), ("test_timeout", -1), ("max_concurrent_runs", 0), ("max_concurrent_runs", 65)],
)
def test_numeric_limits(field, value):
    with pytest.raises(ValidationError):
        WorkerConfig(**{field: value})


def test_generated_keys_are_unique_and_prefixed():
    a, b = generate_fleet_key(), generate_fleet_key()
    assert a != b and a.startswith("hive-key-") and len(a) == len("hive-key-") + 40


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_saved_config_is_owner_only(tmp_path):
    path = tmp_path / "worker.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)
    WorkerConfig(fleet_key="k").save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
