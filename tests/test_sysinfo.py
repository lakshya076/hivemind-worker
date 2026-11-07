import sys

from hivemind_worker import runner as runner_mod
from hivemind_worker import sysinfo


def make_tool(home, name):
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / (f"{name}.exe" if sys.platform == "win32" else name)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_fallback_location_found_when_not_on_path(home, monkeypatch):
    # Services often run with a minimal PATH (the ari unit had a typo'd one).
    monkeypatch.setenv("PATH", "")
    tool = make_tool(home, "hive-fake-engine")
    assert sysinfo.resolve_binary("hive-fake-engine") == str(tool)
    assert sysinfo.is_tool_installed("hive-fake-engine")


def test_directories_are_not_mistaken_for_binaries(home, monkeypatch):
    monkeypatch.setenv("PATH", "")
    (home / ".local" / "bin" / "hive-fake-engine").mkdir(parents=True)
    assert sysinfo.resolve_binary("hive-fake-engine") is None


def test_health_and_runner_use_the_same_lookup(home, monkeypatch):
    # /health must never report an engine the runner can't start, or the reverse.
    monkeypatch.setenv("PATH", "")
    tool = make_tool(home, "hive-fake-engine")
    assert runner_mod._resolve_agent_binary("hive-fake-engine", {"PATH": ""}) == str(tool)
    assert runner_mod._resolve_agent_binary("hive-missing-engine", {"PATH": ""}) is None
