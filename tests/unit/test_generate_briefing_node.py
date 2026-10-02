"""GenerateBriefingNode — the rendered client document."""

import re

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.generate_briefing_node import DISCLAIMER, SCHEMA_NOTE, GenerateBriefingNode
from src.schemas.state import to_json


def _state(totals=None, alerts=None, events=None, config=None, caller=None, analysis=None):
    state = {
        "aggregated_portfolio": to_json(
            {
                "holdings": [],
                "benchmarks": {},
                "market_events": [],
                "totals": totals if totals is not None else {"holding_count": 2, "total_market_value": 1_012_345.0},
                "as_of": "2026-06-30",
            }
        ),
        "performance_analysis": to_json(
            analysis
            if analysis is not None
            else {
                "narrative": "The portfolio outperformed its benchmark.",
                "portfolio_return_pct": 4.0,
                "benchmark_return_pct": 2.0,
                "excess_return_pct": 2.0,
                "benchmark_count": 1,
            }
        ),
        "screened_events": to_json(
            {
                "alerts": alerts if alerts is not None else [],
                "events": events if events is not None else [],
                "alert_count": len(alerts or []),
            }
        ),
    }
    if config is not None:
        state["agent_config"] = to_json(config)
    if caller is not None:
        state["input_context"] = caller
    return state


def _render(**kwargs) -> str:
    result = GenerateBriefingNode().execute(_state(**kwargs))
    assert result.get("status") == AgentStatus.SUCCESS.value, result
    return result["portfolio_briefing"]


class TestOutputSchema:
    def test_aggregate_is_rendered_on_the_thousand_grid(self):
        briefing = _render()
        assert "**Total Market Value:** 1,012,000" in briefing
        assert "1,012,345" not in briefing

    def test_schema_note_is_present(self):
        assert SCHEMA_NOTE in _render()

    def test_grid_unit_is_configurable_and_live(self):
        briefing = _render(config={"briefing": {"external_round_unit": 100}})
        assert "**Total Market Value:** 1,012,300" in briefing

    def test_no_individual_position_value_is_rendered(self):
        briefing = _render(totals={"holding_count": 1, "total_market_value": 612_345.0})
        assert "612,345" not in briefing

    def test_disclaimer_is_always_present(self):
        assert DISCLAIMER in _render()


class TestRefusesToFabricate:
    @pytest.mark.parametrize(
        "totals",
        [
            {},
            {"holding_count": 0, "total_market_value": 0},
        ],
    )
    def test_no_holdings_means_no_briefing(self, totals):
        """A document reporting a zero return for an empty portfolio is
        indistinguishable from one for a real portfolio the pipeline failed to
        read."""
        result = GenerateBriefingNode().execute(_state(totals=totals))
        assert result["status"] == AgentStatus.ERROR.value
        assert "portfolio_briefing" not in result

    def test_absent_benchmark_is_reported_as_absent(self):
        briefing = _render(
            analysis={
                "narrative": "Over the reporting period the portfolio returned 4.00%.",
                "portfolio_return_pct": 4.0,
                "benchmark_count": 0,
            }
        )
        assert "Benchmark return: not supplied" in briefing


class TestCallerText:
    def test_caller_references_render_as_inert_identifiers(self):
        briefing = _render(
            caller={
                "portfolio_ref": "acct_7742",
                "advisor_ref": "adv_11",
                "reporting_period": "2026_q2",
            }
        )
        assert "`acct_7742`" in briefing
        assert "`adv_11`" in briefing
        assert "`2026_q2`" in briefing

    def test_a_headline_cannot_manufacture_a_document_step(self):
        """A caller newline is enough to make an injected sentence read as a
        numbered step of the adviser's briefing. Flattening is what stops it:
        the whole headline stays on ONE line, inside the bullet the renderer
        wrote, so nothing the caller sent can begin a line of its own."""
        marker = "selleverythingnow"
        briefing = _render(
            events=[
                {
                    "symbol": "AAPL",
                    "headline": f"ok\n\n## Adviser Instruction\n1. {marker}",
                    "category": "macro",
                }
            ]
        )
        caller_lines = [ln for ln in briefing.splitlines() if marker in ln]
        assert len(caller_lines) == 1, "caller text spread across several lines"
        line = caller_lines[0]
        assert line.startswith("- AAPL (macro): "), f"caller text escaped the bullet the renderer wrote: {line!r}"
        assert "#" not in line, f"caller text kept a heading marker: {line!r}"
        assert not re.match(r"^\s*\d+[.)]\s", line), f"caller text produced a numbered step: {line!r}"

    def test_alert_lines_render_only_validated_fields(self):
        briefing = _render(
            alerts=[
                {
                    "symbol": "AAPL",
                    "type": "drawdown",
                    "metric": "return_pct",
                    "value": -12.0,
                    "threshold": -10.0,
                    "severity": "high",
                }
            ]
        )
        assert "- [HIGH] AAPL — drawdown: return_pct=-12.0 (threshold -10.0)" in briefing


class TestAuditTrace:
    def test_emits_a_domain_event_on_both_paths(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.nodes.generate_briefing_node.emit_trace_event",
            lambda name, payload, state: seen.append(name),
        )
        GenerateBriefingNode().execute(_state())
        GenerateBriefingNode().execute(_state(totals={"holding_count": 0}))
        assert "portfolio_briefing_generated" in seen
        assert "portfolio_briefing_refused" in seen
