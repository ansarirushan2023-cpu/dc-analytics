-- ============================================================
-- 01_staging.sql  --  raw Parquet -> typed, conformed staging
-- ============================================================
--
-- Staging does type casting, renaming and light conformance only.
-- No business logic, no aggregation, no filtering of legitimate rows.
-- Keeping this layer dumb means a business-rule change never requires
-- re-reading raw files.

CREATE SCHEMA IF NOT EXISTS staging;

-- ------------------------------------------------ retail demand signal
-- Source: ABS Retail Trade (real, or synthesised in offline mode).

CREATE OR REPLACE TABLE staging.retail_turnover AS
SELECT
    CAST(period || '-01' AS DATE)          AS month_start,
    period                                  AS period_label,
    TRIM(state)                             AS state,
    TRIM(industry)                          AS industry,
    CAST(turnover_thousands AS DOUBLE)      AS turnover_thousands
FROM read_parquet($retail_path)
WHERE turnover_thousands IS NOT NULL
  AND turnover_thousands > 0;


-- ------------------------------------------------ DC operations (MODELLED)
-- See src/generate_ops.py. Not observed data.

CREATE OR REPLACE TABLE staging.dc_operations AS
SELECT
    CAST(date AS DATE)                      AS activity_date,
    TRIM(state)                             AS state,
    TRIM(zone)                              AS zone,
    CAST(cartons_picked AS BIGINT)          AS cartons_picked,
    CAST(standard_rate_cph AS DOUBLE)       AS standard_rate_cph,
    CAST(achieved_rate_cph AS DOUBLE)       AS achieved_rate_cph,
    CAST(paid_labour_hours AS DOUBLE)       AS paid_labour_hours
FROM read_parquet($ops_path)
WHERE cartons_picked >= 0;


-- ------------------------------------------------ roster plan (MODELLED)

CREATE OR REPLACE TABLE staging.dc_roster_plan AS
SELECT
    CAST(date AS DATE)                      AS activity_date,
    TRIM(state)                             AS state,
    TRIM(zone)                              AS zone,
    CAST(planned_labour_hours AS DOUBLE)    AS planned_labour_hours,
    CAST(planned_headcount AS INTEGER)      AS planned_headcount
FROM read_parquet($roster_path);


-- ------------------------------------------------ date spine
-- A generated calendar avoids gaps when a state/zone has no activity,
-- and gives every downstream query the same date attributes.

CREATE OR REPLACE TABLE staging.dim_date AS
WITH bounds AS (
    SELECT MIN(activity_date) AS d0, MAX(activity_date) AS d1
    FROM staging.dc_operations
)
SELECT
    d                                       AS date_key,
    EXTRACT(year  FROM d)                   AS year,
    EXTRACT(month FROM d)                   AS month,
    EXTRACT(day   FROM d)                   AS day,
    DATE_TRUNC('month', d)                  AS month_start,
    DATE_TRUNC('week',  d)                  AS week_start,
    EXTRACT(dow FROM d)                     AS day_of_week,
    DAYNAME(d)                              AS day_name,
    CASE WHEN EXTRACT(dow FROM d) IN (0, 6)
         THEN TRUE ELSE FALSE END           AS is_weekend
FROM bounds, UNNEST(GENERATE_SERIES(bounds.d0, bounds.d1, INTERVAL 1 DAY))
     AS t(d);
