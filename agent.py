"""SEC 10-K Risk Factor Delta Agent — CLI entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)

# Ensure the project root is on sys.path so step imports work
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from state import AgentState
from steps import (
    step1_normalize,
    step2_edgar_fetch,
    step3_extract_risks,
    step4_diff_risks,
    step5_news_search,
    step6_synthesize,
)

OUTPUTS = ROOT / "outputs"
OUTPUTS.mkdir(exist_ok=True)

# ── ANSI colour helpers ──────────────────────────────────────────────────────

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def _ok(msg: str) -> str:
    return f"{GREEN}✓{RESET} {msg}"

def _fail(msg: str) -> str:
    return f"{RED}✗{RESET} {msg}"

def _step_line(n: int, total: int, label: str) -> str:
    return f"{CYAN}[{n}/{total}]{RESET} {label}..."

# ── Debug JSONL helper ────────────────────────────────────────────────────────

def _flush_debug(debug_log: list, path: Path) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for entry in debug_log:
            fh.write(json.dumps(entry, default=str) + "\n")
    debug_log.clear()

# ── Step runner with timing + pretty output ───────────────────────────────────

def _run_step(
    n: int,
    total: int,
    label: str,
    fn,
    state: AgentState,
    debug_log: list | None,
    debug_path: Path | None,
) -> AgentState:
    print(_step_line(n, total, label), end="", flush=True)
    t0 = time.monotonic()
    try:
        state = fn(state, debug_log=debug_log)
        elapsed = time.monotonic() - t0
        print(f"\r{_step_line(n, total, label):<55} {_ok(f'{elapsed:.1f}s')}")
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"\r{_step_line(n, total, label):<55} {_fail(str(exc))}")
        raise
    finally:
        if debug_log and debug_path:
            _flush_debug(debug_log, debug_path)
    return state

# ── Summary line helpers ──────────────────────────────────────────────────────

def _ticker_summary(state: AgentState) -> str:
    ti = state.ticker_info
    if ti:
        return f"{BOLD}{ti.ticker}{RESET} → {ti.company_name}"
    return state.user_input

def _edgar_summary(state: AgentState) -> str:
    e = state.edgar
    if not e:
        return "no EDGAR data"
    if e.get("degraded_mode"):
        return f"⚠ Only 1 filing found (FY{e['latest_year']}) — degraded mode"
    return f"Got 10-K for FY{e['latest_year']} and FY{e['prior_year']}"

def _extract_summary(state: AgentState) -> str:
    er = state.extracted_risks
    if not er:
        return "no risks"
    return f"Latest: {len(er.latest_year_risks)} risks, Prior: {len(er.prior_year_risks)} risks"

def _diff_summary(state: AgentState) -> str:
    rd = state.risk_diff
    if not rd:
        return "no diff"
    c = rd.summary_counts
    return (
        f"+{c.added} added, "
        f"~{c.materially_changed} changed, "
        f"-{c.removed} removed"
    )

def _news_summary(state: AgentState) -> str:
    if not state.news_evidence:
        return "no news evidence"
    total_risks = len(state.news_evidence)
    with_news = sum(1 for v in state.news_evidence.values() if v)
    return f"Found evidence for {with_news} of {total_risks} risks"

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SEC 10-K Risk Factor Delta Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python agent.py AAPL\n"
            "  python agent.py tsla --debug\n"
            "  python agent.py ZZZZ          # graceful failure demo\n"
        ),
    )
    parser.add_argument("ticker", help="Stock ticker or company name (e.g. TSLA, Apple)")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Dump every LLM/tool call to outputs/<ticker>_debug.jsonl",
    )
    args = parser.parse_args()

    # ── Input validation (unexpected / malformed input) ───────────────────────
    raw_input = args.ticker.strip()

    if not raw_input:
        print(f"\n{RED}Error: Input is empty.{RESET} "
              f"Provide a US stock ticker (e.g. {CYAN}AAPL{RESET}) or company name (e.g. {CYAN}Apple{RESET}).")
        sys.exit(1)

    if len(raw_input) > 120:
        print(f"{YELLOW}Warning: Input is unusually long ({len(raw_input)} chars). "
              f"Truncating to first 120 characters.{RESET}")
        raw_input = raw_input[:120]

    if not any(c.isalpha() for c in raw_input):
        print(f"\n{RED}Error: Input {raw_input!r} contains no letters.{RESET} "
              f"A stock ticker must contain at least one letter (e.g. {CYAN}TSLA{RESET}).")
        sys.exit(1)

    print(f"\n{BOLD}SEC 10-K Risk Factor Delta Agent{RESET}")
    print(f"Input: {CYAN}{raw_input!r}{RESET}\n")

    state = AgentState(user_input=raw_input)
    debug_log: list | None = [] if args.debug else None
    debug_path: Path | None = None

    TOTAL = 6
    t_start = time.monotonic()

    # ── Step 1: Normalizer ────────────────────────────────────────────────────
    try:
        state = _run_step(1, TOTAL, "Normalizing ticker", step1_normalize.run, state, debug_log, debug_path)
    except Exception as exc:
        print(f"\n{RED}Fatal error in Step 1:{RESET} {exc}")
        sys.exit(1)

    # Now that we have the ticker, set up debug path
    ticker_safe = (state.ticker_info.ticker if state.ticker_info else "UNKNOWN").upper()
    today = date.today().isoformat()
    stem = f"{ticker_safe}_{today}"
    if args.debug:
        debug_path = OUTPUTS / f"{stem}_debug.jsonl"
        debug_path.unlink(missing_ok=True)
        print(f"  {YELLOW}Debug log:{RESET} {debug_path}")

    print(f"  → {_ticker_summary(state)}")

    # Hard abort if LLM flagged input as not a stock identifier
    if state.ticker_info and state.ticker_info.ticker == "INVALID":
        print(
            f"\n{RED}Error: {raw_input!r} does not look like a stock ticker "
            f"or company name.{RESET}\n"
            f"Examples of valid input: {CYAN}AAPL{RESET}, {CYAN}TSLA{RESET}, "
            f"{CYAN}Apple{RESET}, {CYAN}Microsoft{RESET}."
        )
        sys.exit(1)

    # Warn if Step 1 was uncertain — ticker may not resolve correctly
    if state.ticker_info and state.ticker_info.confidence == "low":
        print(
            f"  {YELLOW}⚠ Low-confidence normalization:{RESET} "
            f"'{raw_input}' was mapped to {state.ticker_info.ticker} with low confidence. "
            f"If Step 2 fails, check your ticker spelling."
        )

    # ── Step 2: EDGAR Fetcher ─────────────────────────────────────────────────
    try:
        state = _run_step(2, TOTAL, "Fetching SEC filings", step2_edgar_fetch.run, state, debug_log, debug_path)
    except ValueError as exc:
        msg = str(exc)
        print(f"\n{RED}Fatal error in Step 2:{RESET} {msg}")
        if "not found in SEC EDGAR" in msg:
            print(
                f"\n{YELLOW}Tip:{RESET} Common causes:\n"
                f"  • Misspelled ticker — try the exact NYSE/NASDAQ symbol\n"
                f"  • Foreign company (Toyota, ASML, Shell) — these file 20-F forms, not 10-K\n"
                f"  • Recently delisted company — EDGAR may have no current filings\n"
                f"  • Recent IPO with only one 10-K — agent will run in degraded mode if one exists"
            )
        sys.exit(1)
    except Exception as exc:
        print(f"\n{RED}Fatal error in Step 2:{RESET} {exc}")
        sys.exit(1)

    print(f"  → {_edgar_summary(state)}")

    # ── Step 3: Risk Extractor ────────────────────────────────────────────────
    try:
        state = _run_step(3, TOTAL, "Extracting risk factors", step3_extract_risks.run, state, debug_log, debug_path)
    except Exception as exc:
        print(f"\n{RED}Fatal error in Step 3:{RESET} {exc}")
        sys.exit(1)

    print(f"  → {_extract_summary(state)}")

    # ── Step 4: Differ ────────────────────────────────────────────────────────
    try:
        state = _run_step(4, TOTAL, "Diffing risks across years", step4_diff_risks.run, state, debug_log, debug_path)
    except Exception as exc:
        print(f"\n{RED}Fatal error in Step 4:{RESET} {exc}")
        sys.exit(1)

    print(f"  → {_diff_summary(state)}")

    # ── Step 5: News Search ───────────────────────────────────────────────────
    try:
        state = _run_step(5, TOTAL, "Searching news for new risks", step5_news_search.run, state, debug_log, debug_path)
    except Exception as exc:
        print(f"\n{RED}Fatal error in Step 5:{RESET} {exc}")
        sys.exit(1)

    print(f"  → {_news_summary(state)}")

    # ── Step 6: Synthesizer ───────────────────────────────────────────────────
    memo_path = OUTPUTS / f"{stem}.md"
    state_path = OUTPUTS / f"{stem}.state.json"

    try:
        state = _run_step(6, TOTAL, "Synthesizing memo", step6_synthesize.run, state, debug_log, debug_path)
    except Exception as exc:
        print(f"\n{RED}Fatal error in Step 6:{RESET} {exc}")
        # Still write partial state
        state_path.write_text(state.to_json(), encoding="utf-8")
        print(f"Partial state saved to: {state_path}")
        sys.exit(1)

    # ── Write outputs ──────────────────────────────────────────────────────────
    memo_path.write_text(state.final_memo_md or "", encoding="utf-8")
    state_path.write_text(state.to_json(), encoding="utf-8")

    total_s = time.monotonic() - t_start
    print(f"\n{GREEN}Done{RESET} in {total_s:.1f}s.")
    print(f"  Memo:  {BOLD}{memo_path}{RESET}")
    print(f"  State: {state_path}")
    if args.debug and debug_path:
        print(f"  Debug: {debug_path}")

    if state.errors:
        print(f"\n{YELLOW}Warnings ({len(state.errors)}):{RESET}")
        for e in state.errors:
            print(f"  • {e}")


if __name__ == "__main__":
    main()
