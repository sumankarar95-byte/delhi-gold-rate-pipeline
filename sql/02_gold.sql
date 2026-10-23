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

CREATE OR REPLACE TABLE gold.features AS
SELECT trade_date, delhi_24k_inr_10g AS price,
       ln(price / lag(price) OVER w)                                   AS log_ret,
       avg(price) OVER (w ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)   AS sma_20,
       avg(price) OVER (w ROWS BETWEEN 49 PRECEDING AND CURRENT ROW)   AS sma_50,
       avg(price) OVER (w ROWS BETWEEN 199 PRECEDING AND CURRENT ROW)  AS sma_200
FROM gold.fact_delhi_rate
WINDOW w AS (ORDER BY trade_date);

-- Purity-wise retail board: what the shop counter shows.
CREATE OR REPLACE VIEW gold.v_rate_board AS
SELECT r.trade_date, pu.karat, pu.fineness,
       round(r.delhi_24k_inr_10g * pu.fineness / 0.999, 0)                              AS market_inr_10g,
       round(r.delhi_24k_inr_10g * pu.fineness / 0.999 / 10, 2)                         AS market_inr_g,
       round(r.delhi_24k_inr_10g * pu.fineness / 0.999 * (1 - b.buy_margin_pct/100), 0) AS we_buy_inr_10g,
       round(r.delhi_24k_inr_10g * pu.fineness / 0.999 * (1 + b.sell_margin_pct/100), 0) AS we_sell_inr_10g
FROM gold.fact_delhi_rate r
CROSS JOIN ref.purities pu
CROSS JOIN ref.business_params b;
