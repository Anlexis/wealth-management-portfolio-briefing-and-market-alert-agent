"""AgentCore Platform v1.0"""

# FIN-C2-068 — caller-input security services.
#
# Every value this agent receives from a caller passes through this module
# before it reaches domain logic or the rendered briefing. The rules it
# encodes are deliberately narrow:
#
#   * numbers  — parsed by a finite, bounded parser. NaN and Infinity parse
#                cleanly through float() and then compare False against every
#                threshold, so an unguarded comparison fails OPEN on exactly
#                the decision this agent exists to make.
#   * strings  — locked to inert identifier shapes wherever they render into
#                the briefing. Free caller text in a rendered document is
#                caller-controlled output injection.
#   * headline — the one free-text field that must survive to be useful. It is
#                flattened to a single line, stripped of the characters that
#                manufacture document structure, and length-capped, so no
#                caller newline can turn a headline into a numbered step of the
#                adviser's briefing.
#   * screens  — injection screening runs on the RAW value and again on the
#                markup-stripped value, over keys as well as values, because a
#                sanitizer that removes a control token silently converts a
#                detectable attack into undetectable plain text.
#
# Rejection always names the FIELD and never echoes the value.

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from framework.security.credential_detector import detect_credentials

# --------------------------------------------------------------------------
# Field-name masking
# --------------------------------------------------------------------------

# Field names may carry an index (`holdings[0].market_value`), which is how a
# refusal points at the exact position that failed without quoting its value.
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,64}$")


def mask_field_name(name: object) -> str:
    """Return a field name that is safe to echo back to the caller.

    Field names are caller data too: an unrecognised key may itself carry an
    attack payload or a credential shape, so only names matching a
    conservative pattern (and tripping no credential pattern) are echoed.
    """
    text = name if isinstance(name, str) else str(name)
    if _SAFE_FIELD_NAME.match(text) and not detect_credentials(text):
        return text
    return "<masked field name>"


class ContextValidationError(ValueError):
    """A caller-supplied field failed validation. Message names the field only."""


# --------------------------------------------------------------------------
# Numbers — finite and bounded, failing CLOSED
# --------------------------------------------------------------------------


def finite_in_range(field: str, value: object, low: float, high: float) -> float:
    """Coerce *value* to a finite float within [low, high], or raise.

    Rejects bools (``isinstance(True, int)`` is True in Python, so a JSON
    ``true`` would otherwise arrive as 1), non-numeric strings, NaN, +/-Infinity
    and out-of-range magnitudes. ``float("nan")`` and the string ``"NaN"`` both
    parse without error and then compare False against every bound, which is
    why the finiteness test is explicit rather than left to the comparison.
    """
    name = mask_field_name(field)
    if isinstance(value, bool):
        raise ContextValidationError(f"{name} must be a number.")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if not text:
            raise ContextValidationError(f"{name} must be a number.")
        try:
            number = float(text)
        except ValueError:
            raise ContextValidationError(f"{name} must be a number.") from None
    else:
        raise ContextValidationError(f"{name} must be a number.")
    if not math.isfinite(number):
        raise ContextValidationError(f"{name} must be a finite number.")
    if not (low <= number <= high):
        raise ContextValidationError(f"{name} must be between {low} and {high}.")
    return number


# --------------------------------------------------------------------------
# Strings — inert identifiers
# --------------------------------------------------------------------------

# The inert alphabet for any caller string that renders into the briefing.
_INERT_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# Instrument symbols are conventionally upper-case and may carry a dot or a
# hyphen (share classes, exchange suffixes). They render into the briefing, so
# they are locked just as tightly.
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")

MAX_CONTEXT_KEYS = 8


def validate_inert_identifier(field: str, value: object) -> str:
    """Return *value* as an inert ``[a-z0-9_]{1,32}`` identifier, or raise."""
    name = mask_field_name(field)
    if not isinstance(value, str):
        raise ContextValidationError(f"{name} must be a string.")
    candidate = value.strip().lower()
    if not _INERT_RE.match(candidate):
        raise ContextValidationError(f"{name} must match [a-z0-9_] and be 1-32 characters.")
    return candidate


def validate_symbol(field: str, value: object) -> str:
    """Return *value* as an instrument symbol, or raise.

    Symbols are the only caller strings rendered verbatim into the briefing's
    alert and event lines, so the alphabet is closed rather than sanitised.
    """
    name = mask_field_name(field)
    if not isinstance(value, str):
        raise ContextValidationError(f"{name} must be a string.")
    candidate = value.strip().upper()
    if not _SYMBOL_RE.match(candidate):
        raise ContextValidationError(f"{name} must be 1-12 characters of A-Z, 0-9, '.' or '-'.")
    return candidate


def validate_enum(field: str, value: object, allowed: "frozenset[str]", default: str) -> str:
    """Return *value* if it is in *allowed* (case-folded), else *default*.

    Closed-set labels never carry caller text into the output, so an
    unrecognised label degrades to the documented default rather than failing
    the whole request.
    """
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in allowed:
            return candidate
    return default


