# Model Card: Natural Language to ODRL Policy Assistant

> Auto-generated on 2026-09-19 11:39 UTC by `rai-card` (policy `nl2odrl-rai-v1`).
> Build artifact. Edit `model_card.yaml` and `metrics.json` — not this file.

**Deploy verdict: `PASS`** — 0 error(s), 0 warning(s).

| Field | Value |
|---|---|
| System ID | `nl2odrl-v1` |
| Eval version | `0.3.0` |
| Risk tier | `high` |
| Owner | Policy Language Team — Responsible AI |
| Pipeline | `natural_language` → `odrl` → `cedar` → `opa_rego` |
| Last reviewed | 2026-09-01 |
| Recertify by | 2027-03-01 |
| Gold set hash | `sha256:9f3c1a7e2b44d8c0e15a6b7d4c3e2f1908a7b6c5d4e3f2019283a4b5c6d7e8f9` |
| Model hash | `sha256:c0d1e2f3a4b5c6d7e8f90123456789abcdeffedcba9876543210abcdef012345` |
| Eval harness SHA | `a8b2c4d6e0f1928374655647382910abcdef12` |
| Evaluated on | 2026-09-10 |
| Enforcement encodings | `cedar`, `opa_rego` |

## 1. Intended use

Draft W3C ODRL 2.2 policies from natural-language access rules, then propose Cedar and OPA/Rego encodings of that ODRL for a human policy author. Output is a suggestion. It becomes enforceable only after named sign-off and explicit attach to a policy enforcement point. Evaluation (this repo) measures gold NL ↔ gold ODRL ↔ frozen Cedar/Rego pairs. This repo does not run the converter.

**Intended users**

- Policy authors drafting dataset, document, or API usage rules
- Authorization engineers reviewing Cedar / Rego diffs
- Responsible AI reviewers checking over-permission on a gold set

This card describes a **policy-authoring assistant**. It does not evaluate live authorization requests. Evaluation here means: gold NL vs gold ODRL vs compiled Cedar/Rego, measured by a separate harness.

## 2. Out of scope and prohibited uses

**Out of scope**

- Live authorization of production requests (that is the PEP, not this model)
- Translating statutes, contracts, or court opinions into code
- Credit, lending, insurance, employment, or housing decisions
- Inventing permissions that are not in the source sentence
- Multi-jurisdiction legal interpretation

**Prohibited**

- Auto-attaching generated Cedar or Rego to a PEP / OPA cluster
- Treating model output as legal advice or as a substitute for counsel
- Widening access because the NL was ambiguous (must fail closed)
- Using the assistant to weaken an existing deny/prohibition

## 3. Decision rights

| Control | Value |
|---|---|
| Automation | `human_in_the_loop` |
| Sole determinant | `false` |
| Author override | `true` |
| Sign-off before PEP attach | `true` |
| Fail closed on ambiguous NL | `true` |
| Review | policy-authors@example.org (SLA 5 days) |

## 4. Limitations

Natural language is underspecified. The assistant must not guess missing actors, assets, actions, or constraints into an allow. Temporal, spatial, and duty/obligation clauses are a known weak slice. Ambiguous prompts are out of the automated-suggest path and must fail closed. ODRL is the semantic source of truth; Cedar and Rego are encodings and must not add permits. English gold set only. Not validated as a legal-reasoning system.

### Known failure modes

- **ambiguous_nl** — exclude_from_automated_path: Vague actor/asset/action language must not emit a permit. Route to the author with a clarification prompt; default deny.


## 5. Ethical considerations

Silently granting more rights than the speaker stated is the harm this system can cause. We therefore gate on over-permission rate, ODRL constraint preservation, and encoding agreement (ODRL→Cedar, ODRL→Rego), not on BLEU-like fluency. Protected-attribute fairness is not the primary lens here; privilege expansion is. Authors retain override. Generated policy is not law.

The red-line metric is **over-permission**: the conversion grants a right the source language did not. Under-permission is fail-closed and is tolerated up to a bound.

## 6. Evaluation evidence (not editable)

Frozen gold set of NL sentences with independently authored ODRL JSON-LD and reviewed Cedar/Rego encodings. Metrics are produced by an eval harness that lives outside this dummy repo. CI only consumes the numbers.


| Metric | Value | Policy |
|---|---|---|
| Gold n | 240 | ≥ 100 |
| Semantic match | 0.930 | ≥ 0.900 |
| Constraint preservation | 0.970 | ≥ 0.950 |
| Over-permission rate | 0.008 | ≤ 0.020 |
| Under-permission rate | 0.041 | ≤ 0.100 |
| Cedar agreement | 0.970 | ≥ 0.950 |
| Rego agreement | 0.960 | ≥ 0.950 |

### ODRL construct coverage

