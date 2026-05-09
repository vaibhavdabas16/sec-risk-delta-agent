"""Step 1 — LLM: Normalizer. Canonicalizes user-supplied ticker strings."""
from __future__ import annotations
import re
import time
from pathlib import Path

from state import AgentState, TickerInfo
from llm_client import call_llm_structured

_raw = (Path(__file__).parent.parent / "prompts" / "normalize.md").read_text(encoding="utf-8")
_match = re.search(
    r'<!-- SYSTEM_PROMPT_START -->\n(.*?)<!-- SYSTEM_PROMPT_END -->',
    _raw, re.DOTALL
)
_SYSTEM = _match.group(1).strip() if _match else _raw


def run(state: AgentState, debug_log: list | None = None) -> AgentState:
    start = time.monotonic()

    user_prompt = (
        f"Normalize this user-supplied stock identifier: {state.user_input!r}\n\n"
        "Return a TickerInfo JSON object."
    )

    ticker_info = call_llm_structured(
        system_prompt=_SYSTEM,
        user_prompt=user_prompt,
        response_model=TickerInfo,
        debug_log=debug_log,
    )

    state.ticker_info = ticker_info
    state.step_durations_ms["step1_normalize"] = int((time.monotonic() - start) * 1000)
    return state
