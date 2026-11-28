# Phase-wise implementation plan

> Task list for the audit in `research_papers/CODEBASE_UPGRADE_ROADMAP.md`. Implement one phase at a time. Each phase leaves the pipeline publishable. Do not start Phase 3 until Phase 2’s bench gate is green.

**Goal:** Close the gaps in the roadmap without letting the model write ODRL, and without breaking the current IR → compile → SHACL path.

**Architecture:** `Extractor` still returns one `PolicyIR`. New work is either an optional field with a default, or a pure function that reads an IR and returns an IR or a report (`carve`, `normalise`, `kb.ground_constraints`, `positions.report`, `compare`, `enforce`). `compile_ir` stays the only JSON-LD writer.

**Tech stack:** Python 3.11+, Pydantic, Z3 (`reasoning.py`), rdflib + pyshacl, existing pytest suite (`tests/test_reasoning.py`, `tests/test_ir.py`, `tests/test_compiler.py`).

## Global constraints

- The model never sees Turtle, JSON-LD, or SPARQL. `wire_schema()` remains the response schema (`tests/test_ir.py::test_extractor_sends_the_wire_schema`).
- New `PolicyIR` / `RuleIR` / `DutyIR` fields are optional and have defaults. Existing gold IR JSON must still validate. `tests/test_ir.py` pins the schema hash: a field addition updates that snapshot and `IR_VERSION` together, in the same change.
- `normalise` emits at most **64** simple permission/prohibition rules. Over the cap, it returns the source tree and adds an ambiguity; it does not throw and it does not split obligations.
- `conflict_strategy` is still compiled onto the document. It no longer deletes overlap findings.
- No live model calls in tests. No commits unless explicitly requested.

---

## Phase 1 — Solvers (items 1, then 4)

Unblocks the healthcare review deadlock: a commercial-use prohibition currently flags every `includedIn use` duty (`docs/ODRL_PIPELINE_WALKTHROUGH.md` §6).

### Task 1.1 — Exact-action overlap

**Files:** `odrl_agent/reasoning.py` (`includes`, `analyze`), `tests/test_reasoning.py`

- [ ] Add a failing test: permission `read` + prohibition `use` (purpose = commercial) does **not** emit `ACTION_HIERARCHY_CONFLICT` or `OBLIGATION_PROHIBITED` for an `attribute` duty on the permission.
- [ ] Add a failing test: permission `read` + prohibition `read`, same target and assignee, jointly satisfiable constraints, **does** emit `DEONTIC_CONFLICT` even when `conflict_strategy="prohibit"`.
- [ ] Change `analyze` so prohibition and duty checks use action **equality**, not `includes(prohib.action, perm.action)`.
- [ ] Delete the branch `if ir.conflict_strategy is not None: continue`.
- [ ] Run `pytest tests/test_reasoning.py -q`. Update tests that encoded the old hierarchy-as-conflict behavior; do not weaken the exact-action case.

### Task 1.2 — `carve()` as a pure IR pass

**Files:** new `odrl_agent/carve.py`, `odrl_agent/pipeline.py` (call site only), `tests/test_carve.py`

- [ ] `carve(ir) -> PolicyIR` subtracts the jointly satisfiable fragment of each prohibition from overlapping permissions (same action, target, assignee). Obligations and duties are copied unchanged.
- [ ] Add `PolicyIR.action_closure: Literal["none", "permissions_only"] = "permissions_only"`. Only `permissions_only` may add implied permission rules for narrower actions (`reasoning.action_parents`). It never adds implied prohibitions.
- [ ] Pipeline stores the extractor IR on the draft and compiles `carve(ir)` when a draft flag `emit_carved` is true. Default **off** in this task so published documents do not change until a test opts in.
- [ ] `pipeline.widening` still rejects a repair that only sets `conflict_strategy` and leaves an exact-action conflict in place. Setting the strategy is not a fix.
- [ ] Test the walkthrough sentence shape (read/derive permitted, attribute + delete duties, distribute and commercial-use prohibited): after carve, no `OBLIGATION_PROHIBITED` on `attribute`.

### Task 1.3 — Duty roles on the IR

**Files:** `odrl_agent/ir.py` (`DutyIR`, `RuleIR._check`), `odrl_agent/compiler.py` (`_rule`, `_duty`, `_verify_graph`), `odrl_agent/pipeline.py` (`widening`, `_author_vague_flags`), `tests/test_ir.py`, `tests/test_compiler.py`

