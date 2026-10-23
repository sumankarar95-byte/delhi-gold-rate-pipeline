"""Bronze layer for IBJA benchmark rates (official API, licensed).

The API returns at most the last 30 days per call and has a hit limit, so:
  * each run makes ONE call covering the 30-day window (self-healing: a few
    missed days are back-filled automatically on the next run);
  * the call is skipped when today's PM rate is already landed;
  * raw JSON is kept next to the Parquet for audit / re-parsing.
History beyond 30 days accumulates in bronze over time, or can be seeded
with `import-ibja <csv>` from data purchased from IBJA.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

log = logging.getLogger(__name__)
RETRIES = 3
COLUMNS = ["rate_date", "session", "purity", "gold_rate_inr_10g", "silver_rate_inr_kg", "source"]


class IBJAError(RuntimeError):
    pass


def session_of(rate_time: str) -> str:
    """IBJA publishes ~12:10 ("12AM" in the API, i.e. noon) and ~18:10 ("6PM") sessions."""
    t = rate_time.strip().upper()
    hour = int(re.match(r"\d+", t).group()) if re.match(r"\d+", t) else None
    if t in {"AM", "OPENING"} or hour == 12:
        return "AM"
    if t in {"PM", "CLOSING"} or hour in {6, 18}:
        return "PM"
    raise IBJAError(f"unknown IBJA RateTime {rate_time!r}")


def parse(payload: list[dict], source: str = "api") -> pd.DataFrame:
    """Turn an API response into typed rows. Status-only responses -> empty frame or error."""
    if not isinstance(payload, list):
        raise IBJAError(f"unexpected payload type {type(payload).__name__}")
    rows = [r for r in payload if "RateDate" in r]
    statuses = [r for r in payload if "status" in r]
    for s in statuses:
        msg = s.get("message", "")
        if s.get("status", "").lower() == "invalid" or "limit" in msg.lower():
            raise IBJAError(f"IBJA API: {msg}")
        log.info("ibja: %s", msg)                       # e.g. "No Record Found"
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows)
    out = pd.DataFrame({
        "rate_date": pd.to_datetime(df["RateDate"], format="%d/%m/%Y").dt.date,
        "session": df["RateTime"].map(session_of),
        "purity": df["Purity"].astype(str).str.strip(),
        "gold_rate_inr_10g": pd.to_numeric(df["GoldRate"], errors="coerce"),
        "silver_rate_inr_kg": pd.to_numeric(df.get("SilverRate"), errors="coerce"),
        "source": source,
    })
    return out


def call_api(url: str, token: str, start: date, end: date) -> list[dict]:
    params = {"ACCESS_TOKEN": token, "START_DATE": f"{start:%d/%m/%Y}", "END_DATE": f"{end:%d/%m/%Y}"}
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == RETRIES:
                raise IBJAError(f"IBJA API unreachable after {RETRIES} attempts: {e}") from None
            wait = 10 * attempt
            log.warning("ibja: %s (attempt %d/%d), retrying in %ds", type(e).__name__, attempt, RETRIES, wait)
            time.sleep(wait)


def land(bronze: Path, df: pd.DataFrame, raw: object | None = None, today: date | None = None) -> Path:
    today = today or date.today()
    part = bronze / f"ingest_date={today.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%H%M%S%f}"
    if raw is not None:
        (part / f"ibja_{stamp}.json").write_text(json.dumps(raw))
    out = part / f"ibja_{stamp}.parquet"
    df.assign(ingested_at=datetime.now(timezone.utc))[COLUMNS + ["ingested_at"]].to_parquet(out, index=False)
    return out


def already_have(bronze: Path, day: date) -> bool:
    files = list(bronze.glob("ingest_date=*/*.parquet"))
    if not files:
        return False
    df = pd.concat(pd.read_parquet(f, columns=["rate_date", "session"]) for f in files)
    return ((pd.to_datetime(df.rate_date).dt.date == day) & (df.session == "PM")).any()


def run(settings, today: date | None = None) -> Path | None:
    cfg = settings["sources"]["ibja"]
    token = os.environ.get(cfg["token_env"])
    if not token:
        log.warning("ibja: %s not set - skipping IBJA; rates fall back to the landed-cost model", cfg["token_env"])
        return None
    bronze = settings.path("bronze_ibja")
    today = today or date.today()
    uat = cfg["environment"] == "uat"
    end = today - timedelta(days=4) if uat else today      # UAT only serves rates >= 4 days old
    if not uat and already_have(bronze, end):
        log.info("ibja: PM rate for %s already landed - skipping API call (hit limit)", end)
        return None
    raw = call_api(cfg["uat_url"] if uat else cfg["url"], token, end - timedelta(days=cfg["lookback_days"]), end)
    df = parse(raw)
    out = land(bronze, df, raw, today)
    log.info("ibja: landed %d rows (%s..%s) -> %s", len(df),
             df.rate_date.min() if len(df) else "-", df.rate_date.max() if len(df) else "-", out.name)
    return out


def import_csv(settings, path: str | Path) -> Path:
    """Seed history. CSV columns: date (YYYY-MM-DD or DD/MM/YYYY), session (AM/PM), purity, gold_rate[, silver_rate]."""
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = {"date", "session", "purity", "gold_rate"} - set(df.columns)
    if missing:
        raise IBJAError(f"CSV is missing columns: {sorted(missing)}")
    out = pd.DataFrame({
        "rate_date": pd.to_datetime(df["date"], dayfirst="/" in str(df["date"].iloc[0])).dt.date,
        "session": df["session"].map(session_of),
        "purity": df["purity"].str.strip(),
        "gold_rate_inr_10g": pd.to_numeric(df["gold_rate"].str.replace(",", ""), errors="raise"),
        "silver_rate_inr_kg": pd.to_numeric(df.get("silver_rate", pd.Series(dtype=str)).str.replace(",", ""),
                                            errors="coerce") if "silver_rate" in df else float("nan"),
        "source": f"csv:{Path(path).name}",
    })
    dest = land(settings.path("bronze_ibja"), out)
    log.info("ibja: imported %d rows from %s -> %s", len(out), path, dest.name)
    return dest
