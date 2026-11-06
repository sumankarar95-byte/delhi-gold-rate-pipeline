"""Benchmark open-source time-series foundation models against the pipeline's
own candidates, on exactly the same walk-forward backtest.

    make bench          # writes benchmarks/results/

Contenders
  * Amazon Chronos-2 (amazon/chronos-2)
  * Google TimesFM 2.5 200M (google/timesfm-2.5-200m-pytorch)
  * pipeline candidates: naive, momentum, holt_damped, ar1

Every forecast at origin t sees only prices up to t. Errors are log-return
errors, so they are comparable across models and price levels. A
Diebold-Mariano test (Newey-West variance, lag h-1 for overlapping horizons)
says whether a model's edge over naive is statistically real or luck.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from math import erf, sqrt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from goldpipe import model  # noqa: E402
from goldpipe.config import Settings  # noqa: E402

log = logging.getLogger("bench")
OUT = ROOT / "benchmarks" / "results"
QUANTILES = [0.1, 0.5, 0.9]   # 80% interval: supported by both models' heads


def load_prices(settings) -> np.ndarray:
    with duckdb.connect(str(settings.path("warehouse")), read_only=True) as con:
        return con.execute("SELECT price FROM gold.features ORDER BY trade_date").df()["price"].to_numpy()


def origins_for(n: int, cfg: dict) -> np.ndarray:
    """Same origin grid as goldpipe.model.backtest."""
    max_h = max(cfg["horizons"].values())
    start = max(model.FIT_WINDOW // 2, n - cfg["backtest_days"] - max_h)
    return np.arange(start, n - max_h, cfg["backtest_step"])


def contexts(prices: np.ndarray, origins: np.ndarray, length: int) -> list[np.ndarray]:
    return [prices[max(0, o + 1 - length): o + 1].astype(np.float32) for o in origins]


def run_chronos(ctx: list[np.ndarray], max_h: int, batch: int) -> np.ndarray:
    """-> array (n_origins, max_h, 3) of q10/q50/q90 price forecasts."""
    import torch
    from chronos import BaseChronosPipeline
    pipe = BaseChronosPipeline.from_pretrained("amazon/chronos-2", device_map="cpu", torch_dtype=torch.float32)
    out = []
    for i in range(0, len(ctx), batch):
        q, _ = pipe.predict_quantiles([torch.tensor(c) for c in ctx[i:i + batch]],
                                      prediction_length=max_h, quantile_levels=QUANTILES)
        q = q.numpy() if hasattr(q, "numpy") else np.stack([x.numpy() for x in q])
        out.append(q.reshape(len(ctx[i:i + batch]), max_h, len(QUANTILES)))   # (batch[,1 var], h, q)
    return np.concatenate(out)


def run_timesfm(ctx: list[np.ndarray], max_h: int, batch: int) -> np.ndarray:
    import timesfm
    m = timesfm.TimesFM_2p5_200M_torch.from_pretrained("google/timesfm-2.5-200m-pytorch")
    m.compile(timesfm.ForecastConfig(max_context=max(len(c) for c in ctx), max_horizon=max(32, max_h),
                                     normalize_inputs=True, per_core_batch_size=batch,
                                     use_continuous_quantile_head=True, fix_quantile_crossing=True))
    _, quant = m.forecast(horizon=max_h, inputs=[c.astype(np.float64) for c in ctx])
    # quantile head layout: [mean, q10, q20, ..., q90]
    return np.stack([quant[:, :max_h, 1], quant[:, :max_h, 5], quant[:, :max_h, 9]], axis=-1)


def dm_test(e_model: np.ndarray, e_naive: np.ndarray, h: int) -> float:
    """Two-sided Diebold-Mariano p-value on absolute errors (H0: equal accuracy)."""
    d = np.abs(e_naive) - np.abs(e_model)
    n, dbar = len(d), d.mean()
    dc = d - dbar
    var = dc @ dc / n
    for k in range(1, h):                                   # Newey-West, Bartlett weights
        var += 2 * (1 - k / h) * (dc[k:] @ dc[:-k]) / n
    if var <= 0:
        return float("nan")
    stat = dbar / sqrt(var / n)
    return float(2 * (1 - 0.5 * (1 + erf(abs(stat) / sqrt(2)))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", type=int, default=1024, help="history length given to foundation models")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--models", default="chronos2,timesfm")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    settings = Settings.load()
    cfg = settings["model"]
    prices = load_prices(settings)
    lp = np.log(prices)
    origins = origins_for(len(prices), cfg)
    max_h = max(cfg["horizons"].values())
    log.info("series=%d days, origins=%d (%s..%s), horizons=%s", len(prices), len(origins),
             origins[0], origins[-1], cfg["horizons"])

    # Pipeline candidates (same function the daily pipeline uses).
    bt = model.backtest(lp, cfg)
    rows = [bt.assign(err=bt.actual - bt.mu, in80=np.nan)[["origin", "horizon", "trading_days", "candidate", "err", "in80"]]]

    ctx = contexts(prices, origins, args.context)
    runners = {"chronos2": run_chronos, "timesfm2.5": run_timesfm}
    timings = {}
    for name in args.models.replace("timesfm", "timesfm2.5").split(","):
        t0 = time.time()
        q = runners[name](ctx, max_h, args.batch)                     # (n, max_h, 3) prices
        timings[name] = time.time() - t0
        log.info("%s: %d forecasts in %.0fs", name, len(origins), timings[name])
        for hname, h in cfg["horizons"].items():
            actual = lp[origins + h] - lp[origins]
            mu = np.log(q[:, h - 1, 1]) - lp[origins]
            lo, hi = np.log(q[:, h - 1, 0]) - lp[origins], np.log(q[:, h - 1, 2]) - lp[origins]
            rows.append(pd.DataFrame({"origin": origins, "horizon": hname, "trading_days": h, "candidate": name,
                                      "err": actual - mu, "in80": (actual >= lo) & (actual <= hi)}))
    res = pd.concat(rows, ignore_index=True)

    summary = []
    for (hname, h), g in res.groupby(["horizon", "trading_days"]):
        naive = g[g.candidate == "naive"].set_index("origin").err
        for cand, gc in g.groupby("candidate"):
            e = gc.set_index("origin").err.reindex(naive.index)
            mae, nmae = 100 * e.abs().mean(), 100 * naive.abs().mean()
            summary.append({"horizon": hname, "trading_days": h, "model": cand, "n": e.notna().sum(),
                            "mae_pct": mae, "vs_naive_pct": 100 * (1 - mae / nmae),
                            "dm_p_value": np.nan if cand == "naive" else dm_test(e.to_numpy(), naive.to_numpy(), h),
                            "direction_hit_pct": np.nan,   # filled below
                            "coverage80_pct": 100 * gc.in80.mean() if gc.in80.notna().any() else np.nan})
    s = pd.DataFrame(summary)
    # direction: sign of forecast vs sign of actual move
    act = {h: lp[origins + h] - lp[origins] for h in cfg["horizons"].values()}
    for i, r in s.iterrows():
        if r.model == "naive":
            continue
        gc = res[(res.horizon == r.horizon) & (res.candidate == r.model)].set_index("origin").reindex(origins)
        a = act[r.trading_days]
        mu = a - gc.err.to_numpy()
        s.loc[i, "direction_hit_pct"] = 100 * np.mean(np.sign(mu) == np.sign(a))
    s = s.sort_values(["trading_days", "mae_pct"]).reset_index(drop=True)

    OUT.mkdir(parents=True, exist_ok=True)
    s.to_csv(OUT / "summary.csv", index=False)
    res.to_parquet(OUT / "errors.parquet", index=False)
    with pd.option_context("display.width", 140, "display.float_format", "{:.3f}".format):
        print(s.to_string(index=False))
    write_markdown(s, len(origins), args, timings)
    return 0


def write_markdown(s: pd.DataFrame, n: int, args, timings: dict) -> None:
    lines = ["# Foundation-model benchmark", "",
             f"{n} daily walk-forward origins · foundation-model context {args.context} days · "
             f"errors are log-return errors · DM = Diebold-Mariano p-value vs naive (lower = more convincing).", ""]
    for hname, g in s.groupby("trading_days", sort=True):
        lines += [f"## {g.horizon.iloc[0]} ({hname} trading days)", "",
                  "| Model | MAE % | vs naive | DM p | Direction | 80% band coverage |", "|---|---|---|---|---|---|"]
        for r in g.itertuples():
            lines.append(f"| {r.model} | {r.mae_pct:.3f} | {r.vs_naive_pct:+.1f}% | "
                         f"{'' if pd.isna(r.dm_p_value) else f'{r.dm_p_value:.3f}'} | "
                         f"{'' if pd.isna(r.direction_hit_pct) else f'{r.direction_hit_pct:.0f}%'} | "
                         f"{'' if pd.isna(r.coverage80_pct) else f'{r.coverage80_pct:.0f}%'} |")
        lines.append("")
    lines.append("Runtime (CPU, Apple M4): " + ", ".join(f"{k} {v:.0f}s" for k, v in timings.items()))
    (OUT / "README.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