- [ ] Add `DutyIR.role: Literal["precondition", "obligation", "remedy", "consequence"] = "precondition"` and optional `deadline: ConstraintIR | None = None`.
- [ ] `role="remedy"` is valid only on a prohibition. `role="consequence"` is valid only on a precondition duty or a policy-level obligation. Permission duties stay `precondition` (the current `odrl:duty` meaning).
- [ ] `compiler._rule` emits `duty`, `remedy`, or `consequence` from `role`. `_verify_graph` counts those triples.
- [ ] Maintenance cues (`keep`, `maintain`, `as long as`) in `pipeline._author_vague_flags` add an ambiguity and do not invent a duty.
- [ ] `widening()` treats removal of a remedy as a widening. Update the schema snapshot and `IR_VERSION` in the same commit-sized change as the field.
- [ ] Tests: prohibition+remedy → SHACL-valid document with one `odrl:remedy`; permission does not emit `odrl:remedy`; a “keep encrypted” fixture yields `needs_review` and zero Duty nodes.

**Phase 1 done when:** `pytest tests/test_reasoning.py tests/test_carve.py tests/test_ir.py tests/test_compiler.py -q` passes, and the healthcare-shaped fixture is not held solely for `OBLIGATION_PROHIBITED`.

---

## Phase 2 — Semantics (items 2, 3, 5, then 6)

Set operators gain a meaning. Normal form stays bounded. The bench starts measuring that meaning. Item 6 is the exit gate, not a separate phase.

### Task 2.1 — KB grounding, three verdicts

**Files:** new `odrl_agent/kb.py`, `odrl_agent/reasoning.py` (`_Encoder.atom`, `_solve`, `Finding`), `odrl_agent/publish.py` (hold on Unknown), `tests/test_kb.py`

Checked-in KB, no network. Start with two orders only: spatial (mereological: France ≤ Europe) and purpose (taxonomic: commercial ⊥ nonCommercial). Other left operands stay strings until a later KB file is added.

- [ ] `ConstraintIR.right_concepts: list[str] = []` filled by `kb.ground_constraints(ir)`, not by the model. The model still writes the surface string in `right_operand`.
- [ ] Verdicts: `UNSATISFIABLE_CONSTRAINT` (provable empty), unchanged findings for Compatible, new `UNKNOWN_CONSTRAINT` when γ misses or a `xone` branch is Compatible without a disjoint sibling.
- [ ] Z3 timeout (`unknown`) becomes `UNKNOWN_CONSTRAINT`, not satisfiable (`reasoning._solve` today).
- [ ] `UNKNOWN_CONSTRAINT` sets the draft to `needs_review`. It is not auto-publish and it is not a conflict.
- [ ] Tests from 2602’s shape: France vs Europe Compatible; French vs German Conflict once a language row exists; unmapped `bavaria` Unknown; `xone(commercial, nonCommercial)` without ⊥ Unknown.

### Task 2.2 — `normalise()`, cap 64, obligations untouched

**Files:** new `odrl_agent/normalise.py`, `odrl_agent/pipeline.py`, `tests/test_normalise.py`

- [ ] Permissions and prohibitions: conjoin, rewrite `≥`/`≤`/`≠` into `{=,<,>}`, DNF, split numeric intervals on constants that appear in the partner rule.
- [ ] Obligations and `DutyIR` constraints are copied. A test with obligation `payAmount gt 10` beside a prohibition that mentions `20` still has **one** obligation afterward.
- [ ] Empty intervals drop the permission and leave the unsat finding from Phase 1’s solver.
- [ ] If the simple-rule count would exceed **64**, return the input IR and append an ambiguity `other` with description `normal form exploded`. Do not raise.
- [ ] Pipeline compiles the normal form only when `emit_normal_form` is set. Default off until Task 2.4 flips the bench. Trace keeps both IRs. `canonical_json` of one fixture is snapshotted.

### Task 2.3 — Position report

**Files:** new `odrl_agent/positions.py`, `odrl_agent/verifiers.py`, `odrl_agent/ir.py`, `odrl_agent/bench/evaluator.py`, `tests/test_positions.py`

Depends on Task 1.3 (`role="remedy"`).

- [ ] `PolicyIR.behaviour: Literal["closed", "open"] = "closed"`. `RuleIR.strong: bool = False`.
- [ ] `behaviour="open"` does not insert a universal permission. Publish holds with a fixed reason. `behaviour="closed"` is a no-op for documents that already deny by default.
- [ ] `positions.report(ir)` returns JSON: permission → Permission + NoRight; prohibition → Duty-to-Omit + Right-to-Omission; remedy → Power(assigner) + Subjection(assignee); `strong=True` → Immunity + Disability. Two prohibitions with different assigners produce two Powers.
- [ ] `evaluator.decide` returns false for `policy_type="Offer"` without scoring it as an access decision. Agreements are unchanged.
- [ ] Report is not written into the ODRL graph. SHACL of a `strong=True` permission matches the same graph as `strong=False`.

### Task 2.4 — Bench oracle (item 6)

**Files:** `odrl_agent/bench/evaluator.py`, `odrl_agent/bench/metrics.py`, `odrl_agent/bench/gates.py`, `odrl_agent/bench/generate.py`

