# Prompt Design Appendix
## SEC 10-K Risk Factor Delta Agent

This appendix documents the full system and user prompts for every LLM step in the chain,
explains why each prompt is shaped the way it is, shows how each step's output format
constrains the next step's input, and includes a detailed account of one prompt iteration
that changed after testing (Step 3).

The chain has four LLM steps (Steps 1, 3, 4, 6) and two tool steps (Steps 2, 5).
Tool steps are not documented here because they make no LLM calls.

---

## Prompt loading architecture

Each `.md` file contains both human-readable documentation (rationale, constraint notes,
iteration history) and the actual system prompt sent to the LLM. The system prompt is
delimited by `<!-- SYSTEM_PROMPT_START -->` and `<!-- SYSTEM_PROMPT_END -->` HTML comment
sentinels. Each step's `.py` file uses a regex slice to extract only the operational prompt:

```python
_match = re.search(
    r'<!-- SYSTEM_PROMPT_START -->\n(.*?)<!-- SYSTEM_PROMPT_END -->',
    _raw, re.DOTALL
)
_SYSTEM = _match.group(1).strip() if _match else _raw
```

This ensures the LLM receives only its instructions — not developer notes like "Why this
prompt is shaped this way" or "make grading easy" — while keeping documentation and
operational prompts co-located in a single source-of-truth file. If the sentinels are ever
missing (e.g. a new prompt file that hasn't been wrapped yet), the fallback `else _raw` loads
the entire file, so nothing breaks silently.

---

## Step 1 — Normalizer (LLM)

### Role in the chain
Converts raw user input (which may be a ticker, a company name, a misspelling, or mixed
case) into a canonical `TickerInfo` Pydantic object before any external API is called.

### System prompt

```
You are a financial data normalizer. Your job is to convert a user-supplied stock identifier
into a canonical stock ticker and company name for US-listed securities.

Rules:
0. If the input is clearly not a stock identifier — it reads as a sentence, a question, a SQL
   query, a command, or any natural language phrase that has no plausible mapping to a
   US-listed equity — set ticker to "INVALID", company_name to "Unknown", and confidence to
   "low". Do not attempt to map it to a ticker. The downstream system will detect
   confidence=low and ticker=INVALID and abort with a user-friendly error.
- Normalize the ticker to uppercase with no whitespace.
- If the user supplied a company name instead of a ticker (e.g., "Tesla" → "TSLA"), resolve it.
- If you are highly confident in the mapping, set confidence to "high".
- If you made an educated guess (e.g., common name → ticker), set confidence to "medium".
- If the input is ambiguous or unclear, set confidence to "low" and use your best guess.
- Always return a valid TickerInfo JSON. Never refuse.
- Focus on US equity tickers listed on NYSE, NASDAQ, or AMEX.
- If the input looks like a non-US exchange code (contains a dot followed by a country suffix,
  e.g. "7203.T", "ASML.AS") set confidence to "low" and note in company_name that this
  appears to be a foreign listing not covered by SEC EDGAR.

Return ONLY a JSON object matching the TickerInfo schema.
```

### User prompt template

```
Normalize this user-supplied stock identifier: {user_input!r}

Return a TickerInfo JSON object.
```

### Why this prompt is shaped this way

The most important constraint is "Always return a valid TickerInfo JSON. Never refuse." Without
this, the LLM would return an explanatory sentence like "I cannot resolve this ticker" for
ambiguous input, which would cause a Pydantic parse failure and an unhelpful crash in Step 2.
By requiring the model to always return a structured object, error handling is pushed to the
`confidence` field: downstream code checks `confidence == "low"` and warns the user rather
than crashing.

The instruction to focus on US equities (NYSE, NASDAQ, AMEX) prevents the model from
returning valid-looking but wrong tickers for foreign companies — for example, "Toyota" should
return `TM` (the US ADR ticker), not `7203` (the Tokyo Stock Exchange code), because EDGAR
only indexes US-listed securities and their 10-K filings.

The temperature is set to 0.1 (the lowest meaningful value) to make normalization
deterministic: the same input should resolve to the same ticker on every run, which matters
when users retry a failed run or when tests mock this step.

### How this step's output constrains Step 2

Step 2 (the SEC EDGAR fetcher) calls `_resolve_cik(ticker)` using exactly
`state.ticker_info.ticker`. If this field contains any whitespace, lowercase letters, or a
company name rather than a symbol, the CIK map lookup silently fails. The prompt's "uppercase,
no whitespace" constraint is the contract that Step 2 relies on. Step 2 performs no further
normalization — it trusts Step 1's output completely.

**Output contract → Step 2 dependency:** `TickerInfo.ticker` (uppercase, no whitespace) is what
Step 2's `_resolve_cik()` does an exact-match dictionary lookup on. A lowercase or misspelled
value silently returns nothing from the SEC ticker map, so the normalizer's output format is
load-bearing, not cosmetic. The new `INVALID` sentinel value lets `agent.py` detect a failed
normalization and abort before hitting EDGAR.

---

## Step 3 — Risk Extractor (LLM) · Two calls, one per year

### Role in the chain
Receives the raw Item 1A text extracted from each 10-K filing (HTML-stripped, but still dense
regulatory prose) and atomizes it into a typed list of `Risk` objects: one concern per object,
with a stable ID, a short label, a brief summary, and a verbatim excerpt.

### System prompt

```
You are a financial risk analyst extracting structured data from SEC 10-K filings.

Your task: read the provided Item 1A (Risk Factors) text and return every distinct
risk factor as a structured JSON array.

Rules:
1. Each list item must represent ONE specific risk — not a multi-topic paragraph.
   Split compound risks into separate items.
2. `id`: lowercase snake_case, 3–6 words that uniquely name this risk within the
   filing (e.g., "supply_chain_china_dependency", "cybersecurity_data_breach").
   Use underscores, no spaces, no special characters.
3. `label`: exactly 1 line, maximum 12 words. Start with a noun phrase.
   Bad: "Risks related to our manufacturing operations in Asia and supply chain"
   Good: "Asia manufacturing supply chain concentration risk"
4. `summary`: 2–3 sentences. State the risk, its potential impact, and any
   mitigating context the company mentions. No longer than 60 words total.
5. `raw_excerpt`: 30–60 verbatim words from the filing that best express this
   risk. Do NOT paraphrase. Quote exactly.
6. Return ALL risks you find — do not omit minor ones.
7. Output only the JSON object matching ExtractedRisks schema. No preamble.

The response must be a valid ExtractedRisks JSON with `latest_year_risks` as the
array (use `latest_year_risks` regardless of which year this is; the caller will
route it correctly). Leave `prior_year_risks` as an empty array.
```

### User prompt template

```
Extract all atomic risk factors from this SEC 10-K Item 1A (Risk Factors)
section for fiscal year {year}. Return all results in the
`latest_year_risks` field and leave `prior_year_risks` as an empty array.

TEXT:
{item1a_text[:60000]}
```

### Why this prompt is shaped this way

**The 12-word label constraint** is the single most important element in this prompt. Without
it, the model produces verbose labels like "Risks related to our manufacturing operations in
Asia and supply chain vulnerability" (22 words). These long labels caused Step 4 (the differ)
to treat the same risk appearing in two years as different risks because the cosmetically
reworded wording looked different at the sentence level. Short noun-phrase labels like "Asia
manufacturing supply chain risk" make semantic matching in Step 4 far more reliable because
the model is comparing concise descriptors rather than full paragraphs.

**The `id` snake_case constraint** gives each risk a stable programmatic key. Step 5 (news
search) constructs queries using `risk.label` and stores results in `news_evidence[risk_id]`.
If IDs were free-form strings with spaces or special characters, the dictionary lookups in
Step 5 and Step 6 would silently miss results. The snake_case format also makes the final JSON
state file human-readable.

**The `raw_excerpt` field** is included because Step 5 uses the `label` for news search
queries, but the excerpt serves as an audit anchor: the evaluator can verify that the
extraction is grounded in the actual filing text rather than summarizing it freely. It also
anchors Step 6's memo language in authentic filing terminology rather than the model's
paraphrase.

**The single-year-per-call design** (two separate calls for latest and prior year) is
deliberate. A single call asking for both years at once consistently produced cross-year
contamination: the model would reference prior-year risks in the latest extraction and assign
inconsistent IDs, making the Step 4 diff unreliable. Isolating each year gives the model a
smaller, focused task with no opportunity for cross-contamination.

### How this step's output constrains Step 4

Step 4 receives two arrays of `Risk` objects as JSON. The differ's system prompt explicitly
says "Match semantically — do NOT match by string similarity." This only works because Step 3
has already reduced each risk to a single-concern, short-label object. If Step 4 received raw
Item 1A prose instead, it would need to simultaneously parse, de-duplicate, and compare — a
multi-objective task that degrades all three objectives. The atomic structure from Step 3 is
the prerequisite that makes Step 4's semantic matching tractable.

---

### Prompt iteration: Version 1 → Version 2

**Version 1 system prompt (deprecated)**

```
You are a financial analyst. Read the following SEC 10-K Item 1A section
(Risk Factors) and extract all the risk factors described.

For each risk factor, return:
- id: a short identifier for this risk
- label: a descriptive title for the risk factor
- summary: a paragraph summarizing the risk, its causes, and potential impact
- raw_excerpt: a representative quote from the filing

Return a JSON object with the key `latest_year_risks` containing a list of
all risks you found. Set `prior_year_risks` to an empty list.

Be comprehensive — capture every risk factor mentioned in the section.
```

**What version 1 produced (tested on TSLA FY2024)**

Labels ran 20–30 words:
```
"Risks related to competition in the electric vehicle market, including
 incumbent automakers and new entrants that may have greater resources"
```
Summaries ran 150–200 words. IDs were free-form phrases like
`"ev competition risk factors"` (with spaces) or `"Competition"` (not unique).

**What broke downstream**

When Step 4 received v1 output and tried to compare FY2023 and FY2024 risks, it treated
FY2023's `"EV competition risk"` as a *different* risk from FY2024's 22-word label, producing
a spurious `removed + added` pair instead of `unchanged`. This inflated the diff summary
counts — TSLA showed 14 added and 12 removed risks when the true count was 3 added, 2 changed,
and 1 removed.

**What changed in version 2**

Three specific additions fixed the problem:
1. `label: exactly 1 line, maximum 12 words` with a Bad/Good example pair.
2. `id: lowercase snake_case, 3–6 words` with explicit format rules.
3. `summary: No longer than 60 words total` to prevent bloated context in Step 4.

The Bad/Good label example was the decisive fix: the LLM understood the constraint immediately
once it saw a concrete contrast. The word-count constraint alone (without the example) reduced
label length to ~15 words on average, which was still too long for reliable matching. Adding
the example dropped the average to 6 words.

**Output contract → Steps 4–6 dependency:** Each `Risk` object carries a stable snake_case
`id` field that Step 4's differ uses as its cross-year pairing anchor. Without stable IDs,
Step 4 would have to re-identify risks from raw text, making the semantic diff unreliable. The
same IDs then flow through to Step 5 (news query key) and Step 6 (news join key) — they are
the spine of the entire downstream chain.

---

## Step 4 — Risk Differ (LLM)

### Role in the chain
Receives the two typed Risk arrays from Step 3 and produces a `RiskDiff` — a list of
`DiffItem` objects, each pairing a prior-year risk with a latest-year risk (or marking one
as added/removed) with a verdict and a rationale.

### System prompt

```
You are a risk analyst comparing two years of SEC 10-K Risk Factor sections for
the same company.

You will receive two JSON arrays:
- PRIOR YEAR RISKS: risk factors from last year's 10-K
- LATEST YEAR RISKS: risk factors from this year's 10-K

Your task: for every risk in either year, produce a DiffItem with a verdict.

Verdict definitions (pick exactly one per item):
- `added`: risk appears in latest year but has NO equivalent in prior year.
  The concern is genuinely new.
- `removed`: risk was in prior year but is absent from latest year. The company
  no longer considers it a material risk (or removed the disclosure).
- `materially_changed`: risk appears in BOTH years but the latest wording
  indicates a substantive change in scope, severity, named examples, or
  regulatory framing. Cosmetic rewording alone is NOT material change.
  When in doubt between `materially_changed` and `unchanged`, prefer
  `unchanged` and explain the similarity in the rationale field.
- `unchanged`: risk appears in both years with only cosmetic differences
  (sentence order, minor word substitution, formatting).

Rules:
1. Match semantically — do NOT match by string similarity. A risk about
   "autonomous driving liability" and "self-driving vehicle legal exposure" are
   the SAME risk. A risk about "EV battery supply" and "raw material sourcing"
   are different risks.
1b. Materiality threshold: a risk is `materially_changed` only if the
    latest wording introduces at least ONE of the following:
    - A NEW named regulation, law, or enforcement body not in the
      prior year (e.g. EU AI Act, FTC consent decree)
    - A NEW geographic scope or market (e.g. prior year said "US
      operations", latest adds "China operations")
    - A NEW financial magnitude or quantified exposure (e.g. prior
      year was qualitative, latest names a dollar figure or percentage)
    - A NEW named event or incident (e.g. a specific data breach,
      a named lawsuit, a product recall)
    Adding or removing one named example while the core concern is
    identical = `unchanged`, not `materially_changed`.
2. Every risk from BOTH arrays must appear in exactly one DiffItem.
3. For `added`: set latest_risk_id, leave prior_risk_id null.
4. For `removed`: set prior_risk_id, leave latest_risk_id null.
5. For `materially_changed` or `unchanged`: set BOTH IDs.
6. Use the exact `id` strings from the input arrays — do not invent new IDs.
7. `rationale`: 1–2 sentences explaining WHY you chose this verdict.
8. After listing all items, set summary_counts with keys:
   "added", "removed", "materially_changed", "unchanged" and integer values.

Return only a valid RiskDiff JSON. No preamble or explanation outside the JSON.
```

### User prompt template

```
Compare the risk factors from {prior_year} (prior) and {latest_year} (latest).

PRIOR YEAR RISKS ({prior_year}) — {prior_count} total:
{prior_risks_as_json}

LATEST YEAR RISKS ({latest_year}) — {latest_count} total:
{latest_risks_as_json}

IMPORTANT: All {prior_count} prior-year risks and all {latest_count}
latest-year risks must appear in your output. Do not skip or omit any risk.

Classify each risk as: added, removed, materially_changed, or unchanged.
Apply the materiality threshold from your instructions strictly: a risk is
materially_changed only if it introduces a new named regulation, new geography,
new financial magnitude, or new named incident. Cosmetic rewording = unchanged.
When in doubt, choose unchanged.

Return only a valid RiskDiff JSON. No preamble.
```

*(The count variables `{prior_count}` and `{latest_count}` are injected at runtime from
`len(extracted.prior_year_risks)` and `len(extracted.latest_year_risks)`. They anchor the
model's completeness obligation to the exact number of risks it received, preventing silent
omissions under context pressure.)*

### Why this prompt is shaped this way

**The explicit `materially_changed` vs `unchanged` distinction** is the hardest judgment call
in the entire chain. Without precise definitions, the model defaults to labelling almost every
risk that appears in both years as `materially_changed` — technically correct in the weak sense
that language always shifts slightly, but useless for an investor who wants to know what
*actually* changed. The definition "Cosmetic rewording alone is NOT material change" was added
after the first test run on AAPL produced 23 `materially_changed` items out of 29 risks,
when the ground-truth count was approximately 4.

**"Every risk from BOTH arrays must appear in exactly one DiffItem"** prevents silent omissions.
Without this constraint, the model tended to skip low-confidence matches (risks that were
similar but not identical), producing a diff that was incomplete. The completeness constraint
forces the model to take a position on every risk rather than silently dropping uncertain cases.

**"Use the exact `id` strings from the input arrays — do not invent new IDs"** is critical
because Step 5 builds its news search queries by looking up `latest_risk_id` in the Risk
lookup dictionary. If the differ invents a new ID (e.g. `ev_competition` instead of
`ev_competition_risk`), the lookup silently returns nothing and the risk gets no news evidence.

### How this step's output constrains Steps 5 and 6

Step 5 filters `risk_diff.items` to only those with `verdict in {"added",
"materially_changed"}` and uses each item's `latest_risk_id` to look up the full `Risk`
object's label for the search query. If the verdict enum is incorrect (e.g. a typo like
`"material_changed"` instead of `"materially_changed"`), those risks are silently skipped.
The prompt's explicit enum list and the Pydantic `Literal` type annotation enforce correctness
at parse time. Step 6 similarly groups items by verdict to build memo sections — the four
verdict values map directly to the four memo sections (added, changed, removed, unchanged is
omitted from the memo as it is not interesting to investors).

