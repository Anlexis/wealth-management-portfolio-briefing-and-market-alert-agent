"""AgentCore Platform v1.0"""

# FIN-C2-068 — GenerateBriefingNode
# Inner-graph domain node 4 (terminal): assemble the aggregated portfolio, the
# performance-vs-benchmark analysis, and the screened events/alerts into a
# single client-ready briefing (Markdown).
#
# Output schema. The briefing reports monetary AGGREGATES ONLY, rounded to the
# nearest 1,000, and never an individual position's exact value. The rounding
# is applied here, at the point of rendering, and INDEPENDENTLY ENFORCED at the
# output boundary by PostProcessNode — a render that forgot the grid would
# otherwise ship full-precision client holdings.
#
# Every caller string that reaches this document is either an inert identifier
# (instrument symbols, the caller's own references) or has been flattened to a
# single structure-free line (event headlines). Free caller text in a rendered
# document is caller-controlled output injection: a newline alone is enough to
# make an injected sentence read as a numbered step of the adviser's briefing.
#
# The advisory-only disclaimer is rendered here so it is always present on the
# surfaced output.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json
from src.services.security import flatten_render_text


# The Marketplace runner seeds input_context with its own conversation history on every
# invocation (shared/bootstrap/marketplace_app.py); the caller neither sends that key nor can
# suppress it, and the build_input_context hook can only overwrite its value, never remove it.
# It is platform plumbing rather than caller data, so it is dropped here, before the caller
# contract runs: the unknown-field guard below stays strict for everything a caller can
# actually send, and no value screen is ever asked to judge a transcript that contains this
# agent's own earlier answers. The value may also be None, which this tolerates.
_PLATFORM_CONTEXT_KEYS = frozenset({"conversation_history"})


def _without_platform_context(raw: Any) -> Any:
    """The caller-supplied half of input_context, platform-injected keys removed."""
    if not isinstance(raw, dict):
        return raw
    return {k: v for k, v in raw.items() if k not in _PLATFORM_CONTEXT_KEYS}


logger = logging.getLogger(__name__)

DEFAULT_ROUND_UNIT = 1000

SCHEMA_NOTE = (
    "Monetary figures are reported as aggregates rounded to the nearest 1,000; "
    "individual position values are not reported."
)

DISCLAIMER = (
    "This briefing is informational decision-support material prepared for a "
    "licensed adviser. It is NOT personalized investment advice and NOT a "
    "recommendation to buy or sell any security. No trade is executed by this "
    "agent. Past performance does not guarantee future results."
)


def _round_unit(config: "Dict[str, Any]") -> int:
    """Read the external rounding grid from config, falling back to the default."""
    briefing_cfg = config.get("briefing", {})
    if isinstance(briefing_cfg, dict):
        try:
            unit = int(briefing_cfg.get("external_round_unit", DEFAULT_ROUND_UNIT))
        except (TypeError, ValueError):
            return DEFAULT_ROUND_UNIT
        if unit > 0:
            return unit
    return DEFAULT_ROUND_UNIT


def _on_grid(value: float, unit: int) -> str:
    """Render a monetary aggregate on the approved external grid."""
    snapped = round(float(value) / unit) * unit
    return f"{snapped:,d}"


def _format_alerts(alerts: "List[Dict[str, Any]]") -> str:
    """Render the alert list as Markdown bullet lines.

    Only validated fields are interpolated: an inert instrument symbol, a
    closed-set type/metric/severity, and bounded numbers.
    """
    if not alerts:
        return "No threshold-breach alerts were raised this period."
    lines = ["The following alerts were raised:"]
    for alert in alerts:
        lines.append(
            f"- [{str(alert.get('severity', 'info')).upper()}] "
            f"{alert.get('symbol', 'UNKNOWN')} — {alert.get('type', 'alert')}: "
            f"{alert.get('metric', '')}={alert.get('value', '')} "
            f"(threshold {alert.get('threshold', '')})"
        )
    return "\n".join(lines)


def _format_events(events: "List[Dict[str, Any]]") -> str:
    """Render the relevant-events list as Markdown bullet lines.

    The headline is the one free-text field a caller controls that reaches the
    reader, and it is flattened HERE — at the point where it becomes a line of
    a document. A caller newline would otherwise split the headline into what
    reads as a separate numbered step of the adviser's briefing.
    """
    if not events:
        return "No holding-relevant market events were flagged this period."
    lines = ["Holding-relevant market events:"]
    for event in events:
        headline = flatten_render_text(event.get("headline", ""))
        lines.append(f"- {event.get('symbol', 'UNKNOWN')} ({event.get('category', 'general')}): {headline}")
    return "\n".join(lines)


