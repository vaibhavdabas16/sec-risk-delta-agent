# Step 3: Risk Extractor — VERSION 1 (DEPRECATED)

## What changed and why this version was replaced

This was the first attempt at the extraction prompt. It produced risks that were:
- Too verbose: `label` fields ran 20–30 words, making the Step 4 differ's
  semantic matching unreliable (it would treat cosmetically reworded long labels
  as different risks when they were the same underlying concern).
- Not atomic: a single list item would bundle 2–3 distinct risks into one
  paragraph blob, causing the differ to miss individual risk movements.
- No stable ID: the first version asked the LLM to name risks with free-form
  phrases instead of enforcing snake_case, so IDs were not stable across runs.

**The fix:** added explicit `max 12 words` constraint on `label`, required
`id` to be snake_case, and added Bad/Good examples to the prompt. See
`extract_risks.md` for the current version.

---

## System prompt (v1 — DEPRECATED)

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

## Problems observed in testing

When tested on TSLA's FY2024 10-K, this prompt returned labels like:
  "Risks related to competition in the electric vehicle market, including
   incumbent automakers and new entrants that may have greater resources"

That 22-word label caused Step 4 to treat FY2023's shorter "EV competition risk"
as a *different* risk (verdict: removed + added) instead of recognizing it as the
same risk with cosmetic rewording (verdict: unchanged).

Also, summaries ran 150–200 words, bloating the Step 4 prompt well past the
point where the differ could reliably compare 30 risks from each year.
