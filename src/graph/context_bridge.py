"""AgentCore Platform v1.0"""

# FIN-C2-068 — caller-context bridge across the inner-graph boundary.
#
# Why this exists: GraphNode.execute() invokes the inner graph as
# `subgraph.invoke(user_input, session_id=..., ctx=...)` and does NOT forward
# the outer state's input_context. An inner node reading
# state["input_context"] would therefore always see {} — the briefing
# parameters the caller supplied would be silently absent and every run would
# fall back to the configured defaults.
#
# The sanctioned subclass hooks bridge it:
#
#   PortfolioBriefingGraphNode.extract_input(state)  [runs BEFORE subgraph.invoke]
#       -> set_caller_input_context(state["caller_fields"])
#   DomainWorkflowGraph._extra_initial_state()       [runs INSIDE subgraph.invoke]
#       -> returns {"input_context": get_caller_input_context()}
#
# What crosses is the VALIDATED contract produced by PreProcessNode, never the
# raw request body, so the inner graph only ever sees fields that already
# passed their shape, range and length bounds.
#
# Smuggling the fields inside validated_input is not an option: the framework
# masks that field at every node boundary, so a caller value can be rewritten
# to [MASKED] between hops. This channel is not masked — which is exactly why
# PreProcessNode bounds every field before they enter it.
#
# A ContextVar keeps the hand-off correct per thread/task, so concurrent
# invocations in one process cannot see each other's context.

from contextvars import ContextVar
from typing import Any

_CALLER_INPUT_CONTEXT: ContextVar["dict[str, Any] | None"] = ContextVar("fin_c2_068_caller_input_context", default=None)


def set_caller_input_context(input_context: "dict[str, Any] | None") -> None:
    """Stash the validated caller contract for the imminent inner-graph invoke."""
    _CALLER_INPUT_CONTEXT.set(dict(input_context) if input_context else {})


def get_caller_input_context() -> "dict[str, Any]":
    """Read (without consuming) the stashed contract; {} when none was set."""
    return _CALLER_INPUT_CONTEXT.get() or {}
