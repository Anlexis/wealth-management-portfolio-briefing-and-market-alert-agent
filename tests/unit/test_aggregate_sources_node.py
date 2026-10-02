"""AggregateSourcesNode — the domain load boundary.

Every caller field is validated against explicit bounds here, and anything that
cannot be fully validated is REFUSED rather than degraded: the pipeline
downstream renders a client-facing document, and a briefing assembled from a
partially understood payload looks exactly like one assembled from a complete
payload.
"""

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.aggregate_sources_node import (
    MARKET_VALUE_BOUNDS,
    AggregateSourcesNode,
)
from src.schemas.state import from_json, to_json
from tests.fixtures import holding, request_with_holdings


def _run(payload_text, config=None):
    state = {"validated_input": payload_text}
    if config is not None:
        state["agent_config"] = to_json(config)
    return AggregateSourcesNode().execute(state)


def _aggregate(payload_text, config=None):
    result = _run(payload_text, config)
    assert "aggregated_portfolio" in result, result
    return from_json(result["aggregated_portfolio"])


class TestHappyPath:
    def test_holdings_are_normalized(self):
        agg = _aggregate(
            request_with_holdings(
                holding(symbol="aapl", weight=60.0, market_value=612345.0, return_pct=7.0),
                holding(symbol="GOVT", weight=40.0, market_value=400000.0, return_pct=3.0),
            )
        )
        assert [h["symbol"] for h in agg["holdings"]] == ["AAPL", "GOVT"]
        assert agg["totals"]["holding_count"] == 2
        assert agg["totals"]["total_market_value"] == 1_012_345.0
        assert agg["totals"]["total_weight"] == 100.0

    def test_benchmarks_are_normalized(self):
        agg = _aggregate(
            json.dumps(
                {
                    "holdings": [holding()],
                    "benchmarks": {"S&P 500": {"return_pct": "5.5"}, "agg": 2.0},
                }
            )
        )
        assert agg["benchmarks"] == {
            "s_p_500": {"return_pct": 5.5},
            "agg": {"return_pct": 2.0},
        }

    def test_ticker_alias_is_accepted(self):
        agg = _aggregate(
            json.dumps(
                {
                    "holdings": [{"ticker": "MSFT", "weight": 1, "value": 10, "return": 2}],
                }
            )
        )
        assert agg["holdings"][0]["symbol"] == "MSFT"
        assert agg["holdings"][0]["market_value"] == 10.0

    def test_as_of_date_is_carried(self):
        agg = _aggregate(request_with_holdings(holding(), as_of="2026-06-30"))
        assert agg["as_of"] == "2026-06-30"


