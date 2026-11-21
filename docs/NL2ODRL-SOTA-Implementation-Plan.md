# NL2ODRL → SOTA Implementation Plan (Phase-wise)

**Scope:** `nl2odrl-responsible-ai` v2.0.0 (+ `agentcrash-core` v0.4.0 integration boundary)
**Basis:** [Nl2odrl Architecture Review.md](/C:/Users/sahil/Downloads/Nl2odrl%20Architecture%20Review.md) + code verification against `C:\Dev\Projects\NL-to-ODRL` and `C:\Dev\Projects\AgentCrash` (Nov 2026)
**Live baseline verified:** NL-to-ODRL `.venv` runs `agentcrash-core==0.2.0` today — all imports succeed. The breakage is **latent**: any fresh resolve to `>=0.3.0` hits `ImportError` (`agentcrash.db`, `reference_patch_for` removed in 0.3.0 per `AgentCrash/CHANGELOG.md:33-36`). `pytest --collect-only` totals **189 tests / 20 files** (review says 182 — stale).
**Status:** Plan — no code changed by this document.

## Guiding principles (non-negotiable)

1. **Determinism where machines decide.** Validation, version checks, and persistence are code, never prompt persuasion. LLMs do semantics (extraction, disambiguation, bounded repair) only.
2. **Authorship separation.** The LLM drafts; a deterministic gate plus human/policy approval publishes. The model never holds the publishing credential.
3. **Provenance or it did not happen.** Every emitted rule traces to an input span. Ungrounded entities and silent assumptions are first-class failures, not warnings.
4. **Shrink-only repair.** Bounded repair (max 2 iterations) may narrow grants, never widen them.
5. **No fixture science.** Scenario fixtures remain regression fixtures; all generality claims come from the held-out benchmark (Phase 4).

## Verified starting point (what the plan builds on)

- `odrl_agent/` has 9 files (8 modules + `__init__`): `adapter.py` (688), `cli.py` (895), `dspy_opt.py` (497), `odrl.py` (227), `strict.py` (156), `tools.py` (254), `verifiers.py` (171), `web.py` (1092).
- Execution today: `web.py:build_inline_run` → `run_and_store` → `adapter.py:run_react_loop` (max 12 steps, `muse-spark-1.3`) over `parse_policy` (fixture lookup / empty echo) → `validate_odrl` → `save_odrl(read/write)` on in-memory `Ledger`.
- `LlamaAdapter` (`adapter.py:270-386`): raw `urllib`, `content.find("{")` + `raw_decode` scraping, `OSError`-only retries — no `response_format` JSON Schema, no 429/5xx backoff.
- Validator: `validate_odrl_subset` covers `Set/Offer/Agreement` + `uid` + `permission[].{action,target,assignee}` + `Agreement.assigner` only; values emitted as `rdflib.Literal` (IRIs only for doc `type`); `prohibition`/`obligation`/`duty`/`constraint`/logical operands/`conflict` ignored; `contains_attacker_grant` matches `"attacker-client"` substring only.
- Service: stdlib `ThreadingHTTPServer` (`web.py:1084`), `MAX_ACTIVE_RUNS=2` → `429 E_BUSY`, unbounded `_runs` dict, `log_message` noop, `POST /api/settings` mutates process `os.environ`, `_scripted_transport` monkey-patches `urlopen` + env (restored in `finally`, still thread-unsafe).
- Auth notes (mitigations already present, keep them): `hmac.compare_digest` fail-closed gate, SHA-256 key fingerprints, per-key scoping, `model_status` never returns keys, React `escaper` before `dangerouslySetInnerHTML`.
- DSPy: 10 hardcoded `Offer`-only rows; `score_doc` compares `(action,target,assignee)` triples only; `pack-probe-v1.json` overfit to fixture tokens (`dataset-7`, `research-team`); baseline 0.4667 → optimized 0.7 is not a generality result.
- Dead weight: `strict.py:strict_validate` has zero production callers; `cli.py:599-607` diagnose branches for `poisoned_document`/`concurrent_update` (scenarios dir has only `odrl_convert`, `odrl_convert_poisoned`); `verifiers.py:18` imports private `agentcrash._registry`; zero adoption of `agentcrash.sdk` (`AgentCrash`, `RunContext`, `FaultPolicy`, `verify_sdk_chain`, `/v1/*` control plane, `evaluate.py` fuzzer).

