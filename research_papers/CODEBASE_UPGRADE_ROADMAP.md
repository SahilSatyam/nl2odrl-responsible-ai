# Codebase Upgrade Roadmap (IR-preserving)

Architectural audit of `nl2odrl` against the seven-paper corpus in `research_papers/notes/` and `research_papers/SYNTHESIS.md`.

**Constraint.** The typed `PolicyIR` is the control boundary: the model extracts meaning; `compiler.py` owns ODRL syntax. None of the items below replace that with end-to-end LLM-to-JSON-LD generation (the failure mode both the README and Mustafa et al. 2506 document). Each item either extends the IR schema, hardens a stage before or after the IR, or adds a deterministic layer that consumes a `PolicyIR` or the graph `compile_ir` already emits.

No source files were modified for this report.

---

## 1. How NL becomes ODRL today

```
POST /api/convert  (odrl_agent/app.py, web.validate_convert)
  → pipeline.convert                         (odrl_agent/pipeline.py)
      ingest (strip)
      classify()                             (extractor.py)  regex + sentence count
      Extractor.decompose                    (COMPLEX only; clauses must be verbatim substrings)
      Extractor.extract                      structured JSON → PolicyIR (wire_schema, ir.py)
      Pydantic PolicyIR                      (ir.py IR_VERSION 1.0, extra=forbid)
      ground() / locate()                    quotes must occur in the source
      compile_ir()                           compiler.py → JSON-LD + RDF
      validate_odrl()                        SHACL schemas/odrl-2.2-shapes.ttl
      reasoning.analyze()                    Z3 over the IR (not the graph)
      Extractor.repair ≤ 2, widening()       shrink-only
      Grounder.ground()                      drop ungrounded permissions
      publish gate                           publish.py; ambiguities/assumptions/conflicts hold
```

The model is called only from `Extractor.decompose`, `extract`, `repair`, and `slot_fill` (`extractor.py`). It never sees Turtle or JSON-LD (`SYSTEM_PROMPT`, `wire_schema`).

`PolicyIR` (`odrl_agent/ir.py`) holds `policy_type` ∈ {Set, Offer, Agreement}, optional `conflict_strategy` ∈ {perm, prohibit, invalid}, parties, `rules`, `ambiguities`, `assumptions`. Each `RuleIR` is one modality (`permission` | `prohibition` | `obligation`), one `action` from a closed 41-name `ACTIONS` tuple, `constraints`, `action_refinements`, and `duties` (permissions only). `ConstraintIR` is one triple; `LogicalConstraintIR` is `and` | `or` | `xone` | `andSequence`, depth ≤ 3 (`MAX_CONSTRAINT_DEPTH`). The wire schema sent to the model unrolls logic to depth 1 (`WIRE_LOGICAL_DEPTH`) because provider schema nesting is capped; the reply is still validated against the full model.

`compile_doc` (`compiler.py`) groups rules into `permission` / `prohibition` / `obligation`, mints `urn:nl2odrl:party:` and `urn:nl2odrl:asset:` IRIs, types literals, and emits `LogicalConstraint` / `refinement` / `duty`. Obligations compile as `type: Duty` under the policy `obligation` key. Profile is locked: `PolicyIR._check` rejects anything but `profile == "core"`.

---

## 2. ODRL coverage

| Construct | Status | Where |
|---|---|---|
| Permission, prohibition, policy-level obligation | Supported | `RuleIR.modality`; `compile_doc` |
| Duty on a permission | Supported | `DutyIR`; `RuleIR._check` rejects duties on other modalities |
| Constraint + logical `and`/`or`/`xone`/`andSequence` | Supported in IR and compiler | `LogicalConstraintIR`; `_constraint` uses `@list` for `andSequence` |
| Action refinement vs rule constraint | Supported and distinct | `action_refinements` → `odrl:refinement`; `constraints` → `odrl:constraint` |
| Conflict strategy `perm`/`prohibit`/`invalid` | Stored and emitted | `PolicyIR.conflict_strategy`; `compile_doc` |
| 41 core actions, 35 left operands, 12 operators | Closed vocabularies | `ACTIONS`, `LEFT_OPERANDS`, `OPERATORS` |
| Set operators in the IR | **Accepted, not reasoned** | `reasoning._Encoder.atom` returns `fresh()` for `isA`, `hasPart`, `isPartOf`, `isAnyOf`, `isAllOf`, `isNoneOf` (module docstring, lines 20–22) |
| Action hierarchy `includedIn` | **Applied to prohibitions and duties** | `reasoning.includes` + `analyze` overlap |
| Remedy, consequence | **Missing** | Present in `schemas/odrl-2.2.ttl` (`:remedy`, `:consequence`); absent from `ir.py` |
| Party/asset collections, `inheritFrom` | **Missing** | Vocabulary in `schemas/odrl-2.2.ttl`; no IR fields |
| Profiles beyond `core` | **Blocked** | `PolicyIR._check` |
| `behaviour` open/closed | **Implicit only** | `bench/evaluator.decide` denies when no permission applies; not a field |
| Live enforcement (ODRE) | **Missing** | `bench/evaluator.py` docstring: “not a full ODRL semantics engine (that is the Phase 5 FORCE bridge)” |

