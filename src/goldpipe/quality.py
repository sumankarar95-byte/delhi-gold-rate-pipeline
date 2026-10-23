"""Data quality gate. Every run writes results to audit.dq_results; any failed
`error` check stops the pipeline before bad data reaches the model or report."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    severity: str  # "error" | "warn"
    sql: str       # must return a single row: (passed BOOLEAN, observed VARCHAR)


CHECKS = [
    Check("silver_not_empty", "error",
          "SELECT count(*) > 0, count(*)::VARCHAR FROM silver.market_prices"),
    Check("silver_unique_grain", "error",
          """SELECT count(*) = 0, count(*)::VARCHAR FROM (
               SELECT trade_date, series FROM silver.market_prices
               GROUP BY ALL HAVING count(*) > 1)"""),
    Check("gold_usd_in_range", "error",
          """SELECT bool_and(close BETWEEN 500 AND 20000), round(min(close), 1) || '..' || round(max(close), 1)
             FROM silver.market_prices WHERE series = 'gold_usd_oz'"""),
    Check("usd_inr_in_range", "error",
          """SELECT bool_and(close BETWEEN 40 AND 200), round(min(close), 1) || '..' || round(max(close), 1)
             FROM silver.market_prices WHERE series = 'usd_inr'"""),
    Check("delhi_rate_not_null", "error",
          "SELECT count(*) FILTER (delhi_24k_inr_10g IS NULL) = 0, count(*) FILTER (delhi_24k_inr_10g IS NULL)::VARCHAR FROM gold.fact_delhi_rate"),
    # Only recent rows: historic shocks (e.g. Jan-2026 crash) are real, new ones need a human look.
    Check("no_extreme_move_last_10d", "warn",
          """SELECT coalesce(max(abs(log_ret)), 0) < 0.05, round(coalesce(max(abs(log_ret)), 0) * 100, 2) || '%'
             FROM (SELECT log_ret FROM gold.features ORDER BY trade_date DESC LIMIT 10)"""),
    Check("fx_imputation_below_5pct", "warn",
          "SELECT avg(fx_imputed::INT) < 0.05, round(avg(fx_imputed::INT) * 100, 2) || '%' FROM gold.fact_delhi_rate"),
    Check("data_fresh_within_5_days", "warn",
          "SELECT current_date - max(trade_date) <= 5, (current_date - max(trade_date))::VARCHAR || ' days' FROM gold.fact_delhi_rate"),
]


def run(con, run_id: str, checks: list[Check] = CHECKS) -> pd.DataFrame:
    rows = []
    for c in checks:
        passed, observed = con.execute(c.sql).fetchone()
        rows.append({"run_id": run_id, "checked_at": datetime.now(timezone.utc), "check_name": c.name,
                     "severity": c.severity, "passed": bool(passed), "observed": str(observed)})
        (log.info if passed else log.warning)("dq: %-28s %-5s %s (%s)", c.name, c.severity,
                                               "PASS" if passed else "FAIL", observed)
    df = pd.DataFrame(rows)
    con.execute("CREATE SCHEMA IF NOT EXISTS audit")
    con.register("_dq", df)
    con.execute("CREATE TABLE IF NOT EXISTS audit.dq_results AS SELECT * FROM _dq LIMIT 0")
    con.execute("INSERT INTO audit.dq_results SELECT * FROM _dq")
    con.unregister("_dq")

    failed = df[(~df.passed) & (df.severity == "error")]
    if not failed.empty:
        raise RuntimeError(f"Data quality gate failed: {', '.join(failed.check_name)}")
    return df
