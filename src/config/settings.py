"""
Central configuration for the FPL Automation Bot.

All weights, thresholds, and constants live here. When FPL changes its
scoring rules (as it did with defensive contributions in 2025/26), only
this file needs updating.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── FPL Credentials ──────────────────────────────────────────────────────
    fpl_email: str = Field(default="", description="FPL account email")
    fpl_password: str = Field(default="", description="FPL account password")
    fpl_team_id: int = Field(default=0, description="FPL manager/team ID")

    # ── Bot behaviour ────────────────────────────────────────────────────────
    dry_run: bool = Field(default=True, description="If True, analyse only — no transfers submitted")
    max_hit_per_gw: int = Field(default=8, description="Max transfer hit points accepted per GW (8 = 2 hits)")

    # ── Scheduling ───────────────────────────────────────────────────────────
    minutes_before_deadline: int = Field(default=60, description="Run N minutes before each GW deadline")
    timezone: str = Field(default="Europe/London")
    database_path: str = Field(default="./fpl_bot.db")

    # ── Notifications ────────────────────────────────────────────────────────
    discord_webhook_url: str = Field(default="", description="Discord incoming webhook URL (blank = disabled)")
    slack_webhook_url: str = Field(default="", description="Slack incoming webhook URL (blank = disabled)")

    # ── Self-improvement / learner ────────────────────────────────────────────
    learner_history_gws: int = Field(default=10, description="GWs of history used for weight tuning")
    learner_min_gws: int = Field(default=5, description="Minimum GWs before self-tuning activates")
    lookahead_gameweeks: int = Field(default=3, description="Future GWs considered in optimisation")
    lookahead_decay: float = Field(default=0.85, description="Discount factor per lookahead GW")

    # ── FPL Scoring rules ─────────────────────────────────────────────────────
    # These match the 2025/26 FPL ruleset. Update here if rules change.
    goal_points: dict[str, int] = Field(
        default={"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4},
        description="Points for scoring a goal by position",
    )
    assist_points: int = Field(default=3)
    clean_sheet_points: dict[str, int] = Field(
        default={"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0},
        description="Points for clean sheet by position (60+ min required)",
    )
    minutes_threshold: int = Field(default=60, description="Minutes required for clean sheet / appearance bonus")
    minutes_played_1: int = Field(default=1, description="Points for playing 1-59 minutes")
    minutes_played_2: int = Field(default=2, description="Points for playing 60+ minutes")
    yellow_card_points: int = Field(default=-1)
    red_card_points: int = Field(default=-3)
    own_goal_points: int = Field(default=-2)
    penalty_miss_points: int = Field(default=-2)
    save_points_per_3: int = Field(default=1, description="GK: 1 point per 3 saves")
    bonus_max: int = Field(default=3, description="Max bonus points (top BPS player gets 3)")

    # Defensive contributions (new 2025/26 — update if changed)
    # DEF: 2 pts per 10 CBITs (clearances, blocks, interceptions, tackles)
    def_contribution_pts: int = Field(default=2)
    def_contribution_threshold: int = Field(default=10, description="CBITs required for DEF bonus")
    def_contribution_max_pts: int = Field(default=2, description="Max defensive contribution points per match")
    # MID: 2 pts per 12 CBIRTs
    mid_contribution_pts: int = Field(default=2)
    mid_contribution_threshold: int = Field(default=12, description="CBIRTs required for MID bonus")
    mid_contribution_max_pts: int = Field(default=2)

    # ── xP model weights (tuned by learner.py) ───────────────────────────────
    # Initial defaults — overridden by DB-stored learned weights after GW 5
    xp_weight_xg: float = Field(default=1.0, description="Multiplier on xG contribution to xP")
    xp_weight_xa: float = Field(default=1.0, description="Multiplier on xA contribution to xP")
    xp_weight_cs: float = Field(default=1.0, description="Multiplier on clean sheet probability")
    xp_weight_bonus: float = Field(default=1.0, description="Multiplier on bonus point estimate")
    xp_weight_minutes: float = Field(default=1.0, description="Multiplier on minutes probability")
    xp_weight_fdr: float = Field(default=0.15, description="FDR influence factor (0 = ignore fixtures)")

    # ── Chip strategy thresholds ──────────────────────────────────────────────
    wildcard_xp_threshold: float = Field(
        default=0.85,
        description="Use wildcard if squad xP < best_possible × this value",
    )
    wildcard_bgw_min_players: int = Field(
        default=4,
        description="Use wildcard if this many starters have a BGW",
    )
    free_hit_bgw_min_starters: int = Field(
        default=5,
        description="Use free hit if this many starters have a BGW",
    )
    triple_captain_xp_multiplier: float = Field(
        default=1.6,
        description="Use TC if captain xP > season_avg × this value",
    )
    bench_boost_min_bench_xp: float = Field(
        default=20.0,
        description="Use bench boost if bench total xP exceeds this",
    )

    # ── Squad constraints (should match FPL rules) ────────────────────────────
    squad_size: int = Field(default=15)
    squad_gk: int = Field(default=2)
    squad_def: int = Field(default=5)
    squad_mid: int = Field(default=5)
    squad_fwd: int = Field(default=3)
    max_per_club: int = Field(default=3)
    starting_xi: int = Field(default=11)
    starting_min_gk: int = Field(default=1)
    starting_min_def: int = Field(default=3)
    starting_min_fwd: int = Field(default=1)
    transfer_hit_cost: int = Field(default=4, description="Points deducted per extra transfer")
    max_free_transfer_rollover: int = Field(default=5)
    budget: float = Field(default=100.0, description="Starting budget in £m")

    # ── FPL API ───────────────────────────────────────────────────────────────
    fpl_api_base: str = Field(default="https://fantasy.premierleague.com/api")
    fpl_api_rate_limit_delay: float = Field(default=1.0, description="Seconds between API calls")

    # ── Understat ─────────────────────────────────────────────────────────────
    understat_league: str = Field(default="EPL")
    xg_rolling_gws: int = Field(default=5, description="Rolling window for xG/xA averages")


# Singleton — import this everywhere
settings = Settings()
