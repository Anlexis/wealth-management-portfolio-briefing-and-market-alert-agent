"""AgentCore Platform v1.0"""

# FIN-C2-068 — AggregateSourcesNode
# Inner-graph domain node 1: parse the adviser's portfolio + market-data
# payload, validate EVERY caller field against explicit bounds, and assemble a
# single unified portfolio view keyed by instrument symbol.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).
#
# Fail-closed contract. This node is the domain load boundary, so a payload it
# cannot fully validate is REFUSED, never degraded: the pipeline downstream
# renders a client-facing document, and a briefing assembled from a partially
# understood payload is indistinguishable from one assembled from a complete
# payload. Numbers go through a finite+bounded parser — NaN and Infinity parse
# through float() and then compare False against every threshold, which would
# fail open on precisely the alert decision this agent exists to make.

import json
import logging
import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.security import (
    ContextValidationError,
    bound_text,
    finite_in_range,
    mask_field_name,
    validate_enum,
    validate_symbol,
)

logger = logging.getLogger(__name__)

# Documented bounds for every caller-supplied number.
#
# The market-value ceiling is 11 digits rather than an arbitrary large number
# for a concrete platform reason: the framework's S-2 filter masks any 12-digit
# run inside validated_input before this node runs, so a 12-digit position
# would arrive as the literal string [MASKED] and the payload would stop being
# valid JSON. Refusing it names the field; accepting it would silently zero the
# portfolio.
MARKET_VALUE_BOUNDS = (0.0, 99_999_999_999.0)
WEIGHT_BOUNDS = (0.0, 100.0)
RETURN_PCT_BOUNDS = (-10_000.0, 10_000.0)

# Structural caps. Defaults are used when config/config.yaml declares none.
DEFAULT_LIMITS = {
    "max_holdings": 200,
    "max_benchmarks": 50,
    "max_market_events": 100,
}

_EVENT_CATEGORIES = frozenset(
    {
        "earnings",
        "guidance",
        "dividend",
        "rating",
        "regulatory",
        "corporate_action",
        "macro",
        "general",
    }
)
_EVENT_SEVERITIES = frozenset({"info", "low", "medium", "high"})

# as_of is a rendered caller string: a bare ISO date, nothing else.
_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Free-form labels that are NOT rendered into the briefing are normalized
# rather than refused — the framework may already have rewritten them to
# [MASKED] (a Title-Case run is read as a person's name), and refusing a value
# the platform itself produced would make ordinary payloads unusable.
_LABEL_RE = re.compile(r"[^a-z0-9_]+")


def _normalize_label(value: object, default: str) -> str:
    """Fold a non-rendered free-form label to an inert token."""
    if not isinstance(value, str):
        return default
    folded = _LABEL_RE.sub("_", value.strip().lower()).strip("_")
    return folded[:32] or default


def _limit(limits: "Dict[str, Any]", key: str) -> int:
    """Read one structural cap from config, falling back to the documented default."""
    value = limits.get(key, DEFAULT_LIMITS[key])
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(DEFAULT_LIMITS[key])
    return parsed if parsed > 0 else int(DEFAULT_LIMITS[key])


def _normalize_holding(index: int, raw: object) -> "Dict[str, Any]":
    """Validate and normalize one holding entry, or raise."""
    if not isinstance(raw, dict):
        raise ContextValidationError(f"holdings[{index}] must be an object.")
    symbol_value = raw.get("symbol", raw.get("ticker"))
    if symbol_value is None:
        raise ContextValidationError(f"holdings[{index}].symbol is required.")
    return {
        "symbol": validate_symbol(f"holdings[{index}].symbol", symbol_value),
        "asset_class": _normalize_label(raw.get("asset_class", raw.get("class")), "unclassified"),
        "weight": finite_in_range(f"holdings[{index}].weight", raw.get("weight", 0.0), *WEIGHT_BOUNDS),
        "market_value": finite_in_range(
            f"holdings[{index}].market_value",
            raw.get("market_value", raw.get("value", 0.0)),
            *MARKET_VALUE_BOUNDS,
        ),
        "return_pct": finite_in_range(
            f"holdings[{index}].return_pct",
            raw.get("return_pct", raw.get("return", 0.0)),
            *RETURN_PCT_BOUNDS,
        ),
        "benchmark": _normalize_label(raw.get("benchmark"), ""),
    }


def _normalize_event(index: int, raw: object) -> "Dict[str, Any]":
    """Validate and normalize one market event, or raise."""
    if not isinstance(raw, dict):
        raise ContextValidationError(f"market_events[{index}] must be an object.")
    symbol_value = raw.get("symbol")
    if symbol_value is None:
        raise ContextValidationError(f"market_events[{index}].symbol is required.")
    return {
        "symbol": validate_symbol(f"market_events[{index}].symbol", symbol_value),
        # The one free-text field that survives to the reader. Bounded here so
        # the pipeline cannot be made to carry an unbounded string; the
        # flattening that stops a caller newline from manufacturing a step
        # belongs to the renderer, which is the only place that knows this text
        # is about to become a line of a document.
        "headline": bound_text(raw.get("headline", "")),
        "category": validate_enum(
            f"market_events[{index}].category",
            raw.get("category"),
            _EVENT_CATEGORIES,
            "general",
        ),
        "severity": validate_enum(
            f"market_events[{index}].severity",
            raw.get("severity"),
            _EVENT_SEVERITIES,
            "info",
        ),
    }


