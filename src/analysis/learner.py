"""
Self-improvement / learning module.

After each gameweek resolves, this module:
1. Fetches actual GW points for all players in our squad
2. Computes residuals: actual - predicted for each decision
3. Uses scipy.optimize to tune the xP model weights to minimise MSE
4. Persists updated weights to DB

The updated weights are loaded by ExpectedPointsCalculator for the next GW.

Weight bounds prevent degenerate fits (e.g. weights going to 0 or infinity).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from scipy.optimize import minimize

from src.config.settings import settings
from src.data.database import (
    get_decisions_for_learner,
    get_latest_model_weights,
    save_model_weights,
)

logger = logging.getLogger(__name__)

# Weight names and their bounds (min, max)
WEIGHT_BOUNDS: dict[str, tuple[float, float]] = {
    "xp_weight_xg": (0.5, 2.0),
    "xp_weight_xa": (0.5, 2.0),
    "xp_weight_cs": (0.3, 2.0),
    "xp_weight_bonus": (0.2, 3.0),
    "xp_weight_minutes": (0.5, 2.0),
    "xp_weight_fdr": (0.0, 0.5),
}

WEIGHT_KEYS = list(WEIGHT_BOUNDS.keys())


def _weights_to_array(weights: dict) -> np.ndarray:
    return np.array([weights.get(k, getattr(settings, k)) for k in WEIGHT_KEYS])


def _array_to_weights(arr: np.ndarray) -> dict:
    return {k: float(v) for k, v in zip(WEIGHT_KEYS, arr)}


def _mse(w_arr: np.ndarray, records: list[dict]) -> float:
    """
    Compute mean squared error of predicted vs actual points.

    Each record has:
      - predicted_xp: the xP we predicted at decision time
      - actual_player_pts: actual FPL points earned

    We apply a simple linear rescaling of predicted_xp by the weight vector
    to approximate how well-calibrated the weights are.
    This is a simplified proxy (a full re-run of the xP model per record
    would be expensive and requires the full fixture context).
    """
    total_sq_err = 0.0
    for rec in records:
        predicted = rec.get("predicted_xp", 0.0) or 0.0
        actual = rec.get("actual_player_pts") or rec.get("actual_pts")
        if actual is None:
            continue
        actual = float(actual)
        # The ratio of new weights vs default weights approximates the scaling
        default_w = _weights_to_array({})
        scale = float(np.mean(w_arr / np.maximum(default_w, 1e-6)))
        scaled_predicted = predicted * scale
        total_sq_err += (scaled_predicted - actual) ** 2

    return total_sq_err / max(len(records), 1)


async def run_learning_cycle(current_gw: int) -> Optional[dict]:
    """
    Main entry point called after each GW resolves.

    Loads decision history, optimises weights, persists result.
    Returns the updated weights dict, or None if insufficient data.
    """
    if current_gw < settings.learner_min_gws:
        logger.info(
            "Learner: only GW %d complete (min %d required) — skipping weight tuning",
            current_gw,
            settings.learner_min_gws,
        )
        return None

    records = await get_decisions_for_learner(settings.learner_history_gws)
    valid_records = [r for r in records if r.get("actual_player_pts") is not None]

    if len(valid_records) < 10:
        logger.info("Learner: only %d valid records — insufficient for tuning", len(valid_records))
        return None

    logger.info("Learner: tuning weights on %d decision records", len(valid_records))

    # Load current weights as starting point
    current_weights = await get_latest_model_weights() or {}
    x0 = _weights_to_array(current_weights)

    bounds = [WEIGHT_BOUNDS[k] for k in WEIGHT_KEYS]

    result = minimize(
        fun=_mse,
        x0=x0,
        args=(valid_records,),
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 500, "ftol": 1e-9},
    )

    if not result.success:
        logger.warning("Learner: optimisation did not fully converge: %s", result.message)

    new_weights = _array_to_weights(result.x)
    mse = float(result.fun)

    logger.info("Learner: updated weights (MSE=%.4f):", mse)
    for k, v in new_weights.items():
        old_v = current_weights.get(k, getattr(settings, k))
        logger.info("  %s: %.4f → %.4f", k, old_v, v)

    await save_model_weights(
        gw=current_gw,
        weights=new_weights,
        mse=mse,
        trained_on=len(valid_records),
    )
    return new_weights


async def load_current_weights() -> dict:
    """
    Load the latest learned weights from DB, falling back to settings defaults.
    """
    db_weights = await get_latest_model_weights()
    if db_weights:
        logger.debug("Loaded learned model weights from DB")
        return db_weights
    logger.debug("No learned weights in DB — using settings defaults")
    return {}


def weight_drift_report(old_weights: dict, new_weights: dict) -> str:
    """Human-readable summary of how weights changed."""
    lines = ["Weight update summary:"]
    for k in WEIGHT_KEYS:
        old = old_weights.get(k, getattr(settings, k, 1.0))
        new = new_weights.get(k, old)
        direction = "↑" if new > old else ("↓" if new < old else "=")
        lines.append(f"  {k}: {old:.3f} {direction} {new:.3f}")
    return "\n".join(lines)
