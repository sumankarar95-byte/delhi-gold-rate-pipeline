-- SILVER: IBJA benchmark rates (₹ per 10 g, ex-GST; silver ₹ per kg).
-- {ibja_source} is the bronze Parquet glob, or an empty typed relation when
-- no IBJA data has been landed yet (pipeline still runs on the fallback model).
CREATE OR REPLACE TABLE silver.ibja_rates AS
SELECT rate_date::DATE AS rate_date, session, purity,
       gold_rate_inr_10g::DOUBLE AS gold_rate_inr_10g,
       silver_rate_inr_kg::DOUBLE AS silver_rate_inr_kg,
       source, ingested_at
FROM {ibja_source}
WHERE gold_rate_inr_10g > 0
QUALIFY row_number() OVER (PARTITION BY rate_date, session, purity ORDER BY ingested_at DESC) = 1;
