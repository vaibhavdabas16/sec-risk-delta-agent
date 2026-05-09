"""End-to-end chain tests using mocked LLM and tool calls.

These tests exercise the full state-passing contract between steps
without hitting live APIs. Each step's output becomes the next step's input,
verifying that the data dependency chain is real and not just glued-together
independent calls.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SEC_USER_AGENT", "Test User test@example.com")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test")

from state import (
    AgentState,
    ConfidenceAssessment,
    DiffItem,
    ExtractedRisks,
    Risk,
    RiskDiff,
    TickerInfo,
)


# ── Shared fake data ──────────────────────────────────────────────────────────

FAKE_TICKER_INFO = TickerInfo(ticker="AAPL", company_name="Apple Inc.", confidence="high")

FAKE_EDGAR = {
    "cik": "0000320193",
    "latest_year": "2024",
    "prior_year": "2023",
    "latest_item_1a": "Item 1A Risk Factors... We face intense competition. Our supply chain is at risk.",
    "prior_item_1a": "Item 1A Risk Factors... We face intense competition.",
    "latest_filing_url": "https://www.sec.gov/Archives/edgar/data/320193/fake/10k.htm",
    "prior_filing_url": "https://www.sec.gov/Archives/edgar/data/320193/fake2/10k.htm",
    "degraded_mode": False,
}

FAKE_LATEST_RISKS = [
    Risk(id="competition_risk", label="Intense market competition risk", summary="Apple faces intense competition.", raw_excerpt="We face intense competition."),
    Risk(id="supply_chain_risk", label="Supply chain disruption risk", summary="Supply chain may be disrupted.", raw_excerpt="Our supply chain is at risk."),
]

FAKE_PRIOR_RISKS = [
    Risk(id="competition_risk_2023", label="Market competition risk", summary="Apple faces market competition.", raw_excerpt="We face intense competition."),
]

FAKE_EXTRACTED = ExtractedRisks(
    latest_year_risks=FAKE_LATEST_RISKS,
    prior_year_risks=FAKE_PRIOR_RISKS,
)

FAKE_DIFF = RiskDiff(
    items=[
        DiffItem(verdict="unchanged", latest_risk_id="competition_risk", prior_risk_id="competition_risk_2023", rationale="Same risk, minor rewording."),
        DiffItem(verdict="added", latest_risk_id="supply_chain_risk", prior_risk_id=None, rationale="New risk not present last year."),
    ],
    summary_counts={"added": 1, "removed": 0, "materially_changed": 0, "unchanged": 1},
)

FAKE_NEWS = {
    "supply_chain_risk": [
        {"title": "Apple supplier delays", "url": "https://example.com/1", "snippet": "Supply chain disrupted.", "published_date": "2024-03-01"},
    ]
}

FAKE_MEMO = "# Risk Profile Delta: Apple Inc. (AAPL)\n## Executive Summary\nRisk profile changed."
FAKE_CONFIDENCE = [ConfidenceAssessment(risk_id="supply_chain_risk", confidence="High", justification="Multiple sources.")]


# ── Helper: build a structured-output mock response ──────────────────────────

def _mock_llm_structured(return_value):
    """Returns a patcher that makes call_llm_structured return return_value."""
    return patch("llm_client.call_llm_structured", return_value=return_value)


# ── Step 1 tests ──────────────────────────────────────────────────────────────

class TestStep1Normalize:
    def test_sets_ticker_info_on_state(self):
        from steps import step1_normalize

        with _mock_llm_structured(FAKE_TICKER_INFO):
            state = AgentState(user_input="apple")
            result = step1_normalize.run(state)

        assert result.ticker_info is not None
        assert result.ticker_info.ticker == "AAPL"
        assert "step1_normalize" in result.step_durations_ms

    def test_user_input_passed_to_llm(self):
        from steps import step1_normalize

        captured = {}

        def fake_structured(system_prompt, user_prompt, response_model, **kwargs):
            captured["user_prompt"] = user_prompt
            return FAKE_TICKER_INFO

        with patch("llm_client.call_llm_structured", side_effect=fake_structured):
            state = AgentState(user_input="  Tesla  ")
            step1_normalize.run(state)

        assert "  Tesla  " in captured["user_prompt"]


# ── Step 2 tests (mocked at _get level) ───────────────────────────────────────

class TestStep2EdgarFetch:
    def test_requires_step1_output(self):
        from steps import step2_edgar_fetch

        state = AgentState(user_input="AAPL")
        # ticker_info is None → assertion error
        with pytest.raises(AssertionError):
            step2_edgar_fetch.run(state)

    def test_edgar_data_written_to_state(self):
        """Verify step2 writes the edgar dict with expected keys."""
        from steps import step2_edgar_fetch

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)

        with (
            patch.object(step2_edgar_fetch, "_resolve_cik", return_value="0000320193"),
            patch.object(step2_edgar_fetch, "_get_10k_filings", return_value=[
                {"form": "10-K", "date": "2024-01-30", "accession": "0000320193240001", "primary_doc": "aapl.htm"},
                {"form": "10-K", "date": "2023-11-05", "accession": "0000320193230001", "primary_doc": "aapl.htm"},
            ]),
            patch.object(step2_edgar_fetch, "_fetch_html", return_value="<p>Item 1A. Risk Factors</p><p>risk text</p><p>Item 1B.</p>"),
        ):
            result = step2_edgar_fetch.run(state)

        assert result.edgar is not None
        assert result.edgar["latest_year"] == "2024"
        assert result.edgar["prior_year"] == "2023"
        assert result.edgar["degraded_mode"] is False
        assert "step2_edgar_fetch" in result.step_durations_ms


# ── Step 3 tests ──────────────────────────────────────────────────────────────

class TestStep3ExtractRisks:
    def test_requires_step2_output(self):
        from steps import step3_extract_risks

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        with pytest.raises(AssertionError):
            step3_extract_risks.run(state)

    def test_populates_extracted_risks(self):
        from steps import step3_extract_risks

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR

        mock_output = ExtractedRisks(latest_year_risks=FAKE_LATEST_RISKS, prior_year_risks=[])

        with _mock_llm_structured(mock_output):
            result = step3_extract_risks.run(state)

        assert result.extracted_risks is not None
        assert len(result.extracted_risks.latest_year_risks) == 2

    def test_degraded_mode_skips_prior(self):
        from steps import step3_extract_risks

        edgar_degraded = dict(FAKE_EDGAR, degraded_mode=True, prior_item_1a="")
        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = edgar_degraded

        mock_output = ExtractedRisks(latest_year_risks=FAKE_LATEST_RISKS, prior_year_risks=[])
        call_count = {"n": 0}

        def fake_call(*args, **kwargs):
            call_count["n"] += 1
            return mock_output

        with patch("llm_client.call_llm_structured", side_effect=fake_call):
            result = step3_extract_risks.run(state)

        # Degraded mode: only 1 LLM call (latest only)
        assert call_count["n"] == 1
        assert result.extracted_risks.prior_year_risks == []


# ── Step 4 tests ──────────────────────────────────────────────────────────────

class TestStep4DiffRisks:
    def test_requires_step3_output(self):
        from steps import step4_diff_risks

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        with pytest.raises(AssertionError):
            step4_diff_risks.run(state)

    def test_diff_stored_on_state(self):
        from steps import step4_diff_risks

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = FAKE_EXTRACTED

        with _mock_llm_structured(FAKE_DIFF):
            result = step4_diff_risks.run(state)

        assert result.risk_diff is not None
        assert result.risk_diff.summary_counts["added"] == 1

    def test_degraded_mode_all_added(self):
        """With no prior risks, diff should mark everything as added without LLM."""
        from steps import step4_diff_risks

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = ExtractedRisks(
            latest_year_risks=FAKE_LATEST_RISKS,
            prior_year_risks=[],
        )

        with patch("llm_client.call_llm_structured") as mock_llm:
            result = step4_diff_risks.run(state)
            mock_llm.assert_not_called()

        assert result.risk_diff.summary_counts["added"] == 2
        assert all(i.verdict == "added" for i in result.risk_diff.items)

    def test_summary_counts_recomputed_from_items(self):
        """Verify counts are recomputed from actual items, not trusting LLM output."""
        from steps import step4_diff_risks

        # Craft a diff where LLM returned wrong counts
        bad_diff = RiskDiff(
            items=FAKE_DIFF.items,
            summary_counts={"added": 99, "removed": 0, "materially_changed": 0, "unchanged": 0},
        )

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = FAKE_EXTRACTED

        with _mock_llm_structured(bad_diff):
            result = step4_diff_risks.run(state)

        # Should be recomputed correctly
        assert result.risk_diff.summary_counts["added"] == 1
        assert result.risk_diff.summary_counts["unchanged"] == 1


# ── Step 5 tests ──────────────────────────────────────────────────────────────

class TestStep5NewsSearch:
    def test_requires_step4_output(self):
        from steps import step5_news_search

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        with pytest.raises(AssertionError):
            step5_news_search.run(state)

    def test_only_searches_added_and_changed(self):
        """Unchanged and removed risks should not trigger news searches."""
        from steps import step5_news_search

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = FAKE_EXTRACTED
        state.risk_diff = FAKE_DIFF

        search_calls = []

        def fake_tavily(query, api_key, debug_log):
            search_calls.append(query)
            return [{"title": "News", "url": "https://x.com", "snippet": "text", "published_date": "2024-01-01"}]

        with patch.object(step5_news_search, "_tavily_search", side_effect=fake_tavily):
            result = step5_news_search.run(state)

        # Only "added" risk should be searched (unchanged is excluded)
        assert len(search_calls) == 1
        assert "supply_chain" in search_calls[0].lower() or "Apple" in search_calls[0]

    def test_ddg_fallback_on_tavily_quota(self):
        from steps import step5_news_search

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = FAKE_EXTRACTED
        state.risk_diff = FAKE_DIFF
        os.environ["TAVILY_API_KEY"] = "tvly-test"

        def quota_exhausted(query, api_key, debug_log):
            raise RuntimeError("Tavily quota exhausted")

        ddg_results = [{"title": "DDG result", "url": "https://ddg.com", "snippet": "fallback", "published_date": ""}]

        with (
            patch.object(step5_news_search, "_tavily_search", side_effect=quota_exhausted),
            patch.object(step5_news_search, "_ddg_search", return_value=ddg_results),
        ):
            result = step5_news_search.run(state)

        assert any("quota" in e.lower() for e in result.errors)

    def test_empty_results_recorded_not_fatal(self):
        """Zero news results → empty list stored, no exception."""
        from steps import step5_news_search

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = FAKE_EXTRACTED
        state.risk_diff = FAKE_DIFF

        with patch.object(step5_news_search, "_tavily_search", return_value=[]):
            result = step5_news_search.run(state)

        assert result.news_evidence is not None
        assert result.news_evidence.get("supply_chain_risk") == []


# ── Step 6 tests ──────────────────────────────────────────────────────────────

class TestStep6Synthesize:
    from steps.step6_synthesize import SynthesisOutput  # type: ignore

    def test_requires_step4_output(self):
        from steps import step6_synthesize

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        with pytest.raises(AssertionError):
            step6_synthesize.run(state)

    def test_writes_memo_and_confidence(self):
        from steps import step6_synthesize
        from steps.step6_synthesize import SynthesisOutput

        fake_output = SynthesisOutput(
            memo_markdown=FAKE_MEMO,
            confidence_assessments=FAKE_CONFIDENCE,
        )

        state = AgentState(user_input="AAPL", ticker_info=FAKE_TICKER_INFO)
        state.edgar = FAKE_EDGAR
        state.extracted_risks = FAKE_EXTRACTED
        state.risk_diff = FAKE_DIFF
        state.news_evidence = FAKE_NEWS

        with _mock_llm_structured(fake_output):
            result = step6_synthesize.run(state)

        assert result.final_memo_md == FAKE_MEMO
        assert result.confidence_assessments is not None
        assert len(result.confidence_assessments) == 1
        assert result.confidence_assessments[0]["risk_id"] == "supply_chain_risk"


# ── Full chain integration test ───────────────────────────────────────────────

class TestFullChain:
    """Verify that each step's output is consumed by the next as real input."""

    def test_state_flows_through_all_six_steps(self):
        """Run all 6 steps with mocked LLM and tool calls.

        The key assertion: no step is given state that bypasses the previous
        step's output. We verify by checking that step N+1's input has data
        that step N wrote.
        """
        from steps import (
            step1_normalize,
            step2_edgar_fetch,
            step3_extract_risks,
            step4_diff_risks,
            step5_news_search,
            step6_synthesize,
        )
        from steps.step6_synthesize import SynthesisOutput

        state = AgentState(user_input="aapl")

        fake_synthesis = SynthesisOutput(
            memo_markdown=FAKE_MEMO,
            confidence_assessments=FAKE_CONFIDENCE,
        )

        with (
            patch("llm_client.call_llm_structured") as mock_llm,
            patch.object(step2_edgar_fetch, "_resolve_cik", return_value="0000320193"),
            patch.object(step2_edgar_fetch, "_get_10k_filings", return_value=[
                {"form": "10-K", "date": "2024-01-30", "accession": "0000320193240001", "primary_doc": "aapl.htm"},
                {"form": "10-K", "date": "2023-01-01", "accession": "0000320193230001", "primary_doc": "aapl.htm"},
            ]),
            patch.object(step2_edgar_fetch, "_fetch_html", return_value="<p>Item 1A. Risk Factors</p><p>risk</p><p>Item 1B.</p>"),
            patch.object(step5_news_search, "_tavily_search", return_value=[]),
        ):
            # Configure LLM mock to return appropriate types per call
            mock_llm.side_effect = [
                FAKE_TICKER_INFO,           # Step 1
                ExtractedRisks(             # Step 3 (latest)
                    latest_year_risks=FAKE_LATEST_RISKS,
                    prior_year_risks=[],
                ),
                ExtractedRisks(             # Step 3 (prior)
                    latest_year_risks=FAKE_PRIOR_RISKS,
                    prior_year_risks=[],
                ),
                FAKE_DIFF,                  # Step 4
                fake_synthesis,             # Step 6
            ]

            state = step1_normalize.run(state)
            assert state.ticker_info is not None, "Step 2 requires Step 1 output"

            state = step2_edgar_fetch.run(state)
            assert state.edgar is not None, "Step 3 requires Step 2 output"

            state = step3_extract_risks.run(state)
            assert state.extracted_risks is not None, "Step 4 requires Step 3 output"

            state = step4_diff_risks.run(state)
            assert state.risk_diff is not None, "Step 5 requires Step 4 output"

            state = step5_news_search.run(state)
            assert state.news_evidence is not None, "Step 6 requires Step 5 output"

            state = step6_synthesize.run(state)

        # Final state contains all outputs
        assert state.final_memo_md is not None
        assert state.confidence_assessments is not None
        assert len(state.step_durations_ms) == 6

        # State is JSON-serializable (required by spec)
        dumped = json.loads(state.to_json())
        assert dumped["ticker_info"]["ticker"] == "AAPL"
        assert dumped["final_memo_md"] is not None

    def test_invalid_ticker_fails_at_step2(self):
        """ZZZZ → Step 2 raises ValueError with helpful message, no traceback."""
        from steps import step1_normalize, step2_edgar_fetch

        state = AgentState(user_input="ZZZZ")
        with patch("llm_client.call_llm_structured", return_value=TickerInfo(ticker="ZZZZ", company_name="Unknown", confidence="low")):
            state = step1_normalize.run(state)

        with patch.object(step2_edgar_fetch, "_resolve_cik", side_effect=ValueError("Ticker 'ZZZZ' not found")):
            with pytest.raises(ValueError, match="ZZZZ"):
                step2_edgar_fetch.run(state)

    def test_state_json_serializable_after_full_run(self):
        """AgentState must be JSON-dumpable at any point in the chain."""
        state = AgentState(
            user_input="AAPL",
            ticker_info=FAKE_TICKER_INFO,
            edgar=FAKE_EDGAR,
            extracted_risks=FAKE_EXTRACTED,
            risk_diff=FAKE_DIFF,
            news_evidence=FAKE_NEWS,
            final_memo_md=FAKE_MEMO,
            confidence_assessments=[a.model_dump() for a in FAKE_CONFIDENCE],
        )
        json_str = state.to_json()
        parsed = json.loads(json_str)
        assert parsed["ticker_info"]["ticker"] == "AAPL"
        assert parsed["risk_diff"]["summary_counts"]["added"] == 1
