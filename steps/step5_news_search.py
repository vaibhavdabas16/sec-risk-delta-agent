"""Step 5 — TOOL: News Search. Fetches recent news via Tavily (or DuckDuckGo fallback)."""
from __future__ import annotations
import os
import re
import time
from urllib.parse import quote_plus

import requests

from state import AgentState

TAVILY_ENDPOINT = "https://api.tavily.com/search"
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
MAX_RESULTS_PER_RISK = 3
RATE_SLEEP = 0.5


def _tavily_search(query: str, api_key: str, debug_log: list | None) -> list[dict]:
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": MAX_RESULTS_PER_RISK,
        "include_answer": False,
        "include_raw_content": False,
        "days": 365,
    }
    try:
        resp = requests.post(TAVILY_ENDPOINT, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for r in data.get("results", [])[:MAX_RESULTS_PER_RISK]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", "")[:400],
                "published_date": r.get("published_date", ""),
            })
        if debug_log is not None:
            debug_log.append({"type": "tool_call", "tool": "tavily_search", "query": query, "result_count": len(results)})
        return results
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (402, 429):
            raise RuntimeError("Tavily quota exhausted") from exc
        raise


def _ddg_search(query: str, debug_log: list | None) -> list[dict]:
    """Fallback: scrape DuckDuckGo HTML results."""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SEC-Risk-Delta-Agent/1.0)",
        "Accept": "text/html",
    }
    params = {"q": query, "kl": "us-en"}
    try:
        resp = requests.post(DDG_ENDPOINT, data=params, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text

        results = []
        # Extract result links and snippets via simple regex
        links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
        snippets = re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html, re.S)

        for i, (url, title) in enumerate(links[:MAX_RESULTS_PER_RISK]):
            snippet = re.sub(r"<[^>]+>", "", snippets[i]) if i < len(snippets) else ""
            results.append({
                "title": re.sub(r"<[^>]+>", "", title).strip(),
                "url": url,
                "snippet": snippet.strip()[:400],
                "published_date": "",
            })

        if debug_log is not None:
            debug_log.append({"type": "tool_call", "tool": "ddg_search_fallback", "query": query, "result_count": len(results)})
        return results
    except Exception:
        return []


def run(state: AgentState, debug_log: list | None = None) -> AgentState:
    assert state.risk_diff is not None, "Step 4 must run before Step 5"
    assert state.extracted_risks is not None
    assert state.ticker_info is not None

    start = time.monotonic()

    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    use_tavily = bool(tavily_key)
    if not use_tavily:
        state.add_error("TAVILY_API_KEY not set — using DuckDuckGo fallback for news search.")

    company = state.ticker_info.company_name

    # Build a lookup from risk_id → Risk object for both years
    risk_lookup: dict[str, object] = {}
    for r in state.extracted_risks.latest_year_risks:
        risk_lookup[r.id] = r
    for r in state.extracted_risks.prior_year_risks:
        risk_lookup[r.id] = r

    target_verdicts = {"added", "materially_changed"}
    news_evidence: dict[str, list] = {}

    for item in state.risk_diff.items:
        if item.verdict not in target_verdicts:
            continue

        risk_id = item.latest_risk_id or item.prior_risk_id
        if not risk_id:
            continue

        risk = risk_lookup.get(risk_id)
        label = risk.label if risk else risk_id  # type: ignore[union-attr]

        query = f"{company} {label}"
        time.sleep(RATE_SLEEP)

        try:
            if use_tavily:
                results = _tavily_search(query, tavily_key, debug_log)
            else:
                results = _ddg_search(query, debug_log)
        except RuntimeError as exc:
            # Tavily quota exhausted — switch to DDG for rest
            state.add_error(str(exc))
            use_tavily = False
            results = _ddg_search(query, debug_log)
        except Exception as exc:
            state.add_error(f"News search failed for '{query}': {exc}")
            results = []

        news_evidence[risk_id] = results

    state.news_evidence = news_evidence
    state.step_durations_ms["step5_news_search"] = int((time.monotonic() - start) * 1000)
    return state
