-- ============================================================
-- 03_forecast_accuracy.sql  --  is the forecast worth having?
-- ============================================================
--
-- The forecast produces carton predictions. On its own that is just a
-- number. This file answers the question a manager actually asks:
--
--     "What does a better forecast get me, in dollars?"
--
-- It converts prediction error into labour hours, then into money.

CREATE SCHEMA IF NOT EXISTS marts;

-- ------------------------------------------------------------------
-- staging.forecast_backtest
--
-- The predictions, landed from Parquet. One row per date / state /
-- zone, with the true answer and both models' guesses side by side.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE staging.forecast_backtest AS
SELECT
    CAST(date AS DATE)                  AS activity_date,
    TRIM(state)                         AS state,
    TRIM(zone)                          AS zone,
    CAST(actual AS DOUBLE)              AS actual_cartons,
    CAST(pred_baseline AS DOUBLE)       AS pred_baseline,
    CAST(pred_holt_winters AS DOUBLE)   AS pred_holt_winters
FROM read_parquet($forecast_path);


-- ------------------------------------------------------------------
-- marts.fct_forecast_error
--
-- One row per prediction per model. UNPIVOTing the two models into
-- rows (rather than keeping them as columns) means adding a third
-- model later needs no schema change -- and every metric below is
-- written once instead of twice.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE marts.fct_forecast_error AS
WITH unpivoted AS (
    SELECT activity_date, state, zone, actual_cartons,
           'baseline'      AS model, pred_baseline     AS predicted_cartons
    FROM staging.forecast_backtest
    UNION ALL
    SELECT activity_date, state, zone, actual_cartons,
           'holt_winters'  AS model, pred_holt_winters AS predicted_cartons
    FROM staging.forecast_backtest
),
errors AS (
    SELECT
        *,
        predicted_cartons - actual_cartons            AS error_cartons,
        ABS(predicted_cartons - actual_cartons)       AS abs_error_cartons,
        ABS(predicted_cartons - actual_cartons)
            / NULLIF(actual_cartons, 0)               AS abs_pct_error,

        CASE WHEN predicted_cartons < actual_cartons
             THEN 'under_forecast'                    -- the costly one
             ELSE 'over_forecast' END                 AS error_direction
    FROM unpivoted
)
SELECT
    e.*,

    -- Turn the carton miss into a labour-hour miss, using that zone's
    -- standard pick rate. This is the bridge from "forecast error" to
    -- "staffing error".
    e.error_cartons / z.standard_rate_cph             AS error_labour_hours,

    -- Cost the miss in the direction it went. Over-forecasting wastes
    -- wages outright. Under-forecasting is mostly recovered through
    -- overtime, with only a small share genuinely lost -- that
    -- asymmetry is why direction matters, but it is far less extreme
    -- than treating every missed carton as lost revenue.
    CASE
        WHEN e.error_cartons > 0
        THEN (e.error_cartons / z.standard_rate_cph) * $cost_per_hour
        ELSE (ABS(e.error_cartons) / z.standard_rate_cph)
                 * $cost_per_hour * $overtime_premium
           + ABS(e.error_cartons) * $service_share * $cost_per_unshipped
    END                                               AS error_cost
FROM errors e
JOIN (
    SELECT DISTINCT zone, standard_rate_cph
    FROM staging.dc_operations
) z ON e.zone = z.zone;


-- ------------------------------------------------------------------
-- marts.fct_forecast_scorecard
--
-- The headline table. One row per state / zone / model, carrying the
-- three metrics that matter and the money attached to them.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE marts.fct_forecast_scorecard AS
SELECT
    state,
    zone,
    model,
    COUNT(*)                                          AS days_tested,

    -- MAE: average miss, in cartons.
    AVG(abs_error_cartons)                            AS mae_cartons,

    -- MAPE: average miss as a percentage.
    AVG(abs_pct_error) * 100                          AS mape_pct,

    -- Bias: signed. Negative means we habitually under-forecast,
    -- which means we habitually under-staff.
    AVG(error_cartons)                                AS bias_cartons,

    -- How often we land on the expensive side of the error.
    SUM(CASE WHEN error_direction = 'under_forecast' THEN 1 ELSE 0 END)
        / CAST(COUNT(*) AS DOUBLE) * 100              AS pct_days_under,

    SUM(error_cost)                                   AS total_error_cost,
    SUM(error_cost) / COUNT(*)                        AS avg_daily_error_cost
FROM marts.fct_forecast_error
GROUP BY 1, 2, 3;


-- ------------------------------------------------------------------
-- marts.fct_forecast_value
--
-- The single number to put in front of a manager: what switching from
-- the naive plan to the model is worth, per state and zone.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE marts.fct_forecast_value AS
SELECT
    b.state,
    b.zone,
    b.days_tested,
    b.mape_pct                                        AS baseline_mape,
    h.mape_pct                                        AS model_mape,
    b.mape_pct - h.mape_pct                           AS mape_improvement,

    b.total_error_cost                                AS baseline_cost,
    h.total_error_cost                                AS model_cost,
    b.total_error_cost - h.total_error_cost           AS cost_saved,

    -- Scaled from the test window out to a year, so the number is in
    -- units a manager thinks in. This is an extrapolation, and the
    -- README says so.
    (b.total_error_cost - h.total_error_cost)
        * (365.0 / b.days_tested)                     AS annualised_saving
FROM marts.fct_forecast_scorecard b
JOIN marts.fct_forecast_scorecard h
  ON  b.state = h.state
  AND b.zone  = h.zone
WHERE b.model = 'baseline'
  AND h.model = 'holt_winters';
