"""Forecasting: model competition + conformal ranges, all judged out-of-sample.

* Candidates forecast the cumulative log return of the anchored Delhi rate:
  naive (no change), shrunk momentum, Holt damped trend, AR(1) on returns.
* A walk-forward backtest scores every candidate at every horizon. The winner
  must beat naive by `min_improvement_vs_naive`, otherwise naive is used:
  gold is close to a random walk and the honest answer is often "no edge".
* Ranges are conformal: quantiles of vol-standardised backtest errors, run
  online with Adaptive Conformal Inference (Gibbs & Candès, 2021) so the
  range widens after misses in volatile regimes. Reported coverage is honest:
  each point is judged with quantiles built only from errors already realised.
* IBJA-vs-international basis noise is added to the variance.
"""
from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

log = logging.getLogger(__name__)
FIT_WINDOW = 500
ACI_GAMMA = 0.01      # adaptation speed of the conformal miscoverage level
ACI_WARMUP = 60       # realised errors needed before scoring coverage
CANDIDATES = ("naive", "momentum", "holt_damped", "ar1")


def ewma_vol(log_ret: np.ndarray, halflife: int) -> float:
    r = pd.Series(log_ret).dropna()
    return float(np.sqrt((r ** 2).ewm(halflife=halflife).mean().iloc[-1]))


def candidate_paths(lp: np.ndarray, max_h: int, drift_window: int) -> dict[str, np.ndarray]:
    """Expected cumulative log return for h = 1..max_h from each candidate."""
    r = np.diff(lp[-FIT_WINDOW:])
    hs = np.arange(1, max_h + 1)
    paths = {"naive": np.zeros(max_h), "momentum": 0.5 * r[-drift_window:].mean() * hs}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        holt = ExponentialSmoothing(lp[-FIT_WINDOW:], trend="add", damped_trend=True,
                                    initialization_method="estimated").fit()
    paths["holt_damped"] = np.asarray(holt.forecast(max_h)) - lp[-1]

    m = 0.5 * r.mean()                                   # shrunk long-run drift
    x, y = r[:-1] - m, r[1:] - m
    phi = float(np.clip((x @ y) / (x @ x), -0.5, 0.5)) if x @ x > 0 else 0.0
    steps = m + (r[-1] - m) * phi ** hs                  # mean-reverting AR(1) path of returns
    paths["ar1"] = np.cumsum(steps)
    return paths


def basis_vol(con, prior: float) -> tuple[float, int]:
    """Daily vol of the IBJA/international basis; prior until we have enough IBJA history."""
    b = con.execute("""SELECT basis_ratio FROM gold.fact_ibja_reconciliation
                       WHERE session = 'PM' AND basis_ratio IS NOT NULL ORDER BY rate_date""").df()["basis_ratio"]
    n = len(b)
    if n < 20:
        return prior, n
    return float(np.log(b).diff().std()), n


