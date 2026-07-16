"""Public-bind API key + rate-limit gate."""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import config
from app import main as main_mod
from app.main import app, redact_api_key_text, redact_query_string

# Import smoke helpers without requiring a live server.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import smoke_friends  # noqa: E402

client = TestClient(app)


def test_localhost_mode_allows_health(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", False)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "public_mode" in body
    assert body["osrm_mode"] == "public_demo"
    assert body["osrm_configured"] is False
    assert "max_inflight_plans" in body
    assert "inflight_plans" in body
    assert body.get("workers") == 1
    assert smoke_friends.validate_health_payload(body, expect_public=False) == []


def test_public_mode_rejects_unauthenticated_geocode(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "test-secret-key")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)

    r = client.get("/api/geocode", params={"q": "Keswick"})
    assert r.status_code == 401

    h = client.get("/api/health")
    assert h.status_code == 200

    bad = client.get(
        "/api/geocode",
        params={"q": "Keswick"},
        headers={"X-API-Key": "wrong"},
    )
    assert bad.status_code == 401


def test_public_mode_accepts_valid_key(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "test-secret-key")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)

    with patch("app.main.requests.get") as mock_get:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [{
            "lat": "54.6", "lon": "-3.1",
            "display_name": "Keswick", "name": "Keswick",
        }]
        mock_get.return_value = resp
        ok = client.get(
            "/api/geocode",
            params={"q": "Keswick"},
            headers={"X-API-Key": "test-secret-key"},
        )
    assert ok.status_code == 200
    assert ok.json()["results"][0]["name"] == "Keswick"


def test_public_mode_accepts_api_key_query_param(monkeypatch):
    """EventSource cannot set headers — query param must work for SSE."""
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "test-secret-key")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)

    with patch("app.main.requests.get") as mock_get:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [{
            "lat": "54.6", "lon": "-3.1",
            "display_name": "Keswick", "name": "Keswick",
        }]
        mock_get.return_value = resp
        ok = client.get(
            "/api/geocode",
            params={"q": "Keswick", "api_key": "test-secret-key"},
        )
    assert ok.status_code == 200
    assert ok.json()["results"][0]["name"] == "Keswick"


def test_api_key_redacted_from_logs_and_query_helpers():
    secret = "super-secret-host-key-xyz"
    url = f"/api/route/stream?from_lat=1&api_key={secret}&profile=balanced"
    assert secret in url
    redacted = redact_api_key_text(url)
    assert secret not in redacted
    assert "api_key=***" in redacted

    qs = f"q=Keswick&api_key={secret}".encode()
    out = redact_query_string(qs)
    assert secret.encode() not in out
    assert b"api_key=***" in out

    # smoke_friends helper stays in sync
    assert secret not in smoke_friends.redact_query_secrets(url)

    # Logging filter must not leave the raw key in the rendered message.
    logger = logging.getLogger("app.main")
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info('127.0.0.1:1 - "GET %s HTTP/1.1" 200', url)
        # Also exercise the filter's args path used by uvicorn.access.
        logging.getLogger("uvicorn.access").info(
            '127.0.0.1:1 - "GET %s HTTP/1.1" 200', url
        )
    finally:
        logger.removeHandler(handler)
    assert records
    rendered = records[0].getMessage()
    assert secret not in rendered
    assert "api_key=***" in rendered


def test_smoke_friends_health_helper_flags_bad_payload():
    errs = smoke_friends.validate_health_payload(
        {"status": "ok", "public_mode": False, "osrm_mode": "nope"},
        expect_public=True,
    )
    assert any("public_mode" in e for e in errs)
    assert any("osrm_mode" in e for e in errs)


def test_public_mode_accepts_bearer(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "test-secret-key")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 60)

    with patch("app.main.requests.get") as mock_get:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = [{
            "lat": "54.6", "lon": "-3.1",
            "display_name": "Keswick", "name": "Keswick",
        }]
        mock_get.return_value = resp
        ok = client.get(
            "/api/geocode",
            params={"q": "Keswick"},
            headers={"Authorization": "Bearer test-secret-key"},
        )
    assert ok.status_code == 200


def test_public_mode_without_key_configured_returns_503(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    monkeypatch.setattr(config, "API_KEY", "")
    r = client.get("/api/geocode", params={"q": "Keswick"})
    assert r.status_code == 503


def test_health_byo_osrm_reports_reachable(monkeypatch):
    monkeypatch.setenv(
        "SCENIC_OSRM_URL",
        "http://127.0.0.1:9/route/v1/driving/{coords}",
    )
    with patch("app.main.requests.get") as mock_get:
        resp = MagicMock()
        resp.status_code = 200
        mock_get.return_value = resp
        r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["osrm_mode"] == "byo"
    assert body["osrm_configured"] is True
    assert body["osrm_reachable"] is True


def test_inflight_plan_limit_returns_503(monkeypatch):
    monkeypatch.setattr(config, "MAX_INFLIGHT_PLANS", 1)
    # Drain the module-level semaphore and hold the only slot.
    main_mod._plan_slots = threading.Semaphore(1)
    assert main_mod._plan_slots.acquire(blocking=False)

    try:
        r = client.get(
            "/api/route",
            params={
                "from_lat": 54.6,
                "from_lng": -3.1,
                "to_lat": 54.4,
                "to_lng": -2.9,
            },
        )
        assert r.status_code == 503
        assert "Too many plans" in r.json()["error"]
    finally:
        main_mod._plan_slots.release()
