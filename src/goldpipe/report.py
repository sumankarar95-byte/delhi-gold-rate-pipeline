"""Render a self-contained HTML dashboard for the shop (one file, no server)."""
from __future__ import annotations

import html
import json
from datetime import datetime

import pandas as pd

HORIZON_LABEL = {"next_day": "Next trading day", "one_week": "1 week", "one_month": "1 month"}
ACTION_TONE = {"ACCUMULATE": "up", "LIGHTEN": "down", "HOLD": "flat"}


def inr(x: float) -> str:
    """Indian digit grouping: 1,33,482."""
    s = f"{abs(round(x)):d}"
    head, tail = s[:-3], s[-3:]
    while len(head) > 2:
        tail = head[-2:] + "," + tail
        head = head[:-2]
    return ("-" if x < 0 else "") + "₹" + (head + "," + tail if head else tail)


def _rows(cells: list[list[str]]) -> str:
    return "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in cells)


def render(settings, run_id: str, result: dict, dq: pd.DataFrame, board: pd.DataFrame):
    fc, bt, sig, hist = result["forecast"], result["backtest"], result["signal"], result["history"]
    as_of = result["as_of"]
    recent = hist.iloc[-260:]
    chart = {
        "dates": [str(pd.Timestamp(d).date()) for d in recent.index],
        "price": recent["price"].round(0).tolist(),
        "sma50": recent["sma_50"].round(0).tolist(),
        "fc": [{"h": int(r.trading_days), "point": round(r.point), "lower": round(r.lower),
                "upper": round(r.upper)} for r in fc.itertuples()],
    }
    p = settings["pricing"]
    out_dir = settings.path("reports")
    snapshots = sorted({f.stem.removeprefix("gold_dashboard_") for f in out_dir.glob("gold_dashboard_*.html")}
                       | {str(as_of)}, reverse=True)[:30]
    archive = ('<p class="note">Previous days: ' + " · ".join(
        f'<a href="gold_dashboard_{d}.html" style="color:var(--gold)">{d}</a>' for d in snapshots) + "</p>")

    board_rows = _rows([[f"<b>{r.karat}</b>", inr(r.market_inr_10g), inr(r.market_inr_g),
                         inr(r.we_buy_inr_10g), inr(r.we_sell_inr_10g)] for r in board.itertuples()])
    fc_rows = _rows([[HORIZON_LABEL[r.horizon], inr(r.point), f"{inr(r.lower)} – {inr(r.upper)}",
                      f"{r.prob_up:.0%}"] for r in fc.itertuples()])
    bt_rows = _rows([[HORIZON_LABEL[r.horizon], f"{r.mape_model_pct:.2f}%", f"{r.mape_naive_pct:.2f}%",
                      "✓" if r.beats_naive else "✗", f"{r.direction_hit_pct:.0f}%",
                      f"{r.band_coverage_pct:.0f}%"] for r in bt.itertuples()])
    dq_rows = _rows([[html.escape(r.check_name), r.severity,
                      f'<span class="pill {"ok" if r.passed else ("warn" if r.severity == "warn" else "bad")}">'
                      f'{"PASS" if r.passed else "FAIL"}</span>', html.escape(str(r.observed))]
                     for r in dq.itertuples()])
    conf = int(settings["model"]["confidence"] * 100)

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delhi Gold Desk</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {{ --bg:#faf8f3; --card:#fff; --ink:#1f1b14; --muted:#6b6457; --line:#e8e2d4; --gold:#b8860b;
        --up:#1a7f4b; --down:#b3261e; --flat:#7a6a3a; --band:rgba(184,134,11,.15); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#16140f; --card:#1f1c16;
        --ink:#f1ead9; --muted:#a59c88; --line:#352f24; --gold:#e0b04a; --up:#4cc38a; --down:#f08070;
        --flat:#d4c08a; --band:rgba(224,176,74,.18); }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1100px; margin:0 auto; padding:24px 16px 48px }}
header {{ display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px }}
h1 {{ margin:0; font-size:26px }} h1 span {{ color:var(--gold) }}
h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 12px }}
.meta {{ color:var(--muted); font-size:13px }}
.grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); margin-top:16px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px; overflow-x:auto }}
.wide {{ grid-column:1/-1 }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums }}
th, td {{ text-align:right; padding:7px 8px; border-bottom:1px solid var(--line); white-space:nowrap }}
th:first-child, td:first-child {{ text-align:left }}
th {{ font-weight:600; color:var(--muted); font-size:12px }}
.signal {{ font-size:32px; font-weight:700 }} .up {{ color:var(--up) }} .down {{ color:var(--down) }} .flat {{ color:var(--flat) }}
.kpis {{ display:flex; gap:24px; flex-wrap:wrap; margin-top:10px }}
.kpi b {{ display:block; font-size:20px }} .kpi span {{ color:var(--muted); font-size:12px }}
.pill {{ padding:2px 8px; border-radius:99px; font-size:12px; font-weight:600 }}
.pill.ok {{ background:rgba(26,127,75,.14); color:var(--up) }} .pill.warn {{ background:rgba(184,134,11,.18); color:var(--flat) }}
.pill.bad {{ background:rgba(179,38,30,.14); color:var(--down) }}
.note {{ color:var(--muted); font-size:13px; margin:10px 0 0 }}
.lineage {{ font-family:ui-monospace,Menlo,monospace; font-size:12.5px; color:var(--muted); line-height:1.9 }}
.lineage b {{ color:var(--ink) }}
</style></head><body><main>
<header><h1>Delhi Gold Desk <span>· rate & forecast</span></h1>
<div class="meta">Market data as of <b>{as_of}</b> · generated {datetime.now():%d %b %Y %H:%M} · run <code>{run_id}</code></div></header>

