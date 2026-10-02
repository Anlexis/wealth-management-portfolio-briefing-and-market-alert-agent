"""AgentCore Platform v1.0"""

# FIN-C2-068 — DomainWorkflowGraph (inner BaseGraph).
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full wealth-management portfolio-briefing domain workflow:
#
#   START -> aggregate_sources -> analyze_performance -> screen_events_and_alerts
#         -> generate_briefing -> END
#
# Called by PortfolioBriefingGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Configuration reaches the no-argument domain nodes through state: the outer
# graph loads config/config.yaml and hands it here as
# config["configurable"], and _extra_initial_state() seeds the sections into
# State as JSON strings (ADR-005 msgpack safety). The framework never passes a
# `config` argument to a node's execute(), so a node that read one would run on
# defaults it never declared.
#
# Rules enforced:
#   - Inherits BaseGraph (fully custom topology — no forced backbone)
#   - Implements all 7 BaseGraph ABC methods
#   - register_nodes() does NOT call super() (abstract in BaseGraph)
#   - register_nodes() instantiates every domain node with NO ctor args
#   - Does NOT register initialize / finalize (outer backbone concerns)
#   - get_output() designed together with PortfolioBriefingGraphNode.merge_output()

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.aggregate_sources_node import AggregateSourcesNode
from src.nodes.analyze_performance_node import AnalyzePerformanceNode
from src.nodes.generate_briefing_node import GenerateBriefingNode
from src.nodes.screen_events_and_alerts_node import ScreenEventsAndAlertsNode
from src.schemas.state import State, to_json

# Config sections seeded into State for the no-argument domain nodes.
_SEEDED_SECTIONS = ("alert_thresholds", "limits", "briefing", "analysis")


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for FIN-C2-068.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by PortfolioBriefingGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          -> aggregate_sources        (AggregateSourcesNode)        — unify portfolio + market data
          -> analyze_performance      (AnalyzePerformanceNode)      — performance vs benchmark
          -> screen_events_and_alerts (ScreenEventsAndAlertsNode)   — events + threshold alerts
          -> generate_briefing        (GenerateBriefingNode)        — render client briefing
          -> END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # -- Identity --------------------------------------------------------------

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "fin_c2_068_portfolio_briefing_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # -- Config validation -----------------------------------------------------

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        The configuration arrives as {"configurable": {...}} from the outer
        graph. Every section is optional — each node documents and applies its
        own defaults — so an absent section is not an error. A section present
        but of the wrong TYPE is, because that means the shipped configuration
        says something the pipeline cannot honour.
        """
        configurable = self.config.get("configurable")
        if configurable is None:
            return
        if not isinstance(configurable, dict):
            raise ValueError("DomainWorkflowGraph: config['configurable'] must be a mapping")
        for section in _SEEDED_SECTIONS:
            value = configurable.get(section)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"DomainWorkflowGraph: config['configurable']['{section}'] must be a mapping")

    # -- Node registration -----------------------------------------------------

    def register_nodes(self) -> None:
        """Register all 4 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.

        Every node is instantiated with NO constructor arguments; configuration
        flows in through State, seeded by _extra_initial_state(). Every key
        registered here is referenced in add_edges().
        """
        self._nodes["aggregate_sources"] = AggregateSourcesNode()
        self._nodes["analyze_performance"] = AnalyzePerformanceNode()
        self._nodes["screen_events_and_alerts"] = ScreenEventsAndAlertsNode()
        self._nodes["generate_briefing"] = GenerateBriefingNode()

    # -- Edge wiring -----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the linear portfolio-briefing domain topology.

        Each step passes its partial-dict output into the shared State.
        The topology is intentionally linear — no conditional branching between
        domain nodes. route() is implemented as the ABC requires, but
        add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "aggregate_sources")
        self._sg.add_edge("aggregate_sources", "analyze_performance")
        self._sg.add_edge("analyze_performance", "screen_events_and_alerts")
        self._sg.add_edge("screen_events_and_alerts", "generate_briefing")
        self._sg.add_edge("generate_briefing", END)

    # -- Routing ---------------------------------------------------------------

    def route(self, state: State) -> str:
        """Conditional routing — required by the BaseGraph ABC.

        Annotated with this graph's OWN State: the graph runtime reads a path
        callable's annotation as its input schema and projects away every field
        the annotation does not carry, so an annotation of the framework's bare
        AgentState would hide the domain fields a routing decision needs. This
        topology uses no conditional edge today; the annotation is correct now
        so that adding one later cannot silently route on absent fields.

        Returns END on error so an unexpected call does not re-enter a
        processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "generate_briefing"

    # -- Config / caller-context seeding ---------------------------------------

    def _extra_initial_state(self) -> "dict[str, Any]":
        """Seed runtime config and the caller contract into the inner state.

        Runs INSIDE subgraph.invoke(), which is the only place that can reach
        both. The framework does not forward input_context into a subgraph, so
        the validated caller contract is read back off the bridge here; the
        config sections are serialized (ADR-005 msgpack safety) for the
        no-argument domain nodes to read.
        """
        extra: dict[str, Any] = {}
        configurable = self.config.get("configurable") or {}
        agent_config = {
            section: configurable[section]
            for section in _SEEDED_SECTIONS
            if isinstance(configurable.get(section), dict) and configurable[section]
        }
        if agent_config:
            extra["agent_config"] = to_json(agent_config)
        extra["input_context"] = get_caller_input_context()
        return extra

    # -- Output shape ----------------------------------------------------------

    def get_output(self, state: State) -> "dict[str, Any]":
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by PortfolioBriefingGraphNode.merge_output() in
        graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()   emits: "portfolio_briefing", "status", ...
            Outer merge_output() reads: sub_result.get("portfolio_briefing"),
                                        sub_result.get("status")

        The additional fields are surfaced for observability and for a future
        outer-merge extension; merge_output() maps only portfolio_briefing and
        status into the outer state delta.
        """
        return {
            "portfolio_briefing": state.get("portfolio_briefing"),
            "status": state.get("status"),
            "error_log": state.get("error_log", []),
            "performance_analysis": state.get("performance_analysis"),
            "screened_events": state.get("screened_events"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
