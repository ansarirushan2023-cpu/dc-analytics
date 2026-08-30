"""Generate the HTML dashboard from the warehouse.

Produces a single self-contained page at output/dashboard.html that
opens in any browser with nothing installed. That matters for a
portfolio: a reviewer can look at the work without downloading a tool
they may not have.

Plotly is loaded from a CDN rather than embedded, which keeps the file
around 60 KB instead of ~4 MB -- small enough to sit comfortably in a
git repository.

Design: zones are colour-coded by temperature (amber ambient, cyan
chiller, ice blue freezer) so the palette encodes the data rather than
decorating it.
"""

from __future__ import annotations

import logging

import duckdb
import plotly.graph_objects as go
from plotly.io import to_html

from . import config as cfg

log = logging.getLogger(__name__)

DASHBOARD_FILE = cfg.OUTPUT / "dashboard.html"

# Thermal palette: the colour IS the temperature zone.
INK = "#131A20"
SURFACE = "#1C252D"
LINE = "#2C3945"
TEXT = "#E4EAEF"
MUTED = "#8A9AA8"
ZONE_COLOUR = {
    "ambient": "#E8A33D",   # warm amber
    "chiller": "#5FC9D8",   # pale cyan
    "freezer": "#6E8FE8",   # ice blue
}
ALERT = "#FF6B35"           # hi-vis signal orange
PLAN = "#8A9AA8"

CHART_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="Barlow, sans-serif", size=13, color=TEXT),
    margin=dict(l=56, r=24, t=16, b=48),
    xaxis=dict(gridcolor=LINE, zerolinecolor=LINE, linecolor=LINE),
    yaxis=dict(gridcolor=LINE, zerolinecolor=LINE, linecolor=LINE),
    legend=dict(orientation="h", y=-0.18, font=dict(size=12)),
    hoverlabel=dict(bgcolor=SURFACE, bordercolor=LINE,
                    font=dict(family="Barlow, sans-serif", color=TEXT)),
)

PLOT_CONFIG = {"displayModeBar": False, "responsive": True}


def _fig_html(fig: go.Figure, div_id: str, first: bool) -> str:
    fig.update_layout(**CHART_LAYOUT)
    return to_html(fig, include_plotlyjs="cdn" if first else False,
                   full_html=False, div_id=div_id, config=PLOT_CONFIG)


# ------------------------------------------------------------ queries

def _headline(con) -> dict:
    row = con.execute("""
        SELECT
            (SELECT AVG(baseline_mape) FROM marts.fct_forecast_value),
            (SELECT AVG(model_mape)    FROM marts.fct_forecast_value),
            (SELECT COUNT(*)           FROM marts.fct_daily_labour),
            (SELECT MIN(activity_date) FROM marts.fct_daily_labour),
            (SELECT MAX(activity_date) FROM marts.fct_daily_labour),
            (SELECT AVG(error_cartons) / AVG(actual_cartons) * 100
             FROM marts.fct_forecast_error WHERE model = 'holt_winters')
    """).fetchone()
    base, model, rows, d0, d1, bias = row
    return {
        "baseline_mape": base,
        "model_mape": model,
        "improvement": (base - model) / base * 100,
        "rows": rows,
        "date_from": d0,
        "date_to": d1,
        "bias_pct": bias,
    }


def _dow_board(con) -> list[dict]:
    """The signature finding: which days are structurally mis-planned."""
    rows = con.execute("""
        SELECT day_name,
               AVG(pct_days_understaffed) * 100 AS pct_under,
               AVG(avg_cartons)                 AS cartons,
               AVG(avg_gap_hours)               AS gap_hours
        FROM marts.fct_dow_profile
        GROUP BY day_name, day_of_week
        ORDER BY day_of_week
    """).fetchall()
    # Shift Sunday (dow 0) to the end so the week reads Mon-Sun.
    rows = rows[1:] + rows[:1]
    return [{"day": r[0][:3], "pct_under": r[1],
             "cartons": r[2], "gap": r[3]} for r in rows]