def backtest(lp: np.ndarray, cfg: dict) -> pd.DataFrame:
    """Walk-forward: at each origin only data up to that day is used."""
    horizons = cfg["horizons"]
    max_h = max(horizons.values())
    n, recs = len(lp), []
    start = max(FIT_WINDOW // 2, n - cfg["backtest_days"] - max_h)
    for o in range(start, n - max_h, cfg["backtest_step"]):
        hist = lp[: o + 1]
        sigma = ewma_vol(np.diff(hist), cfg["vol_halflife"])
        paths = candidate_paths(hist, max_h, cfg["drift_window"])
        for name, h in horizons.items():
            actual = lp[o + h] - hist[-1]
            for cand, path in paths.items():
                mu = path[h - 1]
                recs.append({"origin": o, "horizon": name, "trading_days": h, "candidate": cand,
                             "mu": mu, "actual": actual, "z": (actual - mu) / (sigma * np.sqrt(h))})
    return pd.DataFrame(recs)


def adaptive_conformal(origins: np.ndarray, z: np.ndarray, h: int, target: float) -> dict:
    """Online ACI over the backtest. At each origin only errors whose outcome was
    already known (origin <= now - h) are used. Returns final quantiles + coverage."""
    alpha, hits = target, []
    for i, o in enumerate(origins):
        past = z[origins <= o - h]
        if len(past) < ACI_WARMUP:
            continue
        c = np.median(past)
        a = min(max(alpha, 0.002), 0.5)
        lo, hi = np.quantile(past - c, [a / 2, 1 - a / 2])
        hit = lo <= z[i] - c <= hi
        hits.append(hit)
        alpha += ACI_GAMMA * (target - (0 if hit else 1))
    # Centre errors: past drift (e.g. a bull run) must not leak into P(up) or the range
    # when the backtest says drift has no out-of-sample value.
    zc = z - np.median(z)
    a = min(max(alpha, 0.002), 0.5)
    lo, hi = np.quantile(zc, [a / 2, 1 - a / 2])
    return {"q_lo": lo, "q_hi": hi, "z_resid": zc, "alpha": a,
            "holdout_coverage_pct": 100 * float(np.mean(hits)) if hits else float("nan")}


def select_and_calibrate(bt: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, dict]:
    """Score candidates, pick one per horizon, calibrate its range with online ACI."""
    bt = bt.assign(abs_err=(bt.actual - bt.mu).abs(),
                   dir_hit=np.sign(bt.actual) == np.sign(bt.mu))
    scores = (bt.groupby(["horizon", "trading_days", "candidate"])
                .agg(n=("abs_err", "size"), mae_pct=("abs_err", lambda x: 100 * x.mean()),
                     direction_hit_pct=("dir_hit", lambda x: 100 * x.mean()))
                .reset_index())
    chosen, calib = [], {}
    for (hname, h), g in scores.groupby(["horizon", "trading_days"]):
        naive_mae = g.loc[g.candidate == "naive", "mae_pct"].iloc[0]
        best = g.loc[g.mae_pct.idxmin()]
        pick = best.candidate if best.mae_pct <= naive_mae * (1 - cfg["min_improvement_vs_naive"]) else "naive"
        chosen.append((hname, pick))

        sel = bt[(bt.horizon == hname) & (bt.candidate == pick)].sort_values("origin")
        calib[hname] = {"model": pick, **adaptive_conformal(sel.origin.to_numpy(), sel.z.to_numpy(),
                                                            h, 1 - cfg["confidence"])}

    scores["selected"] = [(r.horizon, r.candidate) in chosen for r in scores.itertuples()]
    naive = scores[scores.candidate == "naive"].set_index("horizon")["mae_pct"]
    scores["improvement_vs_naive_pct"] = 100 * (1 - scores.mae_pct / scores.horizon.map(naive))
    scores["holdout_coverage_pct"] = [calib[r.horizon]["holdout_coverage_pct"] if r.selected else np.nan
                                      for r in scores.itertuples()]
    return scores.sort_values(["trading_days", "mae_pct"]).reset_index(drop=True), calib


def forecast(prices: pd.Series, cfg: dict, calib: dict, basis_sigma: float) -> pd.DataFrame:
    lp = np.log(prices.to_numpy())
    max_h = max(cfg["horizons"].values())
    sigma_intl = ewma_vol(np.diff(lp), cfg["vol_halflife"])
    paths = candidate_paths(lp, max_h, cfg["drift_window"])
    last = float(prices.iloc[-1])
    rows = []
    for name, h in cfg["horizons"].items():
        c = calib[name]
        mu = paths[c["model"]][h - 1]
        s_intl = sigma_intl * np.sqrt(h)
        s_tot = np.sqrt(s_intl ** 2 + basis_sigma ** 2 * h)   # add IBJA basis noise to the range
        rows.append({"horizon": name, "trading_days": h, "model": c["model"], "base_price": last,
                     "point": last * np.exp(mu),
                     "lower": last * np.exp(mu + c["q_lo"] * s_tot),
                     "upper": last * np.exp(mu + c["q_hi"] * s_tot),
                     "prob_up": float(np.mean(mu / s_intl + c["z_resid"] > 0)),
                     "daily_vol": sigma_intl, "basis_daily_vol": basis_sigma})
    return pd.DataFrame(rows)


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
    week_sigma = np.sqrt(wk.daily_vol ** 2 + wk.basis_daily_vol ** 2) * np.sqrt(5)
    var_95 = grams / 10 * wk.base_price * (1 - np.exp(-1.645 * week_sigma))
    return {"action": action, "reason": why, "prob_up_week": float(wk.prob_up),
            "above_sma50": bool(above_trend), "inventory_grams": grams, "var95_week_inr": float(var_95)}


def _replace_for_date(con, name: str, df: pd.DataFrame, as_of) -> None:
    """Idempotent per as_of_date; derived tables are rebuilt if their schema changed."""
    con.register("_df", df)
    cols = con.execute(f"SELECT column_name FROM information_schema.columns "
                       f"WHERE table_schema = 'gold' AND table_name = '{name}'").df().column_name.tolist()
    if cols and set(cols) != set(df.columns):
        log.info("model: schema of gold.%s changed - rebuilding", name)
        con.execute(f"DROP TABLE gold.{name}")
    con.execute(f"CREATE TABLE IF NOT EXISTS gold.{name} AS SELECT * FROM _df LIMIT 0")
    con.execute(f"DELETE FROM gold.{name} WHERE as_of_date = ?", [as_of])
    con.execute(f"INSERT INTO gold.{name} BY NAME SELECT * FROM _df")
    con.unregister("_df")


def run(con, settings, run_id: str) -> dict:
    cfg = settings["model"]
    feats = con.execute("SELECT * FROM gold.features ORDER BY trade_date").df().set_index("trade_date")
    prices = feats["price"]
    bt = backtest(np.log(prices.to_numpy()), cfg)
    scores, calib = select_and_calibrate(bt, cfg)
    b_sigma, b_n = basis_vol(con, cfg["basis_daily_vol_prior"])
    fc = forecast(prices, cfg, calib, b_sigma)
    sig = business_signal(fc, feats.iloc[-1], settings["business"])
    as_of = pd.Timestamp(feats.index[-1]).date()
    _replace_for_date(con, "forecast", fc.assign(run_id=run_id, as_of_date=as_of), as_of)
    _replace_for_date(con, "model_backtest", scores.assign(run_id=run_id, as_of_date=as_of), as_of)
    log.info("model: as_of=%s models=%s signal=%s P(up,1w)=%.0f%% basis_vol=%.2f%% (n=%d)", as_of,
             {k: v["model"] for k, v in calib.items()}, sig["action"], 100 * sig["prob_up_week"], 100 * b_sigma, b_n)
    return {"as_of": as_of, "forecast": fc, "backtest": scores, "signal": sig, "history": feats,
            "basis": {"daily_vol": b_sigma, "n_days": b_n}}
