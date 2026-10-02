# FIN-C2-068 — Design (docs/02_design.md)

**Template:** FIN-C2-068 — Wealth Management Portfolio Briefing Agent
**Category:** Cat 2 (domain-specific pipeline)
**Pattern:** document generation
**Industry:** FIN
**Architecture:** Two-layer nested (outer backbone + inner domain workflow)

| Field | Value |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |

> **Advisory-only scope.** This template produces an informational portfolio
> briefing as decision-support material for a licensed adviser. It NEVER
> executes a trade and NEVER issues a personalized buy/sell recommendation.

---

## 1. Purpose

Wealth advisers spend significant preparation time before each client review:
pulling holdings and market data from multiple systems, computing
performance-versus-benchmark figures, scanning for holding-relevant market
events, and writing up a client-ready briefing. FIN-C2-068 automates the
assembly of that briefing from a structured payload the adviser supplies,
producing a consistent, compliance-aware draft the adviser reviews and
finalizes.

The design's governing constraint is that the output is a document a human will
act on. A briefing assembled from a payload the pipeline only half understood
is indistinguishable from one assembled from a complete payload, so **every
stage fails closed**: a request that cannot be fully validated is refused with
a message naming the field, never degraded into a document.

---

## 2. Architecture (two-layer nested)

```
Outer backbone (AgentBaseGraph — fixed):

  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                          |  (RETRY, max 3)
                                       pre_process

  main = PortfolioBriefingGraphNode (GraphNode) -> delegates to the inner graph

Inner domain workflow (DomainWorkflowGraph — BaseGraph, fully custom topology):

  START
    -> aggregate_sources         (AggregateSourcesNode)
    -> analyze_performance       (AnalyzePerformanceNode)
    -> screen_events_and_alerts  (ScreenEventsAndAlertsNode)
    -> generate_briefing         (GenerateBriefingNode)
    -> END
```

- The outer backbone is never modified; `add_edges()` is **not** overridden on
  the outer graph. Its conditional edge uses the inherited `route(self, state)`,
  whose parameter carries no annotation, so the runtime hands it the full State
  rather than projecting fields away.
- The `main` slot is a `GraphNode` subclass (`PortfolioBriefingGraphNode`) that
  returns the inner `DomainWorkflowGraph` from `get_subgraph()`, pulls
  `validated_input` via `extract_input()`, and maps the inner output back via
  `merge_output()`.
- The inner graph inherits `BaseGraph` and implements all 7 ABC methods. Its
  `route()` is annotated with this graph's own `State`, so a conditional edge
  added later cannot silently route on projected-away fields.

### Two routes the framework does not provide

Both are load-bearing, and both are proved end to end rather than at node level:

1. **The caller context.** `GraphNode.execute()` invokes the inner graph as
   `subgraph.invoke(user_input, session_id=..., ctx=...)` and does not forward
   `input_context`. `src/graph/context_bridge.py` carries the VALIDATED caller
   contract across on a `ContextVar`: `extract_input()` stashes it before the
   invoke, and the inner graph's `_extra_initial_state()` reads it back inside.
   The contract cannot ride inside `validated_input` instead — the framework
   masks that field at every node boundary, so a caller value can be rewritten
   between hops.
2. **The runtime configuration.** The framework passes no `config` argument to
   a node's `execute()`, so a node that read one would run on defaults it never
   declared. `src/graph/graph.py` loads `config/config.yaml`, forwards it to the
   inner graph as `config["configurable"]`, and the inner graph seeds it into
   State as `agent_config` for the no-argument domain nodes.

### Directory layout

```
src/graph/graph.py                 — outer graph (FinC2068Agent + Graph alias)
src/graph/domain_workflow_graph.py — inner graph (DomainWorkflowGraph)
src/graph/context_bridge.py        — caller-context hand-off across the boundary
src/nodes/pre_process_node.py      — caller-contract gate
src/nodes/post_process_node.py     — output boundary
src/nodes/aggregate_sources_node.py
src/nodes/analyze_performance_node.py
src/nodes/screen_events_and_alerts_node.py
src/nodes/generate_briefing_node.py
src/services/security.py           — bounded parsers, inert-identifier rules, screens
src/schemas/state.py               — flat State(AgentState) TypedDict
src/api/server.py                  — ASGI entry point (auth, caller contract, caps)
config/agent.yaml                  — flat registry manifest
config/config.yaml                 — runtime parameters
```

---

## 3. State schema (`src/schemas/state.py`)

`State` is a flat `TypedDict` extending `AgentState` — no Pydantic, no bare
dict/list in a checkpointed field; structured fields are JSON strings via
`to_json()` / `from_json()`.

