"""Ingest ABS Retail Trade turnover into raw Parquet.

Two paths:

  live     -- hit the ABS Data API and persist exactly what came back
  offline  -- synthesise a series with the same schema

Offline mode exists so the pipeline is runnable and testable without
network access. It is never silently substituted for a failed live
call: a live run that fails raises, and you choose to fall back.

Raw output is written once and never mutated. Everything downstream
reads from it, so a bad transform is always recoverable without
re-fetching.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from . import config as cfg

log = logging.getLogger(__name__)

RAW_FILE = cfg.RAW / "abs_retail_turnover.parquet"
META_FILE = cfg.RAW / "abs_retail_turnover.meta.json"

SCHEMA = ["period", "state", "industry", "turnover_thousands"]


def _write_meta(source: str, rows: int, **extra) -> None:
    """Record how this raw file was produced.

    Provenance is the difference between a dataset you can defend in an
    interview and one you can't.
    """
    meta = {
        "source": source,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "start_period": cfg.ABS_START_PERIOD,
        **extra,
    }
    META_FILE.write_text(json.dumps(meta, indent=2))
    log.info("wrote provenance -> %s", META_FILE.name)


def fetch_live(timeout: int = 60) -> pd.DataFrame:
    """Pull Retail Trade turnover from the ABS Data API.

    Raises on any non-200 or unparseable payload. A loud failure here is
    correct -- silently returning partial data would poison every
    downstream table.
    """
    url = f"{cfg.ABS_API_BASE}/{cfg.ABS_DATAFLOW}/all"
    params = {"startPeriod": cfg.ABS_START_PERIOD, "format": "csv"}

    log.info("GET %s", url)
    resp = requests.get(
        url, params=params, timeout=timeout,
        headers={"Accept": "text/csv", "User-Agent": "dc-analytics/0.1"},
    )
    resp.raise_for_status()

    from io import StringIO
    raw = pd.read_csv(StringIO(resp.text))
    log.info("ABS returned %d rows, %d cols", len(raw), raw.shape[1])

    df = _normalise_abs_columns(raw)
    _write_meta("abs_data_api", len(df), url=url, dataflow=cfg.ABS_DATAFLOW)
    return df


def _normalise_abs_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """Map ABS SDMX column names onto our schema.

    ABS CSV headers vary by dataflow and change between releases, so this
    resolves them case-insensitively against a candidate list rather than
    hardcoding. If a required column can't be resolved we raise with the
    columns we actually got -- that error message is what makes the fix
    a two-minute job instead of an afternoon.
    """
    lookup = {c.lower().strip(): c for c in raw.columns}

    def resolve(candidates: list[str], label: str) -> str:
        for cand in candidates:
            if cand in lookup:
                return lookup[cand]
        raise KeyError(
            f"could not resolve {label!r} column. "
            f"ABS returned: {sorted(raw.columns)}. "
            f"Check the dataflow in ABS Data Explorer and update "
            f"config.ABS_DATAFLOW / this mapping."
        )

    col_period = resolve(["time_period", "time", "period"], "period")
    col_state = resolve(["region", "state", "asgs_2016"], "state")
    col_industry = resolve(["industry", "iret", "commodity"], "industry")
    col_value = resolve(["obs_value", "value"], "value")

    out = pd.DataFrame({
        "period": raw[col_period].astype(str),
        "state": raw[col_state].astype(str),
        "industry": raw[col_industry].astype(str),
        "turnover_thousands": pd.to_numeric(raw[col_value], errors="coerce"),
    })
    return out.dropna(subset=["turnover_thousands"])


def build_offline(months: int = 96) -> pd.DataFrame:
    """Synthesise a retail turnover series with realistic structure.

    Trend + annual seasonality (December peak) + state scale + noise.
    Not a substitute for the real series -- it exists so the pipeline
    can be developed and tested without the network.
    """
    rng = np.random.default_rng(cfg.RANDOM_SEED)
    periods = pd.date_range(end=pd.Timestamp.today().normalize(),
                            periods=months, freq="MS")

    state_scale = {"NSW": 1.00, "VIC": 0.82, "QLD": 0.66,
                   "WA": 0.36, "SA": 0.23}
    industry_scale = {
        "Food retailing": 1.00,
        "Household goods retailing": 0.34,
        "Clothing and softgoods": 0.19,
        "Department stores": 0.13,
        "Other retailing": 0.41,
    }

    rows = []
    for state in cfg.STATES:
        for industry in cfg.INDUSTRIES:
            base = 3_650_000 * state_scale[state] * industry_scale[industry]
            for i, ts in enumerate(periods):
                trend = 1.0 + 0.0021 * i
                # December lift, February trough.
                seasonal = 1.0 + 0.16 * np.sin(
                    2 * np.pi * ((ts.month - 3) / 12.0)
                )
                if ts.month == 12:
                    seasonal += 0.13
                noise = rng.normal(1.0, 0.028)
                rows.append({
                    "period": ts.strftime("%Y-%m"),
                    "state": state,
                    "industry": industry,
                    "turnover_thousands": round(
                        base * trend * seasonal * noise, 1
                    ),
                })

    df = pd.DataFrame(rows)
    _write_meta("synthetic_offline", len(df), months=months,
                seed=cfg.RANDOM_SEED)
    return df


def run(offline: bool | None = None) -> pd.DataFrame:
    offline = cfg.OFFLINE_MODE if offline is None else offline
    df = build_offline() if offline else fetch_live()

    missing = [c for c in SCHEMA if c not in df.columns]
    if missing:
        raise ValueError(f"ingest produced wrong schema, missing {missing}")

    df = df[SCHEMA].sort_values(["state", "industry", "period"])
    df.to_parquet(RAW_FILE, index=False)
    log.info("wrote %d rows -> %s", len(df), RAW_FILE.name)
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Ingest ABS retail turnover")
    ap.add_argument("--offline", action="store_true",
                    help="synthesise instead of calling the ABS API")
    args = ap.parse_args()
    out = run(offline=args.offline)
    print(out.head(10).to_string(index=False))
    print(f"\n{len(out):,} rows -> {RAW_FILE}")
