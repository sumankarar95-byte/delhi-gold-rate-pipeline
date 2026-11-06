-- SILVER: typed, de-duplicated, FINAL market prices.
-- A bar only counts once it was captured after its market closed (ref.series_calendar);
-- a mid-session snapshot is still moving and is ignored. Bronze is append-only, so the
-- same trade_date may land several times: the most recent final capture wins.
CREATE SCHEMA IF NOT EXISTS silver;

CREATE OR REPLACE TABLE silver.market_prices AS
SELECT m.trade_date::DATE AS trade_date, m.series, m.ticker,
       m.open::DOUBLE AS open, m.high::DOUBLE AS high, m.low::DOUBLE AS low, m.close::DOUBLE AS close,
       m.ingested_at
FROM read_parquet('{bronze}/ingest_date=*/*.parquet', hive_partitioning = true) m
JOIN ref.series_calendar c USING (series)
WHERE m.close IS NOT NULL AND m.close > 0
  AND m.ingested_at >= timezone(c.tz, m.trade_date::DATE + c.close_time)
QUALIFY row_number() OVER (PARTITION BY m.trade_date, m.series ORDER BY m.ingested_at DESC) = 1;
