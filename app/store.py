"""SQLite persistence for scored grid cells and grid metadata."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from . import config


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cells (
                id       TEXT PRIMARY KEY,
                row      INTEGER NOT NULL,
                col      INTEGER NOT NULL,
                lat      REAL NOT NULL,
                lng      REAL NOT NULL,
                min_lat  REAL NOT NULL,
                min_lng  REAL NOT NULL,
                max_lat  REAL NOT NULL,
                max_lng  REAL NOT NULL,
                score    REAL NOT NULL,
                green    REAL NOT NULL,
                blue     REAL NOT NULL,
                grey     REAL NOT NULL,
                source   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cells_rowcol ON cells(row, col);

            CREATE TABLE IF NOT EXISTS saved_routes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                notes       TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '',
                favourite   INTEGER NOT NULL DEFAULT 0,
                rating      INTEGER NOT NULL DEFAULT 0,
                from_lat    REAL NOT NULL,
                from_lng    REAL NOT NULL,
                to_lat      REAL NOT NULL,
                to_lng      REAL NOT NULL,
                preference  REAL NOT NULL DEFAULT 0.7,
                profile     TEXT NOT NULL DEFAULT 'balanced',
                distance_km REAL,
                duration_min REAL,
                scenic_score REAL,
                geojson     TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_saved_fav ON saved_routes(favourite);

            CREATE TABLE IF NOT EXISTS route_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                from_lat   REAL, from_lng REAL, to_lat REAL, to_lng REAL,
                preference REAL, profile TEXT,
                distance_km REAL, duration_min REAL, scenic_score REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )


def set_meta(key: str, value) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


def get_meta(key: str) -> Optional[object]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def upsert_cell(cell, score) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cells
                (id, row, col, lat, lng, min_lat, min_lng, max_lat, max_lng,
                 score, green, blue, grey, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cell.id, cell.row, cell.col, cell.lat, cell.lng,
                cell.min_lat, cell.min_lng, cell.max_lat, cell.max_lng,
                score.score, score.green_frac, score.blue_frac,
                score.grey_frac, score.source,
            ),
        )


def all_cells() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM cells").fetchall()
    return [dict(r) for r in rows]


def cells_in_bbox(min_lat, min_lng, max_lat, max_lng) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM cells
            WHERE max_lat >= ? AND min_lat <= ? AND max_lng >= ? AND min_lng <= ?
            """,
            (min_lat, max_lat, min_lng, max_lng),
        ).fetchall()
    return [dict(r) for r in rows]


def count_cells() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]


def log_history(from_lat, from_lng, to_lat, to_lng, preference, profile,
                distance_km, duration_min, scenic_score) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO route_history
                (from_lat, from_lng, to_lat, to_lng, preference, profile,
                 distance_km, duration_min, scenic_score)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (from_lat, from_lng, to_lat, to_lng, preference, profile,
             distance_km, duration_min, scenic_score),
        )
