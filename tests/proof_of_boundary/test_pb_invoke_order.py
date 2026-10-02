# PB-6 — Invoke-Order Boundary: a full agent.invoke() must execute the fixed
# AgentBaseGraph backbone in order.
#
# The backbone is fixed and is NEVER overridden by this template (add_edges()
# belongs to the framework):
#
#     START -> initialize -> pre_process -> main -> {route} -> post_process
#           -> finalize -> END
#
# The framework records every executed node in `node_history` (an AgentState
# field whose reducer is operator.add, so entries accumulate in execution
# order). Each entry is the node's CLASS NAME, appended by BaseNode.__call__.
#
# For FIN-C2-068 (two-layer nested) the `main` slot is a GraphNode subclass
# (PortfolioBriefingGraphNode) that delegates to the inner DomainWorkflowGraph.
# The inner graph runs with its own state; its inner node_history is NOT merged
# back, so the OUTER node_history contains exactly the five backbone slots.
#
# This test drives a real end-to-end Graph().invoke() over the SAME payload the
# deployment smoke check sends, at the manifest's DECLARED entry trust level. A
# SUCCESS terminal status is required: on any non-SUCCESS status route()
# short-circuits main -> finalize and the post_process slot is skipped, which is
# itself an invoke-order violation this test would catch.
#
# docs/03_test_spec.md section 4.
# Deterministic — no model call, no network.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph, load_runtime_config
from tests.fixtures import valid_request

# The `main`-slot GraphNode class name for THIS template. The other four slot
# names are framework/scaffold fixed.
_MAIN_SLOT_NODE = "PortfolioBriefingGraphNode"

_EXPECTED_ORDER = [
    "InitializeNode",  # framework default  (initialize slot)
    "PreProcessNode",  # caller-contract gate (pre_process slot)
    _MAIN_SLOT_NODE,  # template-specific    (main slot GraphNode)
    "PostProcessNode",  # output boundary      (post_process slot)
    "FinalizeNode",  # framework default    (finalize slot)
]


def _run() -> dict:
    """Run a full end-to-end invocation at the DECLARED entry trust level.

    VERIFIED_EXTERNAL, not INTERNAL: the manifest declares
    required_trust_level: VERIFIED_EXTERNAL, so a suite that only ever ran at
    INTERNAL would pass while every real caller was refused.
    """
    agent = Graph(config=load_runtime_config())
    agent.compile()
    ctx = InvocationContext(
        session_id="pb6",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="test-suite",
    )
    return agent.invoke(valid_request(), ctx=ctx)


class TestInvokeOrderBoundary:
    """PB-6: full agent.invoke() executes the backbone in the fixed order."""

    def test_invoke_reaches_success(self):
        """The full run must terminate SUCCESS — otherwise route() short-circuits
        main -> finalize and the output boundary never runs."""
        result = _run()
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected SUCCESS, got {result.get('status')!r}. result={result!r}"

    def test_output_is_non_empty(self):
        """A successful run must surface a non-empty gated output."""
        assert _run().get("output"), "invoke() surfaced an empty output"

    def test_node_history_is_populated(self):
        """node_history must be a non-empty list of node class-name strings."""
        history = _run().get("node_history")
        assert isinstance(history, list) and history, f"node_history must be a non-empty list, got {history!r}"
        assert all(isinstance(n, str) for n in history), f"node_history entries must be strings, got {history!r}"

    def test_backbone_slot_order(self):
        """The pre_process slot runs before the domain main slot, which runs
        before the post_process slot — a strict ordered subsequence."""
        history = _run().get("node_history", [])
        ordered_slots = ["PreProcessNode", _MAIN_SLOT_NODE, "PostProcessNode"]
        for name in ordered_slots:
            assert name in history, f"Expected backbone slot {name!r} in node_history, got {history!r}"
        positions = [history.index(name) for name in ordered_slots]
        assert positions == sorted(positions), (
            f"Backbone slots executed out of order: {ordered_slots} at {positions}. " f"node_history={history!r}"
        )

    def test_full_backbone_sequence(self):
        """The complete backbone order:
        initialize -> pre_process -> main -> post_process -> finalize."""
        history = _run().get("node_history", [])
        assert history == _EXPECTED_ORDER, (
            "node_history does not match the canonical backbone order.\n"
            f"  expected: {_EXPECTED_ORDER}\n"
            f"  actual:   {history}"
        )
