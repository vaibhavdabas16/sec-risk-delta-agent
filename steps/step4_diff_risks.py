"""Step 4 — LLM: Differ. Semantically matches risks across years and classifies changes."""
from __future__ import annotations
import json
import time
from pathlib import Path

from state import AgentState, DiffItem, RiskDiff, SummaryCounts
from llm_client import call_llm_structured

_SYSTEM = (Path(__file__).parent.parent / "prompts" / "diff_risks.md").read_text(encoding="utf-8")


def run(state: AgentState, debug_log: list | None = None) -> AgentState:
    assert state.extracted_risks is not None, "Step 3 must run before Step 4"
    start = time.monotonic()

    extracted = state.extracted_risks

    if not extracted.prior_year_risks:
        # Degraded mode: no prior year data — everything is "added"
        items = [
            DiffItem(
                verdict="added",
                latest_risk_id=r.id,
                prior_risk_id=None,
                rationale="No prior-year filing available for comparison.",
            )
            for r in extracted.latest_year_risks
        ]
        state.risk_diff = RiskDiff(
            items=items,
            summary_counts=SummaryCounts(added=len(items)),
        )
        state.step_durations_ms["step4_diff_risks"] = int((time.monotonic() - start) * 1000)
        return state

    latest_json = json.dumps([r.model_dump() for r in extracted.latest_year_risks], indent=2)
    prior_json = json.dumps([r.model_dump() for r in extracted.prior_year_risks], indent=2)

    latest_year = state.edgar["latest_year"] if state.edgar else "latest"
    prior_year = state.edgar.get("prior_year", "prior") if state.edgar else "prior"

    user_prompt = (
        f"Compare the risk factors from {prior_year} (prior) and {latest_year} (latest).\n\n"
        f"PRIOR YEAR RISKS ({prior_year}):\n{prior_json}\n\n"
        f"LATEST YEAR RISKS ({latest_year}):\n{latest_json}\n\n"
        "Produce a RiskDiff JSON matching risks across years and classifying each as: "
        "added, removed, materially_changed, or unchanged."
    )

    diff = call_llm_structured(
        system_prompt=_SYSTEM,
        user_prompt=user_prompt,
        response_model=RiskDiff,
        model="gpt-4o",
        debug_log=debug_log,
    )

    # Recompute summary_counts from actual items (in case LLM miscounted)
    counts = SummaryCounts()
    for item in diff.items:
        setattr(counts, item.verdict, getattr(counts, item.verdict) + 1)
    diff.summary_counts = counts

    state.risk_diff = diff
    state.step_durations_ms["step4_diff_risks"] = int((time.monotonic() - start) * 1000)
    return state
