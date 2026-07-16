#!/usr/bin/env python3
"""Demo corridor QA matrix — real planning via API/SSE.

Runs road-first, field, compare_field, and avoid_motorways against featured
presets and docs/demo_corridors.txt extras. Intended for pre-demo dogfood.

Usage (server must be running):
  python scripts/demo_corridor_qa.py
  python scripts/demo_corridor_qa.py --base http://127.0.0.1:8077 --json
  python scripts/demo_corridor_qa.py --quick   # UK + 3 world corridors only
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog_data import featured_presets  # noqa: E402

_SSE_DATA = re.compile(r"^data:\s*(.+)$", re.MULTILINE)


def _load_demo_corridors(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            fl, fg, tl, tg = map(float, parts[:4])
        except ValueError:
            continue
        label = parts[4] if len(parts) >= 5 else f"corridor-{i}"
        rows.append({
            "id": f"demo-{label.lower().replace(' ', '-')}",
            "name": label,
            "from": {"lat": fl, "lng": fg},
            "to": {"lat": tl, "lng": tg},
            "profile": "balanced",
            "preference": 0.75,
            "scope": "demo",
            "tags": ["demo-corridor"],
        })
    return rows


def _corridors(quick: bool) -> list[dict[str, Any]]:
    featured = featured_presets()
    extras = _load_demo_corridors(ROOT / "docs" / "demo_corridors.txt")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in featured + extras:
        key = (
            round(row["from"]["lat"], 4),
            round(row["from"]["lng"], 4),
            round(row["to"]["lat"], 4),
            round(row["to"]["lng"], 4),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    if quick:
        uk = [r for r in out if r.get("scope") == "uk"][:4]
        world = [r for r in out if r.get("scope") == "world"][:3]
        demo = [r for r in out if r.get("scope") == "demo"]
        return uk + world + demo
    return out


def _sse_done(base: str, path: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    url = f"{base.rstrip('/')}{path}?{urlencode(params, doseq=True)}"
    t0 = time.perf_counter()
    with requests.get(url, stream=True, timeout=(10, timeout)) as resp:
        resp.raise_for_status()
        buf = ""
        for chunk in resp.iter_content(decode_unicode=True, chunk_size=None):
            if not chunk:
                continue
            buf += chunk
            for m in _SSE_DATA.finditer(buf):
                try:
                    ev = json.loads(m.group(1))
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "error":
                    return {
                        "ok": False,
                        "error": ev.get("message", "SSE error"),
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                    }
                if ev.get("type") == "done":
                    result = ev.get("result") or ev
                    return {
                        "ok": True,
                        "result": result,
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                    }
    return {"ok": False, "error": "no done event", "elapsed_s": round(time.perf_counter() - t0, 1)}


def _route_ok(payload: dict[str, Any], *, avoid_mw: bool) -> tuple[bool, str]:
    """Validate a plan result or compare leg (chosen route dict)."""
    if payload.get("error"):
        return False, str(payload["error"])
    # Compare legs are flat route cards; full plans nest under "chosen".
    chosen = payload.get("chosen") if "chosen" in payload and isinstance(payload.get("chosen"), dict) else payload
    if not isinstance(chosen, dict):
        return False, "no route payload"
    dist = float(chosen.get("distance_km") or 0)
    score = float(chosen.get("avg_scenic_score") or 0)
    if dist < 1.0:
        return False, f"distance too short ({dist} km)"
    if score <= 0:
        return False, "zero scenic score"
    notes: list[str] = []
    budget_exhausted = payload.get("budget_exhausted") if "budget_exhausted" in payload else None
    budget_reasons = payload.get("budget_reasons") or []
    if budget_exhausted:
        notes.append(f"budget:{','.join(budget_reasons) or 'yes'}")
    sig = payload.get("signals") or {}
    if sig.get("landcover_incomplete"):
        notes.append("landcover_incomplete")
    avoid_met = payload.get("motorway_avoid_met", chosen.get("motorway_avoid_met"))
    if avoid_mw and avoid_met is False:
        reason = payload.get("motorway_avoid_reason") or chosen.get("motorway_avoid_reason") or "unmet"
        notes.append(f"mw_unmet:{reason}")
    return True, "; ".join(notes) if notes else "ok"


def _field_ok(result: dict[str, Any], *, avoid_mw: bool) -> tuple[bool, str]:
    ok, note = _route_ok(result, avoid_mw=avoid_mw)
    if not ok:
        return ok, note
    meta = result.get("field_meta") or {}
    extra: list[str] = []
    cells = meta.get("cells_scored", 0)
    proxy = meta.get("proxy_cells", 0)
    colour = meta.get("colour_cells", 0)
    if proxy and colour < cells:
        extra.append(f"proxy_only:{proxy}/{cells}")
    if meta.get("budget_reasons"):
        extra.append(f"field_budget:{','.join(meta['budget_reasons'])}")
    chosen_reason = meta.get("chosen_reason")
    if chosen_reason and "fallback" in str(chosen_reason).lower():
        extra.append(f"fallback:{chosen_reason}")
    if avoid_mw and result.get("motorway_avoid_met") is False:
        extra.append(f"mw:{result.get('motorway_avoid_reason')}")
    if note != "ok":
        extra.insert(0, note)
    return True, "; ".join(extra) if extra else "ok"


def _compare_field_ok(result: dict[str, Any]) -> tuple[bool, str]:
    road = result.get("road")
    field = result.get("field")
    road_meta = result.get("road_meta") or {}
    field_meta_wrap = result.get("field_meta") or {}
    if not road or not field:
        return False, "missing road or field leg"
    road_payload = {**road_meta, **road}
    field_payload = {**field_meta_wrap, **field, "field_meta": field_meta_wrap.get("field_meta")}
    rok, rnote = _route_ok(road_payload, avoid_mw=False)
    fok, fnote = _field_ok(field_payload, avoid_mw=False)
    if not rok:
        return False, f"road: {rnote}"
    if not fok:
        return False, f"field: {fnote}"
    return True, f"road={rnote}; field={fnote}"


def _base_params(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "from_lat": row["from"]["lat"],
        "from_lng": row["from"]["lng"],
        "to_lat": row["to"]["lat"],
        "to_lng": row["to"]["lng"],
        "profile": row.get("profile") or "balanced",
        "preference": row.get("preference", 0.75),
        "time_budget": "true",
    }


def run_case(
    base: str,
    row: dict[str, Any],
    mode: str,
    *,
    timeout: float,
    avoid_mw: bool = False,
) -> dict[str, Any]:
    params = _base_params(row)
    if avoid_mw:
        params["avoid_motorways"] = "true"
    t0 = time.perf_counter()
    try:
        if mode == "road":
            r = _sse_done(base, "/api/route/stream", params, timeout)
            if not r["ok"]:
                return {"pass": False, "note": r.get("error"), "elapsed_s": r["elapsed_s"]}
            ok, note = _route_ok(r["result"], avoid_mw=avoid_mw)
            return {"pass": ok, "note": note, "elapsed_s": r["elapsed_s"]}
        if mode == "field":
            r = _sse_done(base, "/api/route/field/stream", params, timeout)
            if not r["ok"]:
                return {"pass": False, "note": r.get("error"), "elapsed_s": r["elapsed_s"]}
            ok, note = _field_ok(r["result"], avoid_mw=avoid_mw)
            return {"pass": ok, "note": note, "elapsed_s": r["elapsed_s"]}
        if mode == "compare_field":
            params["compare_field"] = "true"
            r = _sse_done(base, "/api/route/compare/stream", params, timeout)
            if not r["ok"]:
                return {"pass": False, "note": r.get("error"), "elapsed_s": r["elapsed_s"]}
            ok, note = _compare_field_ok(r["result"])
            return {"pass": ok, "note": note, "elapsed_s": r["elapsed_s"]}
        return {"pass": False, "note": f"unknown mode {mode}", "elapsed_s": 0}
    except requests.RequestException as exc:
        return {"pass": False, "note": str(exc), "elapsed_s": round(time.perf_counter() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8077")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--modes", default="road,field,compare_field,road_mw")
    args = ap.parse_args()

    try:
        health = requests.get(f"{args.base.rstrip('/')}/api/health", timeout=10)
        health.raise_for_status()
    except requests.RequestException as exc:
        print(f"FAIL: cannot reach {args.base}/api/health: {exc}", file=sys.stderr)
        return 1

    corridors = _corridors(args.quick)
    mode_list = [m.strip() for m in args.modes.split(",") if m.strip()]
    rows_out: list[dict[str, Any]] = []

    for row in corridors:
        entry: dict[str, Any] = {
            "id": row.get("id", row.get("name")),
            "name": row.get("name", row.get("id")),
            "scope": row.get("scope", "?"),
            "modes": {},
        }
        for mode in mode_list:
            avoid_mw = mode == "road_mw"
            api_mode = "road" if avoid_mw else mode
            res = run_case(args.base, row, api_mode, timeout=args.timeout, avoid_mw=avoid_mw)
            entry["modes"][mode] = res
            print(
                f"[{'PASS' if res['pass'] else 'FAIL'}] {entry['name'][:40]:40} "
                f"{mode:14} {res['elapsed_s']:5.1f}s  {res['note']}"
            )
        rows_out.append(entry)

    passed = sum(
        1 for e in rows_out for m in e["modes"].values() if m["pass"]
    )
    total = sum(len(e["modes"]) for e in rows_out)

    summary = {"passed": passed, "total": total, "corridors": len(rows_out), "rows": rows_out}
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\nPassed {passed}/{total} mode×corridor checks across {len(rows_out)} corridors")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
