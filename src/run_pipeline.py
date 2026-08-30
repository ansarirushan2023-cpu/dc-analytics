"""Run the whole pipeline, in the right order, with gates.

The ordering matters and is easy to get wrong by hand: build_db reads
the forecast output, so forecast must run first. Encoding the order
here means it cannot be got wrong -- by you, by a scheduler, or by
anyone who clones the repo.

Quality checks run after the warehouse is built and BEFORE the
commentary. That ordering is deliberate: commentary is what a person
actually reads, so it must never be generated from data that failed
its checks.

Usage:
    python -m src.run_pipeline
    python -m src.run_pipeline --offline      # no network
    python -m src.run_pipeline --skip-forecast
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from . import build_db, checks, commentary, config as cfg, dashboard, forecast
from . import generate_ops, ingest_abs

log = logging.getLogger("pipeline")

RUN_LOG = cfg.OUTPUT / "run_history.md"


def _step(name: str, fn, *args, **kwargs):
    """Run one step, time it, and let failures stop the run."""
    log.info("── %s", name)
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    log.info("   done in %.1fs", time.perf_counter() - t0)
    return result


def _append_run_log(status: str, detail: str) -> None:
    """Keep a dated record of every run.

    Over weeks this becomes evidence the pipeline actually runs on a
    schedule, rather than having been executed once by hand.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"| {stamp} | {status} | {detail} |\n"

    if not RUN_LOG.exists():
        RUN_LOG.write_text(
            "# Pipeline run history\n\n"
            "| Run (UTC) | Status | Detail |\n"
            "|---|---|---|\n"
        )
    with RUN_LOG.open("a") as fh:
        fh.write(line)


def main(offline: bool = False, skip_forecast: bool = False,
         use_llm: bool | None = None) -> int:
    t_start = time.perf_counter()
    log.info("pipeline start (offline=%s)", offline)

    try:
        _step("1/5  ingest ABS retail turnover",
              ingest_abs.run, offline=offline)

        _step("2/5  generate modelled DC operations",
              generate_ops.run)

        if skip_forecast:
            log.info("── 3/5  forecast SKIPPED")
        else:
            # Must run before build_db: 03_forecast_accuracy.sql reads
            # the parquet this produces.
            _step("3/5  backtest forecast models", forecast.run)

        _step("4/5  build DuckDB warehouse", build_db.build)

    except Exception as exc:
        log.error("pipeline failed: %s", exc)
        _append_run_log("FAILED", f"{type(exc).__name__}: {exc}")
        return 1

    # ---- gate: never publish commentary from data that failed checks
    log.info("── 5/5  data quality checks")
    report = checks.run()
    print()
    print(report.summary())
    print()

    if not report.ok:
        names = ", ".join(r.name for r in report.failed_errors)
        log.error("quality checks FAILED (%s) -- commentary not generated",
                  names)
        _append_run_log("FAILED", f"quality checks failed: {names}")
        return 1

    warnings = [r for r in report.results
                if not r.passed and r.severity == "warning"]
    if warnings:
        log.warning("%d warning(s): %s", len(warnings),
                    ", ".join(w.name for w in warnings))

    text = commentary.run(use_llm=use_llm)
    _step("   dashboard", dashboard.run)

    elapsed = time.perf_counter() - t_start
    n_findings = text.count("\n- ")
    _append_run_log(
        "OK" if not warnings else "OK (warnings)",
        f"{len(report.results)} checks passed, {n_findings} finding(s), "
        f"{elapsed:.0f}s"
    )

    log.info("pipeline complete in %.0fs", elapsed)
    print()
    print(text)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Run the DC analytics pipeline")
    ap.add_argument("--offline", action="store_true",
                    help="synthesise retail data instead of calling ABS")
    ap.add_argument("--skip-forecast", action="store_true",
                    help="reuse the existing forecast output")
    ap.add_argument("--no-llm", action="store_true",
                    help="force template commentary even if a key is set")
    args = ap.parse_args()

    sys.exit(main(offline=args.offline,
                  skip_forecast=args.skip_forecast,
                  use_llm=False if args.no_llm else None))