<div class="grid">
<section class="card"><h2>Today's signal</h2>
<div class="signal {ACTION_TONE[sig['action']]}">{sig['action']}</div>
<p style="margin:6px 0 0">{html.escape(sig['reason'])}</p>
<div class="kpis">
 <div class="kpi"><b>{sig['prob_up_week']:.0%}</b><span>chance higher in 1 week</span></div>
 <div class="kpi"><b>{"Above" if sig['above_sma50'] else "Below"}</b><span>50-day trend</span></div>
 <div class="kpi"><b>{inr(sig['var95_week_inr'])}</b><span>1-week 95% VaR on {sig['inventory_grams']:,} g stock</span></div>
</div></section>

<section class="card"><h2>Rate board (₹, incl. {p['import_duty_pct']:g}% duty + {p['gst_pct']:g}% GST)</h2>
<table><tr><th>Purity</th><th>Market /10g</th><th>/g</th><th>We buy /10g</th><th>We sell /10g</th></tr>{board_rows}</table>
<p class="note">Local premium applied: {inr(p['local_premium_inr_10g'])}/10g. Making charges extra. Run <code>calibrate</code> with the real Delhi board rate to align.</p></section>

<section class="card wide"><h2>24K Delhi ₹/10g — last 12 months + forecast ({conf}% range)</h2>
<div style="position:relative;height:340px"><canvas id="c"></canvas></div></section>

<section class="card"><h2>Forecast</h2>
<table><tr><th>Horizon</th><th>Expected</th><th>{conf}% range</th><th>P(up)</th></tr>{fc_rows}</table>
<p class="note">The range is the useful part: price your buy-back/sell quotes and size stock so a move to either edge doesn't hurt.</p></section>

<section class="card"><h2>Model honesty — walk-forward backtest</h2>
<table><tr><th>Horizon</th><th>Model err</th><th>Naive err</th><th>Beats naive</th><th>Direction</th><th>In range</th></tr>{bt_rows}</table>
<p class="note">Naive = "price stays where it is". Gold is close to a random walk; a ~50% direction hit-rate is normal. Range coverage near {conf}% means the bands are well calibrated.</p></section>

<section class="card"><h2>Data quality gate</h2>
<table><tr><th>Check</th><th>Severity</th><th>Result</th><th>Observed</th></tr>{dq_rows}</table></section>

<section class="card"><h2>Lineage</h2><div class="lineage">
<b>Yahoo Finance</b> GC=F, INR=X<br>→ <b>bronze/</b> append-only Parquet, partitioned by ingest_date<br>
→ <b>silver.market_prices</b> typed, de-duplicated (latest ingest wins)<br>
→ <b>gold.fact_delhi_rate</b> FX forward-fill, landed-cost model<br>
→ <b>gold.features</b> returns, SMA 20/50/200<br>→ <b>audit.dq_results</b> quality gate<br>
→ <b>gold.forecast</b>, <b>gold.model_backtest</b> → this report</div></section>
</div>
{archive}
<p class="note">Decision support only — not financial advice. Forecasts are statistical estimates and can be wrong.</p>
</main>
<script>
const d = {json.dumps(chart)};
const cs = getComputedStyle(document.documentElement), v = n => cs.getPropertyValue(n).trim();
const last = d.dates[d.dates.length - 1], n = d.price.length;
const labels = [...d.dates], fcPoint = Array(n).fill(null), lo = Array(n).fill(null), hi = Array(n).fill(null);
fcPoint[n-1] = lo[n-1] = hi[n-1] = d.price[n-1];
const maxH = Math.max(...d.fc.map(f => f.h));
for (let i = 1; i <= maxH; i++) {{
  const dt = new Date(last); dt.setDate(dt.getDate() + Math.round(i * 7 / 5));
  labels.push(dt.toISOString().slice(0, 10));
  const f = d.fc.find(x => x.h === i);
  fcPoint.push(f ? f.point : null); lo.push(f ? f.lower : null); hi.push(f ? f.upper : null);
}}
const fmt = x => '₹' + Math.round(x).toLocaleString('en-IN');
new Chart(document.getElementById('c'), {{
  type: 'line',
  data: {{ labels, datasets: [
    {{ label: '24K ₹/10g', data: d.price, borderColor: v('--gold'), borderWidth: 2, pointRadius: 0 }},
    {{ label: '50-day avg', data: d.sma50, borderColor: v('--muted'), borderWidth: 1, borderDash: [4,4], pointRadius: 0 }},
    {{ label: 'Range high', data: hi, borderColor: 'transparent', backgroundColor: v('--band'), fill: '+1', spanGaps: true, pointRadius: 0 }},
    {{ label: 'Range low', data: lo, borderColor: 'transparent', pointRadius: 0, spanGaps: true }},
    {{ label: 'Forecast', data: fcPoint, borderColor: v('--gold'), borderDash: [6,4], spanGaps: true, pointRadius: 3 }},
  ] }},
  options: {{ maintainAspectRatio: false, interaction: {{ mode: 'index', intersect: false }},
    plugins: {{ legend: {{ labels: {{ color: v('--muted'), filter: i => !i.text.startsWith('Range') }} }},
               tooltip: {{ callbacks: {{ label: c => c.raw == null ? null : c.dataset.label + ': ' + fmt(c.raw) }} }} }},
    scales: {{ x: {{ ticks: {{ color: v('--muted'), maxTicksLimit: 8 }}, grid: {{ display: false }} }},
              y: {{ ticks: {{ color: v('--muted'), callback: fmt }}, grid: {{ color: v('--line') }} }} }} }}
}});
</script></body></html>"""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "gold_dashboard.html"
    out.write_text(page)
    (out_dir / f"gold_dashboard_{as_of}.html").write_text(page)   # dated snapshot for history
    return out
