# FIN-C2-068 — Wealth Management Portfolio Briefing Agent

> **Category**: Cat 2 (a fixed multi-step pipeline for one job-to-be-done)
> **Industry**: FIN

## Overview

Turns an adviser's portfolio and market data into a client-ready briefing.

Send it a portfolio — positions with their weights, values and period returns, the benchmarks to
compare against, and any market events worth flagging — and it computes performance against the
benchmark, attributes the result to individual positions, screens the events down to the
instruments actually held, raises threshold-breach alerts for drawdowns and over-concentration, and
writes the whole thing up as a Markdown briefing the adviser reviews and finalises.

It is decision-support material, not advice. The briefing describes what happened and flags what
may need attention; it never issues a buy or sell recommendation, and the agent never executes a
trade.

Two properties are worth knowing before you adapt it. First, it refuses rather than guesses: a
request it cannot fully validate — a missing benchmark, a non-finite threshold, a position value it
cannot read — comes back as an error naming the field, because a briefing assembled from a
half-understood payload is indistinguishable from a real one. Second, the document reports monetary
**aggregates only**, rounded to the nearest 1,000, and an output boundary enforces that
independently of the renderer, alongside a scan that withholds the whole briefing if a
credential-shaped string or a personal identifier ever reaches it.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Calling it

`POST /invoke` takes the portfolio as a JSON-encoded string in `input`, plus an optional
`input_context` carrying the briefing parameters:

```json
{
  "input": "{\"holdings\": [{\"symbol\": \"AAPL\", \"weight\": 60.0, \"market_value\": 612345.0, \"return_pct\": 7.0}], \"benchmarks\": {\"sp500\": {\"return_pct\": 5.0}}}",
  "input_context": {"portfolio_ref": "acct_7742", "drawdown_alert_pct": -7.5}
}
```

`weight` is a percentage of the portfolio (0–100), the same unit the concentration alert compares
against. Every `input_context` field is either an inert `[a-z0-9_]` identifier or a bounded number;
unknown fields are refused rather than ignored. When `INVOKE_AUTH_TOKEN` is set, callers present it
as a Bearer token — without it every caller stays anonymous and the trust gate refuses the request
before any work happens.

## Project Structure

```
src/          agent implementation (nodes, graph, schemas, services)
tests/        unit, integration and boundary tests
config/       agent manifest (agent.yaml) and runtime parameters (config.yaml)
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. `config/config.yaml` holds the alert thresholds, the structural caps and the rounding grid.
   Every value there is read at runtime — change one and the behaviour changes.
2. Replace the sample data and benchmarks with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic, and
   `src/services/security.py` for the caller-input contract.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
