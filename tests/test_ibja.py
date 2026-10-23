from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from goldpipe import extract_ibja, quality, warehouse
from conftest import ibja_rows, land, synthetic_market


# ---------- API parsing ----------

def test_parse_success_payload():
    payload = [{"RateDate": "22/10/2026", "RateTime": "12AM", "Purity": "999", "GoldRate": "143152", "SilverRate": "200748"},
               {"RateDate": "22/10/2026", "RateTime": "6PM", "Purity": "916", "GoldRate": "130708", "SilverRate": "199777"}]
    df = extract_ibja.parse(payload)
    assert df.session.tolist() == ["AM", "PM"]
    assert df.rate_date.iloc[0] == date(2026, 10, 22)
    assert df.gold_rate_inr_10g.tolist() == [143152, 130708]


def test_parse_no_record_is_empty_not_error():
    assert extract_ibja.parse([{"status": "success", "message": "No Record Found"}]).empty


@pytest.mark.parametrize("msg", [{"status": "Invalid", "message": "Invalid Access Token."},
                                 {"status": "success", "message": "You have reached the maximum API hit limit."}])
def test_parse_raises_on_auth_and_hit_limit(msg):
    with pytest.raises(extract_ibja.IBJAError):
        extract_ibja.parse([msg])


def test_unknown_session_is_rejected():
    with pytest.raises(extract_ibja.IBJAError):
        extract_ibja.session_of("3PM")


# ---------- ingestion behaviour ----------

def test_skips_without_token(settings, monkeypatch):
    monkeypatch.delenv("IBJA_API_TOKEN", raising=False)
    assert extract_ibja.run(settings) is None


def test_one_call_per_day_then_skips(settings, monkeypatch):
    calls = []
    today = date(2026, 10, 22)

    def fake_api(url, token, start, end):
        calls.append((start, end))
        return [{"RateDate": f"{today:%d/%m/%Y}", "RateTime": t, "Purity": "999", "GoldRate": "143000", "SilverRate": "1"}
                for t in ("12AM", "6PM")]

    monkeypatch.setenv("IBJA_API_TOKEN", "test-token")
    monkeypatch.setattr(extract_ibja, "call_api", fake_api)
    assert extract_ibja.run(settings, today=today) is not None
    assert extract_ibja.run(settings, today=today) is None        # PM already landed: no second hit
    assert len(calls) == 1 and calls[0] == (today - timedelta(days=29), today)
    assert list(settings.path("bronze_ibja").glob("*/*.json"))    # raw payload kept for audit


def test_csv_import(settings, tmp_path):
    csv = tmp_path / "ibja.csv"
    csv.write_text("date,session,purity,gold_rate\n22/10/2026,PM,999,\"1,42,694\"\n")
    extract_ibja.import_csv(settings, csv)
    df = pd.read_parquet(next(settings.path("bronze_ibja").glob("*/*.parquet")))
    assert df.gold_rate_inr_10g.iloc[0] == 142694 and df.rate_date.iloc[0] == date(2026, 10, 22)


# ---------- warehouse: reconciliation + anchoring ----------

def _build(settings, ibja: pd.DataFrame | None, n=300):
    mkt = synthetic_market(n=n)
    land(settings, mkt)
    if ibja is not None:
        extract_ibja.land(settings.path("bronze_ibja"), ibja)
    warehouse.run(settings)
    return warehouse.connect(settings), sorted(mkt.trade_date.unique())


def test_ibja_matched_to_previous_close_no_lookahead(settings):
    days_probe = sorted(synthetic_market(n=300).trade_date.unique())
    d = days_probe[-1]
    con, days = _build(settings, ibja_rows([d], 150000))
    with con:
        intl_day = con.execute("SELECT intl_trade_date FROM gold.fact_ibja_reconciliation LIMIT 1").fetchone()[0]
    assert intl_day == days[-2]          # strictly before the IBJA date


def test_level_anchored_to_latest_ibja(settings):
    days = sorted(synthetic_market(n=300).trade_date.unique())
    after_last = days[-1] + timedelta(days=1)          # IBJA prints after the last COMEX close we have
    con, _ = _build(settings, ibja_rows([after_last], 150000))
    with con:
        price, src = con.execute("SELECT f.price, a.anchor_source FROM gold.features f, gold.anchor a "
                                 "ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert src == "IBJA PM"
    assert price == pytest.approx(150000 * 1.03)        # IBJA ex-GST + 3% GST


def test_falls_back_to_landed_cost_without_ibja(settings):
    con, _ = _build(settings, None)
    with con:
        src, price, modelled = con.execute(
            "SELECT a.anchor_source, f.price, r.delhi_24k_inr_10g FROM gold.anchor a, gold.features f "
            "JOIN gold.fact_delhi_rate r USING (trade_date) ORDER BY trade_date DESC LIMIT 1").fetchone()
    assert src == "landed-cost model" and price == pytest.approx(modelled)


def test_dq_catches_purity_mixup(settings):
    days = sorted(synthetic_market(n=300).trade_date.unique())
    bad = ibja_rows(days[-3:], 150000)
    bad.loc[bad.purity == "916", "gold_rate_inr_10g"] = 150000      # 916 reported at 999 price
    con, _ = _build(settings, bad)
    with con, pytest.raises(RuntimeError, match="ibja_purity_ratios_consistent"):
        quality.run(con, run_id="t")