## Phase overview and sequencing

| Phase | Title | Weeks | Depends on | Exit gate |
|---|---|---|---|---|
| 0 | Stabilize & decouple | 1–2 | — | CI green on pinned deps; Baseline v0 frozen |
| 1 | Typed IR + deterministic compiler | 3–5 | 0 | 100% SHACL pass by construction; ReAct retired |
| 2 | Reasoning, grounding & HITL gate | 6–7 | 1 | 0 undetected conflicts; draft-vs-publish enforced |
| 3 | Service hardening | 6–8 (parallel) | 0 | Concurrent, observable, SSRF-free API |
| 4 | Benchmark & CI gates | 8–10 | 1, 2 | 200+ case benchmark + release gates live |
| 5 | SOTA extensions | post-10 | 4 | Profiles, FORCE equivalence, local SLM path |

Phases 2 and 3 run in parallel after Phase 1 lands. No Phase 5 work starts before Phase 4 metrics exist — that ordering is what prevents a repeat of the DSPy overfit.

---

## Phase 0 — Stabilize & decouple (Weeks 1–2)

**Goal:** a working, honest baseline. Nothing new is built; the repo is made safe to change.

### 0.1 Dependency boundary fix (P0)

- Pin `agentcrash-core>=0.4.0,<0.5.0` in `pyproject.toml` (`>=0.2.0` unbounded is the ImportError vector).
- Remove `from agentcrash import db as db_module` from `odrl_agent/web.py:32` (+ use at `:519`) and `reference_patch_for` imports from `odrl_agent/web.py:33-37` and `odrl_agent/cli.py:34-44`.
- Move ownership home: converted policies, tenants, billing, API keys live **100% in `nl2odrl`** (own SQLite tables / SQLAlchemy models). `agentcrash-core` keeps only trace/verification (`TraceWriter`, control-plane `/v1/*` ingestion).
- Move remediation templates out of `agentcrash.policy` into `odrl_agent/remediation.py` (domain-specific patch bodies). Core keeps generic `policy/v1` + RFC 7396 `policy_patch/v1` + `apply_patch`.
- Fix the 4 test files importing removed symbols: `tests/test_replay.py:14`, `tests/test_api_keys.py:7`, `tests/test_billing.py:8`, `tests/test_web_http.py:250`.
- Why this order: every later phase imports the SDK; the SDK cannot be adopted while `web.py`/`cli.py` crash on install.

### 0.2 Kill global mutation (P0, security)

- `POST /api/settings` (`web.py:918-945`): stop writing `LLAMA_BASE_URL` / `LLAMA_API_KEY` into process `os.environ`. Replace with immutable per-server config + per-request client injection (constructor-injected HTTP client into `LlamaAdapter`).
- Confine `_scripted_transport` (`web.py:382-406`, patch/restore at `:418-425`, `:471-481`) to tests only. Production `run_and_store` never touches `urllib.request.urlopen` or `os.environ` globals.
- Add SSRF guard: validate `base_url` against allowlist (scheme + host), reject `169.254.169.254` / link-local / internal DNS.
- Consequence: multi-tenant key/endpoint leaks and thread-safety hazards close in one pass.

### 0.3 Dead-code sweep + doc honesty

- Delete `cli.py:599-607` dead diagnose branches or gate them behind existing scenario names; wire the live `odrl` branch at `:611` to real scenario discovery.
- Decide `strict.py`: either promote `strict_validate` into the Phase 1 pipeline (recommended — envelope + hash-chain + `ledger_hash` checks) or delete it. No more dead validators.
- Update `README.md` (claims "182 tests", "built on agentcrash-core SDK instrumentation" — both stale) and `verifiers.py` private import (`agentcrash._registry` → public registry export added in Phase 2).

### Phase 0 exit gate

- [ ] Fresh `pip install -e .` resolves `agentcrash-core 0.4.x`; `import odrl_agent.web`, `import odrl_agent.cli` succeed.
- [ ] Full suite green; collection count recorded (currently 189, not 182).
- [ ] **Baseline v0 frozen**: current inline pipeline run over a fixed 50-case sample with recorded model ID, prompt-pack hash, commit SHA, latency/cost. This number is the "before" for every later claim.

---

## Phase 1 — Typed IR + deterministic compiler (Weeks 3–5)

