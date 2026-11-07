import subprocess
from pathlib import Path

import pytest

FLEET_KEY = "hive-key-test0000000000000000000000000000000000"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Point ~ at a temp dir so tests never read the real ~/.hivemind or ~/.env."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home_dir))
    for var in ("HIVEMIND_FLEET_KEY", "HIVEMIND_WORKER_CONFIG", "HIVEMIND_WORKER_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.com")
    return home_dir


def run_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert res.returncode == 0, f"git {' '.join(args)} failed: {res.stderr}"
    return res.stdout.strip()


@pytest.fixture
def git():
    return run_git


@pytest.fixture
def origin_repo(tmp_path) -> Path:
    """A bare origin repository with one commit on main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    run_git(seed, "init", "-b", "main")
    (seed / "app.py").write_text("print('v1')\n", encoding="utf-8")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-m", "initial")
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "clone", "--bare", str(seed), str(origin))
    return origin