- [ ] `decide` uses the same satisfiability oracle as `reasoning.analyze`, including `action_refinements`. Duties still do not flip allow/deny. Add `duty_recorded` so a dropped duty fails the gate separately.
- [ ] New metric `normal_form_equivalence`: gold and prediction have the same simple permission set under `normalise`.
- [ ] Add a small gold-IR slice (not a new 240-case regeneration): one `isPartOf`, one `xone`, one remedy. A mutant that treats `isPartOf` as `eq` fails `gates.evaluate`.
- [ ] Offline `python -m odrl_agent.bench run` passes. Existing 240 cases stay valid because new fields default.

**Phase 2 done when:** the offline bench passes, the cap-64 test passes, and `positions.report` snapshots the two-assigner remedy case.

---

## Phase 3 — Pipeline (items 7, 8, 9, 10)

Extraction and runtime. None of these write ODRL. None run before Phase 2, because the prompt and the enforcer name fields and verdicts that do not exist yet.

### Task 3.1 — OSES fragments (item 7)

**Files:** `prompts/oses/*.md` (syntax, duty role, one example each for Agreement / Offer / Set), `odrl_agent/extractor.py` (`SYSTEM_PROMPT` assembly, repair SCR map), `odrl_agent/bench/pins.py`

- [ ] Load fragments into the system prompt. Do not paste `schemas/odrl-2.2.ttl`.
- [ ] Instruct “in \<place\>” / “for \<purpose\>” to use `isPartOf` / `isA` with the surface string in `right_operand` and an empty `right_concepts` (Task 2.1 fills concepts).
- [ ] SCR map: Pydantic and reasoning codes → one short repair hint. `Extractor.repair` appends the matching hint. It does not send raw SHACL prose.
- [ ] Update the prompt-pack pin. Scripted-LLM test: “within the European Union” yields `spatial` + `isPartOf`. “if violated must compensate” yields `role="remedy"`.

### Task 3.2 — Reference inlining (item 8)

**Files:** `odrl_agent/extractor.py` (`rewrite` or a deterministic pre-pass), `odrl_agent/pipeline.py`

- [ ] Deterministic pass: `Section|Article|Clause N` whose body is an exact span gets inlined into a working copy. Trace records the rewrite. Extract still sees one source and returns one IR.
- [ ] If the referent is missing, set `unresolved_reference` and do not invent a rule. No extra model call on that path.
- [ ] Optional `Extractor.rewrite` only when the deterministic pass fails and the text still contains a dangling pointer. Output clauses must be substrings of the original, same rule as `decompose`.
- [ ] Tests: “Section 2 forbids distribution. Section 5 applies Section 2 to dataset D” → one prohibition whose spans cite both. “see Section 9” with no Section 9 → ambiguity, zero rules.

### Task 3.3 — Asymmetric compare (item 9)

**Files:** new `odrl_agent/compare.py`, `tests/test_compare.py`

Depends on `normalise` and the KB oracle.

- [ ] `compare(provider, requester) -> {symmetric, asymmetric, missing_obligations, extra_permissions}`. No model call.
- [ ] Asymmetric conflict iff a requester permission is not contained in the provider’s simple rules, or a provider obligation has no requester obligation contained in it.
- [ ] Offer vs Agreement is allowed only as this containment check. `compare` does not call `evaluator.decide` on an Offer.
- [ ] Tests: provider “read in Europe”, requester “read in France” → asymmetric ok. Requester “read worldwide” → conflict. Provider obligation with no requester duty → `missing_obligations`.

### Task 3.4 — Enforcement adapter (item 10)

**Files:** new `odrl_agent/enforce.py`, `tests/test_enforce.py`

- [ ] `enforce(doc, ir, context) -> list[tuple[action, result]]`. `doc` must be the `compile_doc` output for `ir` (uid prefix `urn:nl2odrl:policy:` and hash match). Reject anything else.
- [ ] Interpret numeric operators and `dateTime`. Set operators use `kb` denotations. A runtime value with no concept does not satisfy (deny). Design-time Unknown is not re-decided here.
- [ ] Unknown or abstract actions return `(action, action)` and are not executed. Duties are listed as pending preconditions, not run.
- [ ] No Jinja, no second policy language, no `Extractor` import in this module.
- [ ] Tests: six comparison operators on constants, six on `dateTime`, one `isPartOf` deny when the request place is unmapped. No HTTP.

**Phase 3 done when:** `pytest tests/test_compare.py tests/test_enforce.py tests/test_ir.py -q` passes and `test_extractor_sends_the_wire_schema` still passes.

---

## What each phase deliberately does not do

| Phase | Left for later |
|---|---|
| 1 | Knowledge bases, DNF, UFO-L report, prompt rewrites, enforcement |
| 2 | Prompt text, reference inlining, compare API, ODRE adapter |
| 3 | Changing carve/normalise semantics; raising the 64-rule cap |

Out of all three phases: LLM-written JSON-LD, an LLM jury metric, XACML translation, DNF of obligations, a quantified `xone` inside Z3, an Isabelle embedding of UFO-L.