| Field | Type | Producer | Notes |
|---|---|---|---|
| `validated_input` | `Optional[str]` | PreProcessNode | screened, redacted adviser payload |
| `caller_fields` | `Optional[str]` (JSON) | PreProcessNode | the validated caller contract, bridged inward |
| `agent_config` | `Optional[str]` (JSON) | inner graph | runtime configuration for the no-argument nodes |
| `aggregated_portfolio` | `Optional[str]` (JSON) | AggregateSourcesNode | validated holdings + benchmarks + events + totals |
| `performance_analysis` | `Optional[str]` (JSON) | AnalyzePerformanceNode | narrative + figures + per-holding attribution |
| `screened_events` | `Optional[str]` (JSON) | ScreenEventsAndAlertsNode | relevant events + alerts + resolved thresholds |
| `portfolio_briefing` | `Optional[str]` | GenerateBriefingNode | the rendered client briefing |
| `trace_id` / `correlation_id` | `Optional[str]` | framework | audit; not written by node code |

No credential, secret, account-number or client-identifier field exists in
State. Holdings are referenced by instrument symbol only.

---

## 4. Node responsibilities

| Node | Slot / layer | Responsibility | Writes |
|---|---|---|---|
| `PreProcessNode` | outer pre_process | caller contract: validate, screen, redact | `validated_input`, `caller_fields` |
| `PortfolioBriefingGraphNode` | outer main | delegate to the inner graph; contain inner failures | (delta) |
| `PostProcessNode` | outer post_process | **output boundary**: scan, enforce the schema, withhold | `formatted_output`, `result`, `portfolio_briefing` |
| `AggregateSourcesNode` | inner | validate every caller field against explicit bounds | `aggregated_portfolio` |
| `AnalyzePerformanceNode` | inner | performance vs benchmark + attribution narrative | `performance_analysis` |
| `ScreenEventsAndAlertsNode` | inner | match events to holdings; raise threshold alerts | `screened_events` |
| `GenerateBriefingNode` | inner (terminal) | render the briefing on the output grid | `portfolio_briefing`, `status` |

---

## 5. The caller-input contract

### 5.1 The request body

A JSON object. Free text is refused: accepting it produced a confident client
briefing reporting zero holdings, a zero return and "the portfolio outperformed
its benchmark", with success status.

| Field | Shape | Bounds |
|---|---|---|
| `holdings[].symbol` | instrument symbol | 1–12 chars of `A-Z 0-9 . -`, required |
| `holdings[].weight` | percentage of the portfolio | finite, 0–100 |
| `holdings[].market_value` | number | finite, 0–99,999,999,999 |
| `holdings[].return_pct` | percentage | finite, ±10,000 |
| `holdings[].asset_class`, `.benchmark` | label | folded to an inert token |
| `benchmarks.<name>.return_pct` | percentage | finite, ±10,000 |
| `market_events[].symbol` | instrument symbol | as above |
| `market_events[].headline` | free text | bounded; flattened at render |
| `market_events[].category`, `.severity` | closed set | unknown values degrade to the default |
| `as_of` | date | `YYYY-MM-DD` |

The market-value ceiling is 11 digits for a concrete platform reason: the
framework's data filter masks any 12-digit run inside `validated_input` before
the load node runs, so a 12-digit position would arrive as the literal
`[MASKED]` and the payload would stop being valid JSON. Refusing it names the
field; accepting it would silently zero the portfolio.

### 5.2 `input_context`

Every field is either an inert `[a-z0-9_]{1,32}` identifier
(`portfolio_ref`, `advisor_ref`, `reporting_period`) or a bounded number
(`drawdown_alert_pct` in [-100, 0], `concentration_alert_pct` in [0, 100]).
Unknown keys are **refused, not ignored**: an ignored key is not a stripped key
— it survives into `state["input_context"]`, which `InitializeNode` returns
verbatim in its result, where the framework's output scan sees it.

The adapter additionally screens the assembled context with the framework's own
`detect_credentials_in_value` before `invoke()`. A credential-shaped value there
kills the run at the first node with a traceback the caller cannot act on; the
request cannot succeed either way, so it is converted into a 400 naming the
field (never the value). 400, not 422: 422 belongs to the request-model
validator and returns a different body shape.

### 5.3 Numbers

Every caller-controlled number goes through one finite+bounded parser. `NaN`
and `Infinity` parse cleanly through `float()` and then compare False against
every threshold — a silent fail-OPEN on exactly the alert decision this agent
exists to make — so finiteness is asserted explicitly rather than left to the
comparison. Booleans are rejected (`isinstance(True, int)` is True in Python).

### 5.4 Screens

The injection screen covers chat-template control tokens as a **class**
(`<|…|>`, `[INST]`, `<<SYS>>`), not only directive phrases, and runs over the
raw request and again over the markup-stripped text, keys included, after the
JSON parse. Both passes are needed: a markup strip can delete a control token
and forward the surrounding directive as ordinary text, and it can equally
re-assemble a directive spliced apart by inline tags. Directive patterns are
anchored on a verb plus its object so that ordinary financial prose
("Transact as a settlement agent", "Insert Into Trust Holdings") is not refused.

Client-identifier redaction is **structural**: it runs on the parsed payload,
replacing PII-keyed values wholesale and pattern-redacting string values only.
Running it over the serialized payload rewrote a position of 1,240,000 to
`[REDACTED].0` and broke the JSON.

