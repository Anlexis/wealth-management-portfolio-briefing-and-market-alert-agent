"""AgentCore Platform v1.0"""

# FIN-C2-068 — AnalyzePerformanceNode
# Inner-graph domain node 2: compute performance-vs-benchmark figures across the
# aggregated holdings and produce an analyst-style narrative.
#
# Pattern: document generation. The framework ships no LLM client, so this node
# performs deterministic, rule-based performance synthesis from structured
# upstream facts — fully testable without an LLM. The narrative prompt is
# governed through config/config.yaml (analysis.system_prompt), which reaches
# this no-argument node through State: the framework passes no `config`
# argument to execute(), so a node reading one would silently run on defaults
# it never declared.
#
# Advisory-only: the narrative is descriptive performance attribution. It does
# NOT contain a personalized buy/sell recommendation.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are a wealth-management performance analyst. Write a concise, neutral, "
    "factual performance-attribution narrative comparing the portfolio against "
    "its benchmark from the structured figures provided. Do not invent figures. "
    "Do not give personalized investment advice or buy/sell recommendations."
)

# Every value below has already passed AggregateSourcesNode's finite+bounded
# parser, so this node reads floats directly rather than re-coercing strings —
# a second lenient coercion here would be a way back in for a value the load
# boundary refused.


def _weighted_portfolio_return(holdings: "List[Dict[str, Any]]") -> float:
    """Weight-average the per-holding returns; fall back to a simple mean."""
    total_weight = sum(float(h["weight"]) for h in holdings)
    if total_weight > 0:
        return round(sum(float(h["return_pct"]) * float(h["weight"]) for h in holdings) / total_weight, 4)
    if holdings:
        return round(sum(float(h["return_pct"]) for h in holdings) / len(holdings), 4)
    return 0.0


def _benchmark_return(benchmarks: "Dict[str, Any]") -> float:
    """Pick a representative benchmark return (mean across declared benchmarks)."""
    rates = [
        float(entry["return_pct"]) for entry in benchmarks.values() if isinstance(entry, dict) and "return_pct" in entry
    ]
    if not rates:
        return 0.0
    return round(sum(rates) / len(rates), 4)


def _build_narrative(
    portfolio_ret: float,
    benchmark_ret: float,
    excess: float,
    benchmark_count: int,
    top_contributors: "List[Dict[str, Any]]",
    top_detractors: "List[Dict[str, Any]]",
    system_prompt: str,
) -> str:
    """Deterministic analyst narrative built only from validated figures.

    Production note: replace this deterministic synthesis with a call to the
    configured model, passing `system_prompt` and the structured figures.
    """
    parts: List[str] = []
    if benchmark_count == 0:
        # Saying "outperformed" against a benchmark nobody supplied is the
        # fabrication this template must not commit.
        parts.append(
            f"Over the reporting period the portfolio returned {portfolio_ret:.2f}%. "
            "No benchmark was supplied, so no relative comparison is reported."
        )
    else:
        direction = "outperformed" if excess >= 0 else "underperformed"
        parts.append(
            f"Over the reporting period the portfolio returned {portfolio_ret:.2f}% "
            f"versus the benchmark's {benchmark_ret:.2f}%, an excess return of "
            f"{excess:+.2f}%. The portfolio {direction} its benchmark."
        )
    if top_contributors:
        names = ", ".join(f"{h['symbol']} ({float(h['contribution_pct']):+.2f}%)" for h in top_contributors)
        parts.append(f"Leading contributors: {names}.")
    if top_detractors:
        names = ", ".join(f"{h['symbol']} ({float(h['contribution_pct']):+.2f}%)" for h in top_detractors)
        parts.append(f"Principal detractors: {names}.")
    parts.append(
        "These figures are descriptive performance attribution and are provided "
        "for adviser review only; they are not a recommendation to buy or sell "
        "any security."
    )
    logger.debug("AnalyzePerformanceNode: prompt governance active (%d chars)", len(system_prompt))
    return " ".join(parts)


class AnalyzePerformanceNode(FunctionNode):
    """Performance-vs-benchmark analysis across holdings.

    Input state keys:
        aggregated_portfolio: unified portfolio view (from AggregateSourcesNode)
        agent_config:         seeded runtime configuration (JSON string)

    Output state keys (partial dict):
        performance_analysis: {"narrative": str, "portfolio_return_pct": float,
                               "benchmark_return_pct": float,
                               "excess_return_pct": float, "per_holding": [...],
                               "top_contributors": [...], "top_detractors": [...]}
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> "Dict[str, Any]":
        aggregated: Dict[str, Any] = from_json(state.get("aggregated_portfolio"), {}) or {}
        holdings: List[Dict[str, Any]] = aggregated.get("holdings", []) or []
        benchmarks: Dict[str, Any] = aggregated.get("benchmarks", {}) or {}

        config: Dict[str, Any] = from_json(state.get("agent_config"), {}) or {}
        analysis_cfg = config.get("analysis", {})
        system_prompt = DEFAULT_SYSTEM_PROMPT
        if isinstance(analysis_cfg, dict):
            declared = analysis_cfg.get("system_prompt")
            if isinstance(declared, str) and declared.strip():
                system_prompt = declared.strip()

        portfolio_ret = _weighted_portfolio_return(holdings)
        benchmark_ret = _benchmark_return(benchmarks)
        excess = round(portfolio_ret - benchmark_ret, 4)

        per_holding: List[Dict[str, Any]] = []
        for h in holdings:
            weight = float(h["weight"])
            ret = float(h["return_pct"])
            per_holding.append(
                {
                    "symbol": h["symbol"],
                    "return_pct": ret,
                    "vs_benchmark_pct": round(ret - benchmark_ret, 4),
                    # `weight` is a PERCENTAGE of the portfolio (0-100) — the same
                    # unit the concentration alert threshold compares against — so
                    # the contribution is the fractional weight times the return.
                    # Multiplying the two percentages directly (as this node used
                    # to) reports a 60% position returning 7% as a +420%
                    # contribution.
                    "contribution_pct": round(weight / 100.0 * ret, 4),
                }
            )

        ranked = sorted(per_holding, key=lambda x: float(x["contribution_pct"]), reverse=True)
        top_contributors = [h for h in ranked[:3] if float(h["contribution_pct"]) > 0]
        top_detractors = [
            h
            for h in sorted(per_holding, key=lambda x: float(x["contribution_pct"]))[:3]
            if float(h["contribution_pct"]) < 0
        ]

        narrative = _build_narrative(
            portfolio_ret,
            benchmark_ret,
            excess,
            len(benchmarks),
            top_contributors,
            top_detractors,
            system_prompt,
        )

        performance_analysis: Dict[str, Any] = {
            "narrative": narrative,
            "portfolio_return_pct": portfolio_ret,
            "benchmark_return_pct": benchmark_ret,
            "excess_return_pct": excess,
            "benchmark_count": len(benchmarks),
            "per_holding": per_holding,
            "top_contributors": top_contributors,
            "top_detractors": top_detractors,
        }

        emit_trace_event(
            "performance_analyzed",
            {"holding_count": len(holdings), "excess_return_pct": excess},
            state,
        )
        logger.info(
            "AnalyzePerformanceNode: portfolio=%.2f%% benchmark=%.2f%% excess=%+.2f%%",
            portfolio_ret,
            benchmark_ret,
            excess,
        )
        return {"performance_analysis": to_json(performance_analysis)}
