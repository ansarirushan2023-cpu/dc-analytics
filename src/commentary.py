"""Automated weekly variance commentary.

    ###################################################################
    #  THE MODEL NARRATES. IT NEVER CALCULATES.                       #
    #                                                                 #
    #  Every number in the output is computed by SQL in detect().     #
    #  The language model receives those finished figures and turns   #
    #  them into prose. It never sees raw data, never does            #
    #  arithmetic, and never decides what is significant.             #
    #                                                                 #
    #  This is deliberate. LLMs are unreliable at arithmetic and      #
    #  good at explanation, so the boundary is drawn where each side  #
    #  is strong.                                                     #
    ###################################################################

Two writers:

  render_template  -- deterministic, always available, no API key
  render_llm       -- calls a language model, used when a key is set

The template writer is not a fallback afterthought: it is the
correctness baseline. The LLM output is graded against the same facts
the template renders, so a hallucinated figure is detectable.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict

import duckdb

from . import config as cfg

log = logging.getLogger(__name__)

COMMENTARY_FILE = cfg.OUTPUT / "weekly_commentary.md"

# How many standard deviations from the trailing mean before a movement
# is called out. 2.0 is the usual convention -- roughly a 1-in-20 event
# if the series were normal.
SIGMA_THRESHOLD = 2.0


@dataclass
class Finding:
    """One computed fact about the latest week. All arithmetic done."""
    state: str
    zone: str
    week_start: str
    cartons: float
    cartons_wow_pct: float
    sigma_from_mean: float
    productivity_index: float
    productivity_wow_pct: float
    mismatch_cost: float
    direction: str          # "above" or "below" trailing average


# ------------------------------------------------------- detection

def detect(db_path=None, sigma: float = SIGMA_THRESHOLD,
           max_findings: int = 5) -> list[Finding]:
    """Find notable movements in the most recent COMPLETE week.

    "Complete" matters more than it sounds. The newest week in the data
    is usually partial -- if the data ends on a Monday, that week holds
    one day. Comparing a one-day week against seven-day weeks makes
    every series look catastrophic, which is both wrong and the most
    common bug in weekly reporting.

    So: only weeks with a full seven days of activity are eligible.

    All arithmetic here is SQL. The output is a list of finished facts.
    """
    db_path = db_path or cfg.DB_PATH
    con = duckdb.connect(str(db_path), read_only=True)

    try:
        rows = con.execute("""
            WITH complete_weeks AS (
                -- A week qualifies only if the date spine shows all
                -- seven days present in the fact table.
                SELECT d.week_start
                FROM marts.fct_daily_labour f
                JOIN staging.dim_date d ON f.activity_date = d.date_key
                GROUP BY d.week_start
                HAVING COUNT(DISTINCT f.activity_date) = 7
            ),
            latest AS (
                SELECT MAX(week_start) AS wk FROM complete_weeks
            )
            SELECT
                w.state,
                w.zone,
                CAST(w.week_start AS VARCHAR),
                w.cartons_picked,
                COALESCE(w.cartons_wow_pct, 0) * 100,
                CASE
                    WHEN w.cartons_trailing_sd IS NULL
                      OR w.cartons_trailing_sd = 0 THEN 0
                    ELSE (w.cartons_picked - w.cartons_trailing_mean)
                         / w.cartons_trailing_sd
                END,
                w.productivity_index,
                COALESCE(w.productivity_wow_pct, 0) * 100,
                w.mismatch_cost
            FROM marts.fct_weekly_summary w, latest
            WHERE w.week_start = latest.wk
            ORDER BY ABS(
                CASE
                    WHEN w.cartons_trailing_sd IS NULL
                      OR w.cartons_trailing_sd = 0 THEN 0
                    ELSE (w.cartons_picked - w.cartons_trailing_mean)
                         / w.cartons_trailing_sd
                END
            ) DESC
        """).fetchall()
    finally:
        con.close()

    findings = []
    for r in rows:
        sig = r[5]
        if abs(sig) < sigma:
            continue          # not notable; do not report noise
        findings.append(Finding(
            state=r[0], zone=r[1], week_start=r[2],
            cartons=r[3], cartons_wow_pct=r[4], sigma_from_mean=sig,
            productivity_index=r[6], productivity_wow_pct=r[7],
            mismatch_cost=r[8],
            direction="above" if sig > 0 else "below",
        ))

    # Report the most significant few. A commentary listing fifteen
    # items is a table, and nobody reads it.
    return findings[:max_findings]


# --------------------------------------------------------- writers

def render_template(findings: list[Finding]) -> str:
    """Deterministic commentary. No model, no API key, no surprises.

    This is the correctness baseline the LLM output is graded against.
    """
    if not findings:
        return ("## Weekly variance commentary\n\n"
                "No movements beyond the alert threshold this week. "
                "All state and zone combinations sat within two standard "
                "deviations of their eight-week trailing average.\n")

    week = findings[0].week_start
    lines = [f"## Weekly variance commentary — week beginning {week}", ""]
    lines.append(f"{len(findings)} movement(s) exceeded the "
                 f"{SIGMA_THRESHOLD} sigma alert threshold.")
    lines.append("")

    for f in findings:
        lines.append(
            f"- **{f.state} {f.zone}**: {f.cartons:,.0f} cartons, "
            f"{f.cartons_wow_pct:+.1f}% week on week and "
            f"{abs(f.sigma_from_mean):.1f} standard deviations "
            f"{f.direction} the trailing average. "
            f"Productivity index {f.productivity_index:.2f} "
            f"({f.productivity_wow_pct:+.1f}% WoW). "
            f"Staffing mismatch cost ${f.mismatch_cost:,.0f}."
        )
    return "\n".join(lines) + "\n"


SYSTEM_PROMPT = """You write short weekly operations commentary for a \
distribution centre management team.

