"""AgentCore Platform v1.0"""

# FIN-C2-068 — PostProcessNode (outer post_process slot; the output boundary).
#
# Reads the rendered portfolio briefing from state["result"] — written by
# PortfolioBriefingGraphNode.merge_output() from the inner graph's
# portfolio_briefing — and surfaces it only after two INDEPENDENT checks:
#
#   1. a credential / account-number scan, run BEFORE the numeric snap and
#      again after it. Order matters: the snap treats any standalone
#      three-letter upper-case word as a currency marker, so "SSN 123-45-6789"
#      would become "SSN 0-45-6789" and destroy the very pattern the scan is
#      looking for. A verbatim field redaction is order-independent; a PATTERN
#      scan is not.
#   2. a precision gate that enforces the documented output schema — monetary
#      figures are reported as aggregates on the nearest-1,000 grid. The
#      renderer already rounds; this gate is what makes the claim true even if
#      a future renderer forgets.
#
# CONTAINMENT. AgentBaseGraph.get_output() returns
# `state["formatted_output"] or state["result"]` — the fallback applies on
# ERROR status too, and a FALSY formatted_output re-opens it. So on a violation
# this node returns ERROR *and overwrites every output-bearing field with a
# truthy notice*. Raising, returning ERROR without clearing, or clearing to ""
# would all still ship the un-gated briefing inside the error envelope.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.security import detect_credential_shapes
from src.services.llm_factory import resolve_llm
from src.services.llm_review import render_review, review_result

logger = logging.getLogger(__name__)

# Approved external precision: monetary aggregates are reported in units of
# 1,000. Must match GenerateBriefingNode's renderer and the schema note it
# writes into the briefing — the pipeline RENDERS on this grid, this gate
# ENFORCES it.
EXTERNAL_ROUND_UNIT = 1000

# IDENTIFIER GUARDS. This briefing is dense with identifiers — instrument
# symbols (which may be purely numeric, e.g. a Tokyo ticker, and may carry a
# dot or hyphen), the caller's own inert references, ISO dates and a UTC
# timestamp. A monetary token never begins or ends INSIDE such an identifier,
# so the whole grammar is wrapped in single-character guards over this
# template's OWN render alphabet, read off the renderer rather than assumed.
#
# The LEADING guard also carries "." so no alternative can enter a number
# part-way through and rewrite the tail of a decimal fraction.
# The TRAILING guard excludes "." only when a letter or digit follows it, which
# protects "123456.T" (a ticker) while leaving an amount that ends a sentence
# ("... 1234567.") on the grid.
_IDENT_CHAR = r"A-Za-z0-9_\-/:#"
_LEAD_GUARD = rf"(?<![{_IDENT_CHAR}.])"
_TRAIL_GUARD = rf"(?![{_IDENT_CHAR}]|\.[A-Za-z0-9])"

_CURRENCY_MARKER = r"(?:\b[A-Z]{3}|[¥￥$€£円₩])"

# Delimiter between a currency marker and its value: horizontal whitespace and
# at most ONE newline — never a paragraph break. A plain `\s*` spans blank
# lines, so a three-letter upper-case word ending a line would bind to the
# number opening the next block and rewrite document structure
# ("Currency: JPY\n\n3. Cash Position" -> "0. Cash Position").
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"

# A monetary amount may carry a decimal part, and every value alternative
# absorbs it into the SAME token. Without that, the fraction of "9999.99999" is
# a standalone 5+-digit run in its own right and is rewritten into a number the
# output never contained.
#
# The `(?!\.\d)` arm is what makes absorption stick. A plain `(?:\.\d+)?` lets
# the engine backtrack out of the fraction and re-match the integer part alone
# whenever the text right after the fraction fails the trailing guard — and the
# dangling-fraction bug returns ("JPY 1234.56m" -> "JPY 1,000.56m"). Either the
# fraction is taken whole, or there is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

_NUM_TOKEN_RE = re.compile(
    _LEAD_GUARD
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999".
    # The comma-grouped form comes FIRST: matching is leftmost-first, so
    # without it "JPY 1,234" would match as marker + "1" and the snap would
    # mangle the number into "JPY 0,234".
    + rf"(?:(?P<pre>{_CURRENCY_MARKER}{_GATE_DELIM})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_GATE_DELIM}(?:[A-Z]{{3}}\b|[¥￥$€£円₩]))"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))" + _TRAIL_GUARD
)

# ISO 4217 alphabetic codes, used for ONE decision only: whether
# "<three upper-case letters>-<digits>" is a negative amount or an identifier.
# The two are lexically identical — "JPY-9999" (a signed amount, a real leak
# form) and "BRK-1234" (an instrument symbol this template renders) have the
# same shape, so no amount of guard-widening separates them; something has to
# know which three-letter words are currencies. ISO 4217 is a CLOSED,
# standardised vocabulary; the set of instrument identifiers never could be,
# which is why the knowledge sits on this side of the test. Everywhere else the
# grammar still treats ANY standalone three-letter upper-case word as a marker,
# because there a false snap fails safe.
#
# Only the ATTACHED form is narrowed: a SEPARATED marker ("BRK 1234") still
# snaps, so the gate stays fail-safe on the genuinely ambiguous case.
_ISO_CURRENCY_CODES = frozenset(
    "AED AUD BRL CAD CHF CNY DKK EUR GBP HKD IDR ILS INR JPY KRW MXN MYR NOK NZD "
    "PHP PLN RUB SAR SEK SGD THB TRY TWD USD VND ZAR".split()
)

_BLOCKED_NOTICE = (
    "[OUTPUT WITHHELD] The generated briefing did not pass the output boundary "
    "and has not been released. Re-run the request without credential-like "
    "strings or account numbers in the payload."
)


