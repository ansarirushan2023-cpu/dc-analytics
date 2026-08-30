"""Generate the modelled DC operations layer.

    ###################################################################
    #  THIS DATA IS MODELLED, NOT OBSERVED.                           #
    #                                                                 #
    #  No real distribution centre's operational data is used here.   #
    #  Daily carton volume is derived from the ABS state retail        #
    #  turnover signal (which IS real) and then shaped by operating    #
    #  parameters in config.DC_PARAMS chosen to be plausible for a     #
    #  grocery DC.                                                     #
    #                                                                 #
    #  Every result downstream inherits this caveat.                   #
    ###################################################################

Why model it at all: DC operational data is commercially sensitive and
not published by anyone. The alternative to modelling it is not having
a project. Modelling it openly, with the generating parameters visible
and version-controlled, is the honest option -- a reader can inspect
exactly what was assumed and change it.

The demand *signal* is real, so the seasonality, trend and relative
state scale in the output are inherited from published ABS data rather
than invented.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import config as cfg

log = logging.getLogger(__name__)

OPS_FILE = cfg.RAW / "dc_operations.parquet"
ROSTER_FILE = cfg.RAW / "dc_roster_plan.parquet"


def _monthly_to_daily(retail: pd.DataFrame, state: str) -> pd.DataFrame:
    """Spread monthly state turnover across days, applying DOW shape."""
    p = cfg.DC_PARAMS
    rng = np.random.default_rng(cfg.RANDOM_SEED + hash(state) % 1000)

    state_monthly = (
        retail[retail["state"] == state]
        .groupby("period", as_index=False)["turnover_thousands"].sum()
        .sort_values("period")
    )

    frames = []
    for _, row in state_monthly.iterrows():
        month_start = pd.Timestamp(row["period"] + "-01")
        days = pd.date_range(month_start, month_start + pd.offsets.MonthEnd(0),
                             freq="D")

        # Convert monthly turnover to a monthly carton total, then split
        # across days weighted by day-of-week shape.
        month_cartons = row["turnover_thousands"] * p["cartons_per_1k_turnover"]
        weights = np.array([p["dow_multipliers"][d.dayofweek] for d in days])
        weights = weights / weights.sum()

        noise = rng.normal(1.0, p["daily_noise_sd"], size=len(days))
        daily = month_cartons * weights * noise

        frames.append(pd.DataFrame({
            "date": days,
            "state": state,
            "cartons_total": np.round(daily).astype(int),
        }))

    return pd.concat(frames, ignore_index=True)


def build_operations(retail: pd.DataFrame) -> pd.DataFrame:
    """Explode daily volume into per-zone rows with labour required."""
    p = cfg.DC_PARAMS
    rng = np.random.default_rng(cfg.RANDOM_SEED)

    daily = pd.concat(
        [_monthly_to_daily(retail, s) for s in cfg.STATES],
        ignore_index=True,
    )

    rows = []
    for _, r in daily.iterrows():
        month = r["date"].month

        for zone, base_mix in p["zone_mix"].items():
            zs = p["zone_seasonality"][zone]

            # Each zone swings around its own peak month. The cosine is
            # 1.0 at the peak month and dips to -1.0 six months later,
            # so amplitude controls how big the swing is.
            months_from_peak = (month - zs["peak_month"]) % 12
            seasonal = 1.0 + zs["amplitude"] * np.cos(
                2 * np.pi * months_from_peak / 12.0
            )

            # Noise drawn per zone, so zones don't wobble in step.
            wobble = rng.normal(1.0, zs["noise_sd"])

            cartons = int(round(r["cartons_total"] * base_mix
                                * seasonal * wobble))
            rate = p["lines_per_hour"][zone]

            # Actual achieved rate wobbles around the standard.
            achieved = rate * rng.normal(1.0, 0.06)
            productive_hours = cartons / achieved
            paid_hours = productive_hours / (1 - p["non_pick_allowance"])

            rows.append({
                "date": r["date"],
                "state": r["state"],
                "zone": zone,
                "cartons_picked": cartons,
                "standard_rate_cph": rate,
                "achieved_rate_cph": round(achieved, 2),
                "paid_labour_hours": round(paid_hours, 2),
            })

    ops = pd.DataFrame(rows)
    log.info("built %d operations rows across %d states",
             len(ops), ops["state"].nunique())
    return ops


def build_roster_plan(ops: pd.DataFrame) -> pd.DataFrame:
    """Create the *planned* roster -- deliberately imperfect.

    Planning happens ahead of time off a forecast, so it should not
    match actuals. The gap between planned and required labour is the
    entire point of the analysis; a perfect plan would make the project
    say nothing.

    Planners here use a 28-day trailing average, which systematically
    lags turning points -- exactly the real-world failure mode.
    """
    p = cfg.DC_PARAMS
    rng = np.random.default_rng(cfg.RANDOM_SEED + 7)

    ops = ops.sort_values(["state", "zone", "date"]).copy()

    ops["planned_labour_hours"] = (
        ops.groupby(["state", "zone"])["paid_labour_hours"]
        .transform(lambda s: s.rolling(28, min_periods=7).mean())
    )
    # Before the window fills, planners fall back to the first known value.
    ops["planned_labour_hours"] = ops["planned_labour_hours"].fillna(
        ops.groupby(["state", "zone"])["paid_labour_hours"].transform("first")
    )
    # Planner judgement error on top of the mechanical lag.
    ops["planned_labour_hours"] = (
        ops["planned_labour_hours"] * rng.normal(1.0, 0.05, len(ops))
    ).round(2)

    ops["planned_headcount"] = np.ceil(
        ops["planned_labour_hours"] / p["shift_hours"]
    ).astype(int)

    return ops[["date", "state", "zone",
                "planned_labour_hours", "planned_headcount"]]


def run() -> tuple[pd.DataFrame, pd.DataFrame]:
    from .ingest_abs import RAW_FILE

    if not RAW_FILE.exists():
        raise FileNotFoundError(
            f"{RAW_FILE} not found -- run ingest_abs first."
        )

    retail = pd.read_parquet(RAW_FILE)
    ops = build_operations(retail)
    roster = build_roster_plan(ops)

    ops.to_parquet(OPS_FILE, index=False)
    roster.to_parquet(ROSTER_FILE, index=False)
    log.info("wrote %s and %s", OPS_FILE.name, ROSTER_FILE.name)
    return ops, roster


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    ops, roster = run()
    print("\n--- operations (modelled) ---")
    print(ops.head(8).to_string(index=False))
    print("\n--- roster plan (modelled) ---")
    print(roster.head(8).to_string(index=False))
    print(f"\nops rows: {len(ops):,}   roster rows: {len(roster):,}")
    print(f"date range: {ops['date'].min().date()} -> {ops['date'].max().date()}")
