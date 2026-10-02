"""AgentCore Platform v1.0"""

# FIN-C2-068 — PreProcessNode (outer pre_process slot; caller-contract gate).
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings
#  - Read input_context via _without_platform_context(state.get("input_context", {})) — read-only
#
# This node owns the caller contract. It:
#   1. refuses empty or malformed input,
#   2. screens the request for prompt-injection content — including chat
#      template control tokens, over KEYS as well as values, on the raw text
#      and again after markup stripping,
#   3. redacts client identifiers STRUCTURALLY (by field name, and inside
#      string values only), and
#   4. validates the caller context into a bounded, inert contract.
#
# Why the redaction is structural: the previous implementation ran the PII
# regexes over the whole serialized payload, so an ordinary portfolio position
# of 1,240,000 was rewritten to "[REDACTED].0", the payload stopped being valid
# JSON, and the pipeline degraded to a briefing that reported zero holdings and
# a zero return — with SUCCESS status. Redaction now happens after parsing and
# only on string values, so a number is never mistaken for an account number.
#
# Screening is enforced HERE rather than being left to the framework's input
# gate: where that gate is absent or configured off, a phrase-only screen fails
# OPEN and the payload reaches the answer path.

import json
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import to_json
from src.services.security import (
    ContextValidationError,
    detect_credential_shapes,
    screen_injection,
    screen_structure,
    validate_caller_context,
)

# Maximum accepted request size. The adapter caps the body as well; this is the
# node's own bound, because the node — not the entry point — owns the contract.
MAX_INPUT_CHARS = 200_000

# Field names treated as client PII: the VALUE is replaced wholesale. Holdings
# are referenced by anonymized instrument symbol only, so no legitimate field
# under these names is needed downstream.
_PII_KEYS = frozenset(
    {
        "client_name",
        "name",
        "full_name",
        "account_holder",
        "holder",
        "account_number",
        "account_no",
        "account_id",
        "acct",
        "iban",
        "email",
        "e_mail",
        "mail",
        "phone",
        "phone_number",
        "tel",
        "mobile",
        "address",
        "home_address",
        "postal_code",
        "ssn",
        "tax_id",
        "national_id",
        "date_of_birth",
        "dob",
    }
)

_PII_REPLACEMENT = "[REDACTED]"

# The framework's own S-2 filter writes this sentinel over any value it reads as
# personal data — including a 12-digit numeric run — before this node runs.
_MASK_SENTINEL = "[MASKED]"

# Residual patterns applied to STRING VALUES ONLY (never to numbers, and never
# to the serialized payload as a whole).
import re  # noqa: E402  — kept next to the patterns it defines


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


_PII_VALUE_PATTERNS: List["re.Pattern[str]"] = [
    # E-mail addresses.
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # Brokerage / bank account numbers: 8-20 digits, optionally hyphen-grouped.
    # An ISO date (2026-06-30) does not match: the second group needs 4+ digits.
    re.compile(r"\b\d{4}-?\d{4,}-?\d{0,8}\b"),
    # International phone numbers. Anchored on a leading + or a 0 trunk prefix
    # so that a plain numeric string is not swallowed.
    re.compile(r"(?:\+\d{1,3}[- ]?\d{1,4}[- ]?\d{3,4}[- ]?\d{3,4}" r"|\b0\d{1,4}-\d{3,4}-\d{3,4}\b)"),
]

_MAX_DEPTH = 12


def _redact_value(text: str) -> str:
    """Replace residual client-PII patterns inside one string value."""
    for pattern in _PII_VALUE_PATTERNS:
        text = pattern.sub(_PII_REPLACEMENT, text)
    return text


def _redact(obj: Any, depth: int = 0) -> Any:
    """Recursively redact client PII from a PARSED payload.

    Keys named as client identifiers lose their value entirely; string values
    are pattern-redacted; numbers, booleans and nulls are returned untouched —
    which is the whole point: a portfolio position is a number, not a PAN.
    """
    if depth > _MAX_DEPTH:
        return _PII_REPLACEMENT
    if isinstance(obj, dict):
        return {
            key: (_PII_REPLACEMENT if str(key).lower() in _PII_KEYS else _redact(value, depth + 1))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, depth + 1) for item in obj]
    if isinstance(obj, str):
        return _redact_value(obj)
    return obj


