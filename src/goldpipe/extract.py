"""Bronze layer: land raw market data as immutable, date-partitioned Parquet.

Incremental per series: each series resumes from its own high-water mark (with a
small overlap so late revisions are captured); a series never seen before is
back-filled. Only *completed* daily bars are landed: a bar whose market has not
closed yet is still moving and must not be treated as that day's close.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)
OVERLAP_DAYS = 5
RETRIES = 4


def high_water_marks(bronze: Path) -> dict[str, date]:
    files = list(bronze.glob("ingest_date=*/*.parquet"))
    if not files:
        return {}
    df = pd.concat(pd.read_parquet(f, columns=["trade_date", "series"]) for f in files)
    return {s: d.date() for s, d in pd.to_datetime(df.trade_date).groupby(df.series).max().items()}


def last_completed_day(tz: str, close: str, now: datetime | None = None) -> date:
    """Latest trade date whose session has closed in the market's own time zone."""
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    hh, mm = map(int, close.split(":"))
    return now.date() if now.time() >= dtime(hh, mm) else now.date() - timedelta(days=1)


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


def fetch(series: str, spec: dict, start: date, end: date) -> pd.DataFrame:
    """Long-format completed bars for one series: trade_date, series, ticker, open/high/low/close."""
    raw = _download_with_retry(spec["ticker"], start, end + timedelta(days=1))
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = (raw.reset_index()
             .rename(columns=str.lower)
             .rename(columns={"date": "trade_date"})[["trade_date", "open", "high", "low", "close"]])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["series"], df["ticker"] = series, spec["ticker"]
    return df[df.trade_date <= end]


def run(settings, now: datetime | None = None) -> Path | None:
    bronze = settings.path("bronze")
    bronze.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    hwms = high_water_marks(bronze)
    frames = []
    for series, spec in settings["sources"]["yahoo"].items():
        end = last_completed_day(spec["tz"], spec["close"], now)
        if series in hwms:
            start = hwms[series] - timedelta(days=OVERLAP_DAYS)
        else:
            start = end - timedelta(days=365 * settings["sources"]["backfill_years"])
        if spec.get("history_start"):
            start = max(start, date.fromisoformat(spec["history_start"]))
        df = fetch(series, spec, start, end)
        log.info("extract: %-12s %s -> %s (hwm=%s) %d completed bars", series, start, end, hwms.get(series), len(df))
        frames.append(df)

    df = pd.concat(frames, ignore_index=True)
    df["ingested_at"] = now
    part = bronze / f"ingest_date={now.date().isoformat()}"
    part.mkdir(exist_ok=True)
    out = part / f"market_{now:%H%M%S%f}.parquet"
    df.to_parquet(out, index=False)
    log.info("extract: landed %d rows -> %s", len(df), out.relative_to(bronze.parent))
    return out