# --------------------------------------------------------------------------
# Free text that must survive — flattened so it cannot manufacture structure
# --------------------------------------------------------------------------

MAX_HEADLINE_CHARS = 160

# Characters a caller could use to forge Markdown structure inside the rendered
# briefing (a heading, a bullet, a block quote, a code fence, emphasis, a table
# cell). Square brackets are deliberately NOT removed: they carry no structure
# on their own, and stripping them would mangle the platform's own redaction
# sentinel into a bare word. The link form is neutralised instead, below.
_STRUCTURE_CHARS = re.compile(r"[#>*_`|{}]")
_LINK_FORM = re.compile(r"\]\s*\(")
# A leading "12." or "12)" reads as a numbered step once the newline is gone,
# which is the whole point of flattening: strip the enumerator too.
_LEADING_ENUMERATOR = re.compile(r"^\s*\d{1,3}\s*[.)]\s*")


def bound_text(value: object, limit: int = MAX_HEADLINE_CHARS) -> str:
    """Return *value* as a length-bounded string.

    A STRUCTURAL bound only — it caps what the pipeline carries in state. It is
    deliberately NOT a rendering guarantee: flattening belongs to the renderer,
    which is the only place that knows the text is about to become a line of a
    document. Doing both here would leave the renderer's guarantee untestable.
    """
    if not isinstance(value, str):
        return ""
    return value[:limit]


def flatten_render_text(value: object, limit: int = MAX_HEADLINE_CHARS) -> str:
    """Return *value* as a single safe line for rendering into the briefing.

    Newlines, tabs and every other whitespace run collapse to one space, so a
    caller cannot split a headline into what reads as a separate numbered step
    of the adviser's document. Unicode control and format characters are
    dropped, Markdown structure characters are removed, a leading enumerator is
    stripped, and the result is length-capped.
    """
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if unicodedata.category(ch) not in ("Cc", "Cf") or ch in " \t\n\r")
    cleaned = _STRUCTURE_CHARS.sub("", cleaned)
    cleaned = _LINK_FORM.sub("] (", cleaned)
    cleaned = " ".join(cleaned.split())
    cleaned = _LEADING_ENUMERATOR.sub("", cleaned)
    cleaned = cleaned.lstrip("-+=~. ")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


# --------------------------------------------------------------------------
# Injection screening — raw AND markup-stripped, keys AND values
# --------------------------------------------------------------------------

# Chat-template control tokens as a CLASS. The framework scores `<<SYS>>` as a
# low-confidence finding and lets it through, and a phrase-only screen misses
# every one of these, so the class is screened here regardless of wording.
_CONTROL_TOKENS = re.compile(
    r"<\|[^|>]{1,64}\|>"  # <|im_start|>, <|system|>, ...
    r"|\[/?INST\]"  # [INST] / [/INST]
    r"|<</?SYS>>"  # <<SYS>> / <</SYS>>
    r"|<\|?endoftext\|?>",
    re.IGNORECASE,
)

# Directive phrases, anchored on an instruction verb plus its object so that
# ordinary financial prose does not trip them. "Transact as a settlement agent"
# and "Insert Into Trust Holdings" are legitimate domain sentences and must
# pass.
_DIRECTIVE_PHRASES = re.compile(
    r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding)?\s*"
    r"(?:instruction|instructions|rule|rules|prompt|prompts)\b"
    r"|\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)?\s*"
    r"(?:instruction|instructions|rule|rules|prompt|prompts)\b"
    r"|\byou\s+are\s+now\s+(?:a|an|the)\b"
    r"|\bsystem\s*prompt\s*[:=]"
    r"|\boverride\s+(?:your\s+|the\s+)?(?:system\s+)?(?:instruction|instructions|rules?|prompt)\b",
    re.IGNORECASE,
)

# Markup strip used only to build the SECOND screening pass. Removing tags can
# re-assemble a spliced directive ("ig<b>nore all instructions") that the raw
# pass cannot see — and can equally erase a control token, which is why the raw
# pass runs first and independently.
_MARKUP = re.compile(r"<[^>]{0,256}>")


def _screen_one(text: str) -> "list[str]":
    findings: list[str] = []
    if _CONTROL_TOKENS.search(text):
        findings.append("control_token")
    if _DIRECTIVE_PHRASES.search(text):
        findings.append("directive_phrase")
    return findings


def screen_injection(text: object) -> "list[str]":
    """Return injection finding labels for *text*, screening raw and stripped.

    The raw pass catches control tokens before a sanitizer can delete them; the
    markup-stripped pass catches directives spliced apart by inline tags. A
    finding from either pass counts.
    """
    if not isinstance(text, str) or not text:
        return []
    findings = list(_screen_one(text))
    stripped = _MARKUP.sub("", text)
    if stripped != text:
        for label in _screen_one(stripped):
            if label not in findings:
                findings.append(label)
    return findings


