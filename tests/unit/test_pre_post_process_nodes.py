"""PreProcessNode and PostProcessNode — the two ends of the pipeline.

PreProcessNode owns the caller contract; PostProcessNode owns the output
boundary. Both are exercised through execute() directly, with no framework
wrapper in front, so a guarantee that depends on the framework being configured
a particular way cannot pass here by accident.
"""

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from tests.fixtures import holding, request_with_holdings, valid_request


class TestPreProcess:
    def test_valid_request_is_accepted(self):
        result = PreProcessNode().execute({"user_input": valid_request(), "input_context": {}})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert json.loads(result["validated_input"])["holdings"]

    def test_caller_fields_are_serialized_for_the_bridge(self):
        result = PreProcessNode().execute(
            {
                "user_input": valid_request(),
                "input_context": {"portfolio_ref": "acct_1"},
            }
        )
        assert json.loads(result["caller_fields"]) == {"portfolio_ref": "acct_1"}

    def test_rejection_clears_the_validated_input(self):
        result = PreProcessNode().execute({"user_input": "", "input_context": {}})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["validated_input"] == ""
        assert result["caller_fields"] is None

    def test_emits_a_domain_audit_event(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.nodes.pre_process_node.emit_trace_event",
            lambda name, payload, state: seen.append(name),
        )
        PreProcessNode().execute({"user_input": valid_request(), "input_context": {}})
        PreProcessNode().execute(
            {
                "user_input": request_with_holdings(holding(), note="<|im_start|>"),
                "input_context": {},
            }
        )
        assert "portfolio_briefing_request_accepted" in seen
        assert "portfolio_briefing_request_refused" in seen


class TestPostProcess:
    _BRIEFING = "# Portfolio Briefing\n\n**Holdings:** 2  |  **Total Market Value:** 100,000\n"

    def test_clean_briefing_surfaces_unchanged(self):
        result = PostProcessNode().execute({"result": self._BRIEFING})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == self._BRIEFING

    def test_empty_result_is_withheld_with_a_truthy_notice(self):
        """Returning SUCCESS with an empty formatted_output writes the falsy
        value that ACTIVATES the envelope's fallback to state["result"]."""
        result = PostProcessNode().execute({"result": ""})
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"], "the withheld notice must be truthy"
        assert result["result"] == result["formatted_output"]

    @pytest.mark.parametrize(
        "secret",
        [
            "sk-TESTKEY1234567890abcdefghijklmn",
            "1234-5678-9012",
            "123-45-6789",
        ],
    )
    def test_violation_overwrites_every_output_bearing_field(self, secret):
        result = PostProcessNode().execute({"result": f"{self._BRIEFING}\n{secret}"})
        assert result["status"] == AgentStatus.ERROR.value
        for field in ("formatted_output", "result", "portfolio_briefing"):
            assert secret not in result[field]
            assert result[field]

    def test_off_grid_aggregate_is_snapped(self):
        result = PostProcessNode().execute({"result": "**Total Market Value:** 1,234,567"})
        assert result["formatted_output"] == "**Total Market Value:** 1,235,000"

    def test_emits_a_domain_audit_event(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.nodes.post_process_node.emit_trace_event",
            lambda name, payload, state: seen.append(name),
        )
        PostProcessNode().execute({"result": self._BRIEFING})
        PostProcessNode().execute({"result": "leak sk-TESTKEY1234567890abcdefghijklmn"})
        assert "portfolio_briefing_emitted" in seen
        assert "portfolio_briefing_withheld" in seen
