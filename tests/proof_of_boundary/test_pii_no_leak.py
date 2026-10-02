# PB — Domain boundary: no client-PII token survives end to end.
#
# The guarantee is two-pronged, and both prongs are asserted here:
#   (1) PII-KEYED fields (client_name / account_number / email / phone ...) have
#       their value replaced wholesale by PreProcessNode, so a client name or
#       identifier placed in such a key can never surface;
#   (2) account-number and e-mail PATTERNS inside string values are redacted to
#       [REDACTED].
#
# Redaction is STRUCTURAL — it runs on the parsed payload, and only on string
# values. Running it over the serialized payload rewrote an ordinary position of
# 1,240,000 to "[REDACTED].0", the body stopped being valid JSON, and the
# pipeline produced a briefing reporting zero holdings. The numeric-survival
# assertions below are what keep that from coming back.
#
# Holdings are referenced by anonymized instrument symbol only.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph, load_runtime_config
from src.nodes.pre_process_node import PreProcessNode

# Simulated client-PII tokens — NOT real data.
_CLIENT_NAME = "Jane Investor"
_ACCOUNT_KEYED = "1111-2222-3333"
_ACCOUNT_FREETEXT = "9876543210"
_EMAIL = "jane.investor@example.com"

_PAYLOAD_WITH_PII = json.dumps(
    {
        "as_of": "2026-09-30",
        "client_name": _CLIENT_NAME,
        "account_number": _ACCOUNT_KEYED,
        "email": _EMAIL,
        "note": f"Settlement to account {_ACCOUNT_FREETEXT}, confirm at {_EMAIL}.",
        "holdings": [
            {"symbol": "AAPL", "asset_class": "equity", "weight": 60.0, "market_value": 1_240_000.0, "return_pct": 9.0},
            {"symbol": "GOVT", "asset_class": "bond", "weight": 40.0, "market_value": 860_000.0, "return_pct": 3.0},
        ],
        "benchmarks": {"sp500": {"return_pct": 5.0}},
        "market_events": [],
    },
    ensure_ascii=False,
)


def _run(user_input: str) -> dict:
    agent = Graph(config=load_runtime_config())
    agent.compile()
    ctx = InvocationContext(
        session_id="pii",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="test-suite",
    )
    return agent.invoke(user_input, ctx=ctx)


class TestNoPiiLeakE2E:
    def test_run_succeeds(self):
        result = _run(_PAYLOAD_WITH_PII)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    @pytest.mark.parametrize(
        "token",
        [
            _CLIENT_NAME,
            _ACCOUNT_KEYED,
            _ACCOUNT_FREETEXT,
            _EMAIL,
        ],
    )
    def test_identifier_not_in_output(self, token):
        assert token not in _run(_PAYLOAD_WITH_PII).get("output", "")

    def test_briefing_still_reports_the_real_portfolio(self):
        """Redaction must not blank the portfolio.

        A briefing that reports "Holdings: 0" for a two-position portfolio is
        indistinguishable from one for an empty account — and this pipeline
        used to produce exactly that whenever a position had seven digits.
        """
        output = _run(_PAYLOAD_WITH_PII).get("output", "")
        assert "**Holdings:** 2" in output
        assert "2,100,000" in output  # 1,240,000 + 860,000, on the 1,000 grid
        assert "AAPL" in output


class TestPreProcessAuthoritativeStrip:
    """The strip asserted directly on the node that owns it."""

    def _validated(self) -> dict:
        result = PreProcessNode().execute({"user_input": _PAYLOAD_WITH_PII, "input_context": {}})
        assert result["status"] == AgentStatus.SUCCESS.value
        return json.loads(result["validated_input"])

    @pytest.mark.parametrize("key", ["client_name", "account_number", "email"])
    def test_pii_keyed_values_are_replaced(self, key):
        assert self._validated()[key] == "[REDACTED]"

    def test_patterns_inside_string_values_are_redacted(self):
        note = self._validated()["note"]
        assert _ACCOUNT_FREETEXT not in note
        assert _EMAIL not in note
        assert "[REDACTED]" in note

    def test_numeric_positions_survive_untouched(self):
        holdings = self._validated()["holdings"]
        assert [h["market_value"] for h in holdings] == [1_240_000.0, 860_000.0]

    def test_symbols_survive(self):
        holdings = self._validated()["holdings"]
        assert {h["symbol"] for h in holdings} == {"AAPL", "GOVT"}
