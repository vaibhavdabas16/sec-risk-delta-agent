# SEC 10-K Risk Factor Delta Agent

An investor-ready multi-step LLM agent that reads two consecutive SEC 10-K
filings for any US-listed company, diffs the Risk Factors section, and
enriches the delta with recent news evidence — producing a structured markdown
memo and a full JSON state dump.

Built from scratch. No LangChain, LlamaIndex, or agent frameworks.

---

## Chain Structure

```
User input (ticker string)
        │
        ▼
┌─────────────────────────────┐
│  Step 1 · LLM · Normalizer  │  "tsla " → TSLA, Tesla Inc., confidence=high
│  model: gpt-4o-mini         │
└──────────────┬──────────────┘
               │ TickerInfo (typed Pydantic)
               ▼
┌─────────────────────────────────────┐
│  Step 2 · TOOL · SEC EDGAR Fetcher  │  CIK lookup → 10-K HTML → Item 1A text
│  requests + SEC API (no LLM)        │
└──────────────────┬──────────────────┘
                   │ edgar dict: latest_item_1a, prior_item_1a, URLs
                   ▼
┌──────────────────────────────────────┐
│  Step 3 · LLM · Risk Extractor       │  Atomizes Item 1A into Risk[] per year
│  model: gpt-4o (2 calls)            │
└──────────────────┬───────────────────┘
                   │ ExtractedRisks: latest_year_risks[], prior_year_risks[]
                   ▼
┌──────────────────────────────────────┐
│  Step 4 · LLM · Differ               │  Semantic match across years → verdicts
│  model: gpt-4o                       │  added / removed / materially_changed /
└──────────────────┬───────────────────┘  unchanged
                   │ RiskDiff: items[], summary_counts{}
                   ▼
┌────────────────────────────────────────────┐
│  Step 5 · TOOL · News Search               │  Tavily API (→ DDG fallback)
│  tavily-python + requests (no LLM)         │  Top 3 news per added/changed risk
└──────────────────┬─────────────────────────┘
                   │ news_evidence: {risk_id → [snippets]}
                   ▼
┌──────────────────────────────────────────────────┐
│  Step 6 · LLM · Synthesizer + Critic             │  Writes memo + confidence
│  model: gpt-4o                                   │  labels in single call
└──────────────────┬───────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
  outputs/TICKER_DATE.md   outputs/TICKER_DATE.state.json
  (investor memo)          (full typed state dump)
```

### Data dependency (why this is a real chain, not 4 independent calls)

Every step receives **structured output from the previous step** as input:

| Step | Receives from | As |
|------|--------------|-----|
| 2 | Step 1 | `TickerInfo.ticker` → CIK lookup |
| 3 | Step 2 | `edgar["latest_item_1a"]`, `edgar["prior_item_1a"]` |
| 4 | Step 3 | `ExtractedRisks.latest_year_risks[]`, `.prior_year_risks[]` |
| 5 | Step 4 | `RiskDiff.items` filtered to `added`/`materially_changed` |
| 6 | Steps 1–5 | Full `AgentState` (ticker, diff, news, metadata) |

---

## Why chaining beats a single prompt

1. **Context isolation per transformation.** Step 3 (extractor) receives raw
   HTML-stripped text and must atomize it. Step 4 (differ) receives clean
   JSON arrays and must reason about semantic equivalence across years. Giving
   Step 4 raw HTML instead of pre-extracted risks would require it to do two
   conceptually different tasks at once, degrading accuracy on both.

2. **Structured handoffs enforce schema contracts.** Each step's output is a
   Pydantic model with validated fields. The differ's input is guaranteed to be
   a typed list of `Risk` objects with stable IDs — not a raw text blob it needs
   to re-parse. This makes the semantic matching far more reliable and the
   results auditable.

3. **Tool calls interleave at the right moments.** Step 2 (EDGAR fetcher) and
   Step 5 (news search) are tool calls, not LLM calls — the LLM cannot browse
   EDGAR or access today's news. Chaining lets the tool results flow into
   subsequent LLM context as structured data rather than requiring a framework
   to manage tool dispatch and result injection.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   OPENAI_API_KEY   — from https://platform.openai.com
#   TAVILY_API_KEY   — from https://tavily.com (1000 free/month)
#   SEC_USER_AGENT   — "Your Name your.email@example.com"
```

### 3. Run

```bash
python agent.py AAPL
python agent.py tsla --debug    # dumps every LLM I/O to outputs/TSLA_DATE_debug.jsonl
python agent.py ZZZZ            # invalid ticker — graceful error demo
```

### Expected output

```
SEC 10-K Risk Factor Delta Agent
Input: 'AAPL'

[1/6] Normalizing ticker...                ✓ 2.1s
  → AAPL → Apple Inc.
[2/6] Fetching SEC filings...              ✓ 8.4s
  → Got 10-K for FY2024 and FY2023
[3/6] Extracting risk factors...           ✓ 18.2s
  → Latest: 29 risks, Prior: 27 risks
[4/6] Diffing risks across years...        ✓ 12.7s
  → +3 added, ~2 changed, -1 removed
[5/6] Searching news for new risks...      ✓ 4.1s
  → Found evidence for 4 of 5 risks
[6/6] Synthesizing memo...                 ✓ 14.9s

Done in 60.4s.
  Memo:  outputs/AAPL_2025-01-15.md
  State: outputs/AAPL_2025-01-15.state.json
