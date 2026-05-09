"""Step 2 — TOOL: SEC EDGAR Fetcher. No LLM used here."""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from state import AgentState

CACHE_DIR = Path(__file__).parent.parent / "outputs" / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EDGAR_BASE = "https://data.sec.gov"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
TICKER_CACHE = CACHE_DIR / "company_tickers.json"
CACHE_TTL_SECONDS = 86400  # 24h

RATE_SLEEP = 0.15  # SEC allows ≤10 req/sec


def _user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "")
    if not ua:
        raise EnvironmentError(
            "SEC_USER_AGENT env var is required for SEC EDGAR access.\n"
            "Set it to something like: 'Your Name your.email@example.com'"
        )
    return ua


def _get(url: str, *, retries: int = 3, stream: bool = False) -> requests.Response:
    headers = {"User-Agent": _user_agent(), "Accept": "application/json, text/html, */*"}
    for attempt in range(retries):
        time.sleep(RATE_SLEEP)
        resp = requests.get(url, headers=headers, timeout=30, stream=stream)
        if resp.status_code == 200:
            return resp
        if resp.status_code in (403, 429):
            wait = 2 ** attempt
            time.sleep(wait)
            continue
        resp.raise_for_status()
    resp.raise_for_status()
    return resp  # unreachable, satisfies type checker


def _load_ticker_map() -> dict[str, Any]:
    if TICKER_CACHE.exists():
        age = time.time() - TICKER_CACHE.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            return json.loads(TICKER_CACHE.read_text(encoding="utf-8"))

    resp = _get(TICKER_MAP_URL)
    data = resp.json()
    TICKER_CACHE.write_text(json.dumps(data), encoding="utf-8")
    return data


def _resolve_cik(ticker: str) -> str:
    data = _load_ticker_map()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(
        f"Ticker '{ticker}' not found in SEC EDGAR CIK map. "
        "Check the ticker symbol or try with the company's legal name."
    )


def _get_10k_filings(cik: str) -> list[dict]:
    url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
    data = _get(url).json()

    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    filings_10k = []
    for i, form in enumerate(forms):
        if form == "10-K":
            filings_10k.append({
                "form": form,
                "date": dates[i],
                "accession": accessions[i].replace("-", ""),
                "primary_doc": primary_docs[i],
            })
        if len(filings_10k) >= 2:
            break

    # Also check additional filings pages if fewer than 2 found
    if len(filings_10k) < 2 and "files" in data.get("filings", {}):
        for file_info in data["filings"]["files"]:
            if len(filings_10k) >= 2:
                break
            sub_url = f"{EDGAR_BASE}/{file_info['name']}"
            try:
                sub_data = _get(sub_url).json()
                for i, form in enumerate(sub_data.get("form", [])):
                    if form == "10-K":
                        filings_10k.append({
                            "form": form,
                            "date": sub_data["filingDate"][i],
                            "accession": sub_data["accessionNumber"][i].replace("-", ""),
                            "primary_doc": sub_data["primaryDocument"][i],
                        })
                    if len(filings_10k) >= 2:
                        break
            except Exception:
                continue

    return filings_10k


def _build_filing_url(cik: str, accession: str, primary_doc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary_doc}"


def _fetch_html(url: str) -> str:
    resp = _get(url)
    return resp.text


def _extract_item1a(html: str) -> str:
    """Extract Item 1A text using regex; falls back to a broad approach."""
    # Remove HTML tags for easier regex
    clean = re.sub(r"<[^>]+>", " ", html)
    clean = re.sub(r"&nbsp;", " ", clean)
    clean = re.sub(r"&amp;", "&", clean)
    clean = re.sub(r"&lt;", "<", clean)
    clean = re.sub(r"&gt;", ">", clean)
    clean = re.sub(r"&#\d+;", " ", clean)
    clean = re.sub(r"\s{2,}", " ", clean)

    patterns_start = [
        r"Item\s+1A[\.\s]*Risk\s+Factors",
        r"ITEM\s+1A[\.\s]*RISK\s+FACTORS",
        r"Item\s*1A\.",
    ]
    patterns_end = [
        r"Item\s+1B[\.\s]",
        r"ITEM\s+1B[\.\s]",
        r"Item\s+2[\.\s]",
        r"ITEM\s+2[\.\s]",
    ]

    # Collect all start matches across all patterns and try each in order,
    # skipping TOC entries (too short to be the real section).
    all_starts: list[re.Match] = []
    for pat in patterns_start:
        all_starts.extend(re.finditer(pat, clean, re.IGNORECASE))
    all_starts.sort(key=lambda m: m.start())

    for start_match in all_starts:
        end_match = None
        for pat in patterns_end:
            m = re.search(pat, clean[start_match.end():], re.IGNORECASE)
            if m:
                end_match = m
                break

        if end_match:
            section = clean[start_match.start(): start_match.end() + end_match.start()]
        else:
            section = clean[start_match.start(): start_match.start() + 80000]

        # Skip table-of-contents hits — real sections are substantially longer
        if len(section.strip()) > 500:
            return section.strip()

    return ""


def run(state: AgentState, debug_log: list | None = None) -> AgentState:
    assert state.ticker_info is not None, "Step 1 must run before Step 2"
    start_total = time.monotonic()

    ticker = state.ticker_info.ticker

    try:
        cik = _resolve_cik(ticker)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    if debug_log is not None:
        debug_log.append({"type": "tool_call", "tool": "edgar_cik_lookup", "ticker": ticker, "cik": cik})

    filings = _get_10k_filings(cik)

    if len(filings) == 0:
        raise ValueError(f"No 10-K filings found for {ticker} (CIK {cik}) on EDGAR.")

    degraded = len(filings) == 1
    if degraded:
        state.add_error(
            f"Only one 10-K filing found for {ticker}. "
            "Comparison is not possible — running in degraded single-filing mode."
        )

    latest = filings[0]
    latest_url = _build_filing_url(cik, latest["accession"], latest["primary_doc"])
    latest_html = _fetch_html(latest_url)
    latest_item1a = _extract_item1a(latest_html)

    if not latest_item1a:
        state.add_error(f"Regex extraction of Item 1A failed for latest filing ({latest_url}). Section may be missing or formatted unusually.")
        latest_item1a = latest_html[:60000]

    if not degraded:
        prior = filings[1]
        prior_url = _build_filing_url(cik, prior["accession"], prior["primary_doc"])
        prior_html = _fetch_html(prior_url)
        prior_item1a = _extract_item1a(prior_html)

        if not prior_item1a:
            state.add_error(f"Regex extraction of Item 1A failed for prior filing ({prior_url}).")
            prior_item1a = prior_html[:60000]
    else:
        prior = None
        prior_url = None
        prior_item1a = ""

    state.edgar = {
        "cik": cik,
        "latest_year": latest["date"][:4],
        "prior_year": prior["date"][:4] if prior else None,
        "latest_item_1a": latest_item1a[:80000],
        "prior_item_1a": prior_item1a[:80000],
        "latest_filing_url": latest_url,
        "prior_filing_url": prior_url,
        "degraded_mode": degraded,
    }

    if debug_log is not None:
        debug_log.append({
            "type": "tool_call",
            "tool": "edgar_fetch",
            "latest_url": latest_url,
            "prior_url": prior_url,
            "latest_item1a_len": len(latest_item1a),
            "prior_item1a_len": len(prior_item1a),
            "degraded": degraded,
        })

    state.step_durations_ms["step2_edgar_fetch"] = int((time.monotonic() - start_total) * 1000)
    return state
