import pytest
from fastapi.testclient import TestClient

from hivemind_worker.config import WorkerConfig
from hivemind_worker.security import HostAllowlist
from hivemind_worker.server import create_app

from conftest import FLEET_KEY

AUTH = {"Authorization": f"Bearer {FLEET_KEY}"}
LAN_CLIENT = ("192.168.1.10", 50000)


def make_app(tmp_path, **config):
    cfg = WorkerConfig(fleet_key=FLEET_KEY, workspace_root=str(tmp_path / "ws"), **config)
    return create_app(cfg, host_allowlist=HostAllowlist(tailscale_lookup=set))


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def client_for(app, client=LAN_CLIENT, host="10.0.0.2:7422"):
    return TestClient(app, base_url=f"http://{host}", client=client)


def test_root_info_needs_no_auth(app):
    resp = client_for(app).get("/")
    assert resp.status_code == 200
    assert resp.json()["name"] == "HiveMind Worker Daemon"


@pytest.mark.parametrize(
    "headers, detail",
    [
        ({}, "Missing Authorization header"),
        ({"Authorization": FLEET_KEY}, "Invalid Authorization scheme"),
        ({"Authorization": f"Basic {FLEET_KEY}"}, "Invalid Authorization scheme"),
        ({"Authorization": "Bearer wrong-key"}, "Invalid fleet API key"),
    ],
)
def test_protected_endpoints_require_fleet_key(app, headers, detail):
    resp = client_for(app).get("/health", headers=headers)
    assert resp.status_code == 401
    assert detail in resp.json()["detail"]


def test_non_ascii_token_is_rejected_not_a_server_error(app):
    # compare_digest raises on non-ASCII str; this used to return 500.
    client = TestClient(app, base_url="http://10.0.0.2:7422", client=LAN_CLIENT, raise_server_exceptions=False)
    resp = client.get("/health", headers={"Authorization": "Bearer café".encode("utf-8")})
    assert resp.status_code == 401


def test_dispatch_refuses_branch_outside_exp(app, tmp_path):
    body = {"run_id": "r1", "repo_url": "https://x/y.git", "prompt": "p", "branch": "main"}
    resp = client_for(app).post("/dispatch", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert "exp/" in resp.text
    assert not (tmp_path / "ws" / "r1").exists()


@pytest.mark.parametrize("field, value", [("agent_engine", "custom"), ("model", "--dangerously-skip-permissions")])
def test_dispatch_rejects_bad_engine_or_model_before_cloning(app, tmp_path, field, value):
    body = {"run_id": "r1", "repo_url": "https://x/y.git", "prompt": "p", field: value}
    resp = client_for(app).post("/dispatch", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert not (tmp_path / "ws" / "r1").exists()


def test_health_with_key(app):
    resp = client_for(app).get("/health", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["active_runs"] == []


def test_public_source_address_rejected_before_auth(app):
    resp = client_for(app, client=("8.8.8.8", 1)).get("/health", headers=AUTH)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Source address not allowed"


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_browser_requests_rejected(app, headers):
    resp = client_for(app).get("/health", headers={**AUTH, **headers})
    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers


def test_address_bar_navigation_allowed(app):
    resp = client_for(app).get("/", headers={"Sec-Fetch-Site": "none"})
    assert resp.status_code == 200


def test_dns_rebinding_host_rejected(app):
    resp = client_for(app, host="evil.example:7422").get("/health", headers=AUTH)
    assert resp.status_code == 400


def test_create_app_refuses_to_start_without_key(tmp_path):
    with pytest.raises(ValueError):
        create_app(WorkerConfig(workspace_root=str(tmp_path / "ws")))


def test_insecure_mode_serves_loopback_only(tmp_path):
    app = create_app(
        WorkerConfig(workspace_root=str(tmp_path / "ws")),
        insecure_no_auth=True,
        host_allowlist=HostAllowlist(tailscale_lookup=set),
    )
    assert client_for(app, client=("127.0.0.1", 1), host="127.0.0.1:7422").get("/health").status_code == 200
    assert client_for(app, client=LAN_CLIENT).get("/health").status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"run_id": "../escape", "repo_url": "https://x/y.git", "prompt": "p"},
        {"run_id": "r1", "repo_url": "--upload-pack=x", "prompt": "p"},
        {"run_id": "r1", "repo_url": "ext::sh -c x", "prompt": "p"},
        {"run_id": "r1", "repo_url": "https://x/y.git", "prompt": "p", "branch": "--force"},
        {"run_id": "r1", "repo_url": "https://x/y.git", "prompt": "x" * 200_001},
    ],
)
def test_dispatch_rejects_bad_payloads(app, tmp_path, body):
    resp = client_for(app).post("/dispatch", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert not (tmp_path / "ws" / "r1").exists()


def test_unknown_runs_return_404(app):
    client = client_for(app)
    assert client.get("/status/nope", headers=AUTH).status_code == 404
    assert client.delete("/cancel/nope", headers=AUTH).status_code == 404
    assert client.get("/runs", headers=AUTH).json() == []


def test_config_timeouts_reach_the_runner(tmp_path):
    app = make_app(tmp_path, agent_timeout=42, test_timeout=7, max_concurrent_runs=3)
    runner = app.state.runner
    assert (runner.agent_timeout, runner.test_timeout, runner.max_concurrent_runs) == (42, 7, 3)