### Guardrails that already exist

- Schema rejection before any ODRL exists (`parse_ir`).
- Span grounding: quotes must be substrings (`pipeline.ground`, `Extractor.decompose` drops non-substrings).
- Shrink-only repair (`pipeline.widening`): cannot add a permission, drop a prohibition/obligation/duty/constraint, or relax `prohibit`.
- SHACL on the compiled graph (`odrl.py` / `schemas/odrl-2.2-shapes.ttl`).
- Z3: unsatisfiable numeric/date constraints; permission/prohibition overlap; duty/obligation prohibited (`reasoning.analyze`). Unknown Z3 results (timeout) are treated as satisfiable (`_solve`: “never invent a conflict”).
- Publish hold on ambiguities, assumptions, dropped grants, grounding issues, conflicts (`publish.py`, walkthrough §4).
- Verifiers on the hash-chained trace: `OdrlShaclValid`, `NoDeonticConflict`, `GroundedPrincipals`, `NoAttackerGrant` (`verifiers.py`).
- Benchmark: 240 gold-IR cases, false-permission rate, conflict-escape rate, behavioral agreement (`bench/metrics.py`, `bench/gates.py`). README baseline (muse-spark-1.3): SHACL 1.00, span attribution 1.00, behavioral agreement 0.98, checkpoint F1 0.90, constraint F1 0.64, adversarial false-permission 0.17, conflict escape 0.083, ambiguity recall 0.0 (pre-flag).

---

## 3. Research-to-code gap

### Already ahead of, or aligned with, the papers

| Literature claim | This repo |
|---|---|
| AgentODRL / 2506: a single LLM writing ODRL fails on syntax and over-grants | Model fills `PolicyIR` only; compiler owns IRIs and XSD types. README states this as the design. |
| 2506: SHACL detects, it does not reliably teach the LLM | Repair is fed structured violations and is shrink-only, not a free SHACL-message loop. |
| 2506 C1–C9 shape checks | Full ODRL 2.2 shapes, offline, plus graph triple-count in `compiler._verify_graph`. |
| AgentODRL simple vs complex routing | `classify` + `decompose` is a smaller Orchestrator/Splitter. Clauses must be verbatim (stronger than AgentODRL’s LLM split). |
| AgentODRL semantic checkpoints | Span quotes plus `assumptions`/`ambiguities` are deterministic provenance. An LLM jury is not used (and should not be added as the compliance metric; see SYNTHESIS §G.29). |
| Salas 2509 prohibited-by-default | `evaluator.decide`: a request is allowed only if some permission applies and no prohibition does. |
| Salas 2509 `includedIn` as a separate materialisation step | Hierarchy is loaded from `schemas/odrl-2.2.ttl` in `action_parents`, not invented by the model. |
| 2602: design-time conflict must not false-alarm | `_solve` maps Z3 `unknown` to satisfiable. Directionally matches “do not invent Conflict,” but it is binary, not three-valued. |
| ODRE: usage conditions stay in the policy language, not in templates | There is no Jinja/C-level policy layer. |

### Highest-leverage gaps

1. **Overlap is coarser than Salas.** `analyze` treats a prohibition of `use` as a prohibition of every `includedIn` descendant (`includes(prohib.action, perm.action)`). 2509 §3.3 explicitly leaves prohibition inheritance undecided. The walkthrough (§6) shows the flagship sentence cannot be approved: `attribute`/`delete` are `includedIn` `use`, so a commercial-use prohibition fires `OBLIGATION_PROHIBITED` against every duty. `conflict_strategy is not None` then **skips all** permission/prohibition findings (`reasoning.py` ~281), which is not Salas consistent form (drop only the overlapping fragment) and not UFO-L Ax 5.10 (same bearer, same action, Permission vs Duty-to-Omit).

