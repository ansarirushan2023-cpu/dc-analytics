"""Build the DuckDB warehouse from raw Parquet by running the SQL layers.

SQL files are executed in filename order. Parameters (file paths, cost
constants) are bound rather than string-formatted -- the SQL stays
readable and there is no injection surface.
"""

from __future__ import annotations

import logging
import re

import duckdb

from . import config as cfg
from .forecast import FORECAST_FILE
from .generate_ops import OPS_FILE, ROSTER_FILE
from .ingest_abs import RAW_FILE

log = logging.getLogger(__name__)


def _params() -> dict:
    return {
        "retail_path": str(RAW_FILE),
        "ops_path": str(OPS_FILE),
        "roster_path": str(ROSTER_FILE),
        "forecast_path": str(FORECAST_FILE),
        "cost_per_hour": cfg.DC_PARAMS["cost_per_labour_hour"],
        "cost_per_unshipped": cfg.DC_PARAMS["cost_per_unshipped_carton"],
        "overtime_premium": cfg.DC_PARAMS["overtime_premium"],
        "service_share": cfg.DC_PARAMS["service_failure_share"],
    }


def _split_statements(sql: str) -> list[str]:
    """Split a SQL file into statements, ignoring semicolons in comments."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


def _bind_for(stmt: str, params: dict) -> dict:
    """Return only the params this statement references.

    DuckDB rejects a named-parameter dict containing keys the statement
    does not use, so a blanket bind fails on plain DDL like
    CREATE SCHEMA.
    """
    used = set(re.findall(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", stmt))
    return {k: v for k, v in params.items() if k in used}


def build(db_path=None) -> duckdb.DuckDBPyConnection:
    db_path = db_path or cfg.DB_PATH
    params = _params()

    for required in (RAW_FILE, OPS_FILE, ROSTER_FILE):
        if not required.exists():
            raise FileNotFoundError(
                f"{required} missing -- run ingest_abs and generate_ops first."
            )

    con = duckdb.connect(str(db_path))

    sql_files = sorted(cfg.SQL_DIR.glob("*.sql"))
    if not sql_files:
        raise FileNotFoundError(f"no .sql files in {cfg.SQL_DIR}")

    for path in sql_files:
        log.info("executing %s", path.name)
        for i, stmt in enumerate(_split_statements(path.read_text()), 1):
            try:
                bind = _bind_for(stmt, params)
                con.execute(stmt, bind) if bind else con.execute(stmt)
            except Exception as exc:
                raise RuntimeError(
                    f"{path.name} statement {i} failed: {exc}\n"
                    f"--- statement ---\n{stmt[:400]}"
                ) from exc

    log.info("built warehouse -> %s", db_path)
    return con


def summarise(con: duckdb.DuckDBPyConnection) -> None:
    tables = con.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('staging', 'marts')
        ORDER BY table_schema, table_name
    """).fetchall()

    print("\ntables built")
    print("-" * 46)
    for schema, name in tables:
        n = con.execute(
            f'SELECT COUNT(*) FROM "{schema}"."{name}"'
        ).fetchone()[0]
        print(f"  {schema}.{name:<26} {n:>10,} rows")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    con = build()
    summarise(con)
    con.close()