The hardest judgment call in this step is the boundary between `materially_changed` and
`unchanged`. Without an explicit materiality definition, pilot testing on TSLA FY2023→FY2024
showed the model marking cosmetic rewording as `materially_changed` in roughly 40% of cases —
for example, treating the addition of a single named example (a new regulatory body) to an
otherwise identical risk paragraph as a material change. The prompt now defines materiality
via four concrete triggers (new named regulation, new geography, new financial magnitude, new
named incident) and explicitly instructs the model to prefer `unchanged` when in doubt. The
`rationale` field in `DiffItem` is required — not optional — so the evaluator and end user can
verify every materiality judgment without re-reading both filings. This also makes the diff
auditable during the demo: any verdict can be challenged and the rationale serves as the
model's written justification.

**Output contract → Step 5 dependency:** `RiskDiff.items` filtered to `verdict == "added"` or
`verdict == "materially_changed"` is the exact input to Step 5's news query loop. Step 5 does
not search for unchanged or removed risks, so Step 4's verdict taxonomy directly controls how
many external API calls Step 5 makes and therefore the agent's total runtime and Tavily quota
consumption.

---

## Step 6 — Synthesizer + Critic (LLM)

### Role in the chain
Receives the entire accumulated agent state (ticker, filing years, full diff with rationales,
and news snippets per risk) and produces two things in a single call: the final investor-ready
markdown memo and a structured confidence assessment for each material risk.