2. **Set operators are syntax-only.** 2602’s entire contribution is that `isA`/`isPartOf`/… are undefined without a KB, and that `isA` and `isPartOf` share a denotation whose order comes from the KB. Here they are free booleans. `SYSTEM_PROMPT` maps “in Europe” to `spatial eq "<string>"`, so the 2602 GeoNames/ISO case cannot even be stated as a hierarchy. `evaluator._holds` implements `isAnyOf` as comma-split string equality and falls through `isA`/`isPartOf`/`hasPart` to `eq`.

3. **No normal form.** 2603’s compiler (DNF, interval split, `NS(P)\NS(F)`, never split obligations) is absent. Comparison is behavioral sampling of ≤10 requests (`evaluator.requests_for`, `MAX_REQUESTS = 10`), not rule-set inclusion (2509 Thm 1).

4. **Duties are one role.** No `remedy`, `consequence`, or “duty must precede the permission” (2509 Def. 2–3). Prohibitions cannot carry duties (`RuleIR._check`). 2606: Formal Semantics is achievement-only; maintenance duties should be refused or flagged. No Power/Subjection for who may declare a violation.

5. **Routing is lexical, not structural.** AgentODRL’s Rewriter (structure-preserving inlining of Art. 48 → Art. 35) has no counterpart. `unresolved_reference` is an ambiguity kind the model may set; nothing resolves cross-clause references before extract.

6. **No runtime enforcement.** ODRE Algorithm 1 (reduce → interpret constraints → Usage Decision, ontology-coupled functions, default third-party handoff `(a, a)`) is not implemented. The bench decider ignores duties (“Duties and obligations do not change an access decision”).

7. **Prompt guidance is a vocabulary list, not OSES.** 2506’s finding is that raw ontology dumps and raw SHACL messages fail, while distilled syntax/semantics/examples plus per-type templates (Agreement/Offer/Set) work. `SYSTEM_PROMPT` lists terms and a few shapes; it does not separate Offer (do not evaluate; 2606) from Agreement, and it does not teach refinement vs constraint beyond duties.

---

## 4. Top 10 enhancements

Ordered by architectural impact. Effort and impact are High / Medium / Low.

### 1. Carve-out overlap instead of a strategy-shaped short-circuit

- **Category:** Formal semantics
- **Research backing:** Salas et al. 2509 Def. 8 (consistent policy: \(P \not\sqcap F\), \(O \sqsubseteq P\); prohibitions are redundant after the forbidden fragment is removed). Salas et al. 2603 Thm 7 / Cor. 6 (permissions minus prohibitions, prohibition-by-default). 2509 §3.3: do not silently inherit prohibitions along `includedIn`. UFO-L 2606 Ax 5.10: conflict is Permission-to-Act and Duty-to-Omit on the **same** action, not on an ancestor.
- **Current gap:** `odrl_agent/reasoning.py` `analyze`: `includes(prohib.action, perm.action)` plus `if ir.conflict_strategy is not None: continue`. `docs/ODRL_PIPELINE_WALKTHROUGH.md` §6 documents the resulting false `OBLIGATION_PROHIBITED` on the healthcare example. `pipeline.widening` treats adding `conflict_strategy: prohibit` as a successful narrowing, which clears hierarchy findings without changing the rules.
- **Proposed enhancement (IR-compatible):** Keep both the source IR and a derived IR.
  - Add `PolicyIR.action_closure: Literal["none", "permissions_only"] = "permissions_only"`. `permissions_only` materialises implied **permissions** (Play ⇒ Display) as extra permission rules, matching 2509’s materialisation step. Prohibition and duty checks use **exact action equality** unless the field is explicitly set otherwise in a later profile.
  - Replace the strategy short-circuit with a pure function `carve(ir) -> PolicyIR` that subtracts the jointly satisfiable fragment of each prohibition from overlapping permissions (2603’s \(NS(P)\setminus NS(F)\) for the numeric fragment Z3 already decides). `conflict_strategy` stays an **emitted** ODRL property for consumers that still want it; it no longer deletes findings. `invalid` means Ax 5.10 / exact-action overlap is a hard hold.
  - `compile_ir` compiles the carved IR when a flag `emit_normal_form` is set; the trace keeps the extractor IR.
