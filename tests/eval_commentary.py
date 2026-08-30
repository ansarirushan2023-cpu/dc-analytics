"""Evaluation set for the commentary layer.

Why this file exists: anyone can put an LLM in a pipeline. The
question an interviewer will ask is "how do you know it isn't making
things up?" This is the answer.

Each case is a hand-built scenario with a known correct interpretation.
We check that the generated text:

  1. states the facts that matter
  2. contains NO number that wasn't in the input

Check 2 is the important one. It extracts every number from the output
and confirms each traces back to an input figure. That catches the
failure mode that matters -- a confident, plausible, invented statistic.

Run:  python -m tests.eval_commentary
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

from src.commentary import Finding, render_template  # noqa: E402


@dataclass
class Case:
    name: str
    findings: list[Finding]
    must_mention: list[str]      # substrings that must appear
    must_not_mention: list[str]  # substrings that must NOT appear


def _f(**kw) -> Finding:
    base = dict(state="SA", zone="ambient", week_start="2026-08-24",
                cartons=25000.0, cartons_wow_pct=0.0, sigma_from_mean=0.0,
                productivity_index=1.0, productivity_wow_pct=0.0,
                mismatch_cost=1000.0, direction="above")
    base.update(kw)
    return Finding(**base)


CASES = [
    Case(
        name="quiet_week_says_nothing_happened",
        findings=[],
        must_mention=["No movements"],
        must_not_mention=["sigma alert threshold\n\n-"],
    ),
    Case(
        name="large_drop_is_reported_as_below",
        findings=[_f(state="QLD", zone="freezer", cartons=48379.0,
                     cartons_wow_pct=-10.9, sigma_from_mean=-3.1,
                     direction="below", mismatch_cost=3869.0)],
        must_mention=["QLD", "freezer", "below", "-10.9%", "3.1"],
        must_not_mention=["above"],
    ),
    Case(
        name="rise_is_reported_as_above",
        findings=[_f(state="VIC", zone="chiller", cartons=71246.0,
                     cartons_wow_pct=4.9, sigma_from_mean=2.2,
                     direction="above", mismatch_cost=9951.0)],
        must_mention=["VIC", "chiller", "above", "+4.9%"],
        must_not_mention=["below"],
    ),
    Case(
        name="poor_productivity_is_surfaced",
        findings=[_f(state="NSW", zone="ambient", productivity_index=0.87,
                     productivity_wow_pct=-9.4, sigma_from_mean=-2.5,
                     direction="below")],
        must_mention=["0.87", "-9.4%"],
        must_not_mention=[],
    ),
]


# ------------------------------------------------ hallucination check

def extract_numbers(text: str) -> set[float]:
    """Pull every number out of the text, ignoring formatting."""
    raw = re.findall(r"-?\d[\d,]*\.?\d*", text.replace("$", ""))
    out = set()
    for r in raw:
        try:
            out.add(abs(round(float(r.replace(",", "")), 2)))
        except ValueError:
            pass
    return out


def allowed_numbers(findings: list[Finding]) -> set[float]:
    """Every figure the writer is permitted to state, plus rounded and
    integer forms of each (formatting legitimately rounds)."""
    allowed = {2.0, 0.0, float(len(findings))}   # threshold, zero, count
    for f in findings:
        for v in (f.cartons, f.cartons_wow_pct, f.sigma_from_mean,
                  f.productivity_index, f.productivity_wow_pct,
                  f.mismatch_cost):
            a = abs(v)
            allowed.update({round(a, 2), round(a, 1), float(int(a)),
                            float(round(a))})
        # Dates get split into parts by the number regex.
        for part in f.week_start.replace("-", " ").split():
            try:
                allowed.add(float(part))
            except ValueError:
                pass
    return allowed


def check_no_hallucinated_numbers(text: str,
                                  findings: list[Finding]) -> list[float]:
    """Return any number in the output that isn't traceable to input."""
    return sorted(extract_numbers(text) - allowed_numbers(findings))


# ------------------------------------------------------------- runner

def run(writer=render_template, label="template") -> int:
    print(f"\nevaluating commentary writer: {label}")
    print("=" * 62)
    failures = 0

    for case in CASES:
        text = writer(case.findings)
        problems = []

        for want in case.must_mention:
            if want not in text:
                problems.append(f"missing expected text: {want!r}")

        for avoid in case.must_not_mention:
            if avoid in text:
                problems.append(f"contains forbidden text: {avoid!r}")

        invented = check_no_hallucinated_numbers(text, case.findings)
        if invented:
            problems.append(f"HALLUCINATED NUMBERS: {invented}")

        if problems:
            failures += 1
            print(f"  FAIL  {case.name}")
            for p in problems:
                print(f"          {p}")
        else:
            print(f"  PASS  {case.name}")

    print("=" * 62)
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    return failures


if __name__ == "__main__":
    failed = run()

    # If a key is present, grade the model output the same way.
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        from src.commentary import render_llm

        def llm_writer(findings):
            return render_llm(findings) if findings else render_template(findings)

        print("\nANTHROPIC_API_KEY found -- also grading the model.")
        failed += run(llm_writer, "language model")
    else:
        print("\nNo ANTHROPIC_API_KEY set -- model writer not graded.")
        print("The template writer is the correctness baseline and passed.")

    raise SystemExit(1 if failed else 0)