**Goal:** retire the ReAct loop. The LLM extracts meaning; code owns syntax, identity, and persistence.

### 1.1 `PolicyIR` — the contract between uncertainty and determinism (P0)

New `odrl_agent/ir.py` (Pydantic v2, strict):

- `PolicyIR`: `policy_type` (`Set`/`Offer`/`Agreement`), `profile` (default `core`; extensible per Phase 5), `conflict_strategy` (`perm`/`prohibit`/`invalid`), `rules: list[RuleIR]`, `ambiguities: list[AmbiguityFlag]`, `assumptions: list[AssumptionRecord]`.
- `RuleIR`: `modality` (`permission`|`prohibition`|`obligation`), `action` (enum grounded in ODRL 2.2 + active profile), `target`, `assignee`, `assigner`, `constraints: list[ConstraintIR | LogicalConstraintIR]`, `duties: list[DutyIR]`, `source_spans: list[SpanRef]`, `confidence: float`.
- `AmbiguityFlag`: missing assignee/target, vague temporal/numeric bounds ("soon", "temporarily"), contradictory clauses → expected outcome is **flag, not hallucination**.
- Validate IR *before* compilation: unknown action/operand/operator rejected at the schema layer, never at SHACL time.
- Research basis: Mustafa et al. distilled-ontology + SHACL self-correction pattern, but the ontology feeds a **flat typed IR** (small context, no Turtle syntax in the prompt) instead of raw JSON-LD.

### 1.2 Constrained extractor (P1)

New `odrl_agent/extractor.py`, replacing `adapter.py:run_react_loop` as the conversion entry:

- Single structured-output call per clause group: `response_format: {type: "json_schema", schema: PolicyIR}` via `httpx`-based OpenAI-compatible client (new `odrl_agent/llm.py`; retires raw `urllib` in `LlamaAdapter`).
- Transient-error policy: exponential backoff on 429/502/503 (current code retries `OSError` only and fails fast on HTTP errors — `adapter.py:47-50,323-346`).
- Model routing: fast model for triage/simple single-rule inputs; stronger model for complex/repair. No LoRA, no fine-tuning (deferred to Phase 5 pending benchmark).
- `parse_policy` fixture stub shrinks to a test double; production inline path calls the extractor directly with provenance spans.
- Research basis: XGrammar/Outlines/vLLM constrained decoding — 100% structural validity at the IR layer, with reasoning fields *before* structured fields to preserve extraction quality.

### 1.3 Deterministic ODRL 2.2 compiler (P0)

New `odrl_agent/compiler.py` (pure Python, zero LLM calls):

- `compile_ir(policy_ir) -> (json_ld, rdf_graph)`: maps IR nodes to W3C ODRL 2.2 JSON-LD with proper **IRIs** (`odrl:use`, catalog URIs for parties/assets), typed literals (`xsd:dateTime`, `xsd:duration`, `xsd:integer`), `@context`, `permission`/`prohibition`/`obligation`/`duty`/`constraint`/`refinement`.
- Parse with `rdflib.Graph().parse(data=..., format="json-ld")` using a **bundled local `@context`** (offline determinism; no network fetch per validation).
- Retire `odrl.py:_doc_to_graph` literal-builder (`:89-118`) and the 6-field `odrl-subset-shapes.ttl`; add `schemas/odrl-2.2-shapes.ttl` covering Permissions, Prohibitions, Obligations, Duties, Constraints, LogicalConstraints, Refinements + vocabulary membership against `odrl-2.2.ttl`.
- Round-trip property tests: `IR → JSON-LD → rdflib parse → SHACL validate` for every modality; JSON-LD key-order/blank-node-insensitive comparison.

### 1.4 Pipeline orchestrator + bounded repair (P1)

New `odrl_agent/pipeline.py`:

- Fixed stages: `ingest → (conditional decompose) → extract → ground → compile → verify → [repair ≤2] → draft`.
- Conditional clause decomposer only (AgentODRL pattern, no multi-agent sprawl): deterministic heuristic or fast classifier routes `COMPLEX` multi-clause inputs to one LLM decompose call; `SIMPLE` inputs skip it.
- Repair budget: at most 2 iterations, fed with the structured violation report (SHACL + Z3 + grounding); repair may only narrow grants.
- `adapter.py:run_react_loop` and its `content.find("{")` scraping retire; `tools.py:ToolRegistry` keeps `validate_odrl`/`save_odrl` as deterministic functions called by the pipeline, not by the model.

