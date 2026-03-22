"""
JSON API endpoints for htmx partial updates and direct data access.
"""

from __future__ import annotations

import os
import re
import logging
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from dashboard.analysis_runner import run, get_cached, DashboardData

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ── /api/analyse — trigger fresh analysis, return squad partial ───────────────

@router.post("/analyse", response_class=HTMLResponse)
async def analyse(request: Request):
    """
    Run a fresh analysis (bypassing cache).
    Returns the squad-cards partial HTML for htmx to swap in.
    """
    try:
        data = await run(force=True)
        return templates.TemplateResponse("partials/squad_cards.html", {
            "request": request,
            "data": data,
        })
    except Exception as exc:
        logger.exception("Analysis failed: %s", exc)
        return HTMLResponse(
            content=f'<div class="text-red-600 p-4">Analysis failed: {exc}</div>',
            status_code=500,
        )


# ── /api/players — JSON player list ──────────────────────────────────────────

@router.get("/players")
async def api_players(
    pos: str = "",
    dgw: bool = False,
    bgw_exclude: bool = False,
    available: bool = False,
    limit: int = 100,
) -> JSONResponse:
    data = await get_cached()
    if not data:
        return JSONResponse({"error": "No data yet — trigger /api/analyse first"}, status_code=503)

    players = data.all_players_ranked
    if pos:
        players = [p for p in players if p.player.position.value == pos.upper()]
    if dgw:
        players = [p for p in players if p.is_dgw]
    if bgw_exclude:
        players = [p for p in players if not p.is_bgw]
    if available:
        players = [p for p in players if p.player.available]

    return JSONResponse([
        {
            "id": p.player.id,
            "name": p.player.web_name,
            "team": p.player.team,
            "position": p.player.position.value,
            "price": p.player.price,
            "xp_next_gw": p.xp_next_gw,
            "xp_lookahead": p.xp_lookahead,
            "is_dgw": p.is_dgw,
            "is_bgw": p.is_bgw,
            "fdr": p.fdr_next_gw,
            "form": p.player.form,
            "ownership_pct": p.ownership_pct,
            "status": p.player.status,
            "news": p.player.news,
        }
        for p in players[:limit]
    ])


# ── /api/fixtures — JSON fixture grid ────────────────────────────────────────

@router.get("/fixtures")
async def api_fixtures() -> JSONResponse:
    data = await get_cached()
    if not data:
        return JSONResponse({"error": "No data yet"}, status_code=503)

    team_map = data.bootstrap.team_map
    result = {}
    for team_id, gw_infos in data.fixture_grid.items():
        team = team_map.get(team_id)
        result[team_id] = {
            "team_name": team.short_name if team else str(team_id),
            "fixtures": [
                {
                    "gw": info.gw,
                    "fixture_count": info.fixture_count,
                    "is_dgw": info.is_dgw,
                    "is_bgw": info.is_bgw,
                    "fdr": info.average_fdr,
                    "multiplier": info.dgw_multiplier,
                }
                for info in gw_infos
            ],
        }
    return JSONResponse(result)


# ── /api/settings GET — return current threshold values ──────────────────────

@router.get("/settings")
async def api_get_settings() -> JSONResponse:
    from src.config.settings import settings
    return JSONResponse({
        "max_hit_per_gw": settings.max_hit_per_gw,
        "lookahead_gameweeks": settings.lookahead_gameweeks,
        "lookahead_decay": settings.lookahead_decay,
        "wildcard_xp_threshold": settings.wildcard_xp_threshold,
        "wildcard_bgw_min_players": settings.wildcard_bgw_min_players,
        "free_hit_bgw_min_starters": settings.free_hit_bgw_min_starters,
        "triple_captain_xp_multiplier": settings.triple_captain_xp_multiplier,
        "bench_boost_min_bench_xp": settings.bench_boost_min_bench_xp,
        "xp_weight_xg": settings.xp_weight_xg,
        "xp_weight_xa": settings.xp_weight_xa,
        "xp_weight_cs": settings.xp_weight_cs,
        "xp_weight_fdr": settings.xp_weight_fdr,
        "minutes_before_deadline": settings.minutes_before_deadline,
    })


# ── /api/settings POST — save updated thresholds to .env.local ───────────────

@router.post("/settings")
async def api_save_settings(
    request: Request,
    max_hit_per_gw: int = Form(8),
    lookahead_gameweeks: int = Form(3),
    lookahead_decay: float = Form(0.85),
    wildcard_xp_threshold: float = Form(0.85),
    wildcard_bgw_min_players: int = Form(4),
    free_hit_bgw_min_starters: int = Form(5),
    triple_captain_xp_multiplier: float = Form(1.6),
    bench_boost_min_bench_xp: float = Form(20.0),
    xp_weight_xg: float = Form(1.0),
    xp_weight_xa: float = Form(1.0),
    xp_weight_cs: float = Form(1.0),
    xp_weight_fdr: float = Form(0.15),
):
    """Write updated thresholds to .env.local and invalidate cache."""
    updates = {
        "MAX_HIT_PER_GW": str(max_hit_per_gw),
        "LOOKAHEAD_GAMEWEEKS": str(lookahead_gameweeks),
        "LOOKAHEAD_DECAY": str(lookahead_decay),
        "WILDCARD_XP_THRESHOLD": str(wildcard_xp_threshold),
        "WILDCARD_BGW_MIN_PLAYERS": str(wildcard_bgw_min_players),
        "FREE_HIT_BGW_MIN_STARTERS": str(free_hit_bgw_min_starters),
        "TRIPLE_CAPTAIN_XP_MULTIPLIER": str(triple_captain_xp_multiplier),
        "BENCH_BOOST_MIN_BENCH_XP": str(bench_boost_min_bench_xp),
        "XP_WEIGHT_XG": str(xp_weight_xg),
        "XP_WEIGHT_XA": str(xp_weight_xa),
        "XP_WEIGHT_CS": str(xp_weight_cs),
        "XP_WEIGHT_FDR": str(xp_weight_fdr),
    }

    env_path = ".env.local"
    _update_env_file(env_path, updates)
    logger.info("Settings saved to %s", env_path)

    # Reload settings in-process
    from importlib import reload
    import src.config.settings as settings_module
    reload(settings_module)

    # Invalidate cache so next analysis uses new settings
    from dashboard import analysis_runner
    analysis_runner._cache = None
    analysis_runner._cache_time = None

    return RedirectResponse(url="/settings?saved=1", status_code=303)


def _update_env_file(path: str, updates: dict[str, str]) -> None:
    """Write/update key=value pairs in an env file."""
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    written_keys: set[str] = set()
    new_lines = []
    for line in lines:
        match = re.match(r"^([A-Z_]+)=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            new_lines.append(f"{key}={updates[key]}\n")
            written_keys.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in written_keys:
            new_lines.append(f"{key}={value}\n")

    with open(path, "w") as f:
        f.writelines(new_lines)