- **Impact vs effort:** Impact High, effort Medium. No dependency. Later items 3 and 9 assume this overlap definition.
- **Verification:** Unit tests on the walkthrough sentence: commercial-`use` prohibition does **not** flag an `attribute` duty; exact `read` permission + `read` prohibition still flags. A permission of `display` under a permission of `play` appears only when `action_closure="permissions_only"`. A policy with `conflict_strategy="prohibit"` still records a finding when actions are equal and constraints are jointly sat. Existing `tests/test_reasoning.py` cases that expect hierarchy conflicts are updated to the new contract and re-run.

### 2. Three-valued KB denotations for set operators

- **Category:** Conflict detection
- **Research backing:** Mustafa et al. 2602 Defs 3–6, Thm 1, Thm 3. Operators `eq`, `neq`, `isA`, `isPartOf`, `hasPart`, `isAnyOf`, `isAllOf`, `isNoneOf` denote sets in a finite KB \((C,\le,\bot\!\!\bot,\gamma)\). Verdict ∈ {Conflict, Compatible, Unknown}. `isA` and `isPartOf` share a denotation; the order is the KB’s (taxonomic ⊑ vs mereological ⪯). `xone` is Compatible only when every other branch is provably Conflict (ODRL086). Unmapped concepts degrade to Unknown, never to a false Conflict (Prop. 2). Runtime: ungrounded value does not satisfy (Def. 11).
- **Current gap:** `reasoning.py` `_Encoder.atom` / `fresh()` leaves those operators unconstrained, so `satisfiable` cannot return unsat for them. `extractor.SYSTEM_PROMPT` forces `spatial eq "<string>"` and `purpose eq "<word>"`. `bench/evaluator.py` `_holds` treats `isAnyOf` as comma-separated strings and `isA`/`isPartOf`/`hasPart` as string equality. No KB files exist in the repo.
- **Proposed enhancement (IR-compatible):** Do not ask the model to emit denotations.
  - Extend `ConstraintIR` with optional `right_concepts: list[str]` (IRIs). The extractor may still emit a string; a new deterministic stage `odrl_agent/kb.py` grounds `spatial`, `purpose`, `language`, and the other 2602 operands through a checked-in KB (start with a tiny GeoNames-like spatial order and a DPV-like purpose order, the two domains in 2602 Table 5 that the walkthrough actually hits: “European Union”, “commercial”).
  - `analyze` returns a third status `UNKNOWN_CONSTRAINT` when \(\gamma\) misses or when `xone` has a Compatible branch without a disjoint sibling. Unknown **holds** publication (`publish.py`) the same way ambiguities do; it does not auto-become `UNSATISFIABLE_CONSTRAINT`.
  - Encoding stays quantifier-light: per-operand finite sorts, not a direct `xone` under negation (2602 stays in EPR by composing `xone` outside the solver). Z3 `unknown` (timeout) maps to this third status, not to satisfiable (`_solve` today).
- **Impact vs effort:** Impact High, effort Medium. Independent of item 1, but item 1’s `carve` should call this satisfiability oracle once it exists.
- **Verification:** Fixtures, no network: France ⊑ Europe is Compatible with `spatial isPartOf Europe` vs request France; `language isA German` vs French is Conflict; unmapped `bavaria` is Unknown and the draft status is `needs_review`, not a false conflict. `xone(commercial, nonCommercial)` without a \(\bot\!\!\bot\) axiom is Unknown (2602 Table 6 / ODRL086). A numeric `dateTime` contradiction remains `UNSATISFIABLE_CONSTRAINT`.

### 3. Normal form pass that does not split obligations