### Phase 1 exit gate

- [ ] 100% SHACL pass on the regression set **by construction** (no prompt persuasion).
- [ ] Median ≤2 LLM calls/conversion (vs 5–7 today); P95 simple <4s.
- [ ] `prohibition`/`duty`/`constraint` cases validate — the current 6-field blind spot is gone.

---

## Phase 2 — Reasoning, grounding & HITL gate (Weeks 6–7, parallel with Phase 3)

**Goal:** stop over-permission by construction, not by substring.

### 2.1 Entity grounding + least-privilege gate (P1)

New `odrl_agent/grounder.py` + pluggable `EntityCatalog`:

- Resolve `assignee`/`assigner`/`target` against the catalog (URI resolution, ISO-8601 date/duration normalization). Unknown entities → `UNGROUNDED_PRINCIPAL`, blocked from auto-approval.
- Replace `contains_attacker_grant` (`odrl.py:35,215-227`) with three deterministic checks: (a) every `assignee`/`target`/`action` maps to a grounded span or catalog entry, (b) no wildcard/public/unknown-external grants without explicit approval, (c) authorship check — imperative/injected instructions in untrusted spans cannot create grants.
- Research basis: Sep 2026 authorship-hazard result (untrusted LLM never holds publishing authority) + shrink-only compilation.

### 2.2 Symbolic conflict & satisfiability engine (P2 → promoted early)

New `odrl_agent/reasoning.py`:

- Deontic conflicts: overlapping `Permission` + `Prohibition` on same `(target, action, assignee)` without resolving `conflict` strategy.
- Unsatisfiable constraints via Z3/SMT: `dateTime > 2027-01-01 AND dateTime < 2026-01-01`, negative counts, invalid ISO-8601 ranges.
- Action-hierarchy contradictions from `odrl-2.2.ttl` (e.g. prohibiting `odrl:use` while permitting narrower `odrl:distribute`).
- No JVM reasoners (Pellet/HermiT) on the hot path — Python + Z3 only. Full OWL-DL stays out.

### 2.3 Draft-vs-publish HITL gate (P1)

- Pipeline outputs `PolicyDraft` (ODRL JSON-LD + IR provenance + verification report + ambiguity/conflict alerts). Publishing to the durable store requires **Approve & Publish**.
- Auto-publish allowed only when: zero ambiguities, zero conflicts, 100% grounded entities, full SHACL pass. Everything else queues for human review in React Studio (new review screen: side-by-side NL spans ↔ rules, approve/reject/edit + reason).
- New verifiers in `odrl_agent/verifiers.py`: `OdrlShaclValid`, `NoDeonticConflict`, `GroundedPrincipals`, `ExactlyOncePublish` (replacing `OdrlValid`/`NoAttackerGrant`/`ExactlyOnceSave`).

### 2.4 AgentCrash SDK adoption (P1)

- Instrument pipeline boundaries with `AgentCrash(WorkflowConfig(...))`, `ac.run()`, `run.step()`, `run.maybe_fault()` — replacing direct `TraceWriter`/`Ledger` coupling (`adapter.py:26-27,530`, `tools.py:19`).
- `agentcrash-core` exposes public `register_verifier`, `Verdict`, `Verifier` from top-level `agentcrash` (no more `agentcrash._registry` private import); nl2odrl verifiers consume `agentcrash.verifiers.tool_results()` for schema-agnostic events.
- Unlocks non-blocking export, HMAC-signed chains, OTel spans (`export_otel_spans`), and `agentcrash check --fuzz` chaos gates on mocked LLM/store boundaries.
- `strict.py` decision lands here: promote envelope validation into the pipeline or delete.

### Phase 2 exit gate

- [ ] 0 undetected conflicts / unsatisfiable ranges on the reasoning test-suite.
- [ ] Prompt-injection battery with arbitrary principal names (never just `"attacker-client"`): 0 auto-published over-permissions.
- [ ] `agentcrash check --fuzz` green on live-workflow chaos schedules.

---

## Phase 3 — Service hardening (Weeks 6–8, parallel with Phase 2)

**Goal:** production-servable API. No behavior change to the pipeline; the server stops being the demo bottleneck.

