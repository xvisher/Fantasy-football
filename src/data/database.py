"""
SQLite persistence layer.

Tables:
  players          — snapshot of every player each GW
  player_history   — per-player per-GW stats (for xG rolling window)
  xg_stats         — Understat xG/xA enriched data
  model_weights    — current learned xP model coefficients
  decisions_log    — every action taken + actual outcome (for self-improvement)
  gameweeks        — GW metadata, deadlines
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import aiosqlite

from src.config.settings import settings

logger = logging.getLogger(__name__)


CREATE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS gameweeks (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    deadline_time   TEXT NOT NULL,
    finished        INTEGER NOT NULL DEFAULT 0,
    is_current      INTEGER NOT NULL DEFAULT 0,
    is_next         INTEGER NOT NULL DEFAULT 0,
    average_score   INTEGER,
    highest_score   INTEGER,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS players (
    id              INTEGER NOT NULL,
    gw              INTEGER NOT NULL,
    web_name        TEXT NOT NULL,
    team_id         INTEGER NOT NULL,
    element_type    INTEGER NOT NULL,
    now_cost        INTEGER NOT NULL,
    status          TEXT NOT NULL,
    chance_of_playing INTEGER,
    form            REAL NOT NULL DEFAULT 0,
    total_points    INTEGER NOT NULL DEFAULT 0,
    selected_by_pct REAL NOT NULL DEFAULT 0,
    xg_season       REAL NOT NULL DEFAULT 0,
    xa_season       REAL NOT NULL DEFAULT 0,
    news            TEXT,
    raw_json        TEXT,
    PRIMARY KEY (id, gw)
);

CREATE TABLE IF NOT EXISTS player_history (
    player_id       INTEGER NOT NULL,
    gw              INTEGER NOT NULL,
    minutes         INTEGER NOT NULL DEFAULT 0,
    goals_scored    INTEGER NOT NULL DEFAULT 0,
    assists         INTEGER NOT NULL DEFAULT 0,
    clean_sheets    INTEGER NOT NULL DEFAULT 0,
    bonus           INTEGER NOT NULL DEFAULT 0,
    total_points    INTEGER NOT NULL DEFAULT 0,
    xg              REAL NOT NULL DEFAULT 0,
    xa              REAL NOT NULL DEFAULT 0,
    opponent_team   INTEGER,
    was_home        INTEGER,
    value           INTEGER,
    PRIMARY KEY (player_id, gw)
);

CREATE TABLE IF NOT EXISTS xg_stats (
    player_id           INTEGER NOT NULL,
    gw                  INTEGER NOT NULL,
    understat_id        INTEGER,
    xg_per90            REAL NOT NULL DEFAULT 0,
    xa_per90            REAL NOT NULL DEFAULT 0,
    npxg_per90          REAL NOT NULL DEFAULT 0,
    shots_per90         REAL NOT NULL DEFAULT 0,
    key_passes_per90    REAL NOT NULL DEFAULT 0,
    source              TEXT NOT NULL DEFAULT 'understat',
    fetched_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (player_id, gw)
);

CREATE TABLE IF NOT EXISTS model_weights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gw              INTEGER NOT NULL,
    weights_json    TEXT NOT NULL,
    mse             REAL,
    trained_on_gws  INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gw              INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    player_in_id    INTEGER,
    player_out_id   INTEGER,
    chip_used       TEXT,
    predicted_xp    REAL NOT NULL DEFAULT 0,
    actual_pts      REAL,
    reasoning       TEXT,
    dry_run         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_players_gw ON players (gw);
CREATE INDEX IF NOT EXISTS idx_player_history_player ON player_history (player_id);
CREATE INDEX IF NOT EXISTS idx_decisions_gw ON decisions_log (gw);
"""


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript(CREATE_SCHEMA)
        await db.commit()
    logger.info("Database initialised at %s", settings.database_path)


# ── Gameweeks ─────────────────────────────────────────────────────────────────

async def upsert_gameweeks(gws: list[dict]) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executemany(
            """
            INSERT INTO gameweeks (id, name, deadline_time, finished, is_current, is_next, average_score, highest_score)
            VALUES (:id, :name, :deadline_time, :finished, :is_current, :is_next, :average_score, :highest_score)
            ON CONFLICT(id) DO UPDATE SET
                deadline_time = excluded.deadline_time,
                finished      = excluded.finished,
                is_current    = excluded.is_current,
                is_next       = excluded.is_next,
                average_score = excluded.average_score,
                highest_score = excluded.highest_score,
                updated_at    = datetime('now')
            """,
            gws,
        )
        await db.commit()


async def get_upcoming_deadlines() -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, deadline_time FROM gameweeks WHERE finished = 0 ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ── Players ───────────────────────────────────────────────────────────────────

