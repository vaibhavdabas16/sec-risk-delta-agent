"""Step 6 — LLM: Synthesizer + Critic. Writes the final markdown memo and confidence labels."""
from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from state import AgentState, ConfidenceAssessment
from llm_client import call_llm_text, call_llm_structured

_SYSTEM = (Path(__file__).parent.parent / "prompts" / "synthesize.md").read_text(encoding="utf-8")


class SynthesisOutput(BaseModel):
    memo_markdown: str
    confidence_assessments: list[ConfidenceAssessment]


def _build_context(state: AgentState) -> str:
    assert state.ticker_info and state.edgar and state.extracted_risks and state.risk_diff

    risk_lookup: dict[str, Any] = {}
    for r in state.extracted_risks.latest_year_risks:
        risk_lookup[r.id] = r
    for r in state.extracted_risks.prior_year_risks:
        risk_lookup[r.id] = r

    added_items = [i for i in state.risk_diff.items if i.verdict == "added"]
    changed_items = [i for i in state.risk_diff.items if i.verdict == "materially_changed"]
    removed_items = [i for i in state.risk_diff.items if i.verdict == "removed"]

    news = state.news_evidence or {}

    parts = [
        f"# Company: {state.ticker_info.company_name} ({state.ticker_info.ticker})",
        f"# Filing Years: {state.edgar.get('prior_year', 'N/A')} → {state.edgar['latest_year']}",
        f"# Diff Summary: {state.risk_diff.summary_counts.model_dump_json()}",
        "",
        "## ADDED RISKS",
    ]

    for item in added_items:
        rid = item.latest_risk_id or ""
        risk = risk_lookup.get(rid)
        if risk:
            parts.append(f"### [{rid}] {risk.label}")
            parts.append(f"Summary: {risk.summary}")
        snippets = news.get(rid, [])
        if snippets:
            parts.append("News evidence:")
            for s in snippets:
                parts.append(f"  - [{s.get('title','')}]({s.get('url','')}) — {s.get('snippet','')[:200]}")
        else:
            parts.append("News evidence: none found")
        parts.append("")

    parts.append("## MATERIALLY CHANGED RISKS")
    for item in changed_items:
        rid = item.latest_risk_id or item.prior_risk_id or ""
        risk = risk_lookup.get(rid)
        if risk:
            parts.append(f"### [{rid}] {risk.label}")
            parts.append(f"Rationale for change: {item.rationale}")
            parts.append(f"Summary: {risk.summary}")
        snippets = news.get(rid, [])
        if snippets:
            parts.append("News evidence:")
            for s in snippets:
                parts.append(f"  - [{s.get('title','')}]({s.get('url','')}) — {s.get('snippet','')[:200]}")
        else:
            parts.append("News evidence: none found")
        parts.append("")

    parts.append("## REMOVED RISKS")
    for item in removed_items:
        rid = item.prior_risk_id or ""
        risk = risk_lookup.get(rid)
        if risk:
            parts.append(f"- [{rid}] {risk.label}")
        parts.append("")

    if state.errors:
        parts.append("## AGENT WARNINGS")
        for e in state.errors:
            parts.append(f"- {e}")

    return "\n".join(parts)


def run(state: AgentState, debug_log: list | None = None) -> AgentState:
    assert state.risk_diff is not None, "Step 4 must run before Step 6"
    start = time.monotonic()

    context = _build_context(state)

    user_prompt = (
        "Using the structured data below, write the final investor-ready markdown memo AND "
        "produce a confidence_assessments JSON list.\n\n"
        f"{context}\n\n"
        "Return a SynthesisOutput with:\n"
        "1. memo_markdown: the full formatted markdown memo\n"
        "2. confidence_assessments: list of {risk_id, confidence, justification} for each "
        "added or materially_changed risk"
    )

    result = call_llm_structured(
        system_prompt=_SYSTEM,
        user_prompt=user_prompt,
        response_model=SynthesisOutput,
        model="gpt-4o",
        temperature=0.2,
        debug_log=debug_log,
    )

    state.final_memo_md = result.memo_markdown
    state.confidence_assessments = [a.model_dump() for a in result.confidence_assessments]
    state.step_durations_ms["step6_synthesize"] = int((time.monotonic() - start) * 1000)
    return state