---

## 6. The output boundary

The documented output schema is: **monetary figures are reported as aggregates
rounded to the nearest 1,000; individual position values are not reported.**
`GenerateBriefingNode` renders on that grid and writes the schema note into the
document; `PostProcessNode` enforces it independently, so a renderer that
forgot would not ship full-precision client holdings.

Two independent layers, each with its own audit event:

1. **A disallowed-content scan**, run BEFORE the numeric snap and again after
   it. Order matters: the snap treats any standalone three-letter upper-case
   word as a currency marker, so `SSN 123-45-6789` would become `SSN 0-45-6789`
   and destroy the pattern the scan is looking for. The scan walks nested
   structures, not just top-level strings. Its pattern set is the **union** of
   the framework's own credential detector and this template's local patterns —
   wider is safe, narrower is a bypass. The local set carries what the framework
   does not: credential assignments (`password=…`), short `sk-/pk-/ak-` keys,
   account numbers, and the `NNN-NN-NNNN` national-identifier shape.
2. **The precision grid.** Monetary values are recognised by FORM
   (comma-grouped, or a run of 5+ digits) and by CURRENCY CONTEXT (a three-letter
   code or a symbol, before or after, attached or separated by any horizontal
   whitespace, signed or not). Decimal fractions are absorbed into the same
   token so a fraction is never rewritten as a value of its own. The grammar is
   wrapped in identifier guards over this template's own render alphabet, and
   the attached `<three letters>-<digits>` form is decided against ISO 4217 —
   `JPY-9999` is a signed amount, `BRK-1234` is an instrument symbol. Only the
   attached form is narrowed; a separated marker still snaps, so the gate stays
   fail-safe on the genuinely ambiguous case.

**Containment.** The framework's envelope is
`state["formatted_output"] or state["result"]`, and that fallback applies on
ERROR status too — a falsy replacement re-opens it. So a violation returns ERROR
**and overwrites every output-bearing field with a truthy notice**. Raising, or
returning ERROR without clearing, would still ship the un-gated briefing inside
the error envelope. Every non-success path is covered the same way: the caller
contract's refusal, the inner workflow's refusal (contained by
`on_subgraph_error`, which surfaces only closed field-naming reasons and never a
traceback), and the output boundary's own empty-result path.

The outer `get_output()` substitutes a closed-set notice ONLY when no node ever
wrote an output-bearing field — which happens when the framework's own input
gate refuses from inside a node wrapper. It never replaces a value another node
produced, so it cannot stand in for the output boundary's clearing.

---

## 7. Configuration (`config/config.yaml`)

Every value is read at runtime; a declaration with no reader would be dead
configuration.

| Key | Read by |
|---|---|
| `max_retry`, `timeout_s` | the framework's backbone |
| `alert_thresholds.drawdown_pct` / `.concentration_pct` | ScreenEventsAndAlertsNode |
| `limits.max_holdings` / `.max_benchmarks` / `.max_market_events` | AggregateSourcesNode |
| `briefing.external_round_unit` | GenerateBriefingNode |
| `analysis.system_prompt` | AnalyzePerformanceNode |

`config/agent.yaml` is the flat registry manifest: identity, category,
`generation_mode`, entry class, `required_trust_level`, and `requires`. It
declares `secrets: []` and `extras: []` because this template constructs no
client and calls no `ctx.secrets.require()`; declaring an unprovisioned secret
would make the agent fail at compile time.

---

## 8. Trust and error handling

`required_trust_level: VERIFIED_EXTERNAL` is the declared entry contract, and
**every node requires exactly that**. The nodes previously demanded INTERNAL
while the manifest advertised VERIFIED_EXTERNAL, so a caller arriving at the
declared level was refused by the first inner node and the deployed agent could
not serve a request at all. The domain nodes perform deterministic computation
over data the caller supplied and reach no privileged resource, so the entry
contract is the boundary that matters.

The standalone entry point is the auth boundary: with `INVOKE_AUTH_TOKEN` set,
an otherwise-anonymous caller must present it as a Bearer token and runs at
VERIFIED_EXTERNAL; trust established by upstream middleware is never demoted.
Without the variable set, every caller stays anonymous and the trust gate
refuses before any work happens — so the variable is required, not optional.

`PortfolioBriefingGraphNode.error_strategy = "handle"`: an inner failure becomes
a contained, actionable refusal rather than a `SubgraphError` whose traceback
the framework packs into `error_log` while leaving the caller with nothing.

---

## 9. Known platform interactions

- The framework's data filter masks any two-word Title-Case run inside
  `validated_input` as a personal name. An event headline written in title case
  therefore reaches the renderer as `[MASKED]`. The run still succeeds and the
  portfolio figures are unaffected; only that headline degrades.
- The same filter masks 12-digit and 16-digit numeric runs, which is why the
  market-value bound stops at 11 digits (see §5.1).
