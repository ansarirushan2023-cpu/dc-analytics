"""Forecast daily carton volume, and prove the forecast is worth having.

Two models:

  seasonal_naive  -- "next Monday looks like last Monday". The baseline.
                     Deliberately dumb. If the real model can't beat it,
                     the real model isn't earning its complexity.

  holt_winters    -- level + trend + weekly seasonality (ETS).

Both are backtested the same way: hide the last N days, fit on what's
left, predict the hidden days, score against reality. The model never
sees the answers it is graded on.
"""

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from . import config as cfg

log = logging.getLogger(__name__)

FORECAST_FILE = cfg.RAW / "forecast_backtest.parquet"

# How many days to hold back for grading.
TEST_DAYS = 90

# Length of the repeating cycle. 7 = weekly, which is the pattern that
# broke the trailing-average planner.
SEASON_LENGTH = 7


# ------------------------------------------------------------ models

def seasonal_naive(train: pd.Series, horizon: int,
                   season: int = SEASON_LENGTH) -> np.ndarray:
    """Baseline: repeat the last complete week, over and over.

    No fitting, no parameters, no maths beyond copying. That is the
    point -- it is the bar the real model has to clear.
    """
    last_season = train.iloc[-season:].to_numpy()
    repeats = int(np.ceil(horizon / season))
    return np.tile(last_season, repeats)[:horizon]


def holt_winters(train: pd.Series, horizon: int,
                 season: int = SEASON_LENGTH) -> np.ndarray:
    """Level + trend + weekly seasonality.

    additive trend/seasonality: the weekly swing is treated as a fixed
    number of cartons rather than a percentage. Chosen because the
    swing here is driven by store replenishment cycles, which don't
    scale with volume the way a percentage would.

    Falls back to the baseline if the fit fails -- a pipeline that
    dies because one state/zone had a bad series is a pipeline nobody
    can schedule.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = ExponentialSmoothing(
                train,
                trend="add",
                seasonal="add",
                seasonal_periods=season,
                initialization_method="estimated",
            ).fit()
        preds = model.forecast(horizon).to_numpy()

        # Negative cartons are impossible. Clip rather than let a
        # nonsense number flow downstream into labour hours.
        return np.clip(preds, 0, None)

    except Exception as exc:
        log.warning("Holt-Winters failed (%s) -- using baseline", exc)
        return seasonal_naive(train, horizon, season)


# ----------------------------------------------------------- scoring

def score(actual: np.ndarray, predicted: np.ndarray) -> dict:
    """Three numbers, because one is never enough.

    mae   -- average miss, in cartons
    mape  -- average miss as a percentage (comparable across states)
    bias  -- signed average miss. Negative = under-forecasting, which
             means under-staffing, which is the expensive direction.
    """
    err = predicted - actual
    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.abs(err) / np.where(actual == 0, np.nan, actual)

    return {
        "mae": float(np.mean(np.abs(err))),
        "mape": float(np.nanmean(pct) * 100),
        "bias": float(np.mean(err)),
        "bias_pct": float(np.mean(err) / np.mean(actual) * 100),
    }


# --------------------------------------------------------- backtest

def backtest_one(series: pd.Series, test_days: int = TEST_DAYS) -> pd.DataFrame:
    """Run both models on one state/zone series and return predictions."""
    if len(series) < test_days + 4 * SEASON_LENGTH:
        raise ValueError(f"series too short: {len(series)} rows")

    train = series.iloc[:-test_days]
    test = series.iloc[-test_days:]

    return pd.DataFrame({
        "date": test.index,
        "actual": test.to_numpy(),
        "pred_baseline": seasonal_naive(train, test_days),
        "pred_holt_winters": holt_winters(train, test_days),
    })


def run(test_days: int = TEST_DAYS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Backtest every state/zone combination."""
    from .generate_ops import OPS_FILE

    ops = pd.read_parquet(OPS_FILE)
    daily = (
        ops.groupby(["state", "zone", "date"], as_index=False)["cartons_picked"]
           .sum()
           .sort_values("date")
    )

    all_preds, all_scores = [], []

    for (state, zone), grp in daily.groupby(["state", "zone"]):
        series = grp.set_index("date")["cartons_picked"].asfreq("D")
        series = series.interpolate()      # fill any single-day gaps

        preds = backtest_one(series, test_days)
        preds["state"], preds["zone"] = state, zone
        all_preds.append(preds)

        for model in ("baseline", "holt_winters"):
            s = score(preds["actual"].to_numpy(),
                      preds[f"pred_{model}"].to_numpy())
            all_scores.append({"state": state, "zone": zone,
                               "model": model, **s})

    preds_df = pd.concat(all_preds, ignore_index=True)
    scores_df = pd.DataFrame(all_scores)

    preds_df.to_parquet(FORECAST_FILE, index=False)
    log.info("backtested %d series -> %s",
             len(scores_df) // 2, FORECAST_FILE.name)
    return preds_df, scores_df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    preds, scores = run()

    print("\n=== accuracy by model (lower MAPE is better) ===")
    summary = (scores.groupby("model")[["mae", "mape", "bias_pct"]]
                     .mean().round(2))
    print(summary.to_string())

    base = summary.loc["baseline", "mape"]
    hw = summary.loc["holt_winters", "mape"]
    print(f"\nHolt-Winters improves MAPE by "
          f"{(base - hw) / base * 100:.1f}% over the baseline")

    print("\n=== per-zone MAPE ===")
    print(scores.pivot_table(index="zone", columns="model",
                             values="mape").round(2).to_string())
