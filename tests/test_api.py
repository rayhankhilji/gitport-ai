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
    cfg = Settings(api_token="sekret", _env_file=None)
    api = TestClient(create_app(settings=cfg, client=FakeCohereClient()))
    assert api.post("/v1/check", json={"diff": ""}).status_code == 401
    ok = api.post("/v1/check", json={"diff": ""},
                  headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200
