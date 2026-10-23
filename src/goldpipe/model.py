"""Forecasting: an honest, explainable ensemble with walk-forward backtesting.

Gold is close to a random walk, so the model's main job is a well-calibrated
*range* (for pricing and inventory risk), not a magic point forecast. The
backtest is always reported against the naive "tomorrow = today" baseline.
"""
from __future__ import annotations

import logging
import warnings
from statistics import NormalDist

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

log = logging.getLogger(__name__)
FIT_WINDOW = 500


def ewma_vol(log_ret: pd.Series, halflife: int) -> float:
    return float(np.sqrt((log_ret.dropna() ** 2).ewm(halflife=halflife).mean().iloc[-1]))


def drift_forecasts(log_price: pd.Series, h: int, drift_window: int) -> dict[str, float]:
    """Expected h-step log return from each component model."""
    rets = log_price.diff().dropna()
    momentum = 0.5 * rets.iloc[-drift_window:].mean() * h          # shrunk recent drift
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        holt = ExponentialSmoothing(log_price.iloc[-FIT_WINDOW:].to_numpy(), trend="add",
                                    damped_trend=True, initialization_method="estimated").fit()
    holt_mu = float(holt.forecast(h)[-1] - log_price.iloc[-1])
    return {"momentum": momentum, "holt_damped": holt_mu, "ensemble": (momentum + holt_mu) / 2}


def forecast(prices: pd.Series, cfg: dict) -> pd.DataFrame:
    """prices: Series indexed by trade_date. Returns one row per horizon."""
    lp = np.log(prices)
    sigma = ewma_vol(lp.diff(), cfg["vol_halflife"])
    z = NormalDist().inv_cdf(0.5 + cfg["confidence"] / 2)
    last = float(prices.iloc[-1])
    rows = []
    for name, h in cfg["horizons"].items():
        mu = drift_forecasts(lp, h, cfg["drift_window"])["ensemble"]
        s = sigma * np.sqrt(h)
        rows.append({"horizon": name, "trading_days": h, "base_price": last,
                     "point": last * np.exp(mu), "lower": last * np.exp(mu - z * s),
                     "upper": last * np.exp(mu + z * s), "prob_up": NormalDist().cdf(mu / s),
                     "daily_vol": sigma})
    return pd.DataFrame(rows)


def backtest(prices: pd.Series, cfg: dict) -> pd.DataFrame:
    """Walk-forward: at each origin, forecast using only data up to that day."""
    lp = np.log(prices)
    z = NormalDist().inv_cdf(0.5 + cfg["confidence"] / 2)
    n, recs = len(lp), []
    max_h = max(cfg["horizons"].values())
    origins = range(n - cfg["backtest_days"] - max_h, n - max_h, cfg["backtest_step"])
    for o in origins:
        hist = lp.iloc[: o + 1]
        sigma = ewma_vol(hist.diff(), cfg["vol_halflife"])
        for name, h in cfg["horizons"].items():
            mu = drift_forecasts(hist, h, cfg["drift_window"])["ensemble"]
            actual = lp.iloc[o + h] - hist.iloc[-1]
            recs.append({"horizon": name, "trading_days": h, "err_model": abs(actual - mu),
                         "err_naive": abs(actual), "dir_hit": np.sign(actual) == np.sign(mu),
                         "in_band": abs(actual - mu) <= z * sigma * np.sqrt(h)})
    bt = pd.DataFrame(recs)
    out = (bt.groupby(["horizon", "trading_days"])
             .agg(n=("err_model", "size"),
                  mape_model_pct=("err_model", lambda x: 100 * x.mean()),
                  mape_naive_pct=("err_naive", lambda x: 100 * x.mean()),
                  direction_hit_pct=("dir_hit", lambda x: 100 * x.mean()),
                  band_coverage_pct=("in_band", lambda x: 100 * x.mean()))
             .reset_index().sort_values("trading_days"))
    out["beats_naive"] = out.mape_model_pct < out.mape_naive_pct
    return out


def business_signal(fc: pd.DataFrame, latest: pd.Series, cfg_business: dict) -> dict:
    wk = fc.set_index("horizon").loc["one_week"]
    above_trend = latest["price"] > latest["sma_50"]
    if wk.prob_up >= 0.55 and above_trend:
        action, why = "ACCUMULATE", "Uptrend and >55% chance of a higher rate in a week: build stock, buy-back freely."
    elif wk.prob_up <= 0.45 and not above_trend:
        action, why = "LIGHTEN", "Downtrend and <45% chance of a higher rate: keep stock lean, avoid bulk purchases."
    else:
        action, why = "HOLD", "No clear edge: keep normal stock levels and protect margins."
    grams = cfg_business["inventory_grams"]
    var_95 = grams / 10 * wk.base_price * (1 - np.exp(-1.645 * wk.daily_vol * np.sqrt(5)))
    return {"action": action, "reason": why, "prob_up_week": float(wk.prob_up),
            "above_sma50": bool(above_trend), "inventory_grams": grams, "var95_week_inr": float(var_95)}


def run(con, settings, run_id: str) -> dict:
    cfg = settings["model"]
    feats = con.execute("SELECT * FROM gold.features ORDER BY trade_date").df().set_index("trade_date")
    prices = feats["price"]
    fc = forecast(prices, cfg)
    bt = backtest(prices, cfg)
    sig = business_signal(fc, feats.iloc[-1], settings["business"])
    as_of = pd.Timestamp(feats.index[-1]).date()
    for name, df in {"forecast": fc, "model_backtest": bt}.items():
        df = df.assign(run_id=run_id, as_of_date=as_of)
        con.register("_df", df)
        con.execute(f"CREATE TABLE IF NOT EXISTS gold.{name} AS SELECT * FROM _df LIMIT 0")
        con.execute(f"DELETE FROM gold.{name} WHERE as_of_date = ?", [as_of])   # idempotent re-runs
        con.execute(f"INSERT INTO gold.{name} BY NAME SELECT * FROM _df")
        con.unregister("_df")
    log.info("model: as_of=%s signal=%s P(up,1w)=%.0f%%", as_of, sig["action"], 100 * sig["prob_up_week"])
    return {"as_of": as_of, "forecast": fc, "backtest": bt, "signal": sig, "history": feats}