### System prompt

```
You are a senior equity research analyst writing an investor-ready memo about
changes in a company's disclosed risk profile between two consecutive 10-K filings.

You will receive structured data: the company, filing years, added/changed/removed
risks, and recent news snippets for each added or changed risk.

Your output must be a SynthesisOutput JSON with two fields:

### Field 1: memo_markdown
Write a markdown document with EXACTLY these sections in this order:

  # Risk Profile Delta: [Company] ([Ticker])
  ## Filings compared: FY[prior] → FY[latest]

  ## Executive Summary
  [3 sentences: what changed overall, most important new risk, and overall
   direction of risk profile]

  ## Added Risks
  [For each added risk: heading with risk label, 2-sentence description, news
   evidence subsection with bullet points (title + 1-line snippet), confidence
   label]

  ## Materially Changed Risks
  [Same format as Added Risks. After the 2-sentence description, add
   a bold "What changed:" line on its own: one sentence only, stating
   the specific delta from the prior year filing.
   Example format:
   **What changed:** Prior year cited general cybersecurity risk;
   latest year names a specific ransomware incident and quantifies
   remediation cost at $X million.]

  ## Removed Risks
  [Bulleted list: risk label only. No news needed.]

  ## Methodology
  [2–3 sentences: what years were compared, how risks were extracted and diffed,
   why news evidence was gathered, limitations of this automated analysis]

Rules for memo_markdown:
- Investor-ready language — no jargon like "LLM" or "embedding".
- Do not make buy/sell recommendations.
- If a risk has no news evidence, write "No recent news corroborating this risk
  was found." as the news subsection.
- Each added/changed risk must show its confidence label inline:
  **Evidence confidence: High / Medium / Low**
- CRITICAL: Never invent, infer, or paraphrase news that is not
  explicitly present in the provided news_evidence data. The news
  snippets you receive are the ONLY permitted source for the news
  evidence subsection. If no snippets exist for a risk, write
  exactly: "No recent news corroborating this risk was found."
  Do not add any other sentence in that subsection.

### Field 2: confidence_assessments
A JSON list where each item is a ConfidenceAssessment for every ADDED or
MATERIALLY CHANGED risk.

Confidence criteria:
- `High`: 2+ news sources from the last 6 months that directly corroborate
  the risk being current and material.
- `Medium`: at least 1 news source corroborating, OR sources older than
  6 months, OR tangential coverage.
- `Low`: zero news sources found, OR sources that contradict or dismiss the risk.

`justification`: 1 sentence explaining the confidence label, citing source
count and recency where relevant.

Return only valid SynthesisOutput JSON. The memo_markdown must be complete —
do not truncate it.
```