def screen_structure(value: object, _depth: int = 0) -> "list[str]":
    """Depth-first injection screen over a parsed payload, KEYS included.

    Scanning after the parse is what makes ``\\u``-escaped payloads visible:
    the escape is resolved by the parser, so the screen sees the literal token.
    """
    findings: list[str] = []
    if _depth > 12:
        return findings
    if isinstance(value, str):
        findings.extend(screen_injection(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            findings.extend(screen_injection(key if isinstance(key, str) else str(key)))
            findings.extend(screen_structure(item, _depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            findings.extend(screen_structure(item, _depth + 1))
    return sorted(set(findings))


# --------------------------------------------------------------------------
# Credential shapes — the framework's detector as the FLOOR, never the ceiling
# --------------------------------------------------------------------------

# Local patterns kept because the framework's set does NOT carry them. The
# framework describes credential FORMATS (sk_live_, sk-, eyJ, AKIA, Bearer,
# db URIs); an assignment such as `password=hunter2hunter2` matches none of
# those shapes. Delegating wholesale would make this screen NARROWER while
# looking like a tightening, so the two sets are UNIONED.
_LOCAL_CREDENTIAL_PATTERNS: "list[tuple[str, re.Pattern[str]]]" = [
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|pwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)" r"\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
    # sk-/pk-/ak- prefixed keys shorter than the framework's 20-character floor.
    ("short_api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    # A brokerage / bank account number must never reach a client briefing:
    # holdings are referenced by instrument symbol only.
    ("account_number", re.compile(r"\b\d{4}-?\d{4,}-?\d{0,8}\b")),
    # National / tax identifier shape (NNN-NN-NNNN). The framework's credential
    # detector does not carry it, and the framework's PII detector cannot be
    # used here as a floor: its `name` heuristic matches any two Title-Case
    # words, so it fires on this template's own headings ("Portfolio Briefing")
    # and would withhold every briefing. An ISO date is NNNN-NN-NN and does not
    # match this shape.
    ("personal_identifier", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]


def detect_credential_shapes(text: object) -> "list[str]":
    """Return credential-finding labels: framework set UNION local set.

    Wider is safe; narrower is a bypass. If the framework catches a shape this
    gate misses, the framework raises *inside* post-process and the wrapper
    discards the node's containment work — so the framework's detector is the
    floor here, and the local patterns above extend it.
    """
    if not isinstance(text, str) or not text:
        return []
    labels = [str(finding["type"]) for finding in detect_credentials(text)]
    for label, pattern in _LOCAL_CREDENTIAL_PATTERNS:
        if pattern.search(text):
            labels.append(label)
    return sorted(set(labels))


# --------------------------------------------------------------------------
# The caller-context contract
# --------------------------------------------------------------------------
#
# `input_context` carries the request's briefing parameters. Every field is
# either an inert identifier or a bounded number — deliberately, because the
# framework's S-3 gate scans InitializeNode's result, which returns
# `input_context` verbatim, so a credential-shaped value on this channel kills
# the run at the first node with an opaque traceback. A contract restricted to
# inert shapes cannot carry one; the adapter screens anyway, because an
# UNDECLARED key is ignored by validators rather than stripped and still
# reaches that first node.

# Alert thresholds are caller-tunable within these bounds. A drawdown alert
# threshold is a negative return percentage; a concentration threshold is a
# positive weight percentage.
DRAWDOWN_PCT_BOUNDS = (-100.0, 0.0)
CONCENTRATION_PCT_BOUNDS = (0.0, 100.0)

CALLER_CONTEXT_IDENTIFIERS = ("portfolio_ref", "advisor_ref", "reporting_period")
CALLER_CONTEXT_NUMBERS = {
    "drawdown_alert_pct": DRAWDOWN_PCT_BOUNDS,
    "concentration_alert_pct": CONCENTRATION_PCT_BOUNDS,
}
CALLER_CONTEXT_FIELDS = tuple(CALLER_CONTEXT_IDENTIFIERS) + tuple(CALLER_CONTEXT_NUMBERS)


def validate_caller_context(raw: object) -> "dict[str, Any]":
    """Validate the caller context, or raise :class:`ContextValidationError`.

    Unknown keys are REJECTED rather than ignored. Ignoring is not stripping:
    an undeclared key survives into ``state["input_context"]`` and reaches the
    framework's own output scan on the first node, where it fails with an error
    the caller cannot act on.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContextValidationError("input_context must be an object.")
    if not raw:
        return {}
    if len(raw) > MAX_CONTEXT_KEYS:
        raise ContextValidationError(f"input_context accepts at most {MAX_CONTEXT_KEYS} fields.")
    unknown = [key for key in raw if key not in CALLER_CONTEXT_FIELDS]
    if unknown:
        names = ", ".join(mask_field_name(key) for key in sorted(unknown, key=str))
        raise ContextValidationError(f"input_context has unsupported field(s): {names}")

    cleaned: dict[str, Any] = {}
    for field in CALLER_CONTEXT_IDENTIFIERS:
        if field in raw:
            cleaned[field] = validate_inert_identifier(field, raw[field])
    for field, (low, high) in CALLER_CONTEXT_NUMBERS.items():
        if field in raw:
            cleaned[field] = finite_in_range(field, raw[field], low, high)
    return cleaned