async def upsert_players(gw: int, players: list[dict]) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executemany(
            """
            INSERT INTO players
                (id, gw, web_name, team_id, element_type, now_cost, status,
                 chance_of_playing, form, total_points, selected_by_pct,
                 xg_season, xa_season, news, raw_json)
            VALUES
                (:id, :gw, :web_name, :team_id, :element_type, :now_cost, :status,
                 :chance_of_playing, :form, :total_points, :selected_by_pct,
                 :xg_season, :xa_season, :news, :raw_json)
            ON CONFLICT(id, gw) DO UPDATE SET
                now_cost          = excluded.now_cost,
                status            = excluded.status,
                chance_of_playing = excluded.chance_of_playing,
                form              = excluded.form,
                total_points      = excluded.total_points,
                selected_by_pct   = excluded.selected_by_pct,
                xg_season         = excluded.xg_season,
                xa_season         = excluded.xa_season,
                news              = excluded.news,
                raw_json          = excluded.raw_json
            """,
            players,
        )
        await db.commit()


# ── Player history ────────────────────────────────────────────────────────────

async def upsert_player_history(records: list[dict]) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executemany(
            """
            INSERT INTO player_history
                (player_id, gw, minutes, goals_scored, assists, clean_sheets,
                 bonus, total_points, xg, xa, opponent_team, was_home, value)
            VALUES
                (:player_id, :gw, :minutes, :goals_scored, :assists, :clean_sheets,
                 :bonus, :total_points, :xg, :xa, :opponent_team, :was_home, :value)
            ON CONFLICT(player_id, gw) DO UPDATE SET
                total_points = excluded.total_points,
                bonus        = excluded.bonus
            """,
            records,
        )
        await db.commit()


async def get_player_history(player_id: int, last_n_gws: int) -> list[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM player_history
            WHERE player_id = ?
            ORDER BY gw DESC
            LIMIT ?
            """,
            (player_id, last_n_gws),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ── xG stats ──────────────────────────────────────────────────────────────────

async def upsert_xg_stats(records: list[dict]) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executemany(
            """
            INSERT INTO xg_stats
                (player_id, gw, understat_id, xg_per90, xa_per90, npxg_per90,
                 shots_per90, key_passes_per90, source)
            VALUES
                (:player_id, :gw, :understat_id, :xg_per90, :xa_per90,
                 :npxg_per90, :shots_per90, :key_passes_per90, :source)
            ON CONFLICT(player_id, gw) DO UPDATE SET
                xg_per90         = excluded.xg_per90,
                xa_per90         = excluded.xa_per90,
                npxg_per90       = excluded.npxg_per90,
                shots_per90      = excluded.shots_per90,
                key_passes_per90 = excluded.key_passes_per90,
                fetched_at       = datetime('now')
            """,
            records,
        )
        await db.commit()


async def get_xg_stats(player_ids: list[int], gw: int) -> dict[int, dict]:
    if not player_ids:
        return {}
    placeholders = ",".join("?" * len(player_ids))
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM xg_stats WHERE player_id IN ({placeholders}) AND gw = ?",
            (*player_ids, gw),
        )
        rows = await cursor.fetchall()
        return {r["player_id"]: dict(r) for r in rows}


# ── Model weights ─────────────────────────────────────────────────────────────

async def save_model_weights(gw: int, weights: dict, mse: float, trained_on: int) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            """
            INSERT INTO model_weights (gw, weights_json, mse, trained_on_gws)
            VALUES (?, ?, ?, ?)
            """,
            (gw, json.dumps(weights), mse, trained_on),
        )
        await db.commit()
    logger.info("Saved model weights for GW %d (MSE=%.4f)", gw, mse)


async def get_latest_model_weights() -> Optional[dict]:
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT weights_json FROM model_weights ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row["weights_json"])
        return None


# ── Decision log ──────────────────────────────────────────────────────────────

async def log_decision(
    gw: int,
    action_type: str,
    predicted_xp: float,
    reasoning: str,
    player_in_id: Optional[int] = None,
    player_out_id: Optional[int] = None,
    chip_used: Optional[str] = None,
) -> int:
    async with aiosqlite.connect(settings.database_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO decisions_log
                (gw, timestamp, action_type, player_in_id, player_out_id,
                 chip_used, predicted_xp, reasoning, dry_run)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gw,
                datetime.utcnow().isoformat(),
                action_type,
                player_in_id,
                player_out_id,
                chip_used,
                predicted_xp,
                reasoning,
                int(settings.dry_run),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def update_decision_actual_pts(decision_id: int, actual_pts: float) -> None:
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute(
            "UPDATE decisions_log SET actual_pts = ? WHERE id = ?",
            (actual_pts, decision_id),
        )
        await db.commit()


async def get_decisions_for_learner(last_n_gws: int) -> list[dict]:
    """Returns transfer decisions with both predicted_xp and actual_pts for weight tuning."""
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT d.*, ph.total_points as actual_player_pts
            FROM decisions_log d
            LEFT JOIN player_history ph
                ON ph.player_id = d.player_in_id AND ph.gw = d.gw
            WHERE d.action_type = 'transfer'
              AND d.actual_pts IS NOT NULL
              AND d.dry_run = 0
            ORDER BY d.gw DESC
            LIMIT ?
            """,
            (last_n_gws * 3,),  # fetch more rows as there may be multiple decisions per GW
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
