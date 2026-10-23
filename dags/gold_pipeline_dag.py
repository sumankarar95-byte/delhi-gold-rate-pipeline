"""Airflow DAG: one task per stage so failures retry in isolation.

Schedule: 18:30 IST (13:00 UTC) on weekdays, after the Indian session and
before the next day's opening board is set.
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

STAGE = "cd /opt/gold-rate-pipeline && PYTHONPATH=src python -m goldpipe.pipeline {}"

with DAG(
    dag_id="delhi_gold_rate",
    start_date=datetime(2026, 1, 1),
    schedule="0 13 * * 1-5",
    catchup=False,
    default_args={"retries": 3, "retry_delay": timedelta(minutes=10), "owner": "data-eng"},
    tags=["gold", "forecast"],
) as dag:
    extract, transform, quality, report = (
        BashOperator(task_id=s, bash_command=STAGE.format(s))
        for s in ("extract", "transform", "quality", "report")
    )
    extract >> transform >> quality >> report
