"""Nested two-layer graph composition, driven end to end.

Two cross-boundary guarantees are asserted here:
  (i)  routing — node_history must traverse PostProcessNode, so the output
       boundary is not bypassed;
  (ii) data flow — a realistic portfolio must surface in the briefing as REAL
       computed figures, never a degenerate default. The framework does not
       forward input_context into a subgraph, and it passes no `config`
       argument to a node's execute(), so both routes are proved end to end
       rather than at node level.
"""

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.context_bridge import get_caller_input_context, set_caller_input_context
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import (
    FinC2068Agent,
    Graph,
    PortfolioBriefingGraphNode,
    load_runtime_config,
)
from src.schemas.state import State, from_json, to_json

# Weighted return = (10*50 + 4*50)/100 = 7.0; benchmark 5.0 -> excess +2.0.
# Figures that can ONLY appear if the input flowed all the way through.
_PAYLOAD = json.dumps(
    {
        "as_of": "2026-09-30",
        "holdings": [
            {"symbol": "AAPL", "asset_class": "equity", "weight": 50.0, "market_value": 60000.0, "return_pct": 10.0},
            {"symbol": "GOVT", "asset_class": "bond", "weight": 50.0, "market_value": 40000.0, "return_pct": 4.0},
        ],
        "benchmarks": {"sp500": {"return_pct": 5.0}},
        "market_events": [
            {"symbol": "AAPL", "headline": "strong product cycle", "category": "guidance", "severity": "info"},
        ],
    },
    ensure_ascii=False,
)

# A loss-making, concentrated position, so both alert types are reachable and
# each threshold can be shown to move the document on its own.
_LOSS_PAYLOAD = json.dumps(
    {
        "holdings": [
            {"symbol": "AAPL", "asset_class": "equity", "weight": 30.0, "market_value": 60000.0, "return_pct": -12.0},
        ],
        "benchmarks": {"sp500": {"return_pct": 5.0}},
        "market_events": [],
    }
)


def _run(user_input: str, input_context=None) -> dict:
    agent = Graph(config=load_runtime_config())
    agent.compile()
    # The manifest's DECLARED entry contract is VERIFIED_EXTERNAL. Running the
    # suite at INTERNAL would pass while every real caller was refused.
    ctx = InvocationContext(
        session_id="compose",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="test-suite",
    )
    return agent.invoke(user_input, ctx=ctx, input_context=input_context or {})


class TestOuterGraphConstruction:
    def test_state_schema_is_state(self):
        assert FinC2068Agent().state_schema is State

    def test_compile_fills_all_backbone_slots(self):
        agent = FinC2068Agent(config=load_runtime_config())
        agent.compile()
        for slot in ("initialize", "pre_process", "main", "post_process", "finalize"):
            assert agent._nodes.get(slot) is not None, f"backbone slot not filled: {slot}"

    def test_runtime_config_file_is_loaded(self):
        config = load_runtime_config()
        assert config.get("max_retry") == 3
        assert config.get("alert_thresholds", {}).get("drawdown_pct") == -10.0

    def test_parent_config_forwards_every_declared_section(self):
        configurable = PortfolioBriefingGraphNode()._parent_config()["configurable"]
        assert set(configurable) >= {"alert_thresholds", "limits", "briefing", "analysis"}
        assert configurable["agent"]["max_retry"] == 3


class TestInnerGraphConstruction:
    def test_inner_graph_registers_four_domain_nodes(self):
        inner = DomainWorkflowGraph()
        inner.register_nodes()
        assert set(inner._nodes.keys()) == {
            "aggregate_sources",
            "analyze_performance",
            "screen_events_and_alerts",
            "generate_briefing",
        }

    def test_inner_graph_name_and_schema(self):
        inner = DomainWorkflowGraph()
        assert inner.name == "fin_c2_068_portfolio_briefing_workflow"
        assert inner.state_schema is State

    def test_route_is_annotated_with_this_graphs_own_state(self):
        """The graph runtime reads a path callable's annotation as its input
        schema and projects away every field the annotation does not carry."""
        assert DomainWorkflowGraph.route.__annotations__["state"] is State

    def test_extra_initial_state_seeds_config_and_context(self):
        inner = DomainWorkflowGraph(
            config={
                "configurable": {
                    "alert_thresholds": {"drawdown_pct": -3.0},
                }
            }
        )
        set_caller_input_context({"portfolio_ref": "acct_1"})
        extra = inner._extra_initial_state()
        assert from_json(extra["agent_config"])["alert_thresholds"]["drawdown_pct"] == -3.0
        assert extra["input_context"] == {"portfolio_ref": "acct_1"}

    @pytest.mark.parametrize("bad", ["x", 5, ["a"]])
    def test_malformed_config_section_is_rejected(self, bad):
        inner = DomainWorkflowGraph(config={"configurable": {"limits": bad}})
        with pytest.raises(ValueError):
            inner._validate_config()


