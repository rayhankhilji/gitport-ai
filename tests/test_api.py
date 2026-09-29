import pytest
from conftest import FakeCohereClient
from fastapi.testclient import TestClient

from gitport.api import create_app
from gitport.config import Settings


@pytest.fixture
def api(tmp_path):
    cfg = Settings(
        index_path=str(tmp_path / "idx.sqlite3"),
        rules_dir=str(tmp_path / "rules"),
        reports_db=str(tmp_path / "reports.sqlite3"),
        _env_file=None,
    )
    app = create_app(settings=cfg, client=FakeCohereClient(
        verdicts=[{"status": "FAILED", "breaking_changes_detected": True,
                   "critical_flaws": [{"file": "m.sql", "line": 3,
                                       "issue": "unqualified delete"}],
                   "summary": "dangerous migration"}],
    ))
    return TestClient(app)


def test_healthz(api):
    r = api.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_check_with_raw_diff(api, sample_diff):
    r = api.post("/v1/check", json={"diff": sample_diff})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"]["status"] == "FAILED"
    assert body["verdict"]["critical_flaws"][0]["file"] == "m.sql"
    assert body["files_changed"] == 2


def test_check_empty_diff(api):
    r = api.post("/v1/check", json={"diff": ""})
    assert r.status_code == 200
    assert r.json()["verdict"]["status"] == "PASSED"


def test_schema_endpoint(api):
    r = api.get("/v1/schema")
    assert r.status_code == 200
    assert "critical_flaws" in r.json()["properties"]


def test_auth_required_when_token_set(tmp_path):
    cfg = Settings(api_token="sekret",
                   reports_db=str(tmp_path / "reports.sqlite3"),
                   _env_file=None)
    api = TestClient(create_app(settings=cfg, client=FakeCohereClient()))
    # auth covers every /v1/* route, including the read endpoints
    assert api.post("/v1/check", json={"diff": ""}).status_code == 401
    assert api.get("/v1/checks").status_code == 401
    assert api.get("/v1/checks/abc").status_code == 401
    assert api.get("/v1/stats").status_code == 401
    ok = api.post("/v1/check", json={"diff": ""},
                  headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200


def test_check_persistence_roundtrip(api, sample_diff):
    r = api.post("/v1/check", json={"diff": sample_diff})
    assert r.status_code == 200
    check_id = r.json()["id"]

    got = api.get(f"/v1/checks/{check_id}")
    assert got.status_code == 200
    assert got.json()["id"] == check_id
    assert got.json()["verdict"]["status"] == "FAILED"
    assert got.json()["files_changed"] == 2

    missing = api.get("/v1/checks/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["type"] == "http_error"


def test_checks_list_and_stats(api, sample_diff):
    api.post("/v1/check", json={"diff": ""})           # PASSED
    api.post("/v1/check", json={"diff": sample_diff})  # FAILED

    r = api.get("/v1/checks", params={"limit": 10, "offset": 0})
    assert r.status_code == 200
    checks = r.json()
    # newest first; summaries carry no report blob
    assert [c["status"] for c in checks] == ["FAILED", "PASSED"]
    assert "report_json" not in checks[0]
    assert checks[0]["breaking"] is True

    stats = api.get("/v1/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["total"] == 2
    assert body["by_status"] == {"PASSED": 1, "FAILED": 1}
    assert body["avg_elapsed_seconds"] >= 0.0


def test_engine_error_envelope(api, tmp_path):
    # tmp_path exists but is not a git repo → git diff fails → EngineError
    r = api.post("/v1/check", json={"repo_path": str(tmp_path)})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["type"] == "engine_error"
    assert err["request_id"]


def test_validation_error_envelope(api):
    r = api.post("/v1/check", json={"staged": "not-a-bool"})
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "validation_error"


def test_unexpected_error_envelope(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("gitport.api.run_check", boom)
    cfg = Settings(reports_db=str(tmp_path / "r.db"), _env_file=None)
    # starlette's ServerErrorMiddleware re-raises after sending the 502 — the
    # envelope still reaches the client
    api = TestClient(create_app(settings=cfg, client=FakeCohereClient()),
                     raise_server_exceptions=False)
    r = api.post("/v1/check", json={"diff": "x"})
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["type"] == "internal_error"
    assert err["message"] == "kaboom"
    assert err["request_id"]