```

---

## Sample Output

Below is a representative excerpt from a generated memo for Microsoft (MSFT):

```markdown
# Risk Profile Delta: Microsoft Corporation (MSFT)
## Filings compared: FY2023 → FY2024

## Executive Summary
Microsoft's risk profile expanded in FY2024, with three genuinely new risks
related to AI regulatory exposure, geopolitical data-residency requirements,
and integration risk from the Activision acquisition. The removal of the
"pandemic operational disruption" risk reflects normalization post-COVID.
Overall, the profile signals a company navigating rapid AI-driven growth with
commensurate governance and regulatory uncertainty.

## Added Risks

### AI Regulatory and Liability Exposure
Microsoft's expansion of AI products (Copilot, Azure OpenAI Service) exposes
it to emerging regulation under the EU AI Act and potential liability for
AI-generated outputs. The company cites the absence of established legal
precedent as a material risk.

**News evidence:**
- [EU AI Act signed into law, companies face compliance deadlines](https://...) —
  The regulation creates tiered compliance requirements for high-risk AI systems...
- [OpenAI lawsuit raises questions about AI vendor liability](https://...) —
  Legal experts say downstream vendors like Microsoft may face co-defendant risk...

**Evidence confidence: High**
*(2 corroborating sources from the last 6 months)*

...
```

---

## Known Failure Modes

| Scenario | Behavior |
|----------|----------|
| **Invalid ticker** (e.g., `ZZZZ`) | Step 2 raises `ValueError` with a helpful message. The CLI prints the error and exits cleanly — no Python traceback shown to the user. |
| **Recent IPO (one 10-K only)** | Step 2 sets `degraded_mode=True` and logs a warning. Step 4 skips the LLM differ and marks all risks as "added." The memo notes that no comparison is possible. |
| **Foreign filer (20-F, not 10-K)** | Step 2 finds zero 10-K filings and raises `ValueError`. Foreign companies (e.g., Toyota, ASML) file 20-F forms — a future version would add 20-F support. |
| **Delisted ticker** | The SEC ticker map may still contain the CIK, but filings may be from years ago. The memo will be generated but the news search is unlikely to find relevant recent coverage. |
| **EDGAR rate limit (429)** | Step 2 retries with exponential backoff (1s, 2s, 4s). After 3 retries it raises and the CLI exits with a clear message. |
| **Tavily quota exhausted** | Step 5 catches the 402/429 response, logs a warning to `state.errors`, and falls back to DuckDuckGo HTML scraping for the remaining risks. |
| **Item 1A regex fails** | If the filing's HTML structure doesn't match the regex anchors, Step 2 falls back to the first 60k characters of the document and logs a warning. The extraction in Step 3 may then be noisier. |

---

## Running Tests

```bash
cd sec-risk-delta-agent
pytest tests/ -v
```

Tests use `unittest.mock` to avoid hitting live APIs. No API keys required.

---

## What I'd Change With More Time

1. **20-F support.** Foreign filers use 20-F forms, which have different section
   numbering. Adding a filing-type detector and a separate Item 3D extractor
   would cover companies like Toyota, Shell, and ASML.

2. **Semantic chunking for large Item 1A sections.** Companies like JPMorgan
   have 60-page risk sections. The current approach truncates at 60k characters.
   A chunking strategy with summarization would prevent information loss.

3. **VCR-recorded HTTP fixtures.** The current tests mock at the function level.
   Recording actual SEC API responses with `vcrpy` would make the EDGAR fetcher
   tests more realistic and catch formatting changes in EDGAR's API.

4. **Confidence calibration.** The High/Medium/Low confidence rubric is
   rule-based (count of sources × recency). A better approach would train a
   small classifier on labeled examples of "news corroborates risk" vs. "news
   is tangential."

5. **Incremental state checkpointing.** For long runs, saving the state after
   each step would allow resuming from step N if step N+1 fails, avoiding
   re-spending API budget on steps that already succeeded.

6. **Multi-ticker batch mode.** For portfolio-level analysis, running the agent
   on a basket of tickers in parallel (with rate limiting) and producing a
   comparative heat map of risk profile changes.

---

## Project Structure

```
sec-risk-delta-agent/
├── agent.py                     # CLI entry point
├── state.py                     # AgentState Pydantic model + sub-models
├── llm_client.py                # Thin OpenAI wrapper (structured + text)
├── requirements.txt
├── .env.example
├── steps/
│   ├── step1_normalize.py       # LLM: ticker normalization
│   ├── step2_edgar_fetch.py     # TOOL: SEC EDGAR HTTP + Item 1A extraction
│   ├── step3_extract_risks.py   # LLM: atomize risks into JSON arrays
│   ├── step4_diff_risks.py      # LLM: semantic diff across years
│   ├── step5_news_search.py     # TOOL: Tavily / DuckDuckGo news search
│   └── step6_synthesize.py      # LLM: memo generation + confidence labels
├── prompts/
│   ├── normalize.md
│   ├── extract_risks.md
│   ├── extract_risks_v1_DEPRECATED.md   # first attempt + iteration notes
│   ├── diff_risks.md
│   └── synthesize.md
├── outputs/                     # generated memos and state dumps (gitignored)
└── tests/
    ├── conftest.py
    ├── test_edgar_fetch.py      # Step 2 unit tests (mocked HTTP)
    └── test_full_chain.py       # End-to-end with mocked LLM + tools
```