def _parse_request(text: str) -> "Dict[str, Any]":
    """Parse the request as a JSON object, or raise.

    Fail CLOSED. The previous implementation wrapped an unparseable request as
    free text and let the pipeline continue, so "please brief my client"
    produced a confident client briefing reporting zero holdings, a zero
    return and "the portfolio outperformed its benchmark" — with SUCCESS
    status. An adviser cannot tell that document apart from a real one.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        if _MASK_SENTINEL in text:
            # The platform's own data filter rewrote a value before this node
            # ran, so the body is no longer parseable. Saying which mechanism
            # did it is the difference between an actionable message and a
            # confusing one.
            raise ContextValidationError(
                "a value in the request was redacted by the platform data filter "
                "before parsing; numeric fields of 12 or more digits are read as "
                "personal identifiers — reduce the field width and retry"
            ) from None
        raise ContextValidationError("request body must be a JSON object describing the portfolio") from None
    if not isinstance(parsed, dict):
        raise ContextValidationError("request body must be a JSON object describing the portfolio")
    return parsed


class PreProcessNode(FunctionNode):
    """Caller-contract gate: validate, screen and redact before the workflow runs.

    Rejects empty, oversized, hostile or malformed input before the inner
    domain workflow graph runs, and structurally redacts client identifiers so
    no raw PII reaches downstream state.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "Dict[str, Any]":
        user_input = state.get("user_input", "")
        input_context = _without_platform_context(state.get("input_context", {}))  # read-only

        if not isinstance(user_input, str) or not user_input.strip():
            return self._reject("request body is empty or not a string")
        if len(user_input) > MAX_INPUT_CHARS:
            return self._reject(f"request body exceeds {MAX_INPUT_CHARS} characters")

        # Screen the RAW request first: a markup strip can delete a control
        # token outright and forward the surrounding directive as plain text.
        raw_findings = screen_injection(user_input)

        try:
            parsed = _parse_request(user_input.strip())
        except ContextValidationError as exc:
            return self._reject(str(exc))

        # Screen again after parsing, keys included: JSON \u escapes are
        # resolved by the parser, so a post-parse scan sees the literal token.
        parsed_findings = screen_structure(parsed)

        findings = sorted(set(raw_findings) | set(parsed_findings))
        if findings:
            emit_trace_event(
                "portfolio_briefing_request_refused",
                {"reason": "injection_screen", "findings": findings},
                state,
            )
            return self._reject("request rejected by the input screen (" + ", ".join(findings) + ")")

        # The caller context is validated into a bounded, inert contract.
        # Unknown keys are refused rather than ignored: an ignored key is not a
        # stripped key — it survives into state["input_context"] and reaches
        # the framework's own output scan on the very first node.
        try:
            caller_fields = validate_caller_context(input_context)
        except ContextValidationError as exc:
            return self._reject(f"caller context rejected: {exc}")

        redacted = _redact(parsed)
        validated_input = json.dumps(redacted, ensure_ascii=False)

        # A credential shape that survives redaction must not travel onward:
        # it would be blocked at the output boundary anyway, opaquely.
        credential_findings = detect_credential_shapes(validated_input)
        if credential_findings:
            emit_trace_event(
                "portfolio_briefing_request_refused",
                {"reason": "credential_shape", "findings": credential_findings},
                state,
            )
            return self._reject(
                "request rejected: a credential-shaped value was present (" + ", ".join(credential_findings) + ")"
            )

        emit_trace_event(
            "portfolio_briefing_request_accepted",
            {
                "input_chars": len(validated_input),
                "caller_field_count": len(caller_fields),
            },
            state,
        )

        return {
            "validated_input": validated_input,
            "caller_fields": to_json(caller_fields),
            "enriched_context": {
                "source": "WealthManagementPortfolioBriefingAgent",
                "caller_field_count": len(caller_fields),
            },
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _reject(reason: str) -> Dict[str, Any]:
        """Fail CLOSED, naming the field or the rule — never the value.

        `result` and `formatted_output` are set to the refusal notice rather
        than left absent: the framework's envelope falls back to
        state["result"] when formatted_output is falsy, so a rejection that
        writes neither would surface whatever a later node happened to leave
        behind.
        """
        notice = f"[REQUEST REJECTED] {reason}."
        return {
            "status": AgentStatus.ERROR.value,
            "validated_input": "",
            "caller_fields": None,
            "result": notice,
            "formatted_output": notice,
            "error_log": [f"PreProcessNode: {reason}"],
        }
