"""Data quality gates.

The point of these is to fail LOUDLY and EARLY. A pipeline that runs
unattended will eventually be fed something wrong -- a truncated file,
a schema change upstream, a silent type coercion. Without checks, that
becomes a dashboard showing confident nonsense, and nobody notices for
a month.

Each check returns a Result. The runner refuses to continue if any
check with severity "error" fails. Warnings are recorded but do not
stop the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import duckdb

from . import config as cfg

log = logging.getLogger(__name__)


@dataclass
class Result:
    name: str
    passed: bool
    severity: str            # "error" stops the pipeline, "warning" does not
    detail: str = ""
    value: float | None = None


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def failed_errors(self) -> list[Result]:
        return [r for r in self.results
                if not r.passed and r.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.failed_errors

    def summary(self) -> str:
        lines = []
        for r in self.results:
            mark = "PASS" if r.passed else ("FAIL" if r.severity == "error"
                                            else "WARN")
            val = f"  [{r.value:,.2f}]" if r.value is not None else ""
            lines.append(f"  {mark:<5} {r.name}{val}")
            if not r.passed and r.detail:
                lines.append(f"        -> {r.detail}")
        return "\n".join(lines)


# ------------------------------------------------------------ checks

def _row_counts(con) -> list[Result]:
    """Every table should have rows. An empty table means an upstream
    step silently produced nothing."""
    out = []
    tables = con.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('staging', 'marts')
        ORDER BY 1, 2
    """).fetchall()

    if not tables:
        return [Result("tables_exist", False, "error",
                       "no staging or marts tables found")]

    for schema, name in tables:
        n = con.execute(f'SELECT COUNT(*) FROM "{schema}"."{name}"').fetchone()[0]
        out.append(Result(
            f"rows_present.{schema}.{name}",
            passed=n > 0,
            severity="error",
            detail="table is empty",
            value=float(n),
        ))
    return out


def _no_negative_volumes(con) -> Result:
    """Negative cartons are physically impossible."""
    n = con.execute("""
        SELECT COUNT(*) FROM marts.fct_daily_labour
        WHERE cartons_picked < 0 OR required_labour_hours < 0
    """).fetchone()[0]
    return Result("no_negative_volumes", n == 0, "error",
                  f"{n} rows with negative cartons or hours", float(n))


def _no_orphan_dates(con) -> Result:
    """Every activity date must exist in the date spine. A miss here
    means the calendar and the facts disagree, and any date-based
    grouping will silently drop rows."""
    n = con.execute("""
        SELECT COUNT(*) FROM marts.fct_daily_labour f
        LEFT JOIN staging.dim_date d ON f.activity_date = d.date_key
        WHERE d.date_key IS NULL
    """).fetchone()[0]
    return Result("no_orphan_dates", n == 0, "error",
                  f"{n} fact rows have no matching date", float(n))


def _no_duplicate_grain(con) -> Result:
    """One row per date/state/zone. Duplicates double-count everything
    downstream -- the most damaging error because the numbers still
    look plausible."""
    n = con.execute("""
        SELECT COUNT(*) FROM (
            SELECT activity_date, state, zone
            FROM marts.fct_daily_labour
            GROUP BY 1, 2, 3 HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    return Result("no_duplicate_grain", n == 0, "error",
                  f"{n} duplicated date/state/zone combinations", float(n))


def _productivity_in_range(con) -> Result:
    """Productivity should sit near 1.0. Way outside means the pick
    rates or the volume scaling has drifted."""
    row = con.execute("""
        SELECT MIN(productivity_index), MAX(productivity_index)
        FROM marts.fct_daily_labour
    """).fetchone()
    lo, hi = row
    ok = (lo is not None) and lo > 0.4 and hi < 2.0
    return Result("productivity_in_range", ok, "error",
                  f"productivity index spans {lo:.2f} to {hi:.2f}, "
                  f"expected roughly 0.4-2.0")


def _savings_believable(con) -> Result:
    """The sanity check that caught the $33m error.

    Forecast savings should be a small share of the wage bill. If they
    are not, a cost assumption or a volume scale is wrong -- and that
    is exactly the kind of mistake that survives review because the
    pipeline itself ran fine.
    """
    row = con.execute("""
        WITH wages AS (
            SELECT SUM(required_labour_hours) * ?
                   / (COUNT(DISTINCT activity_date) / 365.0) AS annual_wages
            FROM marts.fct_daily_labour
        ),
        saving AS (
            SELECT SUM(annualised_saving) AS s FROM marts.fct_forecast_value
        )
        SELECT saving.s / NULLIF(wages.annual_wages, 0) * 100
        FROM wages, saving
    """, [cfg.DC_PARAMS["cost_per_labour_hour"]]).fetchone()

    pct = row[0] if row and row[0] is not None else None
    ok = pct is not None and 0 <= pct < 25
    return Result("savings_believable", ok, "error",
                  f"forecast savings are {pct:.1f}% of the annual wage "
                  f"bill -- over 25% means a cost or scale assumption is "
                  f"wrong" if pct is not None else "could not compute",
                  pct)


def _forecast_beats_baseline(con) -> Result:
    """The model should beat the naive baseline. If it stops doing so,
    something has changed and the model needs revisiting -- a warning,
    not a failure, because it is a modelling signal not a data fault."""
    row = con.execute("""
        SELECT AVG(baseline_mape), AVG(model_mape)
        FROM marts.fct_forecast_value
    """).fetchone()
    base, model = row
    ok = model is not None and base is not None and model < base
    return Result("forecast_beats_baseline", ok, "warning",
                  f"model MAPE {model:.2f}% is not better than "
                  f"baseline {base:.2f}%" if model else "no forecast data",
                  (base - model) if ok else None)


def _recent_data(con) -> Result:
    """Data should be reasonably fresh. Stale data in a scheduled
    pipeline usually means ingestion silently failed and we are
    rebuilding yesterday's answer."""
    row = con.execute("""
        SELECT DATE_DIFF('day', MAX(activity_date), CURRENT_DATE)
        FROM marts.fct_daily_labour
    """).fetchone()
    days = row[0]
    return Result("data_is_recent", days is not None and days <= 45,
                  "warning",
                  f"most recent activity is {days} days old", float(days or 0))


# ------------------------------------------------------------- runner

def run(db_path=None) -> Report:
    db_path = db_path or cfg.DB_PATH
    con = duckdb.connect(str(db_path), read_only=True)

    report = Report()
    try:
        report.results.extend(_row_counts(con))
        for check in (_no_negative_volumes, _no_orphan_dates,
                      _no_duplicate_grain, _productivity_in_range,
                      _savings_believable, _forecast_beats_baseline,
                      _recent_data):
            try:
                report.results.append(check(con))
            except Exception as exc:
                report.results.append(
                    Result(check.__name__.strip("_"), False, "error",
                           f"check itself failed: {exc}")
                )
    finally:
        con.close()

    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rep = run()
    print("\ndata quality report")
    print("-" * 60)
    print(rep.summary())
    n_fail = len(rep.failed_errors)
    print("-" * 60)
    print(f"{'PASSED' if rep.ok else 'FAILED'} "
          f"({len(rep.results)} checks, {n_fail} blocking failures)")
    raise SystemExit(0 if rep.ok else 1)
