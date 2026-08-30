"""Central configuration for the DC throughput analytics pipeline.

Every tunable lives here so the rest of the codebase has no magic numbers.
"""

from pathlib import Path

# ---------------------------------------------------------------- paths

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "db" / "dc.duckdb"
SQL_DIR = ROOT / "sql"
OUTPUT = ROOT / "output"

for _d in (RAW, DB_PATH.parent, OUTPUT):
    _d.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------ ABS source
#
# Retail Trade, Australia (cat. 8501.0) via the ABS Data API (SDMX-JSON).
#
# IMPORTANT: verify the dataflow id against ABS Data Explorer before you
# rely on this. ABS renames and retires dataflows between releases, and a
# stale id fails with a 404 rather than bad data -- which is the failure
# mode you want, but you still have to fix it.
#
#   https://dataexplorer.abs.gov.au/
#
ABS_API_BASE = "https://data.api.abs.gov.au/rest/data"
ABS_DATAFLOW = "ABS,RT,1.0.0"          # Retail Trade
ABS_START_PERIOD = "2018-01"

# Set true to skip the network entirely and synthesise the retail series.
# The pipeline stays runnable offline; the README records which mode
# produced any given run.
OFFLINE_MODE = False

# States and retail industries carried through the pipeline.
STATES = ["SA", "VIC", "NSW", "QLD", "WA"]
INDUSTRIES = [
    "Food retailing",
    "Household goods retailing",
    "Clothing and softgoods",
    "Department stores",
    "Other retailing",
]


# ------------------------------------------------- modelled DC operations
#
# This layer is MODELLED, not observed. Parameters are set to plausible
# grocery-DC values so the analysis behaves realistically. Anyone reading
# the repo needs to know this -- it is stated in the README and repeated
# in the generator's docstring.

DC_PARAMS = {
    # Cartons picked per labour hour, by temperature zone.
    "lines_per_hour": {"ambient": 165.0, "chiller": 140.0, "freezer": 110.0},
    # Base share of total volume flowing through each zone.
    "zone_mix": {"ambient": 0.62, "chiller": 0.26, "freezer": 0.12},

    # Each zone has its own seasonal life, so the shares above are only
    # a starting point -- they flex month to month.
    #
    #   peak_month -- the month this zone is busiest
    #   amplitude  -- how strong the swing is (0.20 = +/-20%)
    #   noise_sd   -- day-to-day randomness unique to this zone
    #
    # Freezer peaks in the Australian summer (January): ice cream and
    # frozen drinks. Ambient peaks in December: Christmas dry goods.
    # Chiller is the steadiest -- people buy milk all year.
    "zone_seasonality": {
        "ambient": {"peak_month": 12, "amplitude": 0.14, "noise_sd": 0.05},
        "chiller": {"peak_month": 1,  "amplitude": 0.07, "noise_sd": 0.06},
        "freezer": {"peak_month": 1,  "amplitude": 0.30, "noise_sd": 0.09},
    },
    # Paid hours per picker shift.
    "shift_hours": 8.0,
    # Fraction of paid time not spent picking (breaks, travel, induction).
    "non_pick_allowance": 0.14,
    # Cost per labour hour.
    "cost_per_labour_hour": 42.00,

    # What being short actually costs.
    #
    # The naive assumption -- every carton you can't pick is revenue
    # lost forever -- massively overstates it. Real DCs recover most
    # of a shortfall through overtime or next-day catch-up.
    #
    # So: shortfall hours get covered at an overtime premium, and only
    # a small share is treated as genuinely lost service.
    "overtime_premium": 0.50,          # +50% on top of base rate
    "service_failure_share": 0.08,     # 8% of shortfall never recovers
    "cost_per_unshipped_carton": 6.50,
    # Day-of-week volume multipliers (Mon..Sun). Grocery DCs run hard
    # early-week to refill stores after the weekend.
    "dow_multipliers": [1.18, 1.10, 1.02, 1.06, 1.22, 0.86, 0.56],
    # Random noise applied to daily volume (std dev as a fraction).
    "daily_noise_sd": 0.075,
    # Cartons per $1k of state retail turnover -- the link between the
    # real ABS demand signal and modelled DC volume.
    #
    # Calibrated so one state produces single-DC volumes (roughly
    # 15-20k cartons/day in a mid-size state), not the whole state
    # retail network. Getting this wrong inflates every dollar figure
    # downstream, so it is worth sanity-checking the output against a
    # real DC's published throughput.
    "cartons_per_1k_turnover": 0.40,
}

RANDOM_SEED = 20260828
