-- GOLD: the Indian gold rate (what we forecast), plus the international reference.
CREATE SCHEMA IF NOT EXISTS gold;

-- International reference: COMEX gold × USD/INR + duty + GST. Not the forecast target,
-- only used to measure the Indian premium/discount. COMEX calendar; FX forward-filled.
CREATE OR REPLACE TABLE gold.fact_intl_landed_cost AS
WITH cal AS (
    SELECT DISTINCT trade_date FROM silver.market_prices WHERE series = 'gold_usd_oz'
),
wide AS (
    SELECT c.trade_date, g.close AS gold_usd_oz, f.close AS usd_inr
    FROM cal c
    LEFT JOIN silver.market_prices g ON g.trade_date = c.trade_date AND g.series = 'gold_usd_oz'
    LEFT JOIN silver.market_prices f ON f.trade_date = c.trade_date AND f.series = 'usd_inr'
),
filled AS (
    SELECT trade_date, gold_usd_oz,
           last_value(usd_inr IGNORE NULLS) OVER w AS usd_inr,
           usd_inr IS NULL AS fx_imputed
    FROM wide
    WINDOW w AS (ORDER BY trade_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT f.trade_date, f.gold_usd_oz, f.usd_inr, f.fx_imputed,
       f.gold_usd_oz * f.usd_inr / p.grams_per_troy_oz * 10                 AS intl_inr_10g,
       f.gold_usd_oz * f.usd_inr / p.grams_per_troy_oz * 10
           * (1 + p.import_duty_pct / 100)                                  AS landed_ex_gst
FROM filled f CROSS JOIN ref.pricing_params p
WHERE f.usd_inr IS NOT NULL;

-- Gold BeES with gap-filling: on the rare days the primary bar is missing, use the backup
-- ETF scaled by the median primary/backup ratio of their last 60 common days (flagged).
CREATE OR REPLACE TABLE gold.india_etf AS
WITH p AS (SELECT trade_date, close FROM silver.market_prices WHERE series = (SELECT etf_series FROM ref.india_params)),
     b AS (SELECT trade_date, close FROM silver.market_prices WHERE series = (SELECT etf_backup_series FROM ref.india_params)),
     k AS (SELECT median(pc / bc) AS ratio FROM (
               SELECT p.close AS pc, b.close AS bc FROM p JOIN b USING (trade_date)
               ORDER BY trade_date DESC LIMIT 60))
SELECT coalesce(p.trade_date, b.trade_date) AS trade_date,
       coalesce(p.close, b.close * k.ratio)  AS close,
       p.close IS NULL                       AS imputed
FROM p FULL OUTER JOIN b USING (trade_date) CROSS JOIN k
WHERE p.close IS NOT NULL OR (b.close IS NOT NULL AND k.ratio IS NOT NULL);

-- ETF -> IBJA calibration: IBJA 999 PM (₹/10g ex-GST) ÷ Gold BeES close on the same day,
-- median of the most recent IBJA days. Config default until IBJA data is loaded.
CREATE OR REPLACE TABLE gold.etf_calibration AS
WITH pairs AS (
    SELECT i.rate_date, i.gold_rate_inr_10g / e.close AS ratio
    FROM silver.ibja_rates i
    JOIN silver.market_prices e
      ON e.trade_date = i.rate_date AND e.series = (SELECT etf_series FROM ref.india_params)
    WHERE i.purity = (SELECT benchmark_purity FROM ref.ibja_params) AND i.session = 'PM'
    QUALIFY row_number() OVER (ORDER BY i.rate_date DESC) <= (SELECT ratio_window FROM ref.india_params)
)
SELECT CASE WHEN count(rate_date) > 0 THEN median(ratio) ELSE any_value(d.etf_to_ibja_ratio_default) END AS ratio,
       CASE WHEN count(rate_date) > 0 THEN 'IBJA-calibrated' ELSE 'config default' END            AS source,
       count(rate_date)                                                                         AS n_ibja_days,
       max(rate_date)                                                                   AS calibrated_through
FROM pairs RIGHT JOIN ref.india_params d ON true;

-- The Indian rate, one row per Indian trading day.
-- IBJA PM when we have it; otherwise Gold BeES close × calibration ratio.
CREATE OR REPLACE TABLE gold.fact_india_rate AS
WITH etf AS (SELECT trade_date, close, imputed FROM gold.india_etf),
ibja AS (
    SELECT rate_date AS trade_date, gold_rate_inr_10g AS ibja_pm_ex_gst FROM silver.ibja_rates
    WHERE purity = (SELECT benchmark_purity FROM ref.ibja_params) AND session = 'PM'
),
cal AS (SELECT trade_date FROM etf UNION SELECT trade_date FROM ibja)
SELECT c.trade_date, e.close AS etf_close, coalesce(e.imputed, false) AS etf_imputed, k.ratio AS etf_ratio,
       e.close * k.ratio                                            AS etf_implied_ex_gst,
       i.ibja_pm_ex_gst,
       coalesce(i.ibja_pm_ex_gst, e.close * k.ratio)                AS india_999_ex_gst,
       CASE WHEN i.ibja_pm_ex_gst IS NOT NULL THEN 'IBJA PM' ELSE 'Gold BeES × ratio' END AS price_source,
       coalesce(i.ibja_pm_ex_gst, e.close * k.ratio) * (1 + p.gst_pct / 100)
           + p.local_premium_inr_10g                                AS retail_24k_incl_gst
FROM cal c
LEFT JOIN etf e USING (trade_date)
LEFT JOIN ibja i USING (trade_date)
CROSS JOIN gold.etf_calibration k
CROSS JOIN ref.pricing_params p
WHERE coalesce(i.ibja_pm_ex_gst, e.close) IS NOT NULL;

-- IBJA reconciliation: how well the ETF tracks IBJA (same day), and the Indian premium
-- over the international landed cost. International side uses the latest close strictly
-- BEFORE the IBJA date (ASOF): COMEX settles after IBJA publishes. No look-ahead.
CREATE OR REPLACE TABLE gold.fact_ibja_reconciliation AS
WITH bench AS (
    SELECT rate_date, session, gold_rate_inr_10g AS ibja_ex_gst
    FROM silver.ibja_rates
    WHERE purity = (SELECT benchmark_purity FROM ref.ibja_params)
),
with_intl AS (          -- ASOF kept in its own step (DuckDB mis-plans it inside longer join chains)
    SELECT b.*, r.trade_date AS intl_trade_date, r.landed_ex_gst
    FROM bench b ASOF LEFT JOIN gold.fact_intl_landed_cost r ON b.rate_date > r.trade_date
)
SELECT w.rate_date, w.session, w.ibja_ex_gst,
       e.close * k.ratio                                           AS etf_implied_ex_gst,
       w.ibja_ex_gst / e.close                                     AS basis_ratio,   -- IBJA per ETF unit
       100 * (w.ibja_ex_gst / (e.close * k.ratio) - 1)             AS gap_vs_etf_pct,
       w.intl_trade_date,
       w.landed_ex_gst                                             AS computed_ex_gst,
       100 * (w.ibja_ex_gst / w.landed_ex_gst - 1)                 AS gap_vs_computed_pct
FROM with_intl w
LEFT JOIN gold.india_etf e ON e.trade_date = w.rate_date
CROSS JOIN gold.etf_calibration k;

-- Model features on the Indian rate (incl. GST): Indian trading days, rupee moves.
CREATE OR REPLACE TABLE gold.features AS
SELECT trade_date, retail_24k_incl_gst AS price,
       ln(price / lag(price) OVER w)                                   AS log_ret,
       avg(price) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)   AS sma_20,
       avg(price) OVER (w ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)   AS sma_50,
       avg(price) OVER (w ROWS BETWEEN 199 PRECEDING AND CURRENT ROW)  AS sma_200
FROM gold.fact_india_rate
WINDOW w AS (ORDER BY trade_date);

-- Purity-wise retail board (incl. GST): what the shop counter shows.
CREATE OR REPLACE VIEW gold.v_rate_board AS
SELECT f.trade_date, pu.karat, pu.fineness,
       round(f.price * pu.fineness / 0.999, 0)                              AS market_inr_10g,
       round(f.price * pu.fineness / 0.999 / 10, 2)                         AS market_inr_g,
       round(f.price * pu.fineness / 0.999 * (1 - b.buy_margin_pct/100), 0) AS we_buy_inr_10g,
       round(f.price * pu.fineness / 0.999 * (1 + b.sell_margin_pct/100), 0) AS we_sell_inr_10g
FROM gold.features f
CROSS JOIN ref.purities pu
CROSS JOIN ref.business_params b;