You will receive a JSON list of findings. Every number has already been \
calculated. Your job is to explain what they mean in plain business \
language.

Rules, without exception:
- Use ONLY the numbers given to you. Never calculate, derive, estimate \
or infer a figure that is not present.
- Never invent a cause. You may say a movement is consistent with a \
pattern, but you do not know why it happened.
- Keep it under 200 words.
- Lead with the largest movement by absolute sigma.
- Plain sentences. No bullet lists, no headings, no preamble.
- If a productivity index is below 1.0, that means the floor ran slower \
than the engineered standard."""


def render_llm(findings: list[Finding], model: str = "claude-sonnet-4-6") -> str:
    """Ask a language model to narrate the findings.

    Requires ANTHROPIC_API_KEY in the environment. Raises if absent --
    the caller decides whether to fall back, so a missing key never
    silently changes the output.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    import urllib.request

    payload = {
        "model": model,
        "max_tokens": 600,
        "system": SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": json.dumps([asdict(f) for f in findings], indent=2),
        }],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())

    text = "".join(b.get("text", "") for b in body.get("content", []))
    week = findings[0].week_start if findings else "n/a"
    return f"## Weekly variance commentary — week beginning {week}\n\n{text}\n"


# ---------------------------------------------------------- runner

def run(use_llm: bool | None = None, db_path=None) -> str:
    """Produce the commentary and write it to output/.

    use_llm=None means "use the model if a key is available". The
    template output is always correct, so falling back costs accuracy
    nothing -- only readability.
    """
    findings = detect(db_path=db_path)
    log.info("%d finding(s) above %.1f sigma", len(findings), SIGMA_THRESHOLD)

    if use_llm is None:
        use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if use_llm and findings:
        try:
            text = render_llm(findings)
            log.info("commentary written by language model")
        except Exception as exc:
            log.warning("LLM commentary failed (%s) -- using template", exc)
            text = render_template(findings)
    else:
        text = render_template(findings)
        log.info("commentary written from template")

    COMMENTARY_FILE.write_text(text)
    log.info("wrote %s", COMMENTARY_FILE)
    return text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print()
    print(run())
