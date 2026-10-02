"""AnalyzePerformanceNode — performance attribution from validated figures."""

import pytest

from src.nodes.analyze_performance_node import (
    DEFAULT_SYSTEM_PROMPT,
    AnalyzePerformanceNode,
)
from src.schemas.state import from_json, to_json


def _aggregate(holdings, benchmarks=None):
    return to_json(
        {
            "holdings": holdings,
            "benchmarks": benchmarks if benchmarks is not None else {},
            "market_events": [],
            "totals": {"holding_count": len(holdings)},
            "as_of": "",
        }
    )


def _run(holdings, benchmarks=None, config=None):
    state = {"aggregated_portfolio": _aggregate(holdings, benchmarks)}
    if config is not None:
        state["agent_config"] = to_json(config)
    return from_json(AnalyzePerformanceNode().execute(state)["performance_analysis"])


_TWO = [
    {"symbol": "AAPL", "weight": 60.0, "return_pct": 10.0, "market_value": 600.0},
    {"symbol": "GOVT", "weight": 40.0, "return_pct": -5.0, "market_value": 400.0},
]


class TestArithmetic:
    def test_weighted_return(self):
        analysis = _run(_TWO, {"sp500": {"return_pct": 2.0}})
        # (10*60 + -5*40) / 100 = 4.0
        assert analysis["portfolio_return_pct"] == 4.0
        assert analysis["benchmark_return_pct"] == 2.0
        assert analysis["excess_return_pct"] == 2.0

    def test_contribution_uses_the_fractional_weight(self):
        """`weight` is a PERCENTAGE of the portfolio — the same unit the
        concentration alert compares against. Multiplying two percentages
        directly reported a 60% position returning 10% as a +600%
        contribution."""
        analysis = _run(_TWO, {"sp500": {"return_pct": 2.0}})
        by_symbol = {h["symbol"]: h["contribution_pct"] for h in analysis["per_holding"]}
        assert by_symbol == {"AAPL": 6.0, "GOVT": -2.0}

    def test_the_number_moves_with_the_input(self):
        low = _run(
            [{"symbol": "AAPL", "weight": 100.0, "return_pct": 1.0, "market_value": 1.0}], {"b": {"return_pct": 1.0}}
        )
        high = _run(
            [{"symbol": "AAPL", "weight": 100.0, "return_pct": 40.0, "market_value": 1.0}], {"b": {"return_pct": 1.0}}
        )
        assert low["portfolio_return_pct"] == 1.0
        assert high["portfolio_return_pct"] == 40.0
        assert low["excess_return_pct"] < high["excess_return_pct"]

    def test_equal_weighting_fallback_when_no_weights(self):
        analysis = _run(
            [
                {"symbol": "A", "weight": 0.0, "return_pct": 10.0, "market_value": 1.0},
                {"symbol": "B", "weight": 0.0, "return_pct": 20.0, "market_value": 1.0},
            ]
        )
        assert analysis["portfolio_return_pct"] == 15.0

    def test_top_lists_are_ranked_and_signed(self):
        analysis = _run(_TWO, {"sp500": {"return_pct": 2.0}})
        assert [h["symbol"] for h in analysis["top_contributors"]] == ["AAPL"]
        assert [h["symbol"] for h in analysis["top_detractors"]] == ["GOVT"]


class TestNarrative:
    def test_no_benchmark_means_no_comparison_claim(self):
        """Claiming the portfolio "outperformed its benchmark" when no
        benchmark was supplied is a fabrication a reader cannot detect."""
        analysis = _run(_TWO)
        assert analysis["benchmark_count"] == 0
        assert "outperformed" not in analysis["narrative"]
        assert "underperformed" not in analysis["narrative"]
        assert "No benchmark was supplied" in analysis["narrative"]

    def test_benchmark_present_gives_a_direction(self):
        analysis = _run(_TWO, {"sp500": {"return_pct": 2.0}})
        assert "outperformed" in analysis["narrative"]

    def test_narrative_carries_the_advisory_disclaimer(self):
        analysis = _run(_TWO, {"sp500": {"return_pct": 2.0}})
        assert "not a recommendation to buy or sell" in analysis["narrative"]


class TestPromptGovernance:
    def test_default_prompt_is_used_without_config(self):
        node = AnalyzePerformanceNode()
        result = node.execute({"aggregated_portfolio": _aggregate(_TWO)})
        assert "performance_analysis" in result
        assert DEFAULT_SYSTEM_PROMPT.startswith("You are a wealth-management")

    def test_declared_prompt_is_read_from_state(self, monkeypatch):
        """The framework passes no `config` argument to execute(); the seeded
        state is the only live route, and this asserts it is actually read."""
        seen = {}
        import src.nodes.analyze_performance_node as mod

        original = mod._build_narrative

        def spy(*args):
            seen["prompt"] = args[-1]
            return original(*args)

        monkeypatch.setattr(mod, "_build_narrative", spy)
        _run(_TWO, {"sp500": {"return_pct": 2.0}}, config={"analysis": {"system_prompt": "  house style prompt  "}})
        assert seen["prompt"] == "house style prompt"

    @pytest.mark.parametrize("declared", [None, "", "   ", 42, {"a": 1}])
    def test_unusable_declared_prompt_falls_back(self, monkeypatch, declared):
        seen = {}
        import src.nodes.analyze_performance_node as mod

        original = mod._build_narrative
        monkeypatch.setattr(
            mod,
            "_build_narrative",
            lambda *a: (seen.setdefault("prompt", a[-1]), original(*a))[1],
        )
        _run(_TWO, config={"analysis": {"system_prompt": declared}})
        assert seen["prompt"] == DEFAULT_SYSTEM_PROMPT


class TestAuditTrace:
    def test_emits_a_domain_event(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.nodes.analyze_performance_node.emit_trace_event",
            lambda name, payload, state: seen.append(name),
        )
        _run(_TWO)
        assert "performance_analyzed" in seen