- **Category:** Formal semantics
- **Research backing:** 2603 Algorithm 1 and Thms 1–3 (DNF + interval split preserve `match`). Thm 4 (split rules are disjoint). Thm 6: splitting an obligation “pay ≥ 10” against the constant 20 yields three obligations and a **stricter** policy. Cor. 6: emit \(\langle NS(P)\setminus NS(F),\ \emptyset,\ O\rangle\). Complexity \(O(|R|\cdot 2^n)\) so the pass needs a size cap.
- **Current gap:** No normaliser. Logical trees stay as the model wrote them (`LogicalConstraintIR`, depth 3). `compiler._constraint` pretty-prints them but does not decompose. Obligations and permissions share one constraint type, so a naive DNF on every rule would change duty meaning.
- **Proposed enhancement (IR-compatible):** `odrl_agent/normalise.py`: `normalise(ir) -> PolicyIR` after grounding and before `compile_ir`.
  - Permissions and prohibitions: conjoin, rewrite `≥`/`≤`/`≠` to `{=,<,>}`, DNF, split numeric intervals on the partner rule’s constants, drop empty intervals (2603 \(R_\bot\)).
  - Obligations and `DutyIR.constraints`: **copy unchanged**.
  - If the simple-rule count exceeds a fixed cap (for example 64), keep the source tree and add an ambiguity `other` (“normal form exploded”) rather than emitting an exponential policy.
  - The trace stores both IRs. SHACL runs on the compiled normal form.
- **Impact vs effort:** Impact High, effort Medium. Depends on item 1 (carve uses the same simple rules). Item 2’s set constraints stay outside this pass (2603 excludes them).
- **Verification:** Property tests: for random numeric worlds, `evaluator.decide` agrees on the source IR and the normal form (permissions/prohibitions only). The obligation “payAmount gt 10” is still one rule after a sibling prohibition mentions 20. A contradictory interval `count gt 0 AND count lt 0` becomes no permission and an unsat finding. Golden JSON of one normal form is snapshotted so the compiler output stays byte-stable (`canonical_json`).

### 4. Duty roles: precondition, remedy, consequence

- **Category:** IR schema
- **Research backing:** 2509 Def. 2–3: a permission duty is a **prior** event (`e'₀ ≤ e₀`) and must itself be permitted; a remedied prohibition must **not** also sit in \(F\) or the remedy is dead; an obligation consequence must **not** also sit in \(O\). 2606 Prop. 3.1–3.2: prohibition is sanctioned; Formal Semantics covers **achievement** duties only; `odrl:remedy` has no activation procedure in the draft, so the IR must record the role even if evaluation is partial. 2606 Ax 5.4: remedy founds Power(assigner) / Subjection(assignee).
- **Current gap:** `DutyIR` has action, target, assignee, constraints, spans — no role and no deadline. `RuleIR._check`: “only a permission may carry duties”. `compile_ir` always emits `odrl:duty`. `schemas/odrl-2.2.ttl` already defines `remedy` and `consequence`. Walkthrough: “after expiration” is a copied absolute date, not a link to the permission’s end.
- **Proposed enhancement (IR-compatible):** Extend `DutyIR` with `role: Literal["precondition", "obligation", "remedy", "consequence"]` and optional `deadline: ConstraintIR | None`. Allow `role="remedy"` only on prohibitions; `consequence` only on a precondition/obligation duty or a policy-level obligation. `compiler._rule` emits `odrl:remedy` or `odrl:consequence` from the role. Add `DutyIR.satisfied_by: Literal["achievement"] = "achievement"`; if the extractor (or a keyword pass in `pipeline._author_vague_flags`) sees maintenance language (“keep”, “maintain”, “as long as”), set an ambiguity instead of a duty (2606 Prop. 3.2). Do not generate UFO classes in the ODRL graph yet; item 5 reports them.
- **Impact vs effort:** Impact High, effort Medium. Independent. Item 5 reads these roles.
- **Verification:** A prohibition with a remedy compiles to `odrl:remedy` and still SHACL-validates. A permission duty does not. A “keep encrypted” sentence yields `needs_review` and zero `Duty` nodes. Repair `widening()` cannot delete a remedy (extend the prohibition/duty key). Round-trip test: IR → JSON-LD → count of `odrl:remedy` triples equals remedy duties (`_verify_graph` style).

### 5. Closed-world behaviour and a position report