def _is_identifier_hyphen(match: "re.Match[str]") -> bool:
    """True when this match is "<letters>-<digits>", i.e. an identifier.

    Only the marker-then-value branch with an ALPHABETIC marker, an EMPTY
    delimiter and a signed value can be ambiguous; every other form is
    unambiguous and never reaches this test.
    """
    pre = match.group("pre") or ""
    token = match.group("val_after") or ""
    if not pre or not token.startswith(("-", "+")):
        return False
    marker = pre.strip()
    if not marker.isalpha():  # a currency SYMBOL is never an identifier prefix
        return False
    return pre == marker and marker.upper() not in _ISO_CURRENCY_CODES


def _enforce_precision(text: str) -> "Tuple[str, int]":
    """Snap every monetary-form token onto the approved external grid.

    Returns (text, snap_count). A snap means a full-precision monetary figure
    reached the external surface and the gate rounded it. The currency marker,
    the original delimiter whitespace and the explicit sign are all preserved.
    """
    snaps = 0

    def _snap(match: "re.Match[str]") -> str:
        nonlocal snaps
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        if _is_identifier_hyphen(match):
            return match.group(0)
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # whole amount — not just its integer part — is what sits on the grid.
        value = float(token.replace(",", ""))
        if value % EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        snaps += 1
        snapped = round(value / EXTERNAL_ROUND_UNIT) * EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, text), snaps


def _walk_strings(value: object, path: str = "output") -> "List[Tuple[str, str]]":
    """Yield every (path, string) in a candidate output, however deeply nested.

    Caller-facing text can ride inside a nested mapping as easily as at the top
    level, and a gate that scans only top-level strings reports zero findings
    on a token one level down.
    """
    found: List[Tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(_walk_strings(item, f"{path}[{key!r}]"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, f"{path}[{index}]"))
    return found


def scan_disallowed(value: object) -> "List[str]":
    """Return credential/account findings across every string in *value*."""
    findings: List[str] = []
    for _path, text in _walk_strings(value):
        findings.extend(detect_credential_shapes(text))
    return sorted(set(findings))


class PostProcessNode(FunctionNode):
    """The output boundary: scan, enforce the output schema, or withhold.

    Input state keys:
        result: rendered portfolio briefing (from merge_output)

    Output state keys (partial dict):
        formatted_output:   released text, or the withheld notice
        result:             gated alongside formatted_output
        portfolio_briefing: cleared on a violation
        status:             AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:          (on error) closed-set reasons only
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "Dict[str, Any]":
        result = state.get("result")

        if not isinstance(result, str) or not result.strip():
            # Fail CLOSED. Returning SUCCESS with an empty formatted_output
            # would leave the envelope's `formatted_output or result` fallback
            # open on the next field that happens to be populated.
            emit_trace_event(
                "portfolio_briefing_withheld",
                {"reason": "empty_output"},
                state,
            )
            return self._withhold(["empty_output"])

        # Layer 1, run BEFORE the snap: a pattern scan is order-dependent.
        _llm, _ = resolve_llm(None, state)
        _remarks = review_result(
            _llm,
            user_input=str(state.get("user_input") or ""),
            result=result,
            domain="FIN WealthManagementPortfolioBriefingAgent",
        )
        _review = render_review(_remarks)
        # Remarks are LLM text derived from the caller's raw words, so they pass through the
        # same gate the answer does -- appending after the gate would put unscanned text past
        # it. A tripped review is dropped on its own: withholding a correct answer because an
        # advisory remark quoted an identifier would let the review change the outcome, and
        # the whole design rests on it being unable to.
        if _review and isinstance(result, str) and not scan_disallowed(result + _review):
            result = result + _review

        violations = scan_disallowed(result)
        if violations:
            emit_trace_event(
                "portfolio_briefing_withheld",
                {"reason": "disallowed_content", "findings": violations},
                state,
            )
            logger.error("PostProcessNode: output withheld — %s", ", ".join(violations))
            return self._withhold(violations)

        # Layer 2: enforce the documented output schema.
        gated, snaps = _enforce_precision(result)

        # Re-scan after the snap. The rewrite can only ever create digits, but
        # a gate that trusted its own transformation would be exactly the kind
        # of assumption this file exists to remove.
        post_violations = scan_disallowed(gated)
        if post_violations:
            emit_trace_event(
                "portfolio_briefing_withheld",
                {"reason": "disallowed_content_post_snap", "findings": post_violations},
                state,
            )
            return self._withhold(post_violations)

        emit_trace_event(
            "portfolio_briefing_emitted",
            {"briefing_chars": len(gated), "precision_snaps": snaps},
            state,
        )
        return {
            "formatted_output": gated,
            "result": gated,
            "portfolio_briefing": gated,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _withhold(findings: "List[str]") -> "Dict[str, Any]":
        """Return ERROR and overwrite EVERY output-bearing field.

        The notice is truthy on purpose: a falsy replacement re-opens the
        envelope's fallback to state["result"], which is the un-gated briefing
        this method exists to withhold. `findings` are closed-set labels, never
        matched text.
        """
        return {
            "status": AgentStatus.ERROR.value,
            "formatted_output": _BLOCKED_NOTICE,
            "result": _BLOCKED_NOTICE,
            "portfolio_briefing": _BLOCKED_NOTICE,
            "error_log": ["PostProcessNode: output withheld (" + ", ".join(findings) + ")"],
        }


def _security_gate_output(content: str) -> Optional[str]:
    """Report the first disallowed-content label in *content*, or None.

    Kept as the module-level review entry point for the output gate: the
    framework marks the node method @final, so the domain rule lives here and
    the node applies it.
    """
    findings = scan_disallowed(content)
    return findings[0] if findings else None