class TestContextBridge:
    def test_bridge_round_trip(self):
        set_caller_input_context({"advisor_ref": "adv_9"})
        assert get_caller_input_context() == {"advisor_ref": "adv_9"}

    def test_bridge_defaults_to_empty(self):
        set_caller_input_context(None)
        assert get_caller_input_context() == {}


class TestEndToEndInvoke:
    def test_invoke_returns_success(self):
        result = _run(_PAYLOAD)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    def test_output_is_a_populated_briefing(self):
        output = _run(_PAYLOAD).get("output")
        assert isinstance(output, str) and output.strip()
        assert "Portfolio Briefing" in output
        assert "Performance vs Benchmark" in output

    def test_output_reflects_real_computed_figures(self):
        output = _run(_PAYLOAD).get("output", "")
        assert "7.0" in output, (
            "the weighted portfolio return is missing — the inner pipeline did "
            f"not receive the portfolio. output head: {output[:400]!r}"
        )
        assert "5.0" in output
        assert "outperformed" in output
        assert "**Holdings:** 2" in output

    def test_held_symbol_and_event_flow_through(self):
        output = _run(_PAYLOAD).get("output", "")
        assert "AAPL" in output
        assert "strong product cycle" in output

    def test_node_history_traverses_the_output_boundary(self):
        history = _run(_PAYLOAD).get("node_history", [])
        for cls_name in ("PreProcessNode", "PortfolioBriefingGraphNode", "PostProcessNode"):
            assert cls_name in history, f"node_history missing {cls_name} (output boundary bypassed?): {history}"

    def test_caller_context_reaches_the_inner_graph(self):
        """The framework invokes a subgraph WITHOUT forwarding input_context.
        Proving the bridge at node level would not show whether the value
        survives the boundary; this asserts it end to end."""
        output = _run(_PAYLOAD, {"portfolio_ref": "acct_7742"}).get("output", "")
        assert "`acct_7742`" in output

    def test_caller_threshold_changes_the_document(self):
        loose = _run(_LOSS_PAYLOAD, {"drawdown_alert_pct": -80.0}).get("output", "")
        tight = _run(_LOSS_PAYLOAD, {"drawdown_alert_pct": -1.0}).get("output", "")
        assert "drawdown" not in loose
        assert "drawdown" in tight

    def test_both_alert_types_are_independently_reachable(self):
        quiet = _run(_LOSS_PAYLOAD, {"drawdown_alert_pct": -80.0, "concentration_alert_pct": 99.0}).get("output", "")
        assert "No threshold-breach alerts" in quiet
        concentration_only = _run(_LOSS_PAYLOAD, {"drawdown_alert_pct": -80.0, "concentration_alert_pct": 10.0}).get(
            "output", ""
        )
        assert "concentration" in concentration_only
        assert "drawdown" not in concentration_only

    def test_free_text_is_refused_with_an_actionable_notice(self):
        """Non-JSON input used to be wrapped and briefed anyway, producing a
        document that reported zero holdings and a benchmark comparison the
        caller never supplied."""
        result = _run("Summarize this quarter's portfolio performance.")
        assert result.get("status") == AgentStatus.ERROR.value
        assert "REQUEST REJECTED" in (result.get("output") or "")

    def test_anonymous_caller_is_refused(self):
        agent = Graph(config=load_runtime_config())
        agent.compile()
        ctx = InvocationContext(session_id="anon", caller_trust_level=TrustLevel.ANONYMOUS)
        result = agent.invoke(_PAYLOAD, ctx=ctx)
        assert result.get("status") == AgentStatus.ERROR.value
        assert "Portfolio Briefing" not in (result.get("output") or "")


class TestStateRoundTrip:
    """Structured State fields are stored as JSON strings (msgpack safety)."""

    def test_dict_round_trip(self):
        original = {"holdings": [{"symbol": "AAPL", "weight": 30.0}], "totals": {"n": 1}}
        assert from_json(to_json(original)) == original

    def test_list_round_trip(self):
        original = [{"symbol": "X"}, {"symbol": "Y"}]
        assert from_json(to_json(original)) == original

    def test_none_passes_through(self):
        assert to_json(None) is None
        assert from_json(None) is None

    def test_from_json_default_on_empty(self):
        assert from_json(None, {}) == {}
        assert from_json("", []) == []

    def test_from_json_default_on_malformed(self):
        assert from_json("{not valid json", {"fallback": True}) == {"fallback": True}

    def test_to_json_is_str(self):
        assert isinstance(to_json({"a": 1}), str)
