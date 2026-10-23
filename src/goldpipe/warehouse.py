"""DuckDB warehouse: reference tables from config + SQL-defined silver/gold layers."""
from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pandas as pd

from .config import ROOT

log = logging.getLogger(__name__)
SQL_DIR = ROOT / "sql"


def connect(settings) -> duckdb.DuckDBPyConnection:
    path = settings.path("warehouse")
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def load_reference(con, settings) -> None:
    """Config-driven parameters become tables so SQL stays declarative and auditable."""
    p, b = settings["pricing"], settings["business"]
    con.execute("CREATE SCHEMA IF NOT EXISTS ref")
    pricing = pd.DataFrame([{k: v for k, v in p.items() if k != "purities"}])
    purities = pd.DataFrame(list(p["purities"].items()), columns=["karat", "fineness"])
    business = pd.DataFrame([b])
    for name, df in {"pricing_params": pricing, "purities": purities, "business_params": business}.items():
        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE ref.{name} AS SELECT * FROM _df")
        con.unregister("_df")


def run_sql(con, settings, files: list[str] | None = None) -> None:
    files = files or sorted(f.name for f in SQL_DIR.glob("[0-9]*.sql"))
    for name in files:
        sql = (SQL_DIR / name).read_text().replace("{bronze}", settings.path("bronze").as_posix())
        con.execute(sql)
        log.info("transform: ran %s", name)


def run(settings) -> None:
    with connect(settings) as con:
        load_reference(con, settings)
        run_sql(con, settings)
        n = con.execute("SELECT count(*), max(trade_date) FROM gold.fact_delhi_rate").fetchone()
        log.info("transform: gold.fact_delhi_rate rows=%d latest=%s", *n)
