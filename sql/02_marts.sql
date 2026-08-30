-- ============================================================
-- 02_marts.sql  --  business logic and derived metrics
-- ============================================================
--
-- This is where the actual analysis lives. Each mart answers one
-- question and is safe to expose to a BI tool.

CREATE SCHEMA IF NOT EXISTS marts;

-- ------------------------------------------------------------------
-- fct_daily_labour
--
-- The core grain: one row per date / state / zone, carrying required
-- vs planned labour and the cost consequence of the gap.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE marts.fct_daily_labour AS
WITH joined AS (
    SELECT
        o.activity_date,
        o.state,
        o.zone,
        o.cartons_picked,
        o.standard_rate_cph,
        o.achieved_rate_cph,
        o.paid_labour_hours          AS required_labour_hours,
        r.planned_labour_hours,
        r.planned_headcount
    FROM staging.dc_operations o
    LEFT JOIN staging.dc_roster_plan r
           ON  o.activity_date = r.activity_date
           AND o.state         = r.state
           AND o.zone          = r.zone
),
costed AS (
    SELECT
        *,
        planned_labour_hours - required_labour_hours   AS labour_gap_hours,

        -- Productivity against the engineered standard. Below 1.0 means
        -- the floor ran slower than standard that day.
        achieved_rate_cph / NULLIF(standard_rate_cph, 0)
                                                       AS productivity_index,

        -- Overstaffed: hours paid for that weren't needed.
        GREATEST(planned_labour_hours - required_labour_hours, 0)
            * $cost_per_hour                           AS overstaff_cost,

        -- Understaffed: the shortfall is mostly recovered through
        -- overtime, not lost. Charge the overtime premium on those
        -- hours, plus a genuine service penalty on the small share
        -- that never gets recovered.
        GREATEST(required_labour_hours - planned_labour_hours, 0)
            * $cost_per_hour * $overtime_premium
        + GREATEST(required_labour_hours - planned_labour_hours, 0)
            * achieved_rate_cph
            * $service_share
            * $cost_per_unshipped                      AS service_failure_cost
    FROM joined
)
SELECT
    *,
    overstaff_cost + service_failure_cost              AS total_mismatch_cost,
    CASE
        WHEN labour_gap_hours >  0.05 * required_labour_hours THEN 'overstaffed'
        WHEN labour_gap_hours < -0.05 * required_labour_hours THEN 'understaffed'
        ELSE 'balanced'
    END                                                AS staffing_state
FROM costed;


-- ------------------------------------------------------------------
-- fct_weekly_summary
--
-- Weekly rollup with week-on-week movement. This is what the
-- commentary layer reads -- management cares about the week, not the
-- day.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE marts.fct_weekly_summary AS
WITH weekly AS (
    SELECT
        d.week_start,
        f.state,
        f.zone,
        SUM(f.cartons_picked)                    AS cartons_picked,
        SUM(f.required_labour_hours)             AS required_labour_hours,
        SUM(f.planned_labour_hours)              AS planned_labour_hours,
        SUM(f.total_mismatch_cost)               AS mismatch_cost,
        SUM(f.overstaff_cost)                    AS overstaff_cost,
        SUM(f.service_failure_cost)              AS service_failure_cost,
        AVG(f.productivity_index)                AS productivity_index,
        SUM(f.cartons_picked)
            / NULLIF(SUM(f.required_labour_hours), 0) AS cartons_per_hour
    FROM marts.fct_daily_labour f
    JOIN staging.dim_date d ON f.activity_date = d.date_key
    GROUP BY 1, 2, 3
)
SELECT
    *,
    -- Window functions over the state/zone partition give week-on-week
    -- movement without a self-join.
    LAG(cartons_picked)   OVER w              AS cartons_prev_week,
    LAG(mismatch_cost)    OVER w              AS mismatch_cost_prev_week,
    LAG(cartons_per_hour) OVER w              AS cartons_per_hour_prev_week,

    (cartons_picked - LAG(cartons_picked) OVER w)
        / NULLIF(LAG(cartons_picked) OVER w, 0)   AS cartons_wow_pct,
    (cartons_per_hour - LAG(cartons_per_hour) OVER w)
        / NULLIF(LAG(cartons_per_hour) OVER w, 0) AS productivity_wow_pct,

    -- 8-week trailing mean and sample stddev, used to decide whether
    -- this week's movement is signal or noise.
    AVG(cartons_picked) OVER (
        PARTITION BY state, zone ORDER BY week_start
        ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING
    )                                             AS cartons_trailing_mean,
    STDDEV_SAMP(cartons_picked) OVER (
        PARTITION BY state, zone ORDER BY week_start
        ROWS BETWEEN 8 PRECEDING AND 1 PRECEDING
    )                                             AS cartons_trailing_sd
FROM weekly
WINDOW w AS (PARTITION BY state, zone ORDER BY week_start);


-- ------------------------------------------------------------------
-- fct_dow_profile
--
-- Day-of-week shape. Directly actionable: it tells a planner which
-- days are structurally mis-rostered rather than randomly off.
-- ------------------------------------------------------------------

CREATE OR REPLACE TABLE marts.fct_dow_profile AS
SELECT
    d.day_of_week,
    d.day_name,
    f.state,
    f.zone,
    COUNT(*)                                     AS n_days,
    AVG(f.cartons_picked)                        AS avg_cartons,
    AVG(f.required_labour_hours)                 AS avg_required_hours,
    AVG(f.planned_labour_hours)                  AS avg_planned_hours,
    AVG(f.labour_gap_hours)                      AS avg_gap_hours,
    AVG(f.total_mismatch_cost)                   AS avg_mismatch_cost,
    SUM(CASE WHEN f.staffing_state = 'understaffed' THEN 1 ELSE 0 END)
        / CAST(COUNT(*) AS DOUBLE)               AS pct_days_understaffed
FROM marts.fct_daily_labour f
JOIN staging.dim_date d ON f.activity_date = d.date_key
GROUP BY 1, 2, 3, 4
ORDER BY f.state, f.zone, d.day_of_week;