### User prompt template

```
Using the structured data below, write the final investor-ready markdown memo
AND produce a confidence_assessments JSON list.

# Company: {company_name} ({ticker})
# Filing Years: {prior_year} → {latest_year}
# Diff Summary: {summary_counts_json}

## ADDED RISKS
### [{risk_id}] {risk_label}
Summary: {risk_summary}
News evidence:
  - [{title}]({url}) — {snippet}
  ...

## MATERIALLY CHANGED RISKS
### [{risk_id}] {risk_label}
Rationale for change: {diff_rationale}
Summary: {risk_summary}
News evidence: ...

## REMOVED RISKS
- [{risk_id}] {risk_label}

Return a SynthesisOutput with:
1. memo_markdown: the full formatted markdown memo
2. confidence_assessments: list of {risk_id, confidence, justification} for
   each added or materially_changed risk
```

*(The user prompt is built programmatically by `step6_synthesize._build_context()`
using the full AgentState. The template above shows the structure; actual values
come from the accumulated state.)*

### Why this prompt is shaped this way

**Combining memo generation and confidence labeling in a single call** avoids a seventh LLM
call. The confidence label requires the same news evidence that the memo is already assembling
into its news sections, so there is no information cost to combining them. A separate "critic"
call would receive the completed memo as input and re-read the same news snippets — redundant
work with higher latency and token cost.

