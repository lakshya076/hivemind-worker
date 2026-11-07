import pytest
from pydantic import ValidationError

from hivemind_worker.models import (
    DispatchRequest,
    validate_branch_name,
    validate_repo_url,
    validate_run_id,
)


@pytest.mark.parametrize("run_id", ["20261107-120000-abcd1234", "r1", "a.b_c-d"])
def test_valid_run_ids(run_id):
    assert validate_run_id(run_id) == run_id


@pytest.mark.parametrize(
    "run_id", ["", "..", "../x", "a/b", "a\\b", "-x", "C:x", "a..b", "x" * 129]
)
def test_traversal_run_ids_rejected(run_id):
    with pytest.raises(ValueError):
        validate_run_id(run_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/me/repo.git",
        "git@github.com:me/repo.git",
        "ssh://git@github.com/me/repo.git",
        "/srv/git/repo.git",
        "C:\\repos\\origin.git",
    ],
)
def test_valid_repo_urls(url):
    assert validate_repo_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "--upload-pack=touch /tmp/pwned",
        "-oProxyCommand=x",
        "ext::sh -c touch% /tmp/pwned",
        "fd::17",
        "foo::bar",
        "\\\\attacker\\share\\repo",
        "//attacker/share/repo",
        "file:////attacker/share",
        "https://x/y.git\nmore",
        "https://x/ y.git",
        "https://x/y\x7f.git",
    ],
)
def test_injection_repo_urls_rejected(url):
    with pytest.raises(ValueError):
        validate_repo_url(url)


def test_blank_branch_means_none():
    assert validate_branch_name(None) is None
    assert validate_branch_name("  ") is None


@pytest.mark.parametrize(
    "branch",
    ["-f", "--force", "exp/a..b", "exp/a//b", "exp/a/", "exp/a.lock", "exp/a.", "exp/a@{1}", "exp/a b", "exp/a~1", "exp/a^"],
)
def test_bad_branch_names_rejected(branch):
    with pytest.raises(ValueError):
        validate_branch_name(branch)


@pytest.mark.parametrize("branch", ["main", "master", "release/1.0", "exp", "exp/", "expx/a", "refs/heads/main"])
def test_branches_outside_exp_are_rejected(branch):
    # The worker force-pushes the branch, so it must never be main or another real branch.
    with pytest.raises(ValueError, match="exp/"):
        validate_branch_name(branch)


@pytest.mark.parametrize("branch", ["exp/20261107-120000-abcd1234/ari-simd", "exp/ari-strategy-1"])
def test_exp_branches_accepted(branch):
    assert validate_branch_name(branch) == branch


def base_request(**overrides):
    fields = {"run_id": "r1", "repo_url": "https://x/y.git", "prompt": "do it"}
    fields.update(overrides)
    return DispatchRequest(**fields)


@pytest.mark.parametrize("engine, stored", [("aider", "aider"), (" Claude-Code ", "claude-code"), ("claude", "claude"), ("agy", "agy")])
def test_dispatch_request_normalizes_supported_engines(engine, stored):
    assert base_request(agent_engine=engine).agent_engine == stored


def test_dispatch_request_ignores_legacy_workspace_dir():
    # Masters before HiveMind #4 still send it; it must not break dispatch.
    req = DispatchRequest(run_id="r1", repo_url="https://x/y.git", prompt="p", workspace_dir="/etc")
    assert not hasattr(req, "workspace_dir")


def test_dispatch_request_defaults():
    req = base_request()
    assert (req.agent_engine, req.strategy, req.branch) == ("claude-code", "strategy-1", None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_id": "../escape"},
        {"repo_url": "--upload-pack=x"},
        {"branch": "--force"},
        {"branch": "main"},
        {"agent_engine": "cursor"},
        {"agent_engine": "custom"},
        {"model": "--dangerously-skip-permissions"},
        {"model": "gpt 4"},
        {"prompt": ""},
        {"prompt": "x" * 200_001},
        {"repo_url": "https://x/" + "a" * 2048},
        {"strategy_index": 0},
        {"strategy_count": 1001},
        {"test_command": "x" * 4097},
        {"model": "m" * 201},
    ],
)
def test_dispatch_request_rejects_bad_fields(overrides):
    with pytest.raises(ValidationError):
        base_request(**overrides)
