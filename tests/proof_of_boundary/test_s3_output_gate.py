# Output boundary — PostProcessNode withholds, and enforces the output schema.
#
# Two independent layers, each with its own audit event:
#   1. a credential / identifier scan, run BEFORE the numeric snap and again
#      after it (a pattern scan is order-dependent: the snap would rewrite
#      "SSN 123-45-6789" into "SSN 0-45-6789" and destroy the pattern);
#   2. the precision grid — monetary aggregates on the nearest 1,000.
#
# CONTAINMENT. The framework's envelope is `formatted_output or result`, and
# the fallback applies on ERROR status too. So a violation must return ERROR
# *and overwrite every output-bearing field with a truthy value*.

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import (
    EXTERNAL_ROUND_UNIT,
    PostProcessNode,
    _enforce_precision,
    scan_disallowed,
)

# Simulated secrets / identifiers — NOT real.
_SIMULATED_API_KEY = "sk-TESTKEY1234567890abcdefghijklmn"
_SIMULATED_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0." "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
_SIMULATED_BEARER = f"Bearer {_SIMULATED_JWT}"
_SIMULATED_CRED_STR = "password=super_secret_password_abc123"
_SIMULATED_ACCOUNT = "1234-5678-9012"
_SIMULATED_NATIONAL_ID = "123-45-6789"
_SIMULATED_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_SIMULATED_STRIPE_KEY = "sk_live_" + "ABCDEFGHIJKLMNOP1234"
_SIMULATED_CONN = "postgresql://user:examplepassword@example/db"

_CLEAN_BRIEFING = """\
# Portfolio Briefing

**Generated:** 2026-09-30 12:00 UTC
**Positions as of:** 2026-06-30
**Holdings:** 2  |  **Total Market Value:** 100,000

> Monetary figures are reported as aggregates rounded to the nearest 1,000; \
individual position values are not reported.

---

## Performance vs Benchmark

The portfolio outperformed its benchmark.

- Portfolio return: 7.0%
- Benchmark return: 5.0%
- Excess return: 2.0%

## Event Screening & Alerts

No threshold-breach alerts were raised this period.

- BRK-1234 (guidance): guidance raised
- 123456.T (macro): index rebalanced

---

*This briefing is informational decision-support material prepared for a \
licensed adviser.*
"""


def _run(result_text):
    return PostProcessNode().execute({"result": result_text})


class TestDisallowedContentIsWithheld:
    @pytest.mark.parametrize(
        "secret",
        [
            _SIMULATED_API_KEY,
            _SIMULATED_JWT,
            _SIMULATED_BEARER,
            _SIMULATED_CRED_STR,
            _SIMULATED_ACCOUNT,
            _SIMULATED_NATIONAL_ID,
            _SIMULATED_AWS_KEY,
            _SIMULATED_STRIPE_KEY,
            _SIMULATED_CONN,
        ],
    )
    def test_secret_never_reaches_any_output_field(self, secret):
        result = _run(f"{_CLEAN_BRIEFING}\n\nleaked: {secret}")
        assert result["status"] == AgentStatus.ERROR.value
        for field in ("formatted_output", "result", "portfolio_briefing"):
            assert secret not in result[field]

    @pytest.mark.parametrize("secret", [_SIMULATED_API_KEY, _SIMULATED_ACCOUNT])
    def test_every_output_bearing_field_is_overwritten_and_truthy(self, secret):
        """Clearing to "" would re-open the envelope's fallback; the
        replacement must be truthy AND must not be the briefing."""
        result = _run(f"{_CLEAN_BRIEFING}\n\nleaked: {secret}")
        for field in ("formatted_output", "result", "portfolio_briefing"):
            assert result[field], f"{field} was cleared to a falsy value"
            assert "Portfolio Briefing" not in result[field]

    def test_error_log_carries_labels_not_matched_text(self):
        result = _run(f"{_CLEAN_BRIEFING}\n\nleaked: {_SIMULATED_API_KEY}")
        joined = " ".join(result["error_log"])
        assert _SIMULATED_API_KEY not in joined
        assert "openai_key" in joined

    def test_nested_structure_is_walked(self):
        """Caller text can ride inside a nested mapping as easily as at the
        top level; a gate that scans only top-level strings reports zero."""
        nested = {"a": {"b": ["clean", {"c": _SIMULATED_BEARER}]}}
        assert scan_disallowed(nested), "nested leak was not detected"
        # The top-level control proves the walker itself works, so a zero on
        # the nested case cannot be confused with a broken probe.
        assert scan_disallowed(_SIMULATED_BEARER)
        assert scan_disallowed({"a": {"b": ["clean text only"]}}) == []


