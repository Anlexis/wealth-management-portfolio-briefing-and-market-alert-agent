"""ScreenEventsAndAlertsNode — event matching and threshold-breach alerts."""

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.screen_events_and_alerts_node import (
    DEFAULT_CONCENTRATION_PCT,
    DEFAULT_DRAWDOWN_ALERT_PCT,
    ScreenEventsAndAlertsNode,
)
from src.schemas.state import from_json, to_json

_HOLDINGS = [
    {"symbol": "AAPL", "weight": 60.0, "return_pct": -12.0, "market_value": 600.0},
    {"symbol": "GOVT", "weight": 10.0, "return_pct": 3.0, "market_value": 100.0},
]
_EVENTS = [
    {"symbol": "AAPL", "headline": "guidance cut", "category": "guidance", "severity": "high"},
    {"symbol": "TSLA", "headline": "not held", "category": "macro", "severity": "info"},
]


def _run(holdings=None, events=None, config=None, caller=None):
    state = {
        "aggregated_portfolio": to_json(
            {
                "holdings": holdings if holdings is not None else _HOLDINGS,
                "benchmarks": {},
                "market_events": events if events is not None else _EVENTS,
                "totals": {},
                "as_of": "",
            }
        ),
    }
    if config is not None:
        state["agent_config"] = to_json(config)
    if caller is not None:
        state["input_context"] = caller
    return ScreenEventsAndAlertsNode().execute(state)


def _screened(**kwargs):
    result = _run(**kwargs)
    assert "screened_events" in result, result
    return from_json(result["screened_events"])


class TestEventMatching:
    def test_only_held_symbols_are_reported(self):
        screened = _screened()
        assert [e["symbol"] for e in screened["events"]] == ["AAPL"]

    def test_no_events_is_not_an_error(self):
        assert _screened(events=[])["events"] == []


class TestThresholds:
    def test_documented_defaults_apply_without_config(self):
        screened = _screened()
        assert screened["thresholds"] == {
            "drawdown_pct": DEFAULT_DRAWDOWN_ALERT_PCT,
            "concentration_pct": DEFAULT_CONCENTRATION_PCT,
        }

    def test_configured_threshold_is_live(self):
        """A declared value that did not change behaviour would be a dead
        declaration in the shipped configuration."""
        loose = _screened(config={"alert_thresholds": {"drawdown_pct": -50.0}})
        assert not [a for a in loose["alerts"] if a["type"] == "drawdown"]
        tight = _screened(config={"alert_thresholds": {"drawdown_pct": -5.0}})
        assert [a for a in tight["alerts"] if a["type"] == "drawdown"]

    def test_caller_override_beats_config(self):
        screened = _screened(
            config={"alert_thresholds": {"drawdown_pct": -50.0}},
            caller={"drawdown_alert_pct": -5.0},
        )
        assert screened["thresholds"]["drawdown_pct"] == -5.0
        assert [a for a in screened["alerts"] if a["type"] == "drawdown"]

    def test_concentration_alert_uses_the_weight_percentage(self):
        screened = _screened(caller={"concentration_alert_pct": 50.0})
        symbols = {a["symbol"] for a in screened["alerts"] if a["type"] == "concentration"}
        assert symbols == {"AAPL"}  # 60% breaches, 10% does not

    @pytest.mark.parametrize("field", ["drawdown_alert_pct", "concentration_alert_pct"])
    @pytest.mark.parametrize(
        "bad",
        [
            "NaN",
            "Infinity",
            "-Infinity",
            float("nan"),
            float("inf"),
            True,
            "abc",
            [],
            {},
        ],
    )
    def test_non_finite_caller_threshold_fails_closed(self, field, bad):
        """A NaN threshold compares False against every holding and silently
        suppresses the alert the adviser is relying on."""
        result = _run(caller={field: bad})
        assert result["status"] == AgentStatus.ERROR.value
        assert "screened_events" not in result

    @pytest.mark.parametrize("bad", ["NaN", float("inf"), "abc", True])
    def test_non_finite_configured_threshold_fails_closed(self, bad):
        result = _run(config={"alert_thresholds": {"drawdown_pct": bad}})
        assert result["status"] == AgentStatus.ERROR.value

    @pytest.mark.parametrize("bad", [1.0, 100.0, -101.0])
    def test_out_of_range_drawdown_threshold_fails_closed(self, bad):
        assert _run(caller={"drawdown_alert_pct": bad})["status"] == AgentStatus.ERROR.value

    def test_refusal_names_the_field_not_the_value(self):
        result = _run(caller={"drawdown_alert_pct": "9999.5"})
        joined = " ".join(result["error_log"])
        assert "drawdown_alert_pct" in joined
        assert "9999.5" not in joined


class TestSeverity:
    def test_severity_scales_with_the_breach(self):
        holdings = [
            {"symbol": "A", "weight": 1.0, "return_pct": -11.0, "market_value": 1.0},
            {"symbol": "B", "weight": 1.0, "return_pct": -25.0, "market_value": 1.0},
        ]
        alerts = {a["symbol"]: a["severity"] for a in _screened(holdings=holdings, events=[])["alerts"]}
        assert alerts == {"A": "medium", "B": "high"}


class TestAuditTrace:
    def test_emits_a_domain_event(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "src.nodes.screen_events_and_alerts_node.emit_trace_event",
            lambda name, payload, state: seen.append(name),
        )
        _run()
        _run(caller={"drawdown_alert_pct": "NaN"})
        assert "events_and_alerts_screened" in seen
        assert "alert_thresholds_refused" in seen