- FastAPI + Uvicorn (`odrl_agent/web.py` rewrite): async workers, per-tenant config injection (kills the `os.environ` mutation), OpenAPI schema generation.
- Async run queue replaces `RunRegistry` daemon threads + `MAX_ACTIVE_RUNS=2` hard cap: bounded worker pool, backpressure with honest `429 + Retry-After`, bounded `_runs` store (TTL + eviction — current dict grows unbounded).
- Streaming progress: SSE (or WebSocket) replaces 1s `GET /api/runs/{key}` polling (currently up to 120 polls in `App.tsx:137-139`).
- Observability: structured logs, request IDs, Prometheus latency/error/cost metrics, OTel export via `agentcrash-core` (replaces `log_message` noop at `web.py:953-954`).
- Secrets hygiene: keep existing strengths (fingerprints, `compare_digest`, fail-closed), move keys to env/secret-manager only, `localStorage` bearer → httpOnly cookie or short-lived token id.
- React Studio: keep the `escaper` (do not regress `App.tsx:8-14`), add Draft Review screen (§2.3), preserve Convert/Policies/Runs tabs.

### Phase 3 exit gate

- [ ] Sustained concurrency >2 with bounded memory; p95 queue wait SLO defined.
- [ ] Cold-start + request logs traceable end-to-end (request ID → pipeline steps → AgentCrash trace).
- [ ] Security re-test: cross-tenant settings write impossible; SSRF payloads rejected.

---

## Phase 4 — Benchmark & CI gates (Weeks 8–10)

**Goal:** earn every later claim. No prompt/model optimization lands without moving these numbers.

### 4.1 Stratified benchmark (200–300 cases, grouped by source to prevent leakage)

| Tier | Share | Content |
|---|---|---|
| 1 — Core permissions | 25% | Single-rule grants, standard actions (`use`, `read`, `reproduce`, `distribute`, `derive`) |
| 2 — Constraints/prohibitions/duties | 30% | `dateTime`/`elapsedTime`/`spatial`/`purpose`/`count`, `prohibition`, conditional `duty`/`obligation`/`consequence` |
| 3 — Complex multi-rule | 20% | Interacting rules, `and`/`or`/`xone`, refinements, explicit conflict clauses |
| 4 — Ambiguous / underspecified | 15% | Missing parties/targets, "temporarily"/"soon", contradictions → expected: **clarify/reject**, not hallucinate |
| 5 — Adversarial | 10% | Arbitrary principals, role overrides, JSON-breakout, homoglyphs — never the literal `"attacker-client"` |

### 4.2 Metrics (production gates in bold)

- **W3C SHACL pass → 100%** (by construction post-Phase 1).
- **Valid ODRL/Profile term rate → 100%.**
- Semantic Checkpoint F1 (entity/action/modality/constraint, per AgentODRL decomposition + calibrated judge): **≥0.92 overall, ≥0.90 constraints**.
- **False Permission Rate <1.0%; 0% on Tier 5.** Missed-prohibition rate tracked alongside.
- Behavioral equivalence: generated vs gold ODRL over 5–10 synthetic access requests per policy (FORCE / IR evaluator): **≥95% agreement**.
- Conflict/unsat escape rate: **0%**. Ambiguity recall ≥90%; unsupported assumptions <2%. Span-to-rule attribution ≥95%.
- Operational: P95 <4s simple / <10s complex; median **≤2 LLM calls**.

### 4.3 CI shape

- Per-PR (offline, no model): IR→JSON-LD round-trips, Z3 conflict suite, `agentcrash check --fuzz` on mocked LLM/store boundaries, SHACL regression.
- Nightly / release (live model): full stratified benchmark; block prompt-pack or model upgrades on FPR +0.5% or checkpoint-F1 regression.
- Freeze **Baseline v1** here (model ID + prompt-pack hash + SHA). Only then revisit DSPy.

### Phase 4 exit gate

- [x] Benchmark published with version + grouping manifest (`benchmarks/v1/`, 240 cases, `odrl_agent/bench/`).
- [ ] Baseline v1 numbers recorded: needs one live run (`python -m odrl_agent.bench run --mode live --freeze benchmarks/v1/baseline_v1.json`).
- [x] Gates enforced in CI: offline gates per PR (`ci.yml`), live gates nightly and on prompt/model/pin changes (`bench-live.yml`).

---

