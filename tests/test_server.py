"""ASGI middleware: request ids, body limits, rate limits, CORS, JSON logs."""

import json
import logging
import re

from conftest import FakeCohereClient
from fastapi.testclient import TestClient

from gitport.api import create_app
from gitport.config import Settings
from gitport.server import json_logging


def _api(tmp_path, **kw) -> TestClient:
    cfg = Settings(reports_db=str(tmp_path / "reports.sqlite3"),
                   _env_file=None, **kw)
    return TestClient(create_app(settings=cfg, client=FakeCohereClient()))


def test_request_id_on_every_response(tmp_path):
    api = _api(tmp_path)
    ok = api.get("/healthz")
    err = api.get("/v1/checks/does-not-exist")
    assert err.status_code == 404
    for r in (ok, err):
        assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])
    assert ok.headers["x-request-id"] != err.headers["x-request-id"]
    # the header matches the id reported inside the error envelope
    assert err.json()["error"]["request_id"] == err.headers["x-request-id"]


def test_body_limit(tmp_path):
    api = _api(tmp_path, max_request_bytes=128)
    r = api.post("/v1/check", content=b"x" * 1024)
    assert r.status_code == 413
    assert r.json()["error"]["type"] == "request_too_large"
    assert r.headers["x-request-id"]
    # under the limit still reaches the app
    assert api.post("/v1/check", json={"diff": ""}).status_code == 200


def test_rate_limit(tmp_path):
    api = _api(tmp_path, rate_limit_rpm=2)
    assert api.post("/v1/check", json={"diff": ""}).status_code == 200
    assert api.post("/v1/check", json={"diff": ""}).status_code == 200
    r = api.post("/v1/check", json={"diff": ""})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    # healthz is exempt from the quota
    assert api.get("/healthz").status_code == 200


def test_rate_limit_disabled(tmp_path):
    api = _api(tmp_path, rate_limit_rpm=0)
    for _ in range(5):
        assert api.post("/v1/check", json={"diff": ""}).status_code == 200


def test_cors_only_when_configured(tmp_path):
    api = _api(tmp_path, cors_origins="https://ui.example.com, https://x.io")
    r = api.get("/healthz", headers={"Origin": "https://ui.example.com"})
    assert r.headers["access-control-allow-origin"] == "https://ui.example.com"
    r = api.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in r.headers

    plain = _api(tmp_path)
    r = plain.get("/healthz", headers={"Origin": "https://ui.example.com"})
    assert "access-control-allow-origin" not in r.headers


def test_json_logging(capsys):
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        json_logging(Settings(log_format="json", _env_file=None))
        logging.getLogger("gitport.test").warning("hello %s", "world")
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "gitport.test"
        assert payload["message"] == "hello world"
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)


def test_json_logging_text_is_noop():
    root = logging.getLogger()
    before = root.handlers[:]
    json_logging(Settings(log_format="text", _env_file=None))
    assert root.handlers == before
