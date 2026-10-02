"""AgentCore Platform v1.0"""

# FIN-C2-068 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture).
#
# Wealth Management Portfolio Briefing Agent (DocGeneration pattern, Cat 2).
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                             |  (RETRY, max 3)
#                                          pre_process
#
#   The `main` slot is a GraphNode subclass (PortfolioBriefingGraphNode) that
#   delegates the full portfolio-briefing domain workflow to DomainWorkflowGraph
#   (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#   src/graph/context_bridge.py        <- caller-context hand-off to the inner graph
#
# Rules enforced:
#   - FinC2068Agent inherits AgentBaseGraph (L1 Base — direct inheritance)
#   - super().register_nodes() called first (fills initialize + finalize)
#   - PortfolioBriefingGraphNode assigned to self._nodes["main"]
#   - merge_output() returns only changed keys
#   - add_edges() NOT overridden on the outer graph

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

import yaml

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.trust_level import TrustLevel
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json
from src.services.security import detect_credential_shapes

if TYPE_CHECKING:  # import cycle: the inner graph imports the nodes this module wires
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime parameters: src/graph/graph.py -> parents[2] = repository root.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Runtime keys forwarded to the inner graph as its `agent` section.
_RUNTIME_KEYS = ("max_retry", "timeout_s")

# Domain sections forwarded verbatim to the inner graph.
_DOMAIN_SECTIONS = ("alert_thresholds", "limits", "briefing", "analysis")

# Closed-set text, carrying nothing from state. Used only when a refusal
# happened before any node could write an output-bearing field.
_EMPTY_REFUSAL_NOTICE = (
    "[REQUEST REJECTED] The request was refused before the briefing workflow ran. "
    "Check the request payload against the documented input contract and retry."
)


def load_runtime_config() -> "dict[str, Any]":
    """Load config/config.yaml — the dict passed to the graph as ``config=``.

    The platform registry loads this file and constructs the agent with it; a
    standalone entry point must do the same. Constructing the graph bare leaves
    ``self.config`` empty, and every declared runtime value (max_retry,
    timeout_s, the alert thresholds, the structural caps) is then silently
    inert while still appearing in the shipped configuration. Returns {} when
    the file is missing or unreadable, so the pipeline runs on its documented
    defaults rather than failing to start.
    """
    try:
        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return cast("dict[str, Any]", loaded) if isinstance(loaded, dict) else {}


class PortfolioBriefingGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of FinC2068Agent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by the AgentBaseGraph backbone after pre_process and before
    post_process.

    Contracts:
      get_subgraph()    — instantiate DomainWorkflowGraph with the runtime config
      extract_input()   — pull validated_input and stash the caller contract
      merge_output()    — map sub_result fields into the outer state delta
      error_strategy    — "propagate": re-raise inner errors as SubgraphError
    """

    # S-1 declared on the wrapper too: the CI gate only AST-scans FunctionNode
    # subclasses, so a GraphNode main slot passes the pipeline without one and is
    # flagged at review. Same level the nodes in this repo already declare.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    # "handle": convert an inner failure into a contained, actionable refusal.
    #
    # "propagate" re-raises as SubgraphError, which the framework's node wrapper
    # catches and turns into an ERROR result carrying a full traceback in
    # error_log and NO output-bearing field at all. The envelope then falls
    # through to state["result"] — whatever a previous node happened to leave
    # there — and the caller learns nothing about what was wrong with the
    # request. Handling it here lets the refusal name the failed field and
    # nothing else.
    error_strategy: ClassVar[str] = "handle"

    # False: HITL interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    # Bounds on what an inner failure may say to the caller. The domain nodes
    # build their messages from a masked field name plus fixed text, never from
    # a caller value, so surfacing a few of them is safe and useful.
    _MAX_REASONS: ClassVar[int] = 3
    _MAX_REASON_CHARS: ClassVar[int] = 200

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner domain workflow graph.

        Imported lazily to avoid a circular import at module load time.
        The inner graph receives the runtime configuration; its domain nodes
        take no constructor arguments and read their settings from state, which
        the inner graph seeds in _extra_initial_state().
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode (S-1/S-2) validates and PII-strips the caller payload
        and writes the result to validated_input.

        The validated caller contract cannot ride along inside that string: the
        framework masks validated_input at every node boundary, so caller
        values could be rewritten between hops. It is stashed on the bridge
        here instead — the last point that still sees the outer state before
        the framework invokes the subgraph without forwarding input_context.
        """
        set_caller_input_context(from_json(state.get("caller_fields"), {}))
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: "dict[str, Any]") -> "dict[str, Any]":
        """Map the inner graph's sub_result back into the outer state delta.

        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "portfolio_briefing", "status", ...
          This merge_output() reads -> sub_result.get("portfolio_briefing"),
                                       sub_result.get("status")

        PostProcessNode (the outer post_process slot) reads state["result"], so
        the rendered briefing is mapped there as well; without that the final
        surfaced output would always be empty.
        """
        return {
            "portfolio_briefing": sub_result.get("portfolio_briefing"),
            "result": sub_result.get("portfolio_briefing"),
            "status": sub_result.get("status"),
        }

    def on_subgraph_error(self, state: AgentState, error: Exception) -> "dict[str, Any]":
        """Contain an inner-graph failure into an actionable, closed refusal.

        Every output-bearing field is written — not merely left absent. The
        framework's envelope is `formatted_output or result`, so an error path
        that writes neither surfaces whatever the state already held.

        Reasons are taken from the inner error_log, which the domain nodes
        build from a masked field name plus fixed text; each is re-screened for
        credential shapes, capped in length and in number, and anything else
        (a traceback, a path, a raw exception string) is dropped.
        """
        reasons: list[str] = []
        for entry in getattr(error, "error_log", []) or []:
            text = str(entry)
            if "Traceback" in text or "\n" in text:
                continue
            if detect_credential_shapes(text):
                continue
            reasons.append(text[: self._MAX_REASON_CHARS])
            if len(reasons) >= self._MAX_REASONS:
                break
        notice = "[REQUEST REJECTED] " + (
            " ".join(reasons) if reasons else "the portfolio payload could not be processed."
        )
        return {
            "status": AgentStatus.ERROR.value,
            "portfolio_briefing": notice,
            "result": notice,
            "formatted_output": notice,
            "error_log": ["PortfolioBriefingGraphNode: inner workflow refused the request"],
        }

    def _parent_config(self) -> "dict[str, Any]":
        """Forward the runtime config to the inner graph under config["configurable"].

        Reads config/config.yaml live rather than assuming defaults: an empty
        result would make every declared setting dead, which is the failure this
        method exists to prevent.
        """
        runtime = load_runtime_config()
        configurable: dict[str, Any] = {}
        for section in _DOMAIN_SECTIONS:
            value = runtime.get(section)
            if isinstance(value, dict) and value:
                configurable[section] = value
        agent_cfg = {key: runtime[key] for key in _RUNTIME_KEYS if key in runtime}
        if agent_cfg:
            configurable["agent"] = agent_cfg
        return {"configurable": configurable}


class FinC2068Agent(AgentBaseGraph):
    """Outer graph for FIN-C2-068 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in PortfolioBriefingGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode (S-2 input validation + client-PII strip)
      - main:        PortfolioBriefingGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (S-3 output gate)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    The backbone's own conditional edge (main -> route) uses the inherited
    route(self, state), whose parameter carries no annotation, so the graph
    runtime hands it the full State rather than projecting fields away.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "WealthManagementPortfolioBriefingAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = PortfolioBriefingGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    def get_output(self, state: AgentState) -> "dict[str, Any]":
        """Guarantee the caller always receives something to act on.

        The framework's own input gate can refuse a request from INSIDE a
        node's wrapper, before the node's code runs. That path returns an ERROR
        state in which no output-bearing field was ever written, so the
        envelope's `formatted_output or result` resolves to None and the caller
        sees an empty body with no reason.

        This ONLY substitutes when there is nothing at all to show. It never
        blanks or replaces a value another node produced, so it cannot stand in
        for PostProcessNode's withholding: with that clearing removed, the
        un-gated briefing is still truthy in `result` and still reaches the
        caller — which is what keeps the containment test falsifiable.
        """
        envelope: dict[str, Any] = super().get_output(state)
        if not envelope.get("output"):
            envelope["output"] = _EMPTY_REFUSAL_NOTICE
        return envelope

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias — config/agent.yaml declares class: "src.graph.graph.Graph".
Graph = FinC2068Agent
