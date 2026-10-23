-- SILVER: typed, de-duplicated market prices.
-- Bronze is append-only, so the same trade_date may land several times;
-- keep the most recently ingested version (handles late revisions).
CREATE SCHEMA IF NOT EXISTS silver;

CREATE OR REPLACE TABLE silver.market_prices AS
SELECT trade_date::DATE AS trade_date, series, ticker,
       open::DOUBLE AS open, high::DOUBLE AS high, low::DOUBLE AS low, close::DOUBLE AS close,
       ingested_at
FROM read_parquet('{bronze}/ingest_date=*/*.parquet', hive_partitioning = true)
WHERE close IS NOT NULL AND close > 0
QUALIFY row_number() OVER (PARTITION BY trade_date, series ORDER BY ingested_at DESC) = 1;
