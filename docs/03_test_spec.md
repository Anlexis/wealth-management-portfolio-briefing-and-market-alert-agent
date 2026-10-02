# FIN-C2-068 — Test Specification (docs/03_test_spec.md)

**Template:** FIN-C2-068 — Wealth Management Portfolio Briefing Agent
**Category:** Cat 2 (nested) | **Pattern:** document generation | **Inheritance:** L1 Base (`AgentBaseGraph`)

This document describes the tests that ship in this repository. Every table row
below corresponds to code under `tests/`; a row with no test behind it is a
defect in this document.

---

## 1. Scope

- Unit tests for each node (4 inner domain nodes + the 2 outer boundary nodes).
- Graph composition, including the two cross-boundary routes the framework does
  not provide: the caller context bridged into the inner graph, and the runtime
  configuration seeded into state.
- End-to-end contract through the REAL ASGI entry point (`src.api.server:app`),
  at the trust level the manifest declares.
- Boundary tests: caller-input contract, output boundary, PII, import
  isolation, State safety, invoke order, framework gate compliance.

All tests are deterministic — no model call, no network.

---

## 2. Unit test cases

### 2.1 AggregateSourcesNode — `tests/unit/test_aggregate_sources_node.py`

| ID | Case | Expectation |
|---|---|---|
| AGG-01 | Valid payload with holdings + benchmarks | normalized holdings, benchmarks and totals |
| AGG-02 | Unparseable / non-object payload | REFUSED (`AgentStatus.ERROR`), no `aggregated_portfolio` |
| AGG-03 | Absent or empty `holdings` | refused — there is nothing to brief on |
| AGG-04 | Non-finite numeric field (`NaN`, `Infinity`, bool, text) | refused, per field |
| AGG-05 | Out-of-range numeric field | refused, naming the field and the bound |
| AGG-06 | Invalid instrument symbol | refused |
| AGG-07 | Invalid `as_of` | refused (a bare `YYYY-MM-DD` is required) |
| AGG-08 | Refusal message | names the field, never the value |
| AGG-09 | More holdings / benchmarks / events than the cap | refused, not truncated |
| AGG-10 | Cap tightened in `config/config.yaml` | the declared value changes the outcome |
| AGG-11 | Event category / severity outside the closed set | degrades to the documented default |
| AGG-12 | Headline longer than the bound | stored truncated |
| AGG-13 | Audit | `sources_aggregated` and `portfolio_payload_refused` both emitted |

### 2.2 AnalyzePerformanceNode — `tests/unit/test_analyze_performance_node.py`

| ID | Case | Expectation |
|---|---|---|
| PERF-01 | Holdings + one benchmark | `excess_return_pct == portfolio - benchmark` |
| PERF-02 | Weighted vs unweighted return | weight-average when total weight > 0, mean otherwise |
| PERF-03 | Contribution arithmetic | fractional weight × return, not percentage × percentage |
| PERF-04 | Two very different inputs | the reported return moves with the input |
| PERF-05 | Mixed positive/negative contributions | `top_contributors` / `top_detractors` partitioned |
| PERF-06 | **No benchmark supplied** | no "outperformed"/"underperformed" claim is made |
| PERF-07 | Narrative content | carries the not-a-recommendation sentence |
| PERF-08 | `analysis.system_prompt` declared in config | read from the seeded state, not from a `config` argument |
| PERF-09 | Unusable declared prompt | falls back to the documented default |
| PERF-10 | Audit | `performance_analyzed` emitted |

### 2.3 ScreenEventsAndAlertsNode — `tests/unit/test_screen_events_and_alerts_node.py`

| ID | Case | Expectation |
|---|---|---|
| SCR-01 | Holding return below the drawdown threshold | a `drawdown` alert with the right severity |
| SCR-02 | Weight above the concentration threshold | a `concentration` alert |
| SCR-03 | Event for a non-held symbol | filtered out |
| SCR-04 | Event for a held symbol | retained |
| SCR-05 | Threshold declared in `config/config.yaml` | changes the alerts raised |
| SCR-06 | Threshold supplied by the caller | overrides the configured value |
| SCR-07 | Non-finite threshold (caller or config) | fails CLOSED — no alerts are silently suppressed |
| SCR-08 | Out-of-range threshold | refused |
| SCR-09 | Refusal message | names the field, never the value |
| SCR-10 | Audit | `events_and_alerts_screened` / `alert_thresholds_refused` |

### 2.4 GenerateBriefingNode — `tests/unit/test_generate_briefing_node.py`

| ID | Case | Expectation |
|---|---|---|
| BRF-01 | Monetary aggregate | rendered on the nearest-1,000 grid |
| BRF-02 | Schema note | present in the document |
| BRF-03 | `briefing.external_round_unit` changed | the rendered grid changes |
| BRF-04 | Individual position values | never rendered |
| BRF-05 | Disclaimer | always present |
| BRF-06 | No validated holdings | refuses rather than reporting a zero return |
| BRF-07 | No benchmark | reported as "not supplied", not as a comparison |
| BRF-08 | Caller references | rendered as inert identifiers |
| BRF-09 | **Headline containing newlines and a numbered step** | stays inside the bullet the renderer wrote |
| BRF-10 | Alert lines | only validated fields interpolated |
| BRF-11 | Audit | `portfolio_briefing_generated` / `portfolio_briefing_refused` |

### 2.5 PreProcessNode / PostProcessNode — `tests/unit/test_pre_post_process_nodes.py`

