# Cross-Paper Synthesis: Verifiable, Enforceable NL-to-ODRL

This note connects seven papers spanning generation, formal semantics, ontological grounding, SMT conflict detection, and runtime enforcement. Claims below are grounded in the extracted texts under `research_papers/extracted/` and the per-paper notes in `research_papers/notes/`.

**Corpus**

| ID | Short name | Domain (as assigned) |
|---|---|---|
| [2512.00602v1](notes/2512.00602v1.md) | AgentODRL | Multi-agent NL→ODRL |
| [2506.03301v1](notes/2506.03301v1.md) | Ontology-guided + SCR | Ontology-guided translation |
| [2509.05139v1](notes/2509.05139v1.md) | Evaluation & comparison semantics | Formal semantics |
| [2603.12926v1](notes/2603.12926v1.md) | Normalisation | Formal semantics / comparison |
| [2606.24344v1](notes/2606.24344v1.md) | UFO-L grounding | Ontological grounding |
| [2602.19883](notes/2602.19883.md) | Denotational constraint conflicts | Verification / SMT |
| [2409.17602](notes/2409.17602.md) | ODRE | Runtime enforcement |

---

## Comparison table

| Paper | Approach | Permissions | Prohibitions | Duties / remedy / consequence | Constraints | Refinements | Logical operators | Validation / verification | Evaluation |
|---|---|---|---|---|---|---|---|---|---|
| **AgentODRL** 2512.00602 | Orchestrator–Workers (Rewriter, Splitter, Generator); LoRA semantic referee; PYSHACL loop | Generated as `Cpolicy` | Generated | Generated as part of `Cpolicy`; no separate duty semantics | Contextual `Ccontext` | Not distinguished | Parallel/recursive NL structure, not `and`/`or`/`xone` | SHACL grammar + LLM-jury semantic checkpoints | 770 synthetic data-space cases; GPT-4.1 family; AOFP grammar up to **99.89**, semantic **97.93**; +5.39% / +14.52% vs SCR |
| **Ontology-guided** 2506.03301 | Distilled LGT (OSES) + 175/16 human SCR; SHACL scoring | Yes (Agreement/Offer/Set templates) | Example NL has prohibit-USA | Dual role documented (`obligation` vs `duty`) | C8: left/op/right | Not distinguished | Implicit in examples | SHACL shapes (22/20/16 properties); SCR repair | 12 DRK cases; 108 runs; GPT-4/4o; UC1 **91.95%** (217/236) |
| **Eval/compare semantics** 2509.05139 | FOL queries over event relation; ODRL Lite + full extension | Prohibited-by-default: unmatched event violates | Carve-outs of permissions | `DP`/`DPC`/`FR`/`OC` with timestamps; duty ⊂ permissions | SPARQL-filter ops including set ops | Features with \(\gamma_i\) pointing at Action/Asset/Party | \(\land\lor\lnot\); **omits `andSequence`**; XOR for negation | Query evaluation; Thm 1 pairwise comparison on *consistent* Lite policies | Proofs + examples; ODRL2SHACL in UPCAST; no numeric benchmark |
| **Normalisation** 2603.12926 | DNF + interval split; P\\F permissions-only form | Retained (or dual F-only) | Compiled into permission holes | **Explicitly not normalised** (splitting tightens obligations) | Numeric/symbolic `=,<,>`; **no sets, no `isA`** | Same as constraints in the inherited semantics | Flattened to simple conjunctions | Semantics-preservation Thms 1–7; comparison = set equality of simple rules | Proofs + CLI prototype; **no runtime experiments**; size \(O(\|R\|·2^n)\), exp. in attributes |
| **UFO-L grounding** 2606.24344 | Foundational ontology, not denotation; 8 Hohfeldian positions | Weak \(\rho_P\); strong \(\rho_P+\rho_I\) | Sanctioned \(\rho_F\); +remedy \(\rho_R\) Power/Subjection | Achievement-only Formal Semantics; \(\rho_D\); IM cascade unevaluated | Activation conditions only (create no positions) | Not treated | `conflict` perm/prohibit/invalid | Isabelle + Vampire/E/Z3; 39 problems GRND001–036 | All discriminating/entailment checks pass; E/Z3 fail 3 SAT; Vampire finite model |
| **Constraint SMT** 2602.19883 | Denotations in KB; 3-valued Kleene; EPR | Out of scope (constraint pairs, often P vs F refinements) | Same | Out of scope | 8 ops × 3 domains; 14 KB-dependent operands listed | Cross-rule refinements as composition | `and`/`or`/`xone` (cross-rule); **xone needs ⊥⊥ axioms** | Vampire+Z3 **154/154** agree; Isabelle 44 lemmas, 0 sorry; runtime soundness Thm 3 | 6 KB families + 4 structural KBs; 90+21+23+6 problem split |
| **ODRE** 2409.17602 | Language-layered policies; Algorithm 1 reduce→evaluate→perform | Rules whose constraints eval true contribute actions | Same boolean path (no deontic distinction in Alg. 1) | Actions performed or forwarded as \((a,a)\) | Interpreted binary ops; live interpolation | Not distinguished | Implicit `and` of a rule’s constraints | 24 unit tests × 2 engines | All tests < **0.03 s**; six comparison ops + `dateTime` + `otime:*` |