class TestEmptyOutputFailsClosed:
    @pytest.mark.parametrize("empty", ["", "   ", None, 0, [], {}])
    def test_empty_result_is_withheld_not_passed_through(self, empty):
        """The previous contract returned SUCCESS with an empty
        formatted_output — the falsy value that ACTIVATES the envelope's
        fallback to state["result"]."""
        result = _run(empty)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["formatted_output"], "withheld output must stay truthy"


class TestCleanOutputPassesThrough:
    def test_clean_briefing_is_byte_identical(self):
        result = _run(_CLEAN_BRIEFING)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == _CLEAN_BRIEFING
        assert result["result"] == _CLEAN_BRIEFING


class TestPrecisionGrid:
    """The documented schema says monetary aggregates are reported on the
    nearest-1,000 grid. The renderer rounds; this gate enforces."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            # already on the grid -> byte-identical
            ("JPY 1,000", "JPY 1,000"),
            ("**Total Market Value:** 2,100,000", "**Total Market Value:** 2,100,000"),
            # the comma-grouped form must match as a WHOLE token
            ("JPY 1,234", "JPY 1,000"),
            # form-based: 5+ digit runs and comma groups, at any magnitude
            ("1234567", "1,235,000"),
            ("9,999", "10,000"),
            # currency context, both directions, code or symbol
            ("JPY 9999", "JPY 10,000"),
            ("9999 JPY", "10,000 JPY"),
            ("¥9999", "¥10,000"),
            ("9999円", "10,000円"),
            # signed, sign preserved
            ("JPY-9999", "JPY-10,000"),
            ("JPY +9999", "JPY +10,000"),
            # arbitrary horizontal whitespace delimiters
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
        ],
    )
    def test_monetary_forms_snap(self, text, expected):
        assert _enforce_precision(text)[0] == expected

    @pytest.mark.parametrize(
        "text",
        [
            # decimals must survive whole — the fraction is not a value of its own
            "8.512345",
            "9999.99999%",
            "ratio 0.123456",
            # ... and the fraction cannot be backtracked out of
            "JPY 1234.56m",
            # structural tokens this briefing actually renders
            "2026-06-30",
            "2026-09-08 03:55 UTC",
            "STAR 2026",
            "90d",
            "**Holdings:** 2",
            "- Portfolio return: 7.0%",
            "return_pct=-12.5 (threshold -10.0)",
            # identifiers over this template's own render alphabet
            "BRK-1234",
            "SKF-6205",
            "123456.T",
            "sku_48210",
            "`acct_7742`",
            # the delimiter must never span a paragraph break
            "Currency: JPY\n\n3. Cash Position",
        ],
    )
    def test_non_monetary_text_is_byte_identical(self, text):
        assert _enforce_precision(text)[0] == text

    def test_decimal_amount_snaps_as_a_whole_amount(self):
        assert _enforce_precision("JPY 1234.56")[0] == "JPY 1,000"

    def test_separated_marker_still_snaps(self):
        """Only the ATTACHED "<letters>-<digits>" form is narrowed, so the gate
        stays fail-safe on the genuinely ambiguous separated case."""
        assert _enforce_precision("BRK 1234")[0] == "BRK 1,000"

    def test_snap_count_is_reported(self):
        gated, snaps = _enforce_precision("total 1234567 and 9,999")
        assert snaps == 2 and gated == "total 1,235,000 and 10,000"

    def test_off_grid_value_in_a_briefing_is_snapped_not_withheld(self):
        result = _run(_CLEAN_BRIEFING.replace("100,000", "1,234,567"))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "1,235,000" in result["formatted_output"]
        assert "1,234,567" not in result["formatted_output"]

    def test_grid_unit_matches_the_documented_schema(self):
        assert EXTERNAL_ROUND_UNIT == 1000


class TestLayerOrder:
    """The pattern scan runs BEFORE the snap. Reversed, the snap treats any
    standalone three-letter upper-case word as a currency marker and mangles
    the very pattern the scan is looking for."""

    @pytest.mark.parametrize("leak", ["SSN 123-45-6789", "TAX 987-65-4321"])
    def test_identifier_is_withheld_not_mangled(self, leak):
        result = _run(f"{_CLEAN_BRIEFING}\n\n{leak}")
        assert result["status"] == AgentStatus.ERROR.value
        assert leak.split()[1] not in result["formatted_output"]

    @pytest.mark.parametrize("leak", ["SSN 123-45-6789", "TAX 987-65-4321"])
    def test_the_guards_also_leave_the_pattern_intact(self, leak):
        """Belt and braces: even if the order were reversed, the identifier
        guards keep the pattern scannable rather than half-rewritten."""
        assert _enforce_precision(leak)[0] == leak