| ID | Case | Expectation |
|---|---|---|
| PRE-01 | Valid request | `validated_input` set; caller fields serialized for the bridge |
| PRE-02 | Rejection | `validated_input` cleared, `caller_fields` cleared |
| PRE-03 | Audit | accepted / refused events both emitted |
| POST-01 | Clean briefing | surfaced unchanged |
| POST-02 | Empty result | withheld with a TRUTHY notice |
| POST-03 | Disallowed content | every output-bearing field overwritten |
| POST-04 | Off-grid aggregate | snapped |
| POST-05 | Audit | emitted / withheld events both emitted |

---

## 3. Boundary tests

### 3.1 Caller-input contract — `tests/proof_of_boundary/test_s1_input_boundary.py`

Every case calls `execute()` DIRECTLY, with no framework wrapper in front: a
guarantee that only holds while the framework's own gate is configured on is
not a guarantee this template owns.

| ID | Case | Expectation |
|---|---|---|
| IN-01 | Empty / non-string / oversized request | refused |
| IN-02 | Refusal | writes a TRUTHY `formatted_output` and `result` |
| IN-03 | Free text instead of a portfolio | refused, not briefed |
| IN-04 | Chat-template control tokens (`<\|im_start\|>`, `[INST]`, `<<SYS>>`) | refused |
| IN-05 | Hostile field NAME | refused |
| IN-06 | Directive spliced by inline markup | refused (post-strip pass) |
| IN-07 | `\u`-escaped control token | refused (post-parse pass) |
| IN-08 | Legitimate domain prose containing screen keywords | NOT refused |
| IN-09 | Unknown `input_context` key | refused, not ignored |
| IN-10 | Non-inert identifier | refused; case is normalized, not refused |
| IN-11 | Rejected value | never echoed back |
| IN-12 | Non-finite / out-of-range threshold, per field | refused |
| IN-13 | Numeric positions and ISO dates | survive redaction intact |
| IN-14 | Client-identifier keys / patterns | redacted |
| IN-15 | Credential-shaped payload value | refused |

### 3.2 Output boundary — `tests/proof_of_boundary/test_s3_output_gate.py`

| ID | Case | Expectation |
|---|---|---|
| OUT-01 | Credential, key, token, connection string, account or national-identifier shape | withheld |
| OUT-02 | Every output-bearing field | overwritten with a truthy notice |
| OUT-03 | `error_log` | closed-set labels, never matched text |
| OUT-04 | Leak nested inside a mapping | detected (with a top-level control) |
| OUT-05 | Empty result | withheld, fails CLOSED |
| OUT-06 | Clean briefing | byte-identical |
| OUT-07 | Monetary forms (grouped, bare, symbol, signed, symmetric, spaced) | snapped to the grid |
| OUT-08 | Decimals, percentages, ratios | byte-identical |
| OUT-09 | `JPY 1234.56m` | the fraction cannot be backtracked out of |
| OUT-10 | Dates, timestamps, tickers, inert references, counts | byte-identical |
| OUT-11 | `Currency: JPY\n\n3. Cash Position` | the delimiter never spans a paragraph break |
| OUT-12 | `BRK-1234` vs `JPY-9999` | identifier preserved, currency amount snapped |
| OUT-13 | `BRK 1234` (separated) | still snaps — fail-safe on the ambiguous case |
| OUT-14 | `SSN 123-45-6789` / `TAX 987-65-4321` | withheld, not mangled by the snap |

### 3.3 Other boundaries

| ID | Case | Expectation |
|---|---|---|
| PB-6 | Full invoke at the DECLARED trust level | backbone runs in order, terminal SUCCESS (`test_pb_invoke_order.py`) |
| PB-7 | HITL interrupt propagation | skipped — `config/config.yaml` does not enable HITL |
| PB-IMPORT | No prohibited import under `src/` | `test_import_isolation.py` |
| PB-STATE | No credential-like field or prohibited type in State | `test_state_safety.py` |
| PB-PII | No client identifier reaches the output; the portfolio still does | `test_pii_no_leak.py` |
| TC-06/07 | The framework's gate methods cannot be overridden | `test_framework_compliance_tc06_tc07.py` |

---

## 4. End-to-end contract — `tests/integration/test_invoke_contract.py`

Driven over HTTP against the real ASGI app, with Bearer auth.

| ID | Case | Expectation |
|---|---|---|
| E2E-01 | `/health` | reports the agent |
| E2E-02 | Missing / wrong token | 401, with an identical body either way |
| E2E-03 | **The committed `deploy/invoke_payload.json`** | 200 and SUCCESS |
| E2E-04 | Caller payload | the reported holdings and total come from it |
| E2E-05 | Two very different inputs | the reported number moves |
| E2E-06 | Each alert severity path, and the no-alert path | all reachable |
| E2E-07 | Caller context | reaches the rendered document |
| E2E-08 | Rendered aggregate | on the documented grid, with the schema note |
| E2E-09 | Oversized input / unknown context field | 400 |
| E2E-10 | Non-finite threshold, including raw JSON `Infinity` / `NaN` | 400 naming the field |
| E2E-11 | Credential-shaped context value | 400 naming the field, never the value |
| E2E-12 | Ordinary domain text on the same field | 200 |
| E2E-13 | Free text / hostile request | ERROR, nothing published |
| E2E-14 | Error envelope | no traceback, no source path |
| E2E-15 | Credential reaching the rendered document | never released to the caller |

---

## 5. Deployment quality gate

This template performs no retrieval, so retrieval-quality metrics do not apply.
The gate is functional: the committed `deploy/invoke_payload.json` must return a
non-empty briefing carrying the advisory-only disclaimer and the output-schema
note, with the output boundary verified against a credential-injection probe.
E2E-03 asserts that exact payload, so a deployment smoke check that would be
refused fails the suite first.