- **Category:** Validation
- **Research backing:** 2606 Prop. 5.1: `behaviour=open` is a normative vacuum, not a granted Permission. Closed-world declared permissions are **weak** unless `stronglyPermitted`. Prop. 5.2: an evaluator that only says permitted/prohibited is sound but incomplete (four of eight positions missing). Offers must not be evaluated (2606 §2.1). SYNTHESIS tension 1: do not conflate open, universal permission, and SMT Unknown.
- **Current gap:** `evaluator.decide` implements closed-world allow/deny with no field to say so. `PolicyIR.policy_type` can be `Offer`, and `compile_doc` will emit it for enforcement consumers. `profile` cannot name `odrl-l`. Nothing reports Right-to-Omission or Power.
- **Proposed enhancement (IR-compatible):** Add `PolicyIR.behaviour: Literal["closed", "open"] = "closed"` and `RuleIR.strong: bool = False`. `behaviour="open"` does **not** insert a universal permission (2509’s simulation, which cannot carve prohibitions). It sets status `needs_review` with a fixed reason string from Prop. 5.1. `publish` / a new verifier `PositionsComplete` (`verifiers.py`) builds a JSON report from the IR: for each permission, Permission + NoRight; for each prohibition, Duty-to-Omit + Right-to-Omission; for each remedy, Power(assigner) + Subjection(assignee); for `strong=True`, Immunity + Disability. Offers are compiled but `evaluator.decide` and any future enforcer refuse them (return “not an agreement”).
- **Impact vs effort:** Impact Medium, effort Low. Depends on item 4 for remedy Power. Does not require Isabelle.
- **Verification:** Snapshot the position report for one Agreement with a prohibition+remedy: two parties, distinct powers if two assigners (2606 Example 5.1 shape). `behaviour="open"` never auto-publishes. `strong=True` adds the immunity pair and does not change SHACL of the core graph. An Offer gold case is not scored by `agreement()`.

### 6. Make the bench oracle match the semantics it claims

- **Category:** Evaluation
- **Research backing:** 2509 evaluation is query answering over events, not “≤10 synthetic requests.” 2603 Cor. 3: containment is equality of simple rules after normalisation. 2602: set operators are not string `eq`. README: constraint F1 0.64, so the metric that should catch this is the weak one. `bench/evaluator.py` lines 4–8 state the decider is intentionally partial.
- **Current gap:** `decide` ignores duties and obligations. `_holds` mis-implements set operators. `requests_for` caps at 10 and shifts every bound by −1/0/+1 together, so it never separates two operands. `metrics.compute` skips behavioral agreement when `expect.conflict` is set, so conflict cases are not checked for decision equivalence. `conflict_escape_rate` only checks auto-publish.
- **Proposed enhancement (IR-compatible):** Keep gold as IR (do not store gold JSON-LD).
  - Point `decide` at the same satisfiability oracle as `reasoning.analyze` (items 1–2), including refinements (`_applies` currently ignores `action_refinements`).
  - Add a metric `normal_form_equivalence`: after item 3, gold and prediction have the same simple permission set under `normalise`.
  - Add cases whose gold IR uses `isPartOf` / `xone` / a remedy role, generated in `bench/generate.py` beside the existing tiers, so the gate in `bench/gates.py` can fail a mutant that string-compares places.
- **Impact vs effort:** Impact High, effort Medium. Depends on items 1–3 for the oracle; the new cases can land with item 2.
- **Verification:** Offline `python -m odrl_agent.bench run` stays green on today’s gold under the old decider until pins move, then the new metric is gated. A mutant that compiles `isPartOf` as `eq` drops `normal_form_equivalence` or a dedicated set-operator score and `gates.evaluate` fails. Duties still do not flip `decide` (2509 access vs monitoring); a separate `duty_recorded` score checks they were not dropped.

### 7. Distill OSES into the extractor, still targeting PolicyIR

