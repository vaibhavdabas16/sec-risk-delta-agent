"""Step 3 — LLM: Risk Extractor. Atomizes Item 1A text into structured Risk objects."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from state import AgentState, ExtractedRisks, Risk
from llm_client import call_llm_structured

_raw = (Path(__file__).parent.parent / "prompts" / "extract_risks.md").read_text(encoding="utf-8")
_match = re.search(
    r'<!-- SYSTEM_PROMPT_START -->\n(.*?)<!-- SYSTEM_PROMPT_END -->',
    _raw, re.DOTALL
)
_SYSTEM = _match.group(1).strip() if _match else _raw

# GPT-4o-mini context is 128k tokens; Item 1A for large companies can be ~30k chars.
# We truncate conservatively to leave room for the response.
MAX_ITEM1A_CHARS = 60_000


def _extract_for_year(item1a_text: str, year: str, debug_log: list | None) -> list[Risk]:
    truncated = item1a_text[:MAX_ITEM1A_CHARS]
    user_prompt = (
        f"Extract all atomic risk factors from this SEC 10-K Item 1A (Risk Factors) "
        f"section for fiscal year {year}. "
        f"Return all results in the `latest_year_risks` field "
        f"and leave `prior_year_risks` as an empty array.\n\n"
        f"TEXT:\n{truncated}"
    )
    result = call_llm_structured(
        system_prompt=_SYSTEM,
        user_prompt=user_prompt,
        response_model=ExtractedRisks,
        model="gpt-4o",
        debug_log=debug_log,
    )
    # We only need one year's list per call; return whichever is populated
    if result.latest_year_risks:
        return result.latest_year_risks
    return result.prior_year_risks


def run(state: AgentState, debug_log: list | None = None) -> AgentState:
    assert state.edgar is not None, "Step 2 must run before Step 3"
    start = time.monotonic()

    edgar = state.edgar
    latest_year = edgar["latest_year"]
    prior_year = edgar.get("prior_year")

    latest_risks = _extract_for_year(edgar["latest_item_1a"], latest_year, debug_log)

    if edgar.get("degraded_mode") or not edgar["prior_item_1a"]:
        prior_risks: list[Risk] = []
    else:
        prior_risks = _extract_for_year(edgar["prior_item_1a"], prior_year or "prior", debug_log)

    state.extracted_risks = ExtractedRisks(
        latest_year_risks=latest_risks,
        prior_year_risks=prior_risks,
    )

    state.step_durations_ms["step3_extract_risks"] = int((time.monotonic() - start) * 1000)
    return state