def _accuracy_fig(con) -> go.Figure:
    rows = con.execute("""
        SELECT zone, AVG(baseline_mape), AVG(model_mape)
        FROM marts.fct_forecast_value GROUP BY zone ORDER BY 3
    """).fetchall()
    zones = [r[0] for r in rows]

    fig = go.Figure()
    fig.add_bar(name="Seasonal naive baseline", x=zones,
                y=[r[1] for r in rows], marker_color=PLAN,
                hovertemplate="%{x}<br>baseline %{y:.2f}%<extra></extra>")
    fig.add_bar(name="Holt-Winters", x=zones, y=[r[2] for r in rows],
                marker_color=[ZONE_COLOUR[z] for z in zones],
                hovertemplate="%{x}<br>model %{y:.2f}%<extra></extra>")
    fig.update_layout(barmode="group", height=340,
                      yaxis_title="Mean absolute % error")
    return fig


def _actual_vs_pred_fig(con) -> go.Figure:
    """Show the hardest series -- freezer, the one with most to prove."""
    rows = con.execute("""
        SELECT activity_date, actual_cartons, pred_baseline, pred_holt_winters
        FROM staging.forecast_backtest
        WHERE zone = 'freezer' AND state = 'NSW'
        ORDER BY activity_date
    """).fetchall()

    dates = [r[0] for r in rows]
    fig = go.Figure()
    fig.add_scatter(x=dates, y=[r[1] for r in rows], name="Actual",
                    line=dict(color=TEXT, width=2.5))
    fig.add_scatter(x=dates, y=[r[2] for r in rows], name="Baseline",
                    line=dict(color=PLAN, width=1.4, dash="dot"))
    fig.add_scatter(x=dates, y=[r[3] for r in rows], name="Holt-Winters",
                    line=dict(color=ZONE_COLOUR["freezer"], width=2))
    fig.update_layout(height=360, yaxis_title="Cartons per day")
    return fig


def _cost_fig(con) -> go.Figure:
    rows = con.execute("""
        SELECT week_start, zone, SUM(mismatch_cost)
        FROM marts.fct_weekly_summary
        GROUP BY 1, 2 ORDER BY 1
    """).fetchall()

    fig = go.Figure()
    for zone in ("ambient", "chiller", "freezer"):
        pts = [(r[0], r[2]) for r in rows if r[1] == zone]
        fig.add_scatter(x=[p[0] for p in pts], y=[p[1] for p in pts],
                        name=zone.title(), stackgroup="cost",
                        line=dict(width=0.5, color=ZONE_COLOUR[zone]),
                        fillcolor=ZONE_COLOUR[zone].replace(")", ""),
                        opacity=0.85)
    fig.update_layout(height=320, yaxis_title="Weekly mismatch cost (A$)")
    return fig


def _value_rows(con) -> list[tuple]:
    return con.execute("""
        SELECT zone,
               AVG(baseline_mape), AVG(model_mape),
               SUM(annualised_saving)
        FROM marts.fct_forecast_value
        GROUP BY zone ORDER BY 4 DESC
    """).fetchall()


# --------------------------------------------------------------- page

