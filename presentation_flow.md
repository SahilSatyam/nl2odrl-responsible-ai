# Presentation flow: Responsible & Ethical AI

**Audience:** auditors, policy authors, engineering leads  
**System under discussion:** Natural Language → ODRL → Cedar / OPA-Rego **assistant** (this dummy)  
**What we are *not* presenting:** a converter, a credit model, or a live PEP  

**Sources (argument, not slides to paste):**

- [Day 056 — Automated Documentation: The Dynamic Model Card](https://veritas-one-theta.vercel.app/day/056)
- [Day 060 — The White Box Capstone: The Audit Defense](https://veritas-one-theta.vercel.app/day/060)

**Artifacts you will open on stage**

| Order | File | Role for the auditor |
|---|---|---|
| 1 | `model_card.yaml` | Human-owned narrative (PR-reviewed) |
| 2 | `metrics.json` | Eval evidence (no PASS stamp) |
| 3 | `policies/governance.yaml` | Thresholds owned by Responsible AI, not the model |
| 4 | `compliance/bundle.yaml` | Release inventory + attestations |
| 5 | `exhibits/gold_set/` | Frozen NL / ODRL / Cedar / Rego pair |
| 6 | `MODEL_CARD.md` + `COMPLIANCE_BUNDLE.md` | Build artifacts the auditor actually files |
| 7 | `rai-gate.json` | Machine-parseable gate result |

**Setup (before you walk in)**

```bash
python3 -m pip install -r requirements.txt
PYTHONPATH=. python3 generate_card.py   # leave MODEL_CARD.md + COMPLIANCE_BUNDLE.md green
PYTHONPATH=. python3 serve_audit.py     # auditor API on 127.0.0.1:8080
```

Keep `model_card_BROKEN_demo.yaml` and `fixtures/metrics_overpermit_fail.json` ready.

---

## Arc (why this talk exists)

Day 056’s claim: **documentation drift is a falsified record**, not a messy wiki. If the card describes v1.0 and production is v3.2, the audit has already failed. EU AI Act analogue: technical documentation (Art. 11) and transparency (Art. 13) must match the system that is actually running.

Day 060’s claim: when an auditor asks “prove it,” a Jupyter notebook is malpractice. They need a **compliance bundle** — a snapshot they can retrieve, not a story you reconstruct.

This dummy applies both to **policy translation**, not lending:

- The **model card** is the system of record for *what the assistant is allowed to do*.
- The **compliance bundle** is the envelope the auditor takes home: inventory, hashes, attestations, human sign-off.
- The **ethical red line** is **over-permission** (the conversion grants a right the sentence did not). Fluency is not a control.

You never claim this repo *is* the converter. You claim: *this is the RAI layer you wrap around whatever converter you build later.*

---

## Timing (≈ 28 minutes + 7 minutes Q&A)

| Min | Act | Beat |
|---|---|---|
| 0–3 | 0 | Hook: auditor question, not a model demo |
| 3–8 | 1 | What we built (pipeline + what we refused to automate) |
| 8–16 | 2 | Model card as build artifact (Day 056) — live gate |
| 16–23 | 3 | Compliance bundle as audit defense (Day 060) |
| 23–26 | 4 | How an auditor actually walks the files |
| 26–28 | 5 | Close: culture vs. “defraudment engine” |
| +7 | Q&A | Prepared answers below |

---

## Act 0 — Hook (0–3 min)

**Do not open with architecture.** Open with the auditor.

> “Show me the system that turned this English rule into Cedar. What version was running, what it is *not* allowed to do, and who signed off before it could attach to a PEP.”

If the answer is a slide or a stale PDF, you have Day 056’s failure: **documentation drift**.

**Say this once:**

- In ordinary software, stale docs annoy developers.
- In AI, stale docs are a **false record** of intended use, limitations, and evaluation.
- Auditors do not want a tour of the model. They want a **system of record** (deterministic, immutable) sitting next to a **system of intelligence** (probabilistic, changing).

**Show (one sentence on a whiteboard):**

```
NL  →  ODRL (semantic truth)  →  Cedar + Rego (encodings)
         ↑ human card                    ↑ must not widen rights
```

---

## Act 1 — The system, bounded (3–8 min)

**Goal:** freeze scope so nobody asks you to live-compile policies.

**Talk track**

1. The product is an **authoring assistant**, not a policy enforcement point.
2. ODRL is the semantic source of truth. Cedar and Rego are encodings. If they add a `permit` the ODRL does not contain, that is a **silent privilege expansion**.
3. Ambiguous English **fails closed**. Guessing an allow is the unethical default.
4. This repository codes the **envelope** (card, bundle, gate). It does not parse NL.

**Show** `model_card.yaml` sections, in this order only:

1. `intended_use` — suggestion, not enforce.
2. `out_of_scope` / `prohibited_uses` — Day 056 pitfall: the **“not for”** list matters more than the “for” list. Point at: not live authz, not legal advice, not lending, not auto-attach to PEP.
3. `decision_rights` — `human_in_the_loop`, `attach_to_pep_requires_signoff`, `fail_closed_on_ambiguity`.
4. `ethical_considerations` — over-permission, not BLEU.

**Line to land:** “Responsible AI here is *least privilege of generated policy*, not a fairness dashboard from a credit model.”

**Open** `exhibits/gold_set/01.nl.txt` then the matching `.odrl.jsonld`, `.cedar`, `.rego`. One breath: “This is what a gold pair looks like. Evaluation means these stay frozen and hashed. We did not generate them in this talk.”

---

## Act 2 — Dynamic model card (Day 056) (8–16 min)

### 2a. Hybrid source (2 min)

Draw this; then open the files.

| Source | Who writes it | Lives in | Auditor question it answers |
|---|---|---|---|
| Static context | Humans, PR + CODEOWNERS | `model_card.yaml` | What is it *for*? What must it never do? |
| Dynamic facts | Eval harness | `metrics.json` | How did *this* artifact score on the gold set? |
| Policy | Responsible AI / MRM | `policies/governance.yaml` | What numbers are allowed to ship? |
| Compiler + gate | CI | `generate_card.py` | If this fails, the release does not exist. |

**Day 056 rule, restated:** we cannot auto-generate intended use. We must auto-generate hashes, dates, and metrics so the card cannot describe a different binary.

**Anti-pattern to name:** a training job (or eval job) that carries `status: PASS`. That is not a control. Thresholds live in `policies/governance.yaml`. The gate **recomputes**.

### 2b. Docs-as-code (1 min)

- YAML in Git → same review as code (Day 056: human review of static context on every PR).
- Markdown is a **build artifact**. Hand-editing `MODEL_CARD.md` is process failure.
- PDFs are where information goes to die. Auditors get Markdown + `rai-gate.json`.

### 2c. Live demo — happy path (~20 s)

```bash
PYTHONPATH=. python3 generate_card.py
```

Point at the top of `MODEL_CARD.md`:

- Auto-generated timestamp
- Deploy verdict `PASS`
- Gold set hash / model hash / eval harness SHA
- Pipeline `natural_language → odrl → cedar → opa_rego`

**Line:** “This timestamp is proof it is not a Google Doc from last quarter.”

### 2d. Live demo — the moment that lands (~40 s)

Day 056 exercise: **build fails if Limitations is missing.**

```bash
cp model_card.yaml model_card_GOOD_backup.yaml
cp model_card_BROKEN_demo.yaml model_card.yaml
PYTHONPATH=. python3 generate_card.py
```

Non-zero exit. Say out loud:

> “That exit code is what stops this from reaching an auditor as a ‘current’ system. Nobody has to remember to check Limitations. The pipeline will not emit a card.”

Restore immediately:

```bash
cp model_card_GOOD_backup.yaml model_card.yaml
rm model_card_GOOD_backup.yaml
PYTHONPATH=. python3 generate_card.py
```

### 2e. Second failure (only if asked: “does it only check prose?”)

```bash
PYTHONPATH=. python3 generate_card.py --dynamic fixtures/metrics_overpermit_fail.json
```

Over-permission 0.09 vs policy 0.02. **Same mechanism, different trigger.** Restore with the happy-path command.

### 2f. False confidence (30 s)

Day 056 warning: automation can **propagate a lie** if `intended_use` is copy-pasted (“safe to auto-enforce”) while the model is still experimental.

**Mitigation you already have:** PR review of `model_card.yaml`; recertification date; bundle attestations must match the card (`no_auto_pep_attach`, `fail_closed_on_ambiguity`).

Do not log NL samples with PII in the card. Hashes only.

---

## Act 3 — Compliance bundle (Day 060) (16–23 min)

Day 060’s per-prediction bundle (input snapshot, SHAP, recourse) is for **decision systems**. This assistant does not score a person. Map the pattern honestly:

| Day 060 (decision system) | This dummy (policy assistant) | Auditor still gets |
|---|---|---|
| Prediction ID + feature hash | Gold item + `gold_set_hash` | Immutable *what was evaluated* |
| Score + explanation | Semantic match, constraint preservation, over-permission | Measured behavior, not a story |
| Recourse | Author override + fail-closed + PEP sign-off | Human path when the machine is wrong |
| Link to model card + fairness report | Link to `MODEL_CARD.md` + `rai-gate.json` + construct coverage | Versioned narrative + computed verdict |
| `/v1/audit/card` | Open `MODEL_CARD.md` / future `GET /audit/card` | Same object, two transports |
| `/v1/audit/trace/{id}` | Exhibit `01.*` + hashes | Trace of a gold conversion, not a loan decision |

**Do not pretend you built the Transparency Portal API.** Say: “Day 060’s portal is the production shape. Today you are seeing the **bundle contents** that portal would return.”

### 3a. Open `COMPLIANCE_BUNDLE.md`

Walk in this order (auditor filing order):

1. **Gate verdict** — computed, not typed.
2. **Inventory** — if a required artifact is missing, there is no release.
3. **Frozen exhibits** — gold pair paths.
4. **Attestations** — not legal advice; no auto-PEP-attach; fail closed; gold set frozen.
5. **Human sign-off checklist** — *do not automate*. Checkboxes exist so a human still exists.
6. **Residual issues** — a passing gate is not a moral certificate.

### 3b. Open `compliance/bundle.yaml`

This is the human-owned spec. CI checks it **aligns** with the card (same `system_id`, same HITL attestations). That is Day 060’s **separation of duties** in miniature: policy/risk owns the bundle shape; the eval job does not get to declare itself compliant.

### 3c. Separation of duties (must say)

Day 060: if `/v1/audit/fairness` is hardcoded to `"PASS"`, you have built a **defraudment engine**.

Equivalent here: if `metrics.json` contains `"status": "PASS"` and CI trusts it, same fraud. This gate **ignores** embedded status and recomputes over-permission from the number.

**Line:** “The team that trains the translator must not own the function that says the translator is allowed to ship.”

---

## Act 4 — Auditor walkthrough (script) (23–26 min)

Play both parts. Keep it to five questions. This is Day 060’s “audit simulation,” rewritten for NL→ODRL.

**A1. “What is this system, and what must it never do?”**  
`GET /v1/audit/card` (or `MODEL_CARD.md`). Intended use, out of scope, prohibited uses.  
*Pass:* PEP attach is prohibited without sign-off; not legal advice; not live authorization.

**A2. “Which artifact was evaluated, on which gold set?”**  
Same card: `gold_set_hash`, `model_artifact_hash`, `eval_harness_git_sha`.  
*Pass:* hashes, not “latest.”

**A3. “Does the conversion expand rights?”**  
`GET /v1/audit/checks/eval.over_permission` and `GET /v1/audit/policy`.  
*Pass:* number is below cap **and** the cap lives in policy, not in the metrics file.

**A4. “Show me one example of NL, ODRL, Cedar, and Rego.”**  
`GET /v1/audit/exhibits/01`.  
*Pass:* all four parts present; encodings default-deny. You are **not** proving a compiler.

**A5. “Who is accountable if this ships broken?”**  
`GET /v1/audit/bundle` — attestations, contacts, recertify date, human checklist.  
*Pass:* named owners, unexpired recertification, checkboxes still human.

If they ask for SHAP on a rejected loan: **wrong system.** This is not a credit scorer. Recourse here is: reject the generated policy, fail closed, do not attach.

**On stage, prefer curl over files** once `serve_audit.py` is running. Files are the source of truth; the API is how auditors retrieve it without a git clone.

---

## Act 5 — Close (26–28 min)

Three sentences, then stop.

1. **Card** = living technical documentation (Day 056). If it cannot generate, the deploy does not happen.
2. **Bundle** = what the auditor files (Day 060). Inventory, hashes, attestations, sign-off — a system of record around a system of intelligence.
3. **Tools do not fix a rotten culture.** A hardcoded PASS is worse than no portal.

**Optional last line:** “You can charge for this envelope. Buyers with their own auditors will pay for a converter that comes with a card and a bundle, not for a clever prompt.”

---

## Slide outline (if you need slides; prefer files)

1. Title: Responsible AI for NL → ODRL → Cedar/Rego  
2. Auditor question (Act 0)  
3. Pipeline + “not a PEP”  
4. Hybrid source table  
5. Screenshot of `MODEL_CARD.md` header  
6. Terminal: `BUILD FAILED` on missing Limitations  
7. Over-permission as the red line  
8. Bundle + `/v1/audit/bundle`  
9. Auditor five questions via curl  
10. Separation of duties / defraudment engine (no hardcoded PASS)  

You should spend more time in the repo than on slides.

---

## What *not* to do on stage

- Do not live-code a compiler.
- Do not wander into credit, auto loans, or demographic AIR. Out of scope on the card on purpose.
- Do not say the EU AI Act Annex III creditworthiness rules apply to this assistant. Use Art. 11 / 13 / 14 as **analogues** (documentation, transparency, human oversight). Day 060 also notes high-risk Annex III dates moved; do not recite fines unless asked.
- Do not show raw gold sentences that look like personal data.
- Do not skip restoring `model_card.yaml` after the broken demo.

---

## Q&A bank

**“Isn’t this just paperwork?”**  
Paperwork is a PDF written after deploy. This is a **gate**. Missing Limitations or too much over-permission is a failed build.

**“Why ODRL in the middle?”**  
So Cedar and Rego cannot silently diverge. Two encodings, one semantic object. Constraint drop = privilege expansion.

**“Where is fairness?”**  
Wrong primary lens for this system. The harm is **asymmetric error**: false allow. If you later translate policies that name protected groups, that is a *new* card + *new* gold slice — recertify.

**“Where is the `/v1/audit` API?”**  
`PYTHONPATH=. python3 serve_audit.py` then `GET /v1/audit/card`, `/bundle`, `/gate`, `/exhibits/01`. Bearer token required. `/v1/predict` is 404 by design.

**“Can we auto-tick the sign-off checklist?”**  
No. That recreates the hardcoded PASS. Day 060: checklist is the human override interface (Art. 14 analogue).

**“What if the gold set is wrong?”**  
Then you have a frozen wrong record — still better than an unfrozen story. Change the gold set via PR, new hash, new card review, new bundle.

**“Does the gate replace Model Risk?”**  
No. It replaces *forgetting*. Recertification, residual risk, and CODEOWNERS remain human.

---

## Demo cheat-sheet (print this)

```bash
# Happy path
PYTHONPATH=. python3 generate_card.py

# Break: missing limitations
cp model_card.yaml model_card_GOOD_backup.yaml
cp model_card_BROKEN_demo.yaml model_card.yaml
PYTHONPATH=. python3 generate_card.py

# Restore
cp model_card_GOOD_backup.yaml model_card.yaml
rm model_card_GOOD_backup.yaml
PYTHONPATH=. python3 generate_card.py

# Break: over-permission (optional)
PYTHONPATH=. python3 generate_card.py --dynamic fixtures/metrics_overpermit_fail.json
PYTHONPATH=. python3 generate_card.py

# Auditor API
PYTHONPATH=. python3 serve_audit.py
curl -sS -H "Authorization: Bearer auditor-demo-token" http://127.0.0.1:8080/v1/audit/card
curl -sS -H "Authorization: Bearer auditor-demo-token" http://127.0.0.1:8080/v1/audit/bundle
curl -sS -H "Authorization: Bearer auditor-demo-token" http://127.0.0.1:8080/v1/audit/gate
curl -sS -H "Authorization: Bearer auditor-demo-token" http://127.0.0.1:8080/v1/audit/exhibits/01
```

**Leave on screen at the end:** `/v1/audit/card` JSON (or `MODEL_CARD.md`) and `/v1/audit/bundle` attestations.