## Phase 5 — SOTA extensions (post-Week 10, only if Phase 4 is green)

**Goal:** differentiate, in benchmark-priority order. Each item ships only with a measured Tier-level gain.

1. **DSPy revisit (deferred on purpose).** Recompile prompts only over the 200+ benchmark with a constraint-aware metric (validity + modality + constraint/prohibition/duty F1, not triple overlap). Expectation: small, honest gains; kill on overfit. Current `dspy_opt.py` (Offer-only, triple-only) is the anti-pattern to beat.
2. **ODRL Profile registry** (`schemas/profiles/`): AI Vocabulary, Data Spaces, Temporal, DPV-to-ODRL mapping. IR `profile` field selects shapes + vocabulary; core stays default. Directly serves Gaia-X/IDSA interop.
3. **Conditional decomposer hardening:** error analysis from Tier 3 decides whether the Phase 1 heuristic needs a learned router. No multi-agent sprawl, no LoRA until 500+ golden cases exist.
4. **FORCE / formal-semantics bridge** (`odrl_agent/evaluator_bridge.py`, sidecar or CLI): behavioral equivalence + policy diffing (subsumption/equivalence). Static SHACL proves shape; this proves *meaning*.
5. **Local SLM path:** small models + XGrammar/vLLM constrained decoding for air-gapped legal/compliance deployments. Ship when parity on Tiers 1–2 is measured, not before.

Explicitly **not** building: raw-Turtle generation, ODRL 1.1 legacy support, OWL-DL hot-path reasoners, generic "AI safety wrapper" LLMs, ungrounded vector-RAG over policy docs.

## Cross-cutting concerns

- **Testing:** unit + property tests per module (IR validation, compiler round-trips, Z3 suites, grounder allow/deny cases including one allowed + one declined case per guard); chaos regression via `agentcrash check --fuzz`; golden-file traces for pipeline runs.
- **Migration safety:** domain DB migration script (old `agentcrash.db`-joined rows → new nl2odrl-owned tables); `docker-compose.yml`/`Dockerfile` updated for FastAPI/Uvicorn + SSE; `ui/` rebuilt from `ui-react`.
- **Effort honesty:** Phase 0 is 1–2 weeks only if dependency ownership moves in one pass; Phase 1 is the critical path (IR schema design + compiler correctness); Phase 3 is deliberately parallelizable so service work never blocks semantics.
- **Biggest risks:** (a) IR schema churn rippling into compiler + shapes — mitigate with versioned `PolicyIR` + snapshot tests; (b) benchmark construction bias (same authors, same fixtures) — mitigate with grouped splits + external spot-checks (AgentODRL/PolicyBench items); (c) model drift invalidating Baseline v1 — mitigate with pinned model IDs + nightly gates.

## File map (where each change lands)

- Modified: `pyproject.toml`, `odrl_agent/web.py`, `odrl_agent/cli.py`, `odrl_agent/odrl.py`, `odrl_agent/verifiers.py`, `odrl_agent/dspy_opt.py`, `ui-react/src/App.tsx`, `ui-react/src/api.ts`, `tests/test_replay.py`, `tests/test_api_keys.py`, `tests/test_billing.py`, `tests/test_web_http.py`, `README.md`, `Dockerfile`, `docker-compose.yml`.
- New: `odrl_agent/ir.py`, `odrl_agent/extractor.py`, `odrl_agent/llm.py`, `odrl_agent/compiler.py`, `odrl_agent/pipeline.py`, `odrl_agent/grounder.py`, `odrl_agent/reasoning.py`, `odrl_agent/remediation.py`, (`odrl_agent/decomposer.py`, `odrl_agent/evaluator_bridge.py`, `schemas/odrl-2.2-shapes.ttl`, `schemas/profiles/` in later phases).
- Removed or retired: `agentcrash.db` usage, `reference_patch_for` imports, `run_react_loop` as conversion path, `_doc_to_graph` literal builder, `odrl-subset-shapes.ttl`, dead `cli.py` diagnose branches; `strict.py` promoted or deleted.

## Done means

A clean `pip install` on `agentcrash-core 0.4.x`, a deterministic compiler with 100% SHACL conformance, zero silent over-permissions, every rule traceable to source spans, a human publishing every risky draft, and a versioned benchmark that proves generality instead of asserting it. That is the SOTA bar from the review - this sequence is the shortest honest path to it.