class AggregateSourcesNode(FunctionNode):
    """Aggregate portfolio + market data from multiple sources into a unified view.

    Input state keys:
        validated_input / user_input: the redacted adviser payload (JSON string)
        agent_config: seeded runtime configuration (JSON string)

    Output state keys (partial dict):
        aggregated_portfolio: {"holdings": [...], "benchmarks": {...},
                               "market_events": [...], "totals": {...},
                               "as_of": str}
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "Dict[str, Any]":
        payload_text = state.get("validated_input") or state.get("user_input", "")
        config: Dict[str, Any] = from_json(state.get("agent_config"), {}) or {}
        limits = config.get("limits", {}) if isinstance(config.get("limits"), dict) else {}

        try:
            payload = self._parse(payload_text)
            holdings, benchmarks, market_events, as_of = self._validate(payload, limits)
        except ContextValidationError as exc:
            emit_trace_event(
                "portfolio_payload_refused",
                {"reason": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"AggregateSourcesNode: {exc}"],
                # The runner surfaces `formatted_output or result` as `output`. A reason left only in
                # error_log reaches no one: the terminal result carries just `status`, and get_output()
                # does not copy error_log out of the graph -- the caller sees a blank spinner.
                "formatted_output": "Request could not be completed. " + (f"AggregateSourcesNode: {exc}"),
            }

        total_value = round(sum(h["market_value"] for h in holdings), 2)
        total_weight = round(sum(h["weight"] for h in holdings), 4)
        aggregated_portfolio: Dict[str, Any] = {
            "holdings": holdings,
            "benchmarks": benchmarks,
            "market_events": market_events,
            "totals": {
                "holding_count": len(holdings),
                "total_market_value": total_value,
                "total_weight": total_weight,
            },
            "as_of": as_of,
        }

        emit_trace_event(
            "sources_aggregated",
            {
                "holding_count": len(holdings),
                "benchmark_count": len(benchmarks),
                "market_event_count": len(market_events),
            },
            state,
        )
        logger.info(
            "AggregateSourcesNode: %d holdings, %d benchmarks, %d market events",
            len(holdings),
            len(benchmarks),
            len(market_events),
        )
        return {"aggregated_portfolio": to_json(aggregated_portfolio)}

    @staticmethod
    def _parse(payload_text: object) -> "Dict[str, Any]":
        """Parse the upstream payload, failing closed.

        The payload reaching this node has already been validated and redacted
        by PreProcessNode, so a parse failure here means the platform's own S-2
        filter rewrote a value inside it. Refusing is the only safe answer: the
        alternative — the previous free-text fallback — produced a briefing
        that reported an empty portfolio for a caller who supplied a full one.
        """
        if not isinstance(payload_text, str) or not payload_text.strip():
            raise ContextValidationError("the portfolio payload is missing.")
        try:
            parsed = json.loads(payload_text)
        except (json.JSONDecodeError, ValueError):
            raise ContextValidationError("the portfolio payload could not be read as JSON after redaction.") from None
        if not isinstance(parsed, dict):
            raise ContextValidationError("the portfolio payload must be an object.")
        return parsed

    @staticmethod
    def _validate(
        payload: "Dict[str, Any]", limits: "Dict[str, Any]"
    ) -> "tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]], str]":
        """Validate every caller field against explicit bounds, or raise."""
        raw_holdings = payload.get("holdings")
        if not isinstance(raw_holdings, list) or not raw_holdings:
            raise ContextValidationError("holdings must be a non-empty array of positions.")
        max_holdings = _limit(limits, "max_holdings")
        if len(raw_holdings) > max_holdings:
            raise ContextValidationError(f"holdings accepts at most {max_holdings} positions.")
        holdings = [_normalize_holding(i, h) for i, h in enumerate(raw_holdings)]

        raw_benchmarks = payload.get("benchmarks", {})
        if raw_benchmarks in (None, ""):
            raw_benchmarks = {}
        if not isinstance(raw_benchmarks, dict):
            raise ContextValidationError("benchmarks must be an object.")
        max_benchmarks = _limit(limits, "max_benchmarks")
        if len(raw_benchmarks) > max_benchmarks:
            raise ContextValidationError(f"benchmarks accepts at most {max_benchmarks} entries.")
        benchmarks: Dict[str, Any] = {}
        for name, entry in raw_benchmarks.items():
            label = _normalize_label(name, "")
            if not label:
                raise ContextValidationError(f"benchmark name {mask_field_name(name)} is not a usable label.")
            source = entry.get("return_pct", entry.get("return")) if isinstance(entry, dict) else entry
            benchmarks[label] = {
                "return_pct": finite_in_range(f"benchmarks.{label}.return_pct", source, *RETURN_PCT_BOUNDS)
            }

        raw_events = payload.get("market_events", [])
        if raw_events in (None, ""):
            raw_events = []
        if not isinstance(raw_events, list):
            raise ContextValidationError("market_events must be an array.")
        max_events = _limit(limits, "max_market_events")
        if len(raw_events) > max_events:
            raise ContextValidationError(f"market_events accepts at most {max_events} entries.")
        market_events = [_normalize_event(i, e) for i, e in enumerate(raw_events)]

        as_of_raw = payload.get("as_of", "")
        if as_of_raw in (None, ""):
            as_of = ""
        elif isinstance(as_of_raw, str) and _AS_OF_RE.match(as_of_raw.strip()):
            as_of = as_of_raw.strip()
        else:
            raise ContextValidationError("as_of must be a YYYY-MM-DD date.")

        return holdings, benchmarks, market_events, as_of
