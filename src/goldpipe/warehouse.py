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
    ibja = pd.DataFrame([{"benchmark_purity": settings["sources"]["ibja"]["benchmark_purity"]}])
    india = pd.DataFrame([{k: v for k, v in settings["india"].items()}])
    calendar = pd.DataFrame([{"series": k, "ticker": v["ticker"], "tz": v["tz"],
                              "close_time": pd.Timedelta(v["close"] + ":00").to_pytimedelta()}
                             for k, v in settings["sources"]["yahoo"].items()])
    for name, df in {"pricing_params": pricing, "purities": purities, "business_params": business,
                     "ibja_params": ibja, "india_params": india, "series_calendar": calendar}.items():
        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE ref.{name} AS SELECT * FROM _df")
        con.unregister("_df")


EMPTY_IBJA = ("(SELECT NULL::DATE AS rate_date, NULL::VARCHAR AS session, NULL::VARCHAR AS purity, "
              "NULL::DOUBLE AS gold_rate_inr_10g, NULL::DOUBLE AS silver_rate_inr_kg, NULL::VARCHAR AS source, "
              "NULL::TIMESTAMPTZ AS ingested_at WHERE false)")


def ibja_source(settings) -> str:
    bronze = settings.path("bronze_ibja")
    if any(bronze.glob("ingest_date=*/*.parquet")):
        return f"read_parquet('{bronze.as_posix()}/ingest_date=*/*.parquet', hive_partitioning = false)"
    return EMPTY_IBJA


def run_sql(con, settings, files: list[str] | None = None) -> None:
    files = files or sorted(f.name for f in SQL_DIR.glob("[0-9]*.sql"))
    params = {"{bronze}": settings.path("bronze").as_posix(), "{ibja_source}": ibja_source(settings)}
    for name in files:
        sql = (SQL_DIR / name).read_text()
        for k, v in params.items():
            sql = sql.replace(k, v)
        con.execute(sql)
        log.info("transform: ran %s", name)


def run(settings) -> None:
    with connect(settings) as con:
        load_reference(con, settings)
        run_sql(con, settings)
        n = con.execute("SELECT count(*), min(trade_date), max(trade_date) FROM gold.fact_india_rate").fetchone()
        log.info("transform: gold.fact_india_rate rows=%d %s..%s", *n)
        src, ratio, n_cal = con.execute("SELECT source, ratio, n_ibja_days FROM gold.etf_calibration").fetchone()
        latest = con.execute("SELECT trade_date, price_source, india_999_ex_gst FROM gold.fact_india_rate "
                             "ORDER BY trade_date DESC LIMIT 1").fetchone()
        log.info("transform: ETF->IBJA ratio %.1f (%s, %d IBJA days); latest %s %s ₹%.0f/10g ex-GST",
                 ratio, src, n_cal, *latest)