def _reference_line(caller_fields: "Dict[str, Any]") -> str:
    """Render the caller's own references, which are inert identifiers."""
    parts = [
        f"{label}: `{caller_fields[key]}`"
        for key, label in (
            ("portfolio_ref", "Portfolio"),
            ("advisor_ref", "Adviser"),
            ("reporting_period", "Period"),
        )
        if caller_fields.get(key)
    ]
    return "  |  ".join(parts)


class GenerateBriefingNode(FunctionNode):
    """Generate the client portfolio briefing (terminal node).

    Input state keys:
        aggregated_portfolio: unified portfolio view (from AggregateSourcesNode)
        performance_analysis: performance-vs-benchmark analysis
        screened_events:      events + alerts
        agent_config:         seeded runtime configuration (JSON string)
        input_context:        validated caller contract (bridged)

    Output state keys (partial dict):
        portfolio_briefing: rendered Markdown briefing string
        status:             AgentStatus.SUCCESS or AgentStatus.ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "Dict[str, Any]":
        aggregated: Dict[str, Any] = from_json(state.get("aggregated_portfolio"), {}) or {}
        analysis: Dict[str, Any] = from_json(state.get("performance_analysis"), {}) or {}
        screened: Dict[str, Any] = from_json(state.get("screened_events"), {}) or {}
        config: Dict[str, Any] = from_json(state.get("agent_config"), {}) or {}
        caller_fields = _without_platform_context(state.get("input_context", {})) or {}
        if not isinstance(caller_fields, dict):
            caller_fields = {}

        totals = aggregated.get("totals", {}) or {}
        alerts = screened.get("alerts", []) or []
        events = screened.get("events", []) or []
        unit = _round_unit(config)

        if not totals.get("holding_count"):
            # Nothing to report on. Refusing beats rendering a document that
            # states a zero return and a benchmark comparison the caller never
            # supplied — an adviser cannot tell that apart from a real briefing.
            emit_trace_event(
                "portfolio_briefing_refused",
                {"reason": "no validated holdings"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateBriefingNode: no validated holdings to report on."],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. "
                + ("GenerateBriefingNode: no validated holdings to report on."),
            }

        reference_line = _reference_line(caller_fields)
        as_of = aggregated.get("as_of", "")

        lines: List[str] = []
        lines.append("# Portfolio Briefing")
        lines.append("")
        lines.append(f"**Generated:** {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        if as_of:
            lines.append(f"**Positions as of:** {as_of}")
        if reference_line:
            lines.append(f"**References:** {reference_line}")
        lines.append(
            f"**Holdings:** {totals.get('holding_count', 0)}  |  "
            f"**Total Market Value:** {_on_grid(totals.get('total_market_value', 0), unit)}"
        )
        lines.append("")
        lines.append(f"> {SCHEMA_NOTE}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Performance vs Benchmark")
        lines.append("")
        lines.append(str(analysis.get("narrative", "")))
        lines.append("")
        lines.append(f"- Portfolio return: {analysis.get('portfolio_return_pct', 0)}%")
        if analysis.get("benchmark_count"):
            lines.append(f"- Benchmark return: {analysis.get('benchmark_return_pct', 0)}%")
            lines.append(f"- Excess return: {analysis.get('excess_return_pct', 0)}%")
        else:
            lines.append("- Benchmark return: not supplied")
        lines.append("")
        lines.append("## Event Screening & Alerts")
        lines.append("")
        lines.append(_format_alerts(alerts))
        lines.append("")
        lines.append(_format_events(events))
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"*{DISCLAIMER}*")
        portfolio_briefing = "\n".join(lines)

        emit_trace_event(
            "portfolio_briefing_generated",
            {
                "holding_count": totals.get("holding_count", 0),
                "alert_count": len(alerts),
                "briefing_chars": len(portfolio_briefing),
            },
            state,
        )
        logger.info(
            "GenerateBriefingNode: generated briefing (%d chars, %d alerts)",
            len(portfolio_briefing),
            len(alerts),
        )
        return {
            "portfolio_briefing": portfolio_briefing,
            "status": AgentStatus.SUCCESS.value,
        }
