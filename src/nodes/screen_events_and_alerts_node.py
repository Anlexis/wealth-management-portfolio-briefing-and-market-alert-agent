"""AgentCore Platform v1.0"""

# FIN-C2-068 — ScreenEventsAndAlertsNode
# Inner-graph domain node 3: match aggregated market events to the holdings
# actually held, and raise threshold-breach alerts (a holding drawdown beyond
# the configured threshold, or a concentration above it).
#
# Thresholds come from config/config.yaml (alert_thresholds), seeded into State
# by the inner graph, and may be overridden per request by the caller within
# documented bounds. Both routes are exercised end to end: a declared value
# that did not change behaviour would be a dead declaration in shipped config.
#
# Advisory-only: alerts are informational risk flags for the adviser. They are
# NOT automated trade triggers and contain no buy/sell instruction.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.security import (
    CONCENTRATION_PCT_BOUNDS,
    DRAWDOWN_PCT_BOUNDS,
    ContextValidationError,
    finite_in_range,
)


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

# Documented defaults, used when neither config nor the caller declares one.
DEFAULT_DRAWDOWN_ALERT_PCT = -10.0
DEFAULT_CONCENTRATION_PCT = 25.0


def _threshold(
    field: str,
    caller_value: object,
    config_value: object,
    default: float,
    bounds: "tuple[float, float]",
) -> float:
    """Resolve one alert threshold: caller > config > documented default.

    Every candidate goes through the finite+bounded parser. A NaN threshold
    would compare False against every holding and silently suppress the alert
    the adviser is relying on, which is why a bad value fails CLOSED here
    rather than degrading to the default.
    """
    if caller_value is not None:
        return finite_in_range(field, caller_value, *bounds)
    if config_value is not None:
        return finite_in_range(f"config.alert_thresholds.{field}", config_value, *bounds)
    return default


def _severity_for_drawdown(return_pct: float, threshold: float) -> str:
    """Classify drawdown severity relative to the alert threshold."""
    if return_pct <= threshold * 2:
        return "high"
    if return_pct <= threshold:
        return "medium"
    return "low"


class ScreenEventsAndAlertsNode(FunctionNode):
    """Screen holdings for relevant market events; flag threshold-breach alerts.

    Input state keys:
        aggregated_portfolio: unified portfolio view (from AggregateSourcesNode)
        agent_config:         seeded runtime configuration (JSON string)
        input_context:        validated caller contract (bridged from the outer graph)

    Output state keys (partial dict):
        screened_events: {"events": [...], "alerts": [...], "alert_count": int,
                          "thresholds": {...}}
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "Dict[str, Any]":
        aggregated: Dict[str, Any] = from_json(state.get("aggregated_portfolio"), {}) or {}
        holdings: List[Dict[str, Any]] = aggregated.get("holdings", []) or []
        market_events: List[Dict[str, Any]] = aggregated.get("market_events", []) or []

        config: Dict[str, Any] = from_json(state.get("agent_config"), {}) or {}
        declared = config.get("alert_thresholds", {})
        if not isinstance(declared, dict):
            declared = {}
        caller = _without_platform_context(state.get("input_context", {})) or {}
        if not isinstance(caller, dict):
            caller = {}

        try:
            drawdown_threshold = _threshold(
                "drawdown_alert_pct",
                caller.get("drawdown_alert_pct"),
                declared.get("drawdown_pct"),
                DEFAULT_DRAWDOWN_ALERT_PCT,
                DRAWDOWN_PCT_BOUNDS,
            )
            concentration_threshold = _threshold(
                "concentration_alert_pct",
                caller.get("concentration_alert_pct"),
                declared.get("concentration_pct"),
                DEFAULT_CONCENTRATION_PCT,
                CONCENTRATION_PCT_BOUNDS,
            )
        except ContextValidationError as exc:
            emit_trace_event("alert_thresholds_refused", {"reason": str(exc)}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ScreenEventsAndAlertsNode: {exc}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. " + (f"ScreenEventsAndAlertsNode: {exc}"),
            }

        held_symbols = {h["symbol"] for h in holdings}
        relevant_events = [e for e in market_events if e.get("symbol") in held_symbols]

        alerts: List[Dict[str, Any]] = []
        for h in holdings:
            ret = float(h["return_pct"])
            weight = float(h["weight"])
            if ret <= drawdown_threshold:
                alerts.append(
                    {
                        "symbol": h["symbol"],
                        "type": "drawdown",
                        "metric": "return_pct",
                        "value": ret,
                        "threshold": drawdown_threshold,
                        "severity": _severity_for_drawdown(ret, drawdown_threshold),
                    }
                )
            if weight >= concentration_threshold:
                alerts.append(
                    {
                        "symbol": h["symbol"],
                        "type": "concentration",
                        "metric": "weight",
                        "value": weight,
                        "threshold": concentration_threshold,
                        "severity": "medium",
                    }
                )

        screened_events: Dict[str, Any] = {
            "events": relevant_events,
            "alerts": alerts,
            "alert_count": len(alerts),
            "thresholds": {
                "drawdown_pct": drawdown_threshold,
                "concentration_pct": concentration_threshold,
            },
        }

        emit_trace_event(
            "events_and_alerts_screened",
            {
                "relevant_event_count": len(relevant_events),
                "alert_count": len(alerts),
                "drawdown_threshold": drawdown_threshold,
                "concentration_threshold": concentration_threshold,
            },
            state,
        )
        logger.info(
            "ScreenEventsAndAlertsNode: %d relevant events, %d alerts (drawdown<=%.2f, concentration>=%.2f)",
            len(relevant_events),
            len(alerts),
            drawdown_threshold,
            concentration_threshold,
        )
        return {"screened_events": to_json(screened_events)}