STYLE = """
:root {
  --ink:#131A20; --surface:#1C252D; --line:#2C3945;
  --text:#E4EAEF; --muted:#8A9AA8;
  --ambient:#E8A33D; --chiller:#5FC9D8; --freezer:#6E8FE8;
  --alert:#FF6B35;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ink); color:var(--text);
  font-family:'Barlow',system-ui,sans-serif; font-size:16px;
  line-height:1.6; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1080px; margin:0 auto; padding:0 24px 96px}

/* ---- masthead ---- */
header{border-bottom:1px solid var(--line); padding:56px 0 32px; margin-bottom:56px}
.eyebrow{
  font-family:'JetBrains Mono',monospace; font-size:11px;
  letter-spacing:.18em; text-transform:uppercase; color:var(--muted);
  margin:0 0 20px;
}
h1{
  font-family:'Barlow Condensed',sans-serif; font-weight:600;
  font-size:clamp(34px,5.4vw,58px); line-height:1.02;
  letter-spacing:-.01em; margin:0 0 20px; text-transform:uppercase;
}
h1 em{font-style:normal; color:var(--alert)}
.standfirst{font-size:18px; color:var(--muted); max-width:62ch; margin:0}
.meta{
  font-family:'JetBrains Mono',monospace; font-size:11.5px;
  color:var(--muted); margin-top:28px; display:flex; flex-wrap:wrap; gap:28px;
}

/* ---- sections ---- */
section{margin-bottom:72px}
h2{
  font-family:'Barlow Condensed',sans-serif; font-weight:600;
  font-size:26px; letter-spacing:.01em; text-transform:uppercase;
  margin:0 0 6px; padding-bottom:10px; border-bottom:1px solid var(--line);
}
.lede{color:var(--muted); max-width:64ch; margin:16px 0 28px}

/* ---- shift board (signature) ---- */
.board{display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin:8px 0 20px}
.day{
  background:var(--surface); border:1px solid var(--line);
  border-top:3px solid var(--line); padding:16px 12px 14px; text-align:center;
}
.day.hot{border-top-color:var(--alert)}
.day.cool{border-top-color:var(--chiller)}
.day .name{
  font-family:'JetBrains Mono',monospace; font-size:11px;
  letter-spacing:.14em; color:var(--muted); text-transform:uppercase;
}
.day .pct{
  font-family:'Barlow Condensed',sans-serif; font-size:34px;
  font-weight:600; line-height:1.1; margin:8px 0 2px;
}
.day.hot .pct{color:var(--alert)}
.day.cool .pct{color:var(--chiller)}
.day .cap{font-size:11px; color:var(--muted); line-height:1.35}

/* ---- table ---- */
table{width:100%; border-collapse:collapse; font-size:15px; margin-top:8px}
th{
  font-family:'JetBrains Mono',monospace; font-size:10.5px;
  letter-spacing:.13em; text-transform:uppercase; color:var(--muted);
  text-align:right; padding:0 0 10px; border-bottom:1px solid var(--line);
  font-weight:400;
}
th:first-child,td:first-child{text-align:left}
td{
  padding:13px 0; border-bottom:1px solid var(--line); text-align:right;
  font-variant-numeric:tabular-nums;
}
td:first-child{font-weight:500; text-transform:capitalize}
.gain{color:var(--chiller)}

/* ---- callout ---- */
.note{
  border-left:2px solid var(--alert); background:var(--surface);
  padding:18px 22px; margin:28px 0 0; font-size:15px; color:var(--muted);
}
.note strong{color:var(--text); font-weight:600}

footer{
  border-top:1px solid var(--line); padding-top:28px; color:var(--muted);
  font-size:13.5px;
}
footer code{
  font-family:'JetBrains Mono',monospace; font-size:12px;
  background:var(--surface); padding:2px 6px;
}
@media(max-width:720px){
  .board{grid-template-columns:repeat(4,1fr)}
  .meta{gap:16px}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def build_page(con) -> str:
    h = _headline(con)
    board = _dow_board(con)
    values = _value_rows(con)

    worst = max(board, key=lambda d: d["pct_under"])
    best = min(board, key=lambda d: d["pct_under"])

    cells = []
    for d in board:
        cls = "hot" if d["pct_under"] > 50 else (
            "cool" if d["pct_under"] < 10 else "")
        cells.append(
            f'<div class="day {cls}"><div class="name">{d["day"]}</div>'
            f'<div class="pct">{d["pct_under"]:.0f}<span '
            f'style="font-size:16px">%</span></div>'
            f'<div class="cap">days short</div></div>'
        )

    rows_html = "".join(
        f"<tr><td>{z}</td><td>{b:.2f}%</td><td>{m:.2f}%</td>"
        f'<td class="gain">{b - m:.2f} pts</td>'
        f"<td>A${s:,.0f}</td></tr>"
        for z, b, m, s in values
    )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DC throughput &amp; labour planning</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600&family=Barlow+Condensed:wght@500;600&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>{STYLE}</style>
</head><body>
<div class="wrap">

<header>
  <p class="eyebrow">Distribution centre analytics — demand, labour, cost</p>
  <h1>The roster runs short<br>on the <em>same days every week</em></h1>
  <p class="standfirst">Planning labour from a 28-day trailing average
  smooths across a weekly cycle it cannot see. The result is not random
  error: Friday is short on 86% of days, Sunday on none. This pipeline
  quantifies the gap, forecasts the demand that drives it, and prices
  what closing it is worth.</p>
  <div class="meta">
    <span>{h['rows']:,} fact rows</span>
    <span>{h['date_from']} → {h['date_to']}</span>
    <span>Python · SQL · DuckDB</span>
  </div>
</header>

<section>
  <h2>Where the plan breaks</h2>
  <p class="lede">Share of days each weekday was rostered below the
  labour hours the volume actually required, averaged across all states
  and temperature zones.</p>
  <div class="board">{''.join(cells)}</div>
  <div class="note"><strong>{worst['day']} runs short on
  {worst['pct_under']:.0f}% of days; {best['day']} on
  {best['pct_under']:.0f}%.</strong> A trailing average cannot
  distinguish a weekday from a weekend, so it under-books the peak and
  over-books the trough — the same way, every week. The fix is
  day-of-week-aware planning, not more labour.</div>
</section>

<section>
  <h2>Forecast accuracy by zone</h2>
  <p class="lede">Both models were graded on 90 days held back from
  training. Lower is better. The baseline simply repeats the last
  complete week — if the model cannot beat that, it is not earning its
  complexity.</p>
  {{ACCURACY}}
  <div class="note">Holt-Winters improves mean absolute percentage
  error by <strong>{h['improvement']:.1f}%</strong> overall
  ({h['baseline_mape']:.2f}% → {h['model_mape']:.2f}%). Freezer is
  hardest: it swings most with the seasons and carries the smallest
  volume, so the same absolute miss reads as a larger percentage.</div>
</section>

<section>
  <h2>Held-back period, freezer</h2>
  <p class="lede">The hardest series to predict, over the 90 days
  neither model saw during training.</p>
  {{SERIES}}
  <div class="note"><strong>Both models sit above the actual line more
  often than below</strong> — Holt-Winters over-forecasts by
  {h['bias_pct']:.1f}% on average. That is the safer direction for a
  roster, since being slightly over-staffed costs wages while being
  short costs overtime and service. But it is a real limitation, not a
  design choice: a bias-corrected version would subtract the mean
  error, and the honest next step is to test whether that improves
  accuracy or simply moves the error to the expensive side.</div>
</section>

<section>
  <h2>What the mismatch costs</h2>
  <p class="lede">Weekly cost of the gap between rostered and required
  labour. Over-staffing is charged at the wage rate; under-staffing at
  an overtime premium, plus a small share treated as service failure
  that never recovers.</p>
  {{COST}}
</section>

<section>
  <h2>Value of the better forecast</h2>
  <table>
    <thead><tr><th>Zone</th><th>Baseline MAPE</th><th>Model MAPE</th>
    <th>Improvement</th><th>Annualised</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <div class="note">Annualised figures scale the 90-day test window to
  a full year and are <strong>indicative, not a business case</strong>.
  They sit at roughly 2–4% of the modelled wage bill; a pipeline check
  fails the build if they ever exceed 25%, which is what caught an
  early cost assumption that was wrong by an order of magnitude.</div>
</section>

<footer>
  <p><strong>Data provenance.</strong> The retail demand signal is real
  — ABS Retail Trade, catalogue 8501.0. The distribution centre
  operations layer is <strong>modelled</strong>, generated from
  transparent parameters in <code>src/config.py</code>, because DC
  operational data is commercially sensitive and not published. The
  methodology is the deliverable; the dollar values are illustrative.</p>
  <p>Generated by <code>python -m src.dashboard</code>. Full pipeline,
  quality checks and evaluation set in the repository.</p>
</footer>

</div></body></html>"""


def run(db_path=None) -> str:
    db_path = db_path or cfg.DB_PATH
    con = duckdb.connect(str(db_path), read_only=True)

    try:
        page = build_page(con)
        page = page.replace("{ACCURACY}",
                            _fig_html(_accuracy_fig(con), "acc", True))
        page = page.replace("{SERIES}",
                            _fig_html(_actual_vs_pred_fig(con), "ser", False))
        page = page.replace("{COST}",
                            _fig_html(_cost_fig(con), "cost", False))
    finally:
        con.close()

    DASHBOARD_FILE.write_text(page)
    size_kb = DASHBOARD_FILE.stat().st_size / 1024
    log.info("wrote %s (%.0f KB)", DASHBOARD_FILE, size_kb)
    return page


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run()
    print(f"\nOpen it with:  open {DASHBOARD_FILE}")