| Construct | Gold cases |
|---|---:|
| `constraint` | 92 |
| `obligation` | 41 |
| `permission` | 88 |
| `prohibition` | 64 |

### Slices

| Slice | n | Semantic match | Over-permission | In production |
|---|---:|---:|---:|---|
| ambiguous_nl | 35 | 0.610 | 0.110 | no |
| temporal_constraints | 48 | 0.880 | 0.012 | yes |

## 7. Regulatory mapping

| Instrument | Statement |
|---|---|
| EU AI Act | Documented as a high-influence authoring tool with mandatory human oversight (Art. 14 analogue). It is not placed in production as an autonomous decision system. Transparency (Art. 13) is this model card plus the compliance bundle.
 |
| ODRL profile | W3C ODRL 2.2 core (permission, prohibition, obligation, constraint) |
| NIST AI RMF | GOVERN, MAP, MEASURE, MANAGE |

- NIST AI RMF 1.0 (GOVERN / MAP / MEASURE / MANAGE)
- EU AI Act — transparency, human oversight, accuracy (not a legal-advice system)
- W3C ODRL 2.2 Information Model
- OECD AI Principles (robustness, accountability)
- Mitchell et al. 2019 Model Cards

## 8. Contact

- Owner: `policy-language@example.org`
- Model risk: `model-risk@example.org`
- Ethics: `responsible-ai@example.org`

Recertify if the ODRL profile, Cedar schema, or Rego helpers change — encoding drift is a silent allow. Do not read a passing over-permission rate as "safe to auto-deploy."


## 9. Gate results

| Check | Result | Title | Detail |
|---|---|---|---|
| `doc.completeness` | PASS | Required governance fields | All required static governance fields are present. |
| `schema.static` | PASS | Model card schema | Valid. |
| `schema.dynamic` | PASS | Evaluation evidence schema | Valid. |
| `schema.bundle` | PASS | Compliance bundle schema | Valid. |
| `identity.model_id` | PASS | System ID consistency | Card and evidence both describe `nl2odrl-v1`. |
| `identity.lineage` | PASS | Gold set and artifact hashes | Gold-set, model, and eval-harness hashes are present. |
| `doc.pipeline` | PASS | Documented conversion pipeline | Card names NL → ODRL → Cedar → Rego as the intended pipeline. |
| `lifecycle.review_freshness` | PASS | Model card recency | Last reviewed 2026-09-01 (18 days ago). |
| `lifecycle.recertification` | PASS | Recertification not expired | Recertification due 2027-03-01. |
| `rights.sole_determinant` | PASS | Not the sole enforcement decision | Generated policies are decision-support, not a PEP by themselves. |
| `rights.automation_cap` | PASS | High-risk automation cap | Automation `human_in_the_loop` is within cap `human_in_the_loop`. |
| `rights.pep_signoff` | PASS | Human sign-off before PEP attach | Cedar/Rego may attach to a PEP only after named human sign-off. |
| `rights.fail_closed` | PASS | Fail closed on ambiguous NL | Ambiguous natural language must not emit an allow. |
| `rights.override` | PASS | Human override of generated policy | Policy authors can reject or edit generated ODRL/Cedar/Rego. |
| `eval.gold_n` | PASS | Gold-set size | gold_n=240 vs min 100 |
| `eval.semantic_match` | PASS | Semantic match vs gold ODRL | semantic_match=0.930 vs min 0.900 |
| `eval.constraints` | PASS | ODRL constraint preservation | constraint_preservation=0.970 vs min 0.950 |
| `eval.over_permission` | PASS | Over-permission rate (false allow) | over_permission_rate=0.008 vs max 0.020 |
| `eval.under_permission` | PASS | Under-permission rate (false deny) | under_permission_rate=0.041 vs max 0.100 |
| `eval.cedar` | PASS | ODRL → Cedar round-trip agreement | cedar_agree=0.970 vs min 0.950 |
| `eval.rego` | PASS | ODRL → Rego round-trip agreement | rego_agree=0.960 vs min 0.950 |
| `eval.constructs` | PASS | ODRL construct coverage | Gold set covers permission, prohibition, obligation, and constraint. |
| `slices.production_quality` | PASS | In-production slices meet safety floor | In-production slices meet semantic-match and over-permission floors. |
| `slices.documented_exclusions` | PASS | Suppressed slices named on the card | Out-of-production slices are listed under known_failure_modes. |
| `control.computed_verdict` | PASS | Verdict is computed, not declared | Deploy verdict is computed from policy × evidence. |
| `bundle.system_id` | PASS | Bundle describes this system | Compliance bundle system_id matches `nl2odrl-v1`. |
| `bundle.attestations` | PASS | Bundle attestations match the card | Not-legal-advice, no auto-PEP-attach, fail-closed, frozen gold set. |
