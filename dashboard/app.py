"""
FastAPI application — serves the FPL dashboard web UI.

Routes:
  GET  /                   → squad overview (index.html)
  GET  /transfers          → transfer recommendations
  GET  /players            → full player rankings
  GET  /fixtures           → fixture difficulty calendar
  GET  /history            → decision log + model accuracy
  GET  /settings           → tweak thresholds
  POST /api/analyse        → run analysis, return updated squad partial (htmx)
  GET  /api/players        → JSON player list (for JS / htmx filters)
  GET  /api/fixtures       → JSON fixture grid
  GET  /api/settings       → JSON current settings
  POST /api/settings       → save updated settings
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import api as api_router
from dashboard.analysis_runner import get_cached, run

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="FPL Dashboard", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount API sub-router
app.include_router(api_router.router, prefix="/api")


# ── Template helpers ──────────────────────────────────────────────────────────

def _fdr_colour(fdr: float) -> str:
    """Return a Tailwind bg class for a given FDR value."""
    if fdr <= 1.5:
        return "bg-green-700 text-white"
    if fdr <= 2.5:
        return "bg-green-400"
    if fdr <= 3.5:
        return "bg-gray-200"
    if fdr <= 4.5:
        return "bg-red-400 text-white"
    return "bg-red-700 text-white"


def _position_badge(pos: str) -> str:
    colours = {
        "GKP": "bg-yellow-400 text-black",
        "DEF": "bg-green-500 text-white",
        "MID": "bg-blue-500 text-white",
        "FWD": "bg-red-500 text-white",
    }
    return colours.get(pos, "bg-gray-400")


# Register as Jinja2 globals
templates.env.globals["fdr_colour"] = _fdr_colour
templates.env.globals["position_badge"] = _position_badge


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    data = await get_cached()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "data": data,
        "page": "index",
    })


@app.get("/transfers", response_class=HTMLResponse)
async def transfers(request: Request):
    data = await get_cached()
    return templates.TemplateResponse("transfers.html", {
        "request": request,
        "data": data,
        "page": "transfers",
    })


@app.get("/players", response_class=HTMLResponse)
async def players(
    request: Request,
    pos: str = "",
    dgw: bool = False,
    available: bool = False,
):
    data = await get_cached()
    player_list = data.all_players_ranked if data else []

    if pos:
        player_list = [p for p in player_list if p.player.position.value == pos.upper()]
    if dgw:
        player_list = [p for p in player_list if p.is_dgw]
    if available:
        player_list = [p for p in player_list if p.player.available]

    return templates.TemplateResponse("players.html", {
        "request": request,
        "data": data,
        "players": player_list,
        "filter_pos": pos,
        "filter_dgw": dgw,
        "filter_available": available,
        "page": "players",
    })


@app.get("/fixtures", response_class=HTMLResponse)
async def fixtures(request: Request):
    data = await get_cached()
    return templates.TemplateResponse("fixtures.html", {
        "request": request,
        "data": data,
        "page": "fixtures",
    })


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    from src.data.database import get_decisions_for_learner, get_latest_model_weights, init_db
    from src.config.settings import settings
    await init_db()
    decisions = await get_decisions_for_learner(38)
    weights = await get_latest_model_weights() or {}
    data = await get_cached()
    return templates.TemplateResponse("history.html", {
        "request": request,
        "data": data,
        "decisions": decisions,
        "weights": weights,
        "page": "history",
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    data = await get_cached()
    from src.config.settings import settings
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "data": data,
        "settings": settings,
        "page": "settings",
        "saved": request.query_params.get("saved") == "1",
    })


@app.on_event("startup")
async def startup():
    """Pre-warm: run analysis on startup so the UI is ready immediately."""
    from src.data.database import init_db
    await init_db()
    logger.info("Dashboard starting — running initial analysis...")
    try:
        await run(force=True)
        logger.info("Initial analysis complete")
    except Exception as exc:
        logger.warning("Initial analysis failed (will retry on first page load): %s", exc)
