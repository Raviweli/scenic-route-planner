#!/usr/bin/env python3
"""Dogfood smoke for the friends demo (public gate + shared key).

Expects a running friends-up server (default http://127.0.0.1:8000) and
``SCENIC_API_KEY`` in the environment matching the host.

Checks:
  - GET /api/health → public_mode, osrm_mode
  - protected endpoint rejects without key (401)
  - authorized geocode or stream handshake succeeds with key

Usage (from repo root, after friends-up):
  $env:SCENIC_API_KEY = "…"
  python scripts/smoke_friends.py
  # or: .\\scripts\\dev.ps1 smoke-friends
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

# Shared with tests — keep pure / no network.
_API_KEY_RE = re.compile(r"([?&]api_key=)([^&\s\"]+)", re.IGNORECASE)
_API_KEY_QS = re.compile(r"(^|[?&])(api_key=)([^&]*)", re.IGNORECASE)


def redact_query_secrets(text: str) -> str:
    """Strip raw ``api_key`` values from URLs / log lines (replace with ***)."""
    if not text or "api_key=" not in text.lower():
        return text
    out = _API_KEY_RE.sub(r"\1***", text)
    if "://" not in text and text.lower().startswith("api_key="):
        out = _API_KEY_QS.sub(
            lambda m: f"{m.group(1)}{m.group(2)}***" if m.group(3) else m.group(0),
            out,
        )
    return out


def redact_query_string(qs: bytes | str) -> bytes:
    """Redact ``api_key`` in a raw query string (ASGI ``query_string``)."""
    if isinstance(qs, bytes):
        raw = qs.decode("latin-1")
        as_bytes = True
    else:
        raw = qs
        as_bytes = False
    if "api_key=" not in raw.lower():
        return qs if as_bytes else raw.encode("latin-1")
    out = _API_KEY_QS.sub(
        lambda m: f"{m.group(1)}{m.group(2)}***" if m.group(3) else m.group(0),
        raw,
    )
    return out.encode("latin-1")


def validate_health_payload(body: dict[str, Any], *, expect_public: bool = True) -> list[str]:
    """Return a list of validation errors (empty = OK)."""
    errs: list[str] = []
    if body.get("status") != "ok":
        errs.append(f"status={body.get('status')!r} (want 'ok')")
    if expect_public and body.get("public_mode") is not True:
        errs.append(f"public_mode={body.get('public_mode')!r} (want True)")
    mode = body.get("osrm_mode")
    if mode not in ("public_demo", "byo"):
        errs.append(f"osrm_mode={mode!r} (want public_demo|byo)")
    if "max_inflight_plans" not in body:
        errs.append("missing max_inflight_plans")
    return errs


def _base_url() -> str:
    return os.environ.get("SCENIC_SMOKE_BASE", "http://127.0.0.1:8000").rstrip("/")


def main() -> int:
    base = _base_url()
    key = os.environ.get("SCENIC_API_KEY", "").strip()
    if not key:
        print("FAIL: SCENIC_API_KEY is not set (must match the friends-up host).", file=sys.stderr)
        return 1

    print(f"smoke-friends → {base}")

    try:
        health = requests.get(f"{base}/api/health", timeout=10)
    except requests.RequestException as exc:
        print(f"FAIL: cannot reach {base}/api/health: {exc}", file=sys.stderr)
        print("  Start the host first: .\\scripts\\dev.ps1 friends-up", file=sys.stderr)
        return 1

    if health.status_code != 200:
        print(f"FAIL: /api/health HTTP {health.status_code}", file=sys.stderr)
        return 1
    body = health.json()
    errs = validate_health_payload(body, expect_public=True)
    if errs:
        print("FAIL: health payload:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"  health OK  public_mode={body['public_mode']}  osrm_mode={body['osrm_mode']}")

    # Reject without key.
    unauth = requests.get(f"{base}/api/geocode", params={"q": "Keswick"}, timeout=15)
    if unauth.status_code != 401:
        print(f"FAIL: expected 401 without key, got {unauth.status_code}", file=sys.stderr)
        return 1
    print("  reject without key OK (401)")

    # Authorized geocode (header).
    auth = requests.get(
        f"{base}/api/geocode",
        params={"q": "Keswick"},
        headers={"X-API-Key": key},
        timeout=20,
    )
    if auth.status_code != 200:
        print(f"FAIL: authorized geocode HTTP {auth.status_code}: {auth.text[:200]}", file=sys.stderr)
        return 1
    print("  authorized geocode OK")

    # SSE stream handshake via query key (EventSource path) — read first event.
    stream_url = (
        f"{base}/api/route/stream"
        f"?from_lat=54.6009&from_lng=-3.1371"
        f"&to_lat=54.4287&to_lng=-2.9613"
        f"&preference=0&profile=balanced"
        f"&api_key={key}"
    )
    # Sanity: redacted form must not contain the raw key.
    redacted = redact_query_secrets(stream_url)
    if key in redacted:
        print("FAIL: redact_query_secrets left raw key in URL", file=sys.stderr)
        return 1

    try:
        with requests.get(stream_url, stream=True, timeout=30) as resp:
            if resp.status_code != 200:
                print(
                    f"FAIL: stream handshake HTTP {resp.status_code}",
                    file=sys.stderr,
                )
                return 1
            # Read until first SSE data line or a small byte budget.
            buf = ""
            for chunk in resp.iter_content(chunk_size=256, decode_unicode=True):
                if not chunk:
                    continue
                buf += chunk
                if "\n\n" in buf or len(buf) > 4096:
                    break
            if "data:" not in buf:
                print(f"FAIL: stream opened but no SSE data: {buf[:200]!r}", file=sys.stderr)
                return 1
    except requests.RequestException as exc:
        print(f"FAIL: stream handshake: {exc}", file=sys.stderr)
        return 1
    print("  stream handshake OK (query api_key)")

    # Ensure split/join helpers don't leak via path logging shape.
    parts = urlsplit(stream_url)
    safe_qs = redact_query_string(parts.query.encode("utf-8")).decode("latin-1")
    safe_url = urlunsplit((parts.scheme, parts.netloc, parts.path, safe_qs, ""))
    if key in safe_url:
        print("FAIL: redacted stream URL still contains key", file=sys.stderr)
        return 1

    print("OK: friends smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
