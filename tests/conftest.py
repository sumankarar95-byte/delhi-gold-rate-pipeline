"""Fixtures build a tiny synthetic bronze layer, so tests never hit the network."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
import yaml

from goldpipe.config import ROOT, Settings


@pytest.fixture
def settings(tmp_path):
    raw = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    raw["paths"] = {"bronze": str(tmp_path / "bronze"), "bronze_ibja": str(tmp_path / "bronze_ibja"),
                    "warehouse": str(tmp_path / "wh.duckdb"), "reports": str(tmp_path / "reports")}
    raw["model"].update(backtest_days=120, backtest_step=2)
    return Settings(raw)


def synthetic_market(n=400, seed=7, start=date(2025, 1, 1)) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n).date
    gold = 2600 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n)))
    fx = 85 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    rows = []
    for series, ticker, px in (("gold_usd_oz", "GC=F", gold), ("usd_inr", "INR=X", fx)):
        rows.append(pd.DataFrame({"trade_date": days, "open": px, "high": px, "low": px, "close": px,
                                  "series": series, "ticker": ticker}))
    return pd.concat(rows, ignore_index=True)


def land(settings, df: pd.DataFrame, ingest: str = "2026-01-01", ingested_at=None):
    part = settings.path("bronze") / f"ingest_date={ingest}"
    part.mkdir(parents=True, exist_ok=True)
    df = df.assign(ingested_at=ingested_at or datetime.now(timezone.utc))
    df.to_parquet(part / f"market_{len(list(part.iterdir()))}.parquet", index=False)


def ibja_rows(days, rate_999: float, sessions=("AM", "PM")) -> pd.DataFrame:
    """IBJA-shaped rows (ex-GST ₹/10g) with consistent purity ratios."""
    rows = []
    for d in days:
        for s in sessions:
            for purity, fin in (("999", 0.999), ("916", 0.916)):
                rows.append({"rate_date": d, "session": s, "purity": purity,
                             "gold_rate_inr_10g": round(rate_999 * fin / 0.999), "silver_rate_inr_kg": 200000.0,
                             "source": "test"})
    return pd.DataFrame(rows)
