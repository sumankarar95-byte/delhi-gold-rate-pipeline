from datetime import datetime, timedelta, timezone

import pytest

from goldpipe import model, quality, warehouse
from conftest import land, synthetic_market


def build(settings):
    warehouse.run(settings)
    return warehouse.connect(settings)


def test_landed_cost_formula(settings):
    df = synthetic_market(n=5)
    land(settings, df)
    with build(settings) as con:
        gold_usd, fx, rate = con.execute(
            "SELECT gold_usd_oz, usd_inr, delhi_24k_inr_10g FROM gold.fact_delhi_rate LIMIT 1").fetchone()
    expected = gold_usd * fx / 31.1035 * 10 * 1.06 * 1.03
    assert rate == pytest.approx(expected)


def test_latest_ingest_wins_on_duplicates(settings):
    df = synthetic_market(n=5)
    old = datetime.now(timezone.utc) - timedelta(days=1)
    land(settings, df, ingested_at=old)
    revised = df.copy()
    revised.loc[revised.series == "gold_usd_oz", "close"] = 3000.0
    land(settings, revised, ingest="2026-01-02")
    with build(settings) as con:
        n, closes = con.execute("SELECT count(*), list(DISTINCT close) FROM silver.market_prices "
                                "WHERE series = 'gold_usd_oz'").fetchone()
    assert n == 5 and closes == [3000.0]


def test_fx_forward_filled_over_fx_holiday(settings):
    df = synthetic_market(n=10)
    holiday = sorted(df.trade_date.unique())[4]
    df = df[~((df.series == "usd_inr") & (df.trade_date == holiday))]
    land(settings, df)
    with build(settings) as con:
        imputed, fx, prev_fx = con.execute("""
            SELECT fx_imputed, usd_inr, lag(usd_inr) OVER (ORDER BY trade_date)
            FROM gold.fact_delhi_rate QUALIFY trade_date = ?""", [holiday]).fetchone()
    assert imputed and fx == prev_fx


def test_quality_gate_blocks_bad_prices(settings):
    df = synthetic_market(n=30)
    df.loc[(df.series == "usd_inr") & (df.index % 7 == 0), "close"] = 999.0   # corrupt FX feed
    land(settings, df)
    with build(settings) as con, pytest.raises(RuntimeError, match="usd_inr_in_range"):
        quality.run(con, run_id="t")


def test_quality_results_are_audited(settings):
    land(settings, synthetic_market(n=30))
    with build(settings) as con:
        quality.run(con, run_id="t1")
        assert con.execute("SELECT count(*) FROM audit.dq_results WHERE run_id = 't1'").fetchone()[0] == len(quality.CHECKS)


def test_forecast_bands_are_ordered_and_widen_with_horizon(settings):
    land(settings, synthetic_market())
    with build(settings) as con:
        res = model.run(con, settings, run_id="t")
    fc = res["forecast"].sort_values("trading_days")
    assert (fc.lower < fc.point).all() and (fc.point < fc.upper).all()
    assert fc.prob_up.between(0, 1).all()
    width = fc.upper - fc.lower
    assert width.is_monotonic_increasing
    assert res["signal"]["action"] in {"ACCUMULATE", "HOLD", "LIGHTEN"}


def test_model_rerun_is_idempotent(settings):
    land(settings, synthetic_market())
    with build(settings) as con:
        model.run(con, settings, run_id="a")
        model.run(con, settings, run_id="b")
        n = con.execute("SELECT count(*) FROM gold.forecast").fetchone()[0]
    assert n == len(settings["model"]["horizons"])


def test_backtest_has_no_lookahead(settings):
    """Changing the future must not change a forecast made in the past."""
    import numpy as np
    df = synthetic_market()
    land(settings, df)
    with build(settings) as con:
        prices = con.execute("SELECT trade_date, price FROM gold.features ORDER BY 1").df().set_index("trade_date").price
    cut = 300
    a = model.forecast(prices.iloc[:cut], settings["model"])
    shocked = prices.copy()
    shocked.iloc[cut:] *= np.linspace(1, 2, len(prices) - cut)
    b = model.forecast(shocked.iloc[:cut], settings["model"])
    assert a.equals(b)
