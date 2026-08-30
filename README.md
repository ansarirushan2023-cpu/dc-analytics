# DC Throughput & Labour Planning Analytics

An end-to-end analytics pipeline that forecasts distribution centre
workload from published retail demand data, compares it against a
planned roster, and prices the cost of getting the match wrong.

**Stack:** Python · SQL / DuckDB · Plotly · GitHub Actions

```bash
pip install -r requirements.txt
python -m src.run_pipeline --offline
open output/dashboard.html
```

One command runs ingestion, modelling, forecasting, the warehouse
build, 18 data quality checks, the weekly commentary and the
dashboard. Around 10 seconds end to end.

---

## Data provenance — read this first

This project mixes real and modelled data, and it matters which is
which.

| Layer | Source | Real? |
|---|---|---|
| Retail turnover demand signal | ABS Retail Trade (cat. 8501.0) | **Real** |
| DC carton volume, pick rates, labour hours | `src/generate_ops.py` | **Modelled** |
| Roster plan | `src/generate_ops.py` | **Modelled** |

Distribution centre operational data is commercially sensitive and not
published by anyone. Rather than pretend otherwise, the operational
layer is generated from transparent parameters in `src/config.py` —
pick rates by temperature zone, day-of-week volume shape, cost per
labour hour — chosen to be plausible for a grocery DC.

The demand *signal* driving it is real, so seasonality, trend and
relative state scale are inherited from published ABS data rather than
invented.

**The methodology is the deliverable. The dollar figures are
illustrative.**

---

## What it found

### The roster fails on the same days every week

The modelled planner uses a 28-day trailing average — a common
real-world heuristic. It fails predictably:

| Day | Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|---|
| % of days understaffed | 79% | 62% | 40% | 51% | **86%** | 7% | 0% |

A trailing average cannot distinguish a weekday from a weekend, so it
under-books the peak and over-books the trough — the same way, every
week. This is structural, not random. The fix is day-of-week-aware
planning, not more labour.

### Holt-Winters beats the naive baseline

Both models were graded on 90 days held back from training.

| Zone | Baseline MAPE | Holt-Winters MAPE | Improvement |
|---|---|---|---|
| Ambient | 9.74% | 7.53% | 2.21 pts |
| Chiller | 12.48% | 10.81% | 1.67 pts |
| Freezer | 22.44% | 18.51% | 3.93 pts |
| **Overall** | **14.89%** | **12.29%** | **17.5%** |

Freezer is hardest: it swings most with the seasons and carries the
smallest volume, so the same absolute miss reads as a larger
percentage.

*(Figures from one run. The pipeline generates data up to the current
date, so exact numbers shift slightly between runs.)*

---

## Architecture

```
ABS Data API ──► data/raw/*.parquet ──► DuckDB ──► dashboard.html
                        │                  │       commentary.md
                 generate_ops.py      sql/01_staging.sql   (typing)
                 (modelled layer)     sql/02_marts.sql     (business logic)
                                      sql/03_forecast_accuracy.sql
```

Raw Parquet is written once and never mutated, so a business-rule
change never requires re-fetching source data. Staging does typing and
conformance only; every metric definition lives in the marts layer, in
one place.

### Why these tools

**Parquet over CSV** — columnar and typed. The schema travels with the
file, so types are not re-inferred on every read, and column-selective
reads are roughly 7x faster than CSV on this dataset.

**DuckDB over pandas or Postgres** — the transformation layer is
written in SQL rather than pandas, but without requiring anyone to
stand up a database server to run the repo. At 43,000 rows performance
was not the driver; reproducibility was. The SQL is standard and would
port to Postgres or Snowflake with minimal change.

**Holt-Winters over Prophet or ARIMA** — the failure being modelled is
seasonal, and Holt-Winters models seasonality explicitly with three
parameters that each mean something plain in English. It is graded
against a deliberately dumb baseline so the added complexity has to
earn itself.

---

## What's in the repository

| Path | What it does |
|---|---|
| `src/ingest_abs.py` | ABS Retail Trade ingestion, with offline fallback |
| `src/generate_ops.py` | Modelled DC operations and roster plan |
| `src/forecast.py` | Seasonal naive baseline + Holt-Winters, backtested |
| `src/build_db.py` | Executes the SQL layers into DuckDB |
| `src/checks.py` | 18 data quality gates |
| `src/commentary.py` | Weekly variance detection and narration |
| `src/dashboard.py` | Generates the HTML report |
| `src/run_pipeline.py` | Runs everything, in order, with gates |
| `tests/eval_commentary.py` | Evaluation set for the commentary layer |

### Data quality gates

The pipeline refuses to publish commentary from data that failed its
checks. Alongside the usual row counts, nulls and duplicate-grain
tests, one check is worth naming:

`savings_believable` compares forecast savings to the modelled wage
bill and fails the build if they exceed 25%. It exists because an
early version of the cost model produced savings larger than a third
of total wages — a number that looked fine in the pipeline and was
obviously wrong the moment it was compared against something known.
The manual sanity check became an automatic one.

### On the AI component

The commentary layer uses a language model to write the weekly "what
changed and why" summary. **It narrates; it never calculates.**

Every figure is computed in SQL first. The model receives finished
numbers and turns them into prose. It never sees raw data, never does
arithmetic, and never decides what counts as significant.

`tests/eval_commentary.py` grades the output against known scenarios
and — the part that matters — extracts every number from the generated
text and confirms each traces back to an input. A fabricated statistic
fails the build.

A deterministic template writer produces the same facts without a
model. It is the correctness baseline, not a fallback: the pipeline
runs fully without an API key, and a missing key costs readability,
never accuracy.

---

## Known limitations

Stated here rather than left to be discovered.

- **The operations layer is modelled.** Real DC data would behave
  differently, particularly around promotions, public holidays and
  stock-outs, none of which are represented.
- **The forecast over-predicts by about 3%.** That is the safer
  direction for a roster, but it is a real bias, not a design choice.
  A bias-corrected variant is the obvious next test.
- **One holdout window, not walk-forward validation.** A rolling
  backtest re-fitting each week would be more rigorous and is what
  production would use. This was a time-budget decision.
- **Zone independence is synthetic.** Zones are given their own
  seasonal profiles, but their correlation structure is invented
  rather than observed.

---

## Roadmap

- [x] Ingestion, modelled operations, staging and marts
- [x] Forecast backtesting against a naive baseline, costed
- [x] Quality gates, AI commentary, evaluation set, scheduled runs
- [x] HTML dashboard
- [ ] Power BI version (`.pbix`) — the production target in most
      workplaces; not built here because Power BI Desktop is
      Windows-only
- [ ] Walk-forward validation
- [ ] Bias-corrected forecast variant