---

## End-to-end lifecycle integration

The seven papers do not propose one stack, but they **compose cleanly if each is given a stage with an explicit contract**.

```
 NL use case
    │
    ▼
[1] Orchestrator  (AgentODRL)
    │  classify simple | parallel | recursive
    ├─ Rewriter: structure-preserving inlining
    ├─ Splitter: asset / party / purpose units → Agreement|Offer|Set
    ▼
[2] Generator  (2506 LGT+OSES+SCR inside AgentODRL Generator)
    │  distilled ontology, per-type templates, SCR catalog
    │  LoRA/checkpoint semantic reflection
    │  PYSHACL syntax loop  (AgentODRL + 2506 shapes C1–C9)
    ▼
    A-level ODRL graph  (+ optional odrl-l: terms from 2606)
    │
    ▼
[3] Normaliser  (2603 on Lite fragment)
    │  N(R) → DNF simple rules → interval split vs partner constants V
    │  compile F into P holes  (or dual);  NEVER split obligations
    │  drop R_⊥ empty intervals
    ▼
[4a] Constraint verifier  (2602)          [4b] Position instantiator (2606)
    │  per-leftOperand KB                 │  activate → Pos(π,e)
    │  denotation ⊓ → Conflict|OK|Unknown │  Ax 5.10 Permission vs Duty-to-Omit
    │  xone only if ⊥⊥ closed             │  Power/Subjection for each remedy
    │  alignments, no false conflicts     │  behaviour=open ≠ granted Permission
    ▼
[5] Comparison / negotiation  (2509 Thm 1 + 2603 Cor 3)
    │  consistent form: P ⊓̸ F, O ⊑ P
    │  asymmetric: requester P ⊑ provider P' and obligations covered
    │  symmetric: query equivalence for lock-step contracts
    ▼
[6] Runtime enforcement  (ODRE Alg. 1 + 2509 monitor)
    │  B1 interpolation of live values (tokens, GPS) — not extra constraints
    │  transformConstraints → evaluate → Usage Decision
    │  append performed action as event e; evaluate 2509 queries (duties, remedies)
    │  ungrounded KB value → default-deny (2602 Def 11), not silent allow
    ▼
    Audit: Evaluation Report + UFO-L relators (who declared violation)
```

**Why this order.** Generation papers maximise syntactic/semantic *fidelity to NL* but do not decide what a policy *means* in a world. Salas evaluation is the meaning for monitoring and comparison; normalisation is the **interop compiler** so fragment-limited tools (including ODRE’s six operators) still see an equivalent policy. 2602 is the missing **KB** for set operators that 2603 explicitly refuses. 2606 sits beside 2509: verdicts vs **positions/authority**. ODRE is the only paper that actually **runs** operators and actions; it should consume *already verified* A-level graphs, not C-level policies with hidden constraints.