- **Category:** Ontology-guided translation
- **Research backing:** 2506 §3.2–3.3: raw TTL/W3C text causes hallucinations (`odrl:use` as `leftOperand`, invented `odrl:location`); distilled syntax, the duty dual role (`obligation` vs `duty`), and examples outperform ontology-only. Separate templates per Agreement, Offer, Set. SCR is human-readable and iterative; raw SHACL messages do not repair well. AgentODRL uses this paper as the SCR baseline and beats it with routing, not by deleting the ontology guide. Headline 91.95% is SHACL-criteria accuracy on 12 cases, not a reason to copy their end-to-end KG generation.
- **Current gap:** `extractor.SYSTEM_PROMPT` is one vocabulary dump plus a few shape recipes. It tells the model “in \<place\>” → `spatial eq string`, which fights item 2. It does not say when to use `action_refinements` vs `constraints`, when a policy is an Offer vs an Agreement, or that a remedy is not a permission duty. `DECOMPOSE_PROMPT` is four lines. Repair (`Extractor.repair`) forwards pipeline violation strings; it has no curated SCR list for the systematic mistakes 2506 names.
- **Proposed enhancement (IR-compatible):** Add `prompts/oses/` fragments loaded by `Extractor` (syntax, duty-role, one example per policy type) and a short SCR table keyed by `reasoning` codes and Pydantic messages (`format_validation_errors`). The schema sent to the model remains `wire_schema()`. Instruct set-like places and purposes to use `isPartOf` / `isA` with the surface string in `right_operand` and **no invented IRI** (item 2 grounds it). Do not paste `schemas/odrl-2.2.ttl` into the prompt.
- **Impact vs effort:** Impact Medium, effort Low. Best after items 2 and 4 so the prompt names fields that exist. Independent of the compiler.
- **Verification:** Snapshot `SYSTEM_PROMPT` hash in `bench/pins.py` (pins already hash the prompt pack). A fixture with “within the European Union” yields `spatial` + `isPartOf` (or `eq` only if the KB stage is off) and zero assumptions. A fixture “prohibited, and if violated must compensate” yields `role="remedy"` once item 4 exists. Repair of an Action-used-as-operand schema error succeeds from the SCR entry without a second free-form rewrite. No test calls a live model.

### 8. Reference-inlining stage in front of extract

- **Category:** Multi-agent routing (still one IR)
- **Research backing:** AgentODRL 2512 §3–4: recursive texts (Article 48 depends on Article 35) need a Rewriter that inlines references and **keeps clause boundaries**, then a Splitter on asset / party / purpose. On recursive cases, Rewriter→Splitter→Generator reaches semantic score 82.00 vs 61.53 for the generator alone (Table 2, GPT-4.1-nano). Their Orchestrator is below the best fixed path (80.22 vs 88.07) but cheaper — so routing must be measurable, not trusted.
- **Current gap:** `classify` is a regex (`_COMPLEX_MARKERS`) and a sentence count. `decompose` splits but does not inline. `AmbiguityKind` includes `unresolved_reference`, and the prompt mentions pronouns, but nothing walks “as defined in paragraph 2”. Parallel policies are whatever the one extract call emits.
- **Proposed enhancement (IR-compatible):** A deterministic pre-pass first: if the text matches `Article|Section|Clause\s+\d+` or “as defined above”, and the referent is an exact span, inline it into a working copy and record the rewrite in the trace. Only if that fails, one extra structured call (`Extractor.rewrite`) whose output clauses must be traceable to source spans (same containment rule as `decompose`). Then the existing extract call fills **one** `PolicyIR`. Do not add a Generator agent that emits JSON-LD. Log which path ran so a bench slice can compare rewrite-on vs rewrite-off (AgentODRL Table 2).
- **Impact vs effort:** Impact Medium, effort Medium. Independent. Uses the same `StructuredLlm` as extract.
- **Verification:** A fixture “Section 2 forbids distribution. Section 5 applies Section 2 to dataset D” produces a prohibition on D whose span cites both sections. A fixture with a dangling “see Section 9” sets `unresolved_reference` and does not invent a rule. `decompose` still drops non-substrings. Token/call count is a bench field (already `median_llm_calls`).

### 9. Asymmetric comparison of two IRs

- **Category:** Formal semantics
- **Research backing:** 2509 Def. 5 and Thm 1: a requester conflicts with a provider iff the requester’s permissions are not contained in the provider’s, or some provider obligation has no requester obligation contained in it. Symmetric conflict is inequivalence (Def. 4). 2603 Cor. 3: after normalisation, containment is subset of simple rules. Offers are the negotiation artefact (2606: do not evaluate them as agreements).
- **Current gap:** The pipeline converts one text to one document. There is no `compare(provider, requester)`. `evaluator.agreement` samples requests and treats any decision mismatch as equal weight; it cannot say “the request is stricter, so allow the exchange.”
- **Proposed enhancement (IR-compatible):** `odrl_agent/compare.py`: `compare(provider: PolicyIR, requester: PolicyIR) -> {symmetric, asymmetric, missing_obligations, extra_permissions}` using `normalise` (item 3) and the satisfiability oracle (items 1–2). No model call. Expose it as a function the API can wrap later; do not add a second generation path. Refuse to compare an Offer as if it were already accepted; compare Offer (provider) to Agreement (request) only as asymmetric containment.
- **Impact vs effort:** Impact Medium, effort Low once item 3 exists. Depends on items 1 and 3.
- **Verification:** Provider “read in Europe”; requester “read in France” (KB from item 2) is asymmetric-OK. Requester “read worldwide” is asymmetric conflict. Identical IRs are symmetric-equivalent. An obligation on the provider with no matching requester obligation is a conflict even when permissions match (2509 Thm 1, condition 2).

