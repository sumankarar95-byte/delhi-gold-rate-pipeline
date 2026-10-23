"""CLI entry point. Each stage is independently runnable so an orchestrator
(Airflow, cron, GitHub Actions) can schedule and retry them separately.

    python -m goldpipe.pipeline run              # full pipeline
    python -m goldpipe.pipeline extract|transform|quality|model|report
    python -m goldpipe.pipeline calibrate 128450 # fallback when no IBJA data: today's real 24K ₹/10g
    python -m goldpipe.pipeline import-ibja rates.csv   # seed IBJA history
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import uuid
import warnings
from datetime import datetime

from . import extract, extract_ibja, model, quality, report, warehouse
from .config import ROOT, Settings

log = logging.getLogger("goldpipe")
warnings.filterwarnings("ignore", message="The inplace keyword")  # pandas 3 noise from yfinance/duckdb


def _run_id() -> str:
    return f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def stage_quality(settings, run_id):
    with warehouse.connect(settings) as con:
        return quality.run(con, run_id)


def stage_model(settings, run_id):
    with warehouse.connect(settings) as con:
        return model.run(con, settings, run_id)


def stage_report(settings, run_id, result=None, dq=None):
    with warehouse.connect(settings) as con:
        result = result or model.run(con, settings, run_id)
        if dq is None:
            dq = con.execute("SELECT * FROM audit.dq_results WHERE run_id = "
                             "(SELECT max(run_id) FROM audit.dq_results)").df()
        board = con.execute("SELECT * FROM gold.v_rate_board WHERE trade_date = "
                            "(SELECT max(trade_date) FROM gold.fact_delhi_rate) ORDER BY fineness DESC").df()
        anchor = con.execute("SELECT * FROM gold.anchor").df().iloc[0].to_dict()
        recon = con.execute("SELECT * FROM gold.fact_ibja_reconciliation "
                            "ORDER BY rate_date DESC, session DESC LIMIT 8").df()
        return report.render(settings, run_id, result, dq, board, anchor, recon)


def calibrate(settings, actual_rate: float) -> None:
    """Set local premium so the model matches the observed Delhi 24K rate today."""
    with warehouse.connect(settings) as con:
        modelled, current_premium = con.execute(
            "SELECT delhi_24k_inr_10g, p.local_premium_inr_10g FROM gold.fact_delhi_rate, ref.pricing_params p "
            "ORDER BY trade_date DESC LIMIT 1").fetchone()
    premium = round(actual_rate - (modelled - current_premium))
    cfg = ROOT / "config" / "settings.yaml"
    text = re.sub(r"(local_premium_inr_10g:\s*)-?[\d.]+", rf"\g<1>{premium}", cfg.read_text())
    cfg.write_text(text)
    log.info("calibrate: local premium set to ₹%s per 10 g (re-run `transform` to apply)", premium)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="goldpipe")
    ap.add_argument("stage", choices=["run", "extract", "transform", "quality", "model", "report",
                                      "calibrate", "import-ibja"])
    ap.add_argument("value", nargs="?", help="calibrate: actual Delhi 24K ₹/10g; import-ibja: CSV path")
    ap.add_argument("--config", default=ROOT / "config" / "settings.yaml")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    settings, run_id = Settings.load(args.config), _run_id()
    log.info("run_id=%s stage=%s", run_id, args.stage)
    try:
        if args.stage in ("run", "extract"):
            extract.run(settings)
            extract_ibja.run(settings)
        if args.stage == "import-ibja":
            if not args.value:
                ap.error("import-ibja needs a CSV path")
            extract_ibja.import_csv(settings, args.value)
        if args.stage in ("run", "transform"):
            warehouse.run(settings)
        dq = stage_quality(settings, run_id) if args.stage in ("run", "quality") else None
        if args.stage == "model":
            stage_model(settings, run_id)
        if args.stage in ("run", "report"):
            out = stage_report(settings, run_id, dq=dq)
            log.info("report: %s", out)
        if args.stage == "calibrate":
            if args.value is None:
                ap.error("calibrate needs today's actual Delhi 24K rate, e.g. calibrate 128450")
            calibrate(settings, float(args.value))
    except Exception:
        log.exception("pipeline failed (run_id=%s)", run_id)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
