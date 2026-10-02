"""Shared request fixtures for the FIN-C2-068 suite.

The canonical portfolio request lives in ONE place and is read from
`deploy/invoke_payload.json`, the same file the deployment smoke check sends.
Tests and deployment therefore assert the same input contract: a payload that
the suite accepts but the deployed agent refuses cannot happen silently.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PAYLOAD_PATH = _REPO_ROOT / "deploy" / "invoke_payload.json"


def deployment_payload() -> "dict[str, Any]":
    """The committed deployment smoke payload, verbatim."""
    return json.loads(_PAYLOAD_PATH.read_text(encoding="utf-8"))


def valid_request() -> str:
    """The canonical portfolio request string (the payload's `input` value)."""
    value = deployment_payload()["input"]
    assert isinstance(value, str)
    return value


def valid_request_obj() -> "dict[str, Any]":
    """The canonical portfolio request, parsed."""
    obj = json.loads(valid_request())
    assert isinstance(obj, dict)
    return obj


def request_with(**overrides: Any) -> str:
    """The canonical request with top-level keys replaced."""
    obj = valid_request_obj()
    obj.update(overrides)
    return json.dumps(obj)


def holding(**overrides: Any) -> "dict[str, Any]":
    """One valid holding, with optional field overrides."""
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "asset_class": "equity",
        "weight": 10.0,
        "market_value": 1000.0,
        "return_pct": 5.0,
        "benchmark": "sp500",
    }
    base.update(overrides)
    return base


def request_with_holdings(*holdings: "dict[str, Any]", **extra: Any) -> str:
    """A request built from the given holdings."""
    payload: dict[str, Any] = {
        "holdings": list(holdings),
        "benchmarks": {"sp500": {"return_pct": 2.0}},
        "market_events": [],
    }
    payload.update(extra)
    return json.dumps(payload)