**The rigid section order** (`Executive Summary → Added → Changed → Removed → Methodology`) is
mandated because the memo is written directly to disk as a self-contained document. An investor
reading it without context needs a consistent structure. Free-form section ordering would also
make it harder to test output correctness programmatically.

**"Investor-ready language — no jargon like 'LLM' or 'embedding'"** prevents the model from
leaking implementation details into the output. Early test runs without this constraint
produced memos containing phrases like "the language model extracted" and "embedding-based
matching was used" — language that would undermine credibility in a real investor document.

**The explicit confidence rubric** (High = 2+ sources in 6 months, Medium = 1 source or older,
Low = 0 sources) means the evaluator can verify each confidence label against the news snippets
in the state JSON without subjective debate. The rubric is rule-based by design: the agent is
not trying to infer whether the risk is *actually* material — only whether the news evidence
*corroborates* the disclosed risk.

**Temperature 0.2** is used here rather than the 0.1 used in Steps 1 and 4. Slight creative
latitude produces fluent, investor-ready prose; a temperature of 0.1 makes the memo mechanical
and formulaic. A temperature above 0.3 introduces hallucinated risk labels and fabricated
detail not present in the structured input data — so 0.2 is the intentional midpoint between
robotic and unreliable.

**The SynthesisOutput schema imposes a critical structural constraint:** `memo_markdown` must be
a single complete string, not a list of sections. `agent.py` writes the memo to disk with a
single `Path.write_text()` call — returning a list would require joining logic that does not
exist in the codebase and would silently produce a malformed output file.

