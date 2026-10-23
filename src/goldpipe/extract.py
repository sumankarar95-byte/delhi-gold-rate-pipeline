"""Bronze layer: land raw market data as immutable, date-partitioned Parquet.

Incremental: reads the high-water mark from existing partitions and only pulls
what is new (with a small overlap so late revisions are captured).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)
OVERLAP_DAYS = 5
RETRIES = 4


def high_water_mark(bronze: Path) -> date | None:
    files = list(bronze.glob("ingest_date=*/*.parquet"))
    if not files:
        return None
    df = pd.concat(pd.read_parquet(f, columns=["trade_date"]) for f in files)
    return pd.to_datetime(df["trade_date"]).max().date()


def _download_with_retry(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Yahoo rate-limits shared CI runners; back off exponentially before giving up."""
    for attempt in range(1, RETRIES + 1):
        try:
            raw = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
            if not raw.empty:
                return raw
            err = f"empty response for {ticker}"
        except Exception as e:  # network / rate-limit errors surface as varied types
            err = repr(e)
        if attempt < RETRIES:
            wait = 2 ** attempt * 5
            log.warning("extract: %s (attempt %d/%d), retrying in %ds", err, attempt, RETRIES, wait)
            time.sleep(wait)
    raise RuntimeError(f"extract failed after {RETRIES} attempts: {err}")


def fetch(tickers: dict[str, str], start: date, end: date) -> pd.DataFrame:
    """Return long-format rows: trade_date, series, ticker, open/high/low/close."""
    frames = []
    for series, ticker in tickers.items():
        raw = _download_with_retry(ticker, start, end + timedelta(days=1))
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        df = (raw.reset_index()
                 .rename(columns=str.lower)
                 .rename(columns={"date": "trade_date"})[["trade_date", "open", "high", "low", "close"]])
        df["series"], df["ticker"] = series, ticker
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    return out


def run(settings, today: date | None = None) -> Path | None:
    bronze = settings.path("bronze")
    bronze.mkdir(parents=True, exist_ok=True)
    today = today or date.today()
    hwm = high_water_mark(bronze)
    start = (hwm - timedelta(days=OVERLAP_DAYS)) if hwm else \
        today - timedelta(days=365 * settings["sources"]["backfill_years"])
    tickers = settings["sources"]["yahoo"]
    log.info("extract: %s -> %s (hwm=%s)", start, today, hwm)

    df = fetch(tickers, start, today)
    df["ingested_at"] = datetime.now(timezone.utc)
    part = bronze / f"ingest_date={today.isoformat()}"
    part.mkdir(exist_ok=True)
    out = part / f"market_{datetime.now():%H%M%S}.parquet"
    df.to_parquet(out, index=False)
    log.info("extract: landed %d rows -> %s", len(df), out.relative_to(bronze.parent))
    return out