**Data contracts between stages**

- Generator → Normaliser: well-formed rules (2509: one Action; one spec per core component; boolean combos per-component).
- Normaliser → SMT: simple conjunctive rules plus the set-constraint subset that 2603 dropped, tagged with operand domain (taxonomic/mereological/nominal).
- SMT → Enforcer: only Compatible (or Unknown-at-design with deny-if-ungrounded-at-runtime).
- Enforcer → Monitor: Usage Decision becomes candidate events; duty timestamps \(e'_0 \le e_0\) (2509 eq. 5).

---

## Contradictions and tensions

### 1. Open world vs closed world vs “normative vacuum”

| Source | Claim |
|---|---|
| W3C Formal Semantics (quoted in 2606) | `behaviour=open`: silence implies permission; `closed` (default): silence implies prohibition |
| Salas 2509 | **Prohibited-by-default** as the practical data-sharing choice; permitted-by-default simulated by a universal permission, which **cannot carve permitted subsets out of prohibitions** |
| Salas 2603 | Same; can dual-normalise to prohibitions-only under permission-by-default |
| UFO-L 2606 **Prop. 5.1** | Under `open`, unregulated actions carry **no Permission, No Right, Duty, or Right** — a runtime convention, **not** a granted Permission. Closed-world declared permissions are **weak**, not strong (no Immunity) |
| Constraint SMT 2602 | Open-world **KB** incompleteness → Unknown; for `xone`, missing \(\bot\!\!\bot\) **blocks exclusivity** even if one branch looks satisfied (ODRL086) |

**Implication:** an NL-to-ODRL product must **not** equate three different “opens”: (a) evaluator default-allow, (b) Salas universal permission, (c) UFO-L vacuum, (d) SMT Unknown. Recommend: **closed-world evaluation + weak permissions + explicit `odrl-l:stronglyPermitted` when NL says “irrevocable” + SMT Unknown ≠ allow**.

### 2. Conflict resolution strategy

- ODRL IM `odrl:conflict`: `perm` / `prohibit` / `invalid` (2606).
- Salas: overlapping P and F are **redundant after consistent form**; prohibitions exist to **carve** permissions, then disappear (2603 Thm 7).
- UFO-L Ax 5.10: Permission to Act and Duty to Omit same \(\langle a,t\rangle\) is a **logical contradiction**, not a strategy flag.
- AgentODRL/2506: no conflict operator in the reported metrics.

**Implication:** `invalid` aligns with Ax 5.10 and with “refuse to generate overlapping P/F”. `perm`/`prohibit` are **extra-semantic policies** that 2509/2603 would compile away. Do not let the LLM emit overlapping rules and hope `conflict` saves them; normalise or reject.

### 3. What “duty” means

- 2506 LGT: Duty as **obligation** *or* **permission condition**.
- 2509: duty of a permission is a **prior event** (`e'_0 ≤ e_0`) and **must itself be permitted**; remedies/consequences are temporally directed; putting them in \(F\) or \(O\) makes remedies meaningless.
- 2603: **cannot** DNF-split obligations (would require *all* subintervals).
- 2606: Formal Semantics is **achievement-only**; Dam et al. 22 patterns include maintenance/process with **no evaluator transition**; IM remedy cascade **unevaluated**.
- ODRE: a duty is just another rule whose action is `perform`ed or forwarded.

**Implication:** NL “must keep encrypted” cannot be compiled to current Formal Semantics. Generation should tag duty *role* (`obligation` | `duty` | `remedy` | `consequence`) and **refuse maintenance duties** or route them to an extension profile.

### 4. Set operators and `isA` vs `isPartOf`

- 2509: distinct operators, SPARQL-filter, `isA` rewritten as `hasPart` on class sets.
- 2603: **excludes** set constraints and `=type`.
- 2602: `isA` and `isPartOf` get **identical denotations**; the distinction is entirely the KB’s \(\le\) (⊑ vs ⪯). Operator label is “redundant given the KB’s semantic domain”.

**Implication:** a generator that emits `isPartOf` without a mereological KB is meaningless. Normaliser and SMT must be **sequenced**, not substituted.

### 5. Offers and Sets

- AgentODRL Splitter **assigns** Agreement/Offer/Set by heuristics.
- 2506 has separate LGTs and SHACL cardinality (Offer: assigner only; Agreement: both parties).
- 2606: **Offers must not be evaluated**; Sets create **no bond**. Grounding is Agreement-centric.
- ODRE examples include `"@type": "Set"`.

**Implication:** generated Offers are **templates for negotiation** (2509 asymmetric comparison), not inputs to ODRE. Evaluating an Offer as if it were an Agreement is a spec violation.

### 6. Enforcement vs monitoring vs generation quality

- ODRE Algorithm 1 is **synchronous constraint-true → do action**; no event log, no prohibition-as-violation-with-remedy, no obligation deadlines.
- 2509 monitoring is **log-complete closed-world**.
- 2602 runtime: ungrounded value **does not satisfy** (default-deny), while analysis Unknown does not block authoring.
- AgentODRL “grammar 99.89%” is SHACL, **not** “this policy is enforceable or conflict-free”.

**Implication:** SHACL-valid JSON-LD can still be SMT-Conflict, UFO-L incomplete, and ODRE-unenforcible (`read` abstract). Metrics must be staged.

### 7. Comparison of policies: query containment vs simple-rule identity vs denotation intersection

These are **compatible layers**: 2603 identity of split rules *implements* 2509 rule containment for numeric/symbolic Lite; 2602 denotation intersection is the analogue for KB-backed set constraints; 2509 query containment remains the gold definition once duties exist.

### 8. Who is the authority?

ODRE asks who performs actions / supplies `spatial` and leaves it unspecified. 2606 answers: assigner Power to `decl(a)`, assignee Subjection; two theaters ⇒ **two Powers**. AgentODRL never generates this. **Mustafa 2026 UFO-L is the missing authoring target for “who may sanction”.**

### 9. Hidden behaviour in templates (ODRE vs everyone else)

ODRE C/B2 levels can implement extra conditions in Jinja/Java—the paper itself calls this a **bad practice** that destroys comparability. Salas comparison/normalisation and 2602 denotations **assume** all conditions are ODRL constraints. **Ban C-level constraint encoding in an NL-to-ODRL product.**

---

## Actionable architecture blueprint

Design recommendations for a state-of-the-art, verifiable, enforceable NL-to-ODRL system, each traceable to the corpus.

### A. Generation

1. **Complexity-routed MAS** (AgentODRL): Orchestrator + Rewriter (inline references) + Splitter (asset/party/purpose) + Generator. Do not use a single LLM for recursive legal text.
2. **Distilled, type-specific prompts** (2506): Agreement vs Offer vs Set LGTs; OSES (syntax, duty dual-role, examples). Never dump ODRL22.ttl + full W3C rec.
3. **SCR catalog** (≥175 Agreement rules as a seed) applied iteratively; do not rely on raw SHACL diagnostics as the only repair signal (2506: LLMs are stateless w.r.t. SHACL).
4. **Two-channel quality:** PYSHACL closed loop (syntax) + independent semantic checkpoint model (LoRA or rules). Max-reflection budget scales with model weakness (nano needed ~7).
5. **Emit `odrl-l:`** (`stronglyPermitted`, `hasRemedy`, assigner Right, Power/Subjection) when NL implies irrevocability or sanction (2606 P2–P4).
6. **Well-formedness gates** (2509): every rule names an Action; refinements vs constraints not mixed across components in one boolean; no `andSequence` until semantics exist.

### B. Compile / interop

7. **Normalise** generated graphs to simple disjoint permissions (2603) for downstream fragment tools. Keep original graph for audit.
8. **Obligations stay atomic.** Interval-split only P and F.
9. **Consistent form** before any comparison: remove P∩F and unpermitted O (2509 Def 8).

### C. Verify

10. **Constraint SMT in EPR** (2602) with bidirectional denotation axioms, typed KBs (GeoNames/ISO 3166, DPV, BCP 47, …), and witness-complete alignments. Three-valued output.
11. **`xone` policy:** refuse to generate `xone` unless the KB has disjointness covering other branches; otherwise rewrite to explicit pairwise Conflicts or leave Unknown (ODRL085 vs 086).
12. **Ax 5.10 SMT check:** Permission(x,a,t) ∧ Duty(x,rfr(a),t) unsat (2606).
13. **QA suite:** unsatisfiable `and`, subsumed conjuncts (France ∧ Europe), DSSC downstream-only-restricts via \(\sqsubseteq_c\) (2602).
14. **Do not treat Unknown as Conflict** (false deny of sharing) **or as Compatible** (false allow). Surface it to authors.

### D. Negotiate / compare

15. **Asymmetric containment** for data-space exchange (2509 Def 5; Thm 1): requester permissions ⊆ provider permissions; every provider obligation matched by a tighter requester obligation.
16. **Symmetric equivalence** only when both parties must match exactly.
17. Offers are **negotiation artefacts**, not evaluator inputs (2606).

### E. Enforce

18. **ODRE A-level interpreter** with ontology-coupled functions; Python or SPARQL both proven < 30 ms for core comparison ops.
19. **B1 interpolation** for secrets and live context; conditions remain ODRL terms. No B2/C constraint logic.
20. **Default-deny ungrounded runtime values** (2602 Def 11); extend KB rather than fail open.
21. After a Usage Decision, **append an event** and run 2509 queries for duties/remedies/consequences (ODRE does not).
22. **Synchronous vs monitoring:** ODRE for request-time; Salas evaluator for logs. ODRL cannot label this—add a profile term.
23. **Extend ontology + interpreter together** for time/geo/N-ary (ODRE 3.2); do not overload `read`.

### F. Authority and product semantics

24. **Pin `behaviour=closed`** for data spaces; document that this is weak permission. Strong permission is an explicit profile flag, not the open-world default (2606).
25. **Every remedy names a Power holder** (default: assigner). Multi-provider aggregators must not share one evaluator-implicit authority (KulturPortal scenario).
26. **Achievement duties only** unless a maintenance profile is specified; be honest in NL generation when the target semantics cannot express the clause.
27. **Staged acceptance tests**, not one score: SHACL (2506/AgentODRL) ∧ SMT (2602) ∧ position instantiation (2606) ∧ ODRE unit tests ∧ 2509 query fixtures.
28. **Evaluation data:** AgentODRL’s 770-case structural split + 2506’s 12 DRK seeds + 2602’s 154 TPTP/SMT problems + 2606’s 39 GRND problems + ODRE’s 24 policies. That is the regression pack; do not report only grammar%.

### G. What not to copy

29. Do not use LLM-jury semantic scores as a **compliance** metric (AgentODRL).
30. Do not translate away from ODRL into XACML as the general solution (ODRE related work).
31. Do not materialise action-inclusion for **prohibitions** without an explicit product decision (2509 §3.3 leaves this open).

---

## Suggested ownership map (for this repository)

| Component in an NL-to-ODRL stack | Primary papers |
|---|---|
| Prompting / ontology distillation / SCR | 2506.03301 |
| Multi-agent routing, LoRA referee, SHACL loop | 2512.00602 |
| Event semantics, duties, comparison theorems | 2509.05139 |
| DNF, interval split, P-only compilation | 2603.12926 |
| Legal positions, `behaviour`, remedy authority | 2606.24344 |
| Set operators, KBs, Z3/Vampire, `xone` | 2602.19883 |
| Runtime interpreter, interpolation, extensions | 2409.17602 |
