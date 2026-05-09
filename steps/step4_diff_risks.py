"""Step 4 — LLM: Differ. Semantically matches risks across years and classifies changes."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from state import AgentState, DiffItem, RiskDiff, SummaryCounts
from llm_client import call_llm_structured

_raw = (Path(__file__).parent.parent / "prompts" / "diff_risks.md").read_text(encoding="utf-8")
_match = re.search(
    r'<!-- SYSTEM_PROMPT_START -->\n(.*?)<!-- SYSTEM_PROMPT_END -->',
    _raw, re.DOTALL
)
_SYSTEM = _match.group(1).strip() if _match else _raw


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

    prior_count = len(extracted.prior_year_risks)
    latest_count = len(extracted.latest_year_risks)

    user_prompt = (
        f"Compare the risk factors from {prior_year} (prior) "
        f"and {latest_year} (latest).\n\n"
        f"PRIOR YEAR RISKS ({prior_year}) — {prior_count} total:\n"
        f"{prior_json}\n\n"
        f"LATEST YEAR RISKS ({latest_year}) — {latest_count} total:\n"
        f"{latest_json}\n\n"
        f"IMPORTANT: All {prior_count} prior-year risks and all "
        f"{latest_count} latest-year risks must appear in your output. "
        f"Do not skip or omit any risk.\n\n"
        "Classify each risk as: added, removed, materially_changed, "
        "or unchanged.\n"
        "Apply the materiality threshold from your instructions strictly: "
        "a risk is materially_changed only if it introduces a new named "
        "regulation, new geography, new financial magnitude, or new named "
        "incident. Cosmetic rewording = unchanged. When in doubt, choose "
        "unchanged.\n\n"
        "Return only a valid RiskDiff JSON. No preamble."
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
