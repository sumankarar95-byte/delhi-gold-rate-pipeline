-- GOLD: business-ready Delhi rate fact table + technical features.
CREATE SCHEMA IF NOT EXISTS gold;

-- Gold trading calendar drives the grain; FX is forward-filled onto it
-- (FX and COMEX holidays don't line up).
CREATE OR REPLACE TABLE gold.fact_delhi_rate AS
WITH cal AS (
    SELECT DISTINCT trade_date FROM silver.market_prices
),
wide AS (
    SELECT c.trade_date,
           g.close AS gold_usd_oz,
           f.close AS usd_inr
    FROM cal c
    LEFT JOIN silver.market_prices g ON g.trade_date = c.trade_date AND g.series = 'gold_usd_oz'
    LEFT JOIN silver.market_prices f ON f.trade_date = c.trade_date AND f.series = 'usd_inr'
),
filled AS (
    SELECT trade_date,
           last_value(gold_usd_oz IGNORE NULLS) OVER w AS gold_usd_oz,
           last_value(usd_inr     IGNORE NULLS) OVER w AS usd_inr,
           gold_usd_oz IS NULL AS gold_imputed,
           usd_inr IS NULL     AS fx_imputed
    FROM wide
    WINDOW w AS (ORDER BY trade_date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT f.trade_date, f.gold_usd_oz, f.usd_inr, f.gold_imputed, f.fx_imputed,
       f.gold_usd_oz * f.usd_inr / p.grams_per_troy_oz * 10                       AS intl_inr_10g,
       f.gold_usd_oz * f.usd_inr / p.grams_per_troy_oz * 10
           * (1 + p.import_duty_pct / 100) * (1 + p.gst_pct / 100)
           + p.local_premium_inr_10g                                              AS delhi_24k_inr_10g
FROM filled f CROSS JOIN ref.pricing_params p
WHERE f.gold_usd_oz IS NOT NULL AND f.usd_inr IS NOT NULL
  AND NOT f.gold_imputed;   -- only real gold trading days

-- IBJA reconciliation. Each IBJA print is matched to the latest international
-- close strictly BEFORE its date (ASOF JOIN): COMEX closes ~03:00 IST next day,
-- so that is the last price actually known when IBJA published. No look-ahead.
CREATE OR REPLACE TABLE gold.fact_ibja_reconciliation AS
WITH bench AS (
    SELECT rate_date, session, gold_rate_inr_10g AS ibja_ex_gst
    FROM silver.ibja_rates
    WHERE purity = (SELECT benchmark_purity FROM ref.ibja_params)
)
SELECT b.rate_date, b.session, b.ibja_ex_gst,
       r.trade_date AS intl_trade_date, r.intl_inr_10g,
       r.delhi_24k_inr_10g / (1 + p.gst_pct / 100)               AS computed_ex_gst,
       b.ibja_ex_gst / r.intl_inr_10g                            AS basis_ratio,   -- duty + domestic premium
       100 * (b.ibja_ex_gst / (r.delhi_24k_inr_10g / (1 + p.gst_pct / 100)) - 1) AS gap_vs_computed_pct
FROM bench b
ASOF LEFT JOIN gold.fact_delhi_rate r ON b.rate_date > r.trade_date
CROSS JOIN ref.pricing_params p;

-- Anchor: the ratio that maps international ₹/10g to the Delhi retail rate (incl. GST).
-- Latest IBJA print when we have one, else the landed-cost model.
CREATE OR REPLACE TABLE gold.anchor AS
WITH ibja AS (
    SELECT rate_date AS anchor_date, session,
           basis_ratio * (1 + p.gst_pct / 100) AS ratio, 'IBJA ' || session AS anchor_source
    FROM gold.fact_ibja_reconciliation CROSS JOIN ref.pricing_params p
    WHERE basis_ratio IS NOT NULL
    ORDER BY rate_date DESC, session DESC LIMIT 1
),
model AS (
    SELECT trade_date AS anchor_date, NULL AS session,
           delhi_24k_inr_10g / intl_inr_10g AS ratio, 'landed-cost model' AS anchor_source
    FROM gold.fact_delhi_rate ORDER BY trade_date DESC LIMIT 1
)
SELECT * FROM ibja
UNION ALL
SELECT * FROM model WHERE NOT EXISTS (SELECT 1 FROM ibja);

-- Features on the anchored series: international history (long, liquid) scaled to
-- today's Delhi level. Returns are those of international ₹ gold; the level is IBJA's.
CREATE OR REPLACE TABLE gold.features AS
SELECT trade_date, r.intl_inr_10g * a.ratio AS price,
       ln(price / lag(price) OVER w)                                   AS log_ret,
       avg(price) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)   AS sma_20,
       avg(price) OVER (w ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)   AS sma_50,
       avg(price) OVER (w ROWS BETWEEN 199 PRECEDING AND CURRENT ROW)  AS sma_200
FROM gold.fact_delhi_rate r CROSS JOIN gold.anchor a
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
