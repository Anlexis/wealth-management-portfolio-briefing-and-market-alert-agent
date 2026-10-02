"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# WARNING — ADR-005 (msgpack safety): structured fields (dict / list[dict]) are
# stored as JSON STRINGS, not bare Python containers — a bare dict/list in a
# checkpointed State field is a CoE gate-state-safety violation. Producers
# serialize with to_json() on write; consumers deserialize with from_json()
# on read.
#
# FIN-C2-068 — Wealth Management Portfolio Briefing Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Advisory-only note: this template produces an informational client briefing.
# It NEVER executes trades and NEVER issues a personalized buy/sell
# recommendation. All output is decision-support material for a licensed
# adviser.
#
# PII / client-confidentiality note: client-identifying data (client name,
# account number, contact details) is stripped by PreProcessNode (S-1/S-2)
# before any field is written to State. Downstream domain nodes never see raw
# client identifiers, and no client PII is persisted to the checkpoint DB
# (config.pii_log_disabled = true). Holdings are referenced by anonymized
# instrument symbol only, never by owner.

import json
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (ADR-005 msgpack safety).

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input -> the supplied ``default`` so a missing or
    corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for FIN-C2-068.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode / PortfolioBriefingGraphNode.merge_output
    # ------------------------------------------------------------------

    # PII-stripped portfolio + market-data payload produced by PreProcessNode
    # (S-2). Raw input (and any client identifier) is NOT persisted beyond
    # PreProcessNode.
    validated_input: Optional[str]

    # JSON STRING (to_json) of the VALIDATED caller contract taken from
    # input_context by PreProcessNode: portfolio_ref / advisor_ref /
    # reporting_period (inert identifiers) and the optional alert-threshold
    # overrides (finite, bounded numbers). Carried to the inner graph over
    # src/graph/context_bridge.py, because the framework does not forward
    # input_context into a subgraph.
    caller_fields: Optional[str]

    # JSON STRING (to_json) of the runtime configuration sections seeded by
    # DomainWorkflowGraph._extra_initial_state(). The framework passes no
    # `config` argument to a node's execute(), so this is how a no-argument
    # domain node reads config/config.yaml.
    agent_config: Optional[str]

    # Final client portfolio briefing (Markdown / structured text). Written by
    # GenerateBriefingNode (inner graph); surfaced via merge_output -> result.
    portfolio_briefing: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # AggregateSourcesNode output
    # JSON STRING (to_json) of the unified portfolio + market view assembled
    # from the (already PII-stripped) sources. Deserialised dict shape:
    # {"holdings": [{"symbol": str, "asset_class": str, "weight": float,
    #               "market_value": float, "return_pct": float, ...}],
    #  "benchmarks": {"<name>": {"return_pct": float, ...}},
    #  "as_of": str, "market_events": [{...}], "totals": {...}}
    # Consumers (AnalyzePerformanceNode / ScreenEventsAndAlertsNode /
    # GenerateBriefingNode) read it back via from_json().
    aggregated_portfolio: Optional[str]

    # AnalyzePerformanceNode output
    # JSON STRING (to_json) of the performance-vs-benchmark analysis.
    # Deserialised dict shape:
    # {"narrative": str, "portfolio_return_pct": float,
    #  "benchmark_return_pct": float, "excess_return_pct": float,
    #  "per_holding": [{"symbol": str, "return_pct": float,
    #                  "vs_benchmark_pct": float, "contribution_pct": float}],
    #  "top_contributors": [...], "top_detractors": [...]}
    # Consumer (GenerateBriefingNode) reads it via from_json().
    performance_analysis: Optional[str]

    # ScreenEventsAndAlertsNode output
    # JSON STRING (to_json) of holding-relevant market events + threshold-breach
    # alerts. Deserialised dict shape:
    # {"events": [{"symbol": str, "headline": str, "category": str,
    #             "severity": str}],
    #  "alerts": [{"symbol": str, "type": str, "metric": str,
    #             "value": float, "threshold": float, "severity": str}],
    #  "alert_count": int}
    # Consumer (GenerateBriefingNode) reads it via from_json().
    screened_events: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