class TestFailClosed:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            None,
            "not json",
            "[1,2,3]",
            '"a string"',
        ],
    )
    def test_unparseable_payload_is_refused(self, bad):
        """The previous free-text fallback turned an unreadable payload into a
        briefing that reported an empty portfolio."""
        result = _run(bad)
        assert result["status"] == AgentStatus.ERROR.value
        assert "aggregated_portfolio" not in result

    @pytest.mark.parametrize("holdings", [None, [], "", {}, "AAPL"])
    def test_missing_holdings_is_refused(self, holdings):
        result = _run(json.dumps({"holdings": holdings}))
        assert result["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize("field", ["weight", "market_value", "return_pct"])
    @pytest.mark.parametrize(
        "bad",
        [
            "NaN",
            "Infinity",
            "-Infinity",
            float("nan"),
            float("inf"),
            True,
            "abc",
            None,
            [],
            {},
        ],
    )
    def test_non_finite_numeric_field_is_refused(self, field, bad):
        result = _run(request_with_holdings(holding(**{field: bad})))
        assert result["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("weight", -1.0),
            ("weight", 101.0),
            ("market_value", -1.0),
            ("market_value", MARKET_VALUE_BOUNDS[1] + 1),
            ("return_pct", 10_001.0),
            ("return_pct", -10_001.0),
        ],
    )
    def test_out_of_range_numeric_field_is_refused(self, field, bad):
        result = _run(request_with_holdings(holding(**{field: bad})))
        assert result["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize(
        "bad",
        [
            "toolongsymbolvalue",
            "aa pl",
            "AA$PL",
            "",
            None,
            42,
            "a" * 13,
        ],
    )
    def test_invalid_symbol_is_refused(self, bad):
        result = _run(request_with_holdings(holding(symbol=bad)))
        assert result["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize("bad", ["30 June 2026", "2026/06/30", "2026-6-30", 20260630])
    def test_invalid_as_of_is_refused(self, bad):
        result = _run(request_with_holdings(holding(), as_of=bad))
        assert result["status"] == AgentStatus.ERROR.value

    def test_refusal_names_the_field_not_the_value(self):
        result = _run(request_with_holdings(holding(market_value="9999999999999999")))
        joined = " ".join(result["error_log"])
        assert "market_value" in joined
        assert "9999999999999999" not in joined


class TestStructuralCaps:
    def test_default_holding_cap_is_enforced(self):
        payload = request_with_holdings(*[holding() for _ in range(201)])
        assert _run(payload)["status"] == AgentStatus.ERROR.value

    def test_configured_cap_is_live(self):
        """A declared cap that did not change behaviour would be a dead
        declaration in the shipped configuration."""
        payload = request_with_holdings(*[holding() for _ in range(5)])
        assert "aggregated_portfolio" in _run(payload)
        tightened = _run(payload, {"limits": {"max_holdings": 3}})
        assert tightened["status"] == AgentStatus.ERROR.value

    def test_market_event_cap_is_enforced(self):
        events = [{"symbol": "AAPL", "headline": "x"} for _ in range(101)]
        payload = json.dumps({"holdings": [holding()], "market_events": events})
        assert _run(payload)["status"] == AgentStatus.ERROR.value

    def test_benchmark_cap_is_enforced(self):
        benchmarks = {f"b{i}": {"return_pct": 1.0} for i in range(51)}
        payload = json.dumps({"holdings": [holding()], "benchmarks": benchmarks})
        assert _run(payload)["status"] == AgentStatus.ERROR.value


class TestRenderedCallerText:
    def test_headline_is_length_capped(self):
        """A structural bound on what the pipeline carries. The flattening that
        stops a caller newline from manufacturing a document step belongs to
        the renderer, and is asserted there."""
        agg = _aggregate(
            json.dumps(
                {
                    "holdings": [holding()],
                    "market_events": [{"symbol": "AAPL", "headline": "x" * 500}],
                }
            )
        )
        assert len(agg["market_events"][0]["headline"]) == 160

    @pytest.mark.parametrize("bad", [None, 42, ["x"], {"a": 1}])
    def test_non_string_headline_becomes_empty(self, bad):
        agg = _aggregate(
            json.dumps(
                {
                    "holdings": [holding()],
                    "market_events": [{"symbol": "AAPL", "headline": bad}],
                }
            )
        )
        assert agg["market_events"][0]["headline"] == ""

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("earnings", "earnings"),
            ("EARNINGS", "earnings"),
            ("not-a-category", "general"),
            (None, "general"),
            (42, "general"),
        ],
    )
    def test_category_is_a_closed_set(self, value, expected):
        agg = _aggregate(
            json.dumps(
                {
                    "holdings": [holding()],
                    "market_events": [{"symbol": "AAPL", "headline": "x", "category": value}],
                }
            )
        )
        assert agg["market_events"][0]["category"] == expected


class TestAuditTrace:
    def test_refusal_and_success_both_emit_a_domain_event(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.nodes.aggregate_sources_node.emit_trace_event",
            lambda name, payload, state: seen.append(name),
        )
        _run(request_with_holdings(holding()))
        _run("not json")
        assert "sources_aggregated" in seen
        assert "portfolio_payload_refused" in seen