### 10. Enforcement adapter on the compiled graph

- **Category:** Enforcement
- **Research backing:** Cimmino et al. 2409 Algorithm 1: reduce interpolated values, `transformConstraints`, `evaluate`, then `perform` or hand the action back as `(a, a)`. Conditions stay ODRL terms; templates only inject dynamic data (the paper’s own “bad practice” warning). Operators actually implemented: `lt`/`lteq`/`eq`/`neq`/`gt`/`gteq` plus `dateTime`. Sub-30 ms. 2602 Def. 11: if the runtime value has no KB concept, do not satisfy the constraint (default deny), while design-time Unknown only holds publication (item 2).
- **Current gap:** No enforcer. `bench/evaluator.decide` is a partial, IR-level boolean and skips duties. `EntityCatalog` exists in `grounder.py` but `run_conversion` builds `Grounder` without one (walkthrough §6), so parties are `urn:nl2odrl:` slugs, not external identifiers an enforcer could resolve.
- **Proposed enhancement (IR-compatible):** New `odrl_agent/enforce.py` that accepts **only** `compile_ir` output plus a request context map (ODRE’s \(M\)). It interprets `odrl:constraint` operators the compiler emitted, including the set-operator denotations from item 2. It does not call `Extractor`. Unsupported actions return `(action, action)` for a human (ODRE Usage Decision). Interpolation is a side map of operand → runtime literal, never a second policy language. Duties are reported as pending preconditions, not executed (2509 monitoring is a log; this adapter is the synchronous half only).
- **Impact vs effort:** Impact Medium, effort Medium. Depends on items 2 and 4 for an honest constraint and duty report. Not required to publish policies.
- **Verification:** Port the spirit of ODRE’s 24 tests: six operators × constant, six × `dateTime`, plus one `isPartOf` case that denies when the KB grounding of the request is missing. No HTTP in the test. A C-level-style hidden constraint cannot appear because the function rejects documents that did not come from `compile_doc` (uid prefix `urn:nl2odrl:policy:` and a hash match against the IR).

---

## 5. Phase-wise implementation order

The build order is **not** the impact ranking in §4. Phase 1 fixes the review-queue deadlock without a knowledge base. Phase 2 adds denotations and normal form. Phase 3 extends extraction and runtime only after those passes exist. Item 6 (bench oracle) is the exit gate of Phase 2, with a single healthcare regression test landing in Phase 1 so that phase is shippable on its own.

| Phase | Items | Exit |
|---|---|---|
| **1. Solvers** | 1 → 4 | Healthcare example no longer stuck on false `OBLIGATION_PROHIBITED`; remedies compile |
| **2. Semantics** | 2 → 3 → 5, then 6 | Set operators are three-valued; normal form capped at 64; position report; bench measures the new oracle |
| **3. Pipeline** | 7 → 8 → 9 → 10 | Prompts, inlining, comparison, and enforcement all consume the IR and the passes above |

Task-level plan: [`research_papers/IMPLEMENTATION_PHASES.md`](IMPLEMENTATION_PHASES.md).

Every phase keeps three invariants: the model still emits only `PolicyIR`; new IR fields have defaults so existing gold JSON still parses; `normalise` refuses to emit more than 64 simple rules.

## 6. Explicitly out of this roadmap

- Replacing `PolicyIR` with AgentODRL’s Generator or with 2506’s end-to-end KG construction.
- An LLM jury as a release metric (AgentODRL semantic score). Span grounding stays the attribution check.
- Encoding `xone` as a quantified formula inside Z3 (2602: that leaves EPR).
- DNF-splitting obligations (2603 Thm 6).
- Translating ODRL to XACML for enforcement (ODRE related work: lossy).
- A full Isabelle embedding of UFO-L. Item 5 is a finite report over the IR, which is what this codebase can test.