**The anti-hallucination rule** (the CRITICAL instruction added to the prompt) is necessary
because in early testing the model would fill empty `news_evidence` slots with
plausible-sounding but fabricated headlines, making the confidence labels appear grounded when
they were not. The explicit rule reduced fabricated news citations to zero across all test runs
after it was added.

### How Step 5's output shapes this prompt

`news_evidence` is a dict keyed by the same `risk_id` values produced in Step 3 — this is the
join key that lets Step 6 attach the correct news snippets to each risk in the memo. Step 6
iterates over `news_evidence[risk_id]` for every added or materially changed risk; without
Step 5's structured output, confidence assessments would have no evidence base and the model
would fabricate citations. If the IDs were inconsistent across steps, the join would silently
succeed but every risk would render with "No recent news found", making the confidence labels
meaningless.

### What changed from v1

The original `synthesize` prompt did not include the `**What changed:**` line for materially
changed risks. This caused the memo to describe each risk's current state without ever stating
what was different from the prior year filing — making the delta analysis impossible to verify
without re-reading both filings. An investor reading the v1 memo could not determine whether
"cybersecurity risk" was the same concern as the prior year or a genuinely escalated disclosure;
the `**What changed:**` line makes the delta explicit and checkable.

### How this step's output enters the final deliverable

Step 6 is the terminal step. The `memo_markdown` field is written to
`outputs/{TICKER}_{DATE}.md`. The `confidence_assessments` list is stored in
`AgentState.confidence_assessments` and serialized into `outputs/{TICKER}_{DATE}.state.json`.
The full state JSON captures every intermediate result (TickerInfo, EDGAR metadata, all
extracted risks, the full diff with rationales, all news snippets) making the entire chain
auditable without re-running it.

---

## Summary: How each step's output format enables the next

| Step | Output format | Why that format is required by the next step |
|------|--------------|---------------------------------------------|
| 1 — Normalize | `TickerInfo` (ticker: str, confidence: Literal) | Step 2 passes `ticker` directly to the SEC CIK map lookup. Any non-uppercase or whitespace-containing string causes a silent miss. |
| 2 — EDGAR (tool) | `edgar` dict with `latest_item_1a`, `prior_item_1a` as strings | Step 3 interpolates these strings directly into its user prompt. The dict also carries `latest_year` / `prior_year` used to label the extraction. |
| 3 — Extract | Two `list[Risk]` with `id` (snake_case), `label` (≤12 words), `summary`, `raw_excerpt` | Step 4's semantic matching relies on short, atomic labels. Step 5 uses `id` as the dict key for `news_evidence` storage. |
| 4 — Differ | `RiskDiff` with `items: list[DiffItem]` each having `verdict` (Literal enum) and exact `id` references | Step 5 filters by `verdict` and looks up `latest_risk_id` in the risk lookup dict. Step 6 groups by verdict to build memo sections. |
| 5 — News (tool) | `news_evidence: dict[risk_id → list[{title, url, snippet, date}]]` | Step 6 iterates over this dict by risk_id to build the news subsections of the memo and to apply the confidence rubric. |
| 6 — Synthesize | `SynthesisOutput.memo_markdown` (str) + `confidence_assessments` (list) | Written directly to disk. No further processing. |
