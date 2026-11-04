# NL → ODRL Responsible AI dummy

This repository is **not** a policy converter. It is the Responsible / Ethical AI **envelope** you would put around a system that:

1. Reads natural-language access rules
2. Drafts **W3C ODRL 2.2**
3. Encodes that ODRL as **Cedar** and **OPA/Rego**
4. Is **evaluated** against a frozen gold set before anyone attaches output to a policy enforcement point (PEP)

**Coded here:** model card, compliance bundle, eval-evidence schema, CI governance gate, auditor JSON API.

**Also coded here:** the NL-to-ODRL converter itself (`odrl_agent/`:
NL parsing, ODRL 2.2 generation, scenario runner, ODRL invariants,
`odrl-host` CLI). Cedar/Rego compilation and live authorization remain
out of scope.

The converter is built on the
[`agentcrash-core`](https://pypi.org/project/agentcrash-core/) PyPI
package (reliability framework: SDK instrumentation, fault injection,
deterministic verification, control plane). Install it with the
converter extras:

```bash
pip install -e ".[converter]"
odrl-host run --scenario odrl_convert_timeout --seed 42
```

Python 3.11+ is required for the converter (`odrl_agent`); the envelope
(`rai_card`, auditor API, gates) still supports Python 3.9+.

## LLM API key (only needed for live model calls)

Everything above the converter line — envelope, card/bundle/gate,
auditor API, full test suite, `odrl-host seed`, `check`, `diagnose`,
`serve` — works **with no key**. Only live model calls need one:

| Needs `LLAMA_API_KEY` + `LLAMA_BASE_URL` | Works offline, no key |
|---|---|
| `odrl-host run` (live ReAct conversion) | `odrl-host seed` (fixture-backed runs into `agentcrash.db`) |
| `odrl-host optimize-prompts` (live DSPy probe; refuses without a key) | `odrl-host check` / `diagnose` (fixture reference patch; `diagnose` only tries the live patch path when `LLAMA_BASE_URL` is set) |
| Live metrics (`scripts/metrics.py`) | `agentcrash check --plugin odrl_agent.verifiers`, PII scan, full pytest suite |

Set both before any live call (any OpenAI-compatible
`{LLAMA_BASE_URL}/chat/completions` endpoint works; default model is
`muse-spark-1.3`):

```bash
export LLAMA_API_KEY=<your-key>      # header only, never logged or traced
export LLAMA_BASE_URL=<your-endpoint>/v1
# optional tuning (defaults shown):
# export LLAMA_TIMEOUT_S=60
# export LLAMA_MAX_TOKENS=8000
```

Without a key, `odrl-host run` fails closed with `E_MODEL_API`
(exit 5) telling you exactly which variables are missing — that is
intentional, not an install problem.

The ethical red line is **over-permission**: the conversion grants a right the source sentence did not. Fluency is not a control.

Speaker notes: [`presentation_flow.md`](presentation_flow.md). Argument sources: [Day 056 (dynamic model card)](https://veritas-one-theta.vercel.app/day/056), [Day 060 (audit defense / compliance bundle)](https://veritas-one-theta.vercel.app/day/060).

---

## Requirements

- Python **3.9+** (`python3 --version`)
- `pip`
- `curl` (to call the auditor API)
- Optional: GitHub Actions (workflow already in `.github/workflows/rai-gate.yml`)

---

## 1. Clone and enter the repo

```bash
cd dummy-project
```

---

## 2. Create a virtualenv and install dependencies

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

`requirements.txt` installs:

| Package | Why |
|---|---|
| `pydantic`, `pyyaml` | Card / bundle / policy schemas |
| `eval_type_backport` | Union types on Python 3.9 |
| `pytest`, `httpx` | Tests (including the API) |
| `fastapi`, `uvicorn` | Auditor portal |

Every Python command below assumes you are in the **repo root** with the venv active, and:

```bash
export PYTHONPATH=.
```

If you skip `PYTHONPATH=.`, you will get `ModuleNotFoundError: rai_card`.

---

## 3. Generate the model card and compliance bundle

This is the CI step. It merges:

- `model_card.yaml` — human-authored, PR-reviewed
- `metrics.json` — eval evidence (never a PASS stamp)
- `policies/governance.yaml` — thresholds owned by Responsible AI
- `compliance/bundle.yaml` — release inventory and attestations

```bash
export PYTHONPATH=.
python3 generate_card.py
```

**Success** writes:

- `MODEL_CARD.md` — auditor-facing card (build artifact; do not hand-edit)
- `COMPLIANCE_BUNDLE.md` — release envelope (build artifact)
- `rai-gate.json` — machine-readable gate (gitignored)

Exit `0` = ship. Exit `1` = governance block. Exit `2` = missing files / import error.

Useful flags:

```bash
python3 generate_card.py --help

python3 generate_card.py \
  --static model_card.yaml \
  --dynamic metrics.json \
  --policy policies/governance.yaml \
  --bundle compliance/bundle.yaml \
  --output MODEL_CARD.md \
  --bundle-output COMPLIANCE_BUNDLE.md \
  --report rai-gate.json
```

Same entry via module:

```bash
python3 -m rai_card
```

---

## 4. Run tests

```bash
export PYTHONPATH=.
python3 -m pytest
```

You should see the gate tests and the auditor API tests pass.

---

## 5. Run the auditor API

Auditors should not grep git. They call a **read-only** portal. Every `/v1/audit/*` call **recomputes** the gate from files on disk. There is no hardcoded `PASS`. There is no converter behind `/v1/predict` (that route is **404 on purpose**).

```bash
export PYTHONPATH=.
export AUDIT_API_TOKEN=auditor-demo-token   # change this outside the talk
# optional:
# export AUDIT_API_HOST=127.0.0.1
# export AUDIT_API_PORT=8080
# export RAI_ROOT="$(pwd)"                  # repo root if you start the server elsewhere

python3 serve_audit.py
```

Leave that terminal running. In another terminal:

```bash
export TOKEN=auditor-demo-token
export BASE=http://127.0.0.1:8080

# Public health (no token)
curl -sS "$BASE/v1/health"

# Auditor map
curl -sS -H "Authorization: Bearer $TOKEN" \
  -H "X-Audit-Role: auditor" \
  "$BASE/v1/audit"

# What is this system allowed to do?
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/card" | python3 -m json.tool

# Release envelope
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/bundle" | python3 -m json.tool

# Computed verdict (not a stamp in metrics.json)
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/gate" | python3 -m json.tool

# Responsible AI thresholds
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/policy" | python3 -m json.tool

# Raw eval evidence
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/metrics" | python3 -m json.tool

# Gold-set trace: NL + ODRL + Cedar + Rego
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/exhibits"
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/exhibits/01" | python3 -m json.tool

# Single control
curl -sS -H "Authorization: Bearer $TOKEN" \
  "$BASE/v1/audit/checks/eval.over_permission"

# Not an inference API
curl -sS -X POST "$BASE/v1/predict" -H "Content-Type: application/json" -d '{}'
```

Interactive OpenAPI: [http://127.0.0.1:8080/docs](http://127.0.0.1:8080/docs) — audit routes still need `Authorization: Bearer …`.

| Endpoint | Auth | Auditor question |
|---|---|---|
| `GET /v1/health` | none | Is the portal up? (admits it does not predict) |
| `GET /v1/audit` | Bearer | What can I retrieve? |
| `GET /v1/audit/card` | Bearer | What is it for, and what must it never do? |
| `GET /v1/audit/bundle` | Bearer | What shipped, who attested, what is the checklist? |
| `GET /v1/audit/gate` | Bearer | Did policy × evidence pass *right now*? |
| `GET /v1/audit/metrics` | Bearer | Raw numbers (evidence only) |
| `GET /v1/audit/policy` | Bearer | What thresholds did Responsible AI set? |
| `GET /v1/audit/exhibits` | Bearer | Which gold items exist? |
| `GET /v1/audit/exhibits/{id}` | Bearer | Show one frozen NL→ODRL→Cedar/Rego pair |
| `GET /v1/audit/checks/{id}` | Bearer | One control, with evidence |
| `POST /v1/predict` | n/a | **404** — this is not a PEP |

Wrong or missing token → `401` / `403`.

---

## 6. Break the build (talk / Q&A)

Restore after each demo.

### Missing `limitations` (Day 056 beat)

```bash
export PYTHONPATH=.
cp model_card.yaml model_card_GOOD_backup.yaml
cp model_card_BROKEN_demo.yaml model_card.yaml
python3 generate_card.py          # exit 1
cp model_card_GOOD_backup.yaml model_card.yaml
rm model_card_GOOD_backup.yaml
python3 generate_card.py          # exit 0
```

### Over-permission above policy

```bash
export PYTHONPATH=.
python3 generate_card.py --dynamic fixtures/metrics_overpermit_fail.json
python3 generate_card.py          # restore happy path
```

Or edit `metrics.json` → `fidelity.over_permission_rate` to `0.09` (policy cap is `0.02` in `policies/governance.yaml`).

---

## Repository map

| Path | Who writes it | Role |
|---|---|---|
| `model_card.yaml` | Humans, via PR | Intended use, limitations, HITL, prohibited uses |
| `model_card_BROKEN_demo.yaml` | Demo | Same card minus `limitations` |
| `metrics.json` | Eval harness (outside this repo) | Measurements only |
| `policies/governance.yaml` | Responsible AI / MRM | Thresholds, required fields, automation cap |
| `compliance/bundle.yaml` | Release / MRM | Inventory + attestations |
| `exhibits/gold_set/` | Frozen examples | What a gold pair looks like |
| `fixtures/metrics_overpermit_fail.json` | Tests / talk | Over-permission failure |
| `generate_card.py` | CI | Merge + gate + write artifacts |
| `serve_audit.py` | Runtime | Auditor HTTP portal |
| `rai_card/` | This dummy | Schema, gate, renderer, API |
| `tests/` | This dummy | Gate + API contracts |
| `.github/workflows/rai-gate.yml` | CI | pytest, happy path, broken card must fail |
| `MODEL_CARD.md` / `COMPLIANCE_BUNDLE.md` | Generated | What an auditor files |
| `presentation_flow.md` | Speakers | Minute-by-minute talk |

---

## What the gate checks

- Completeness (Mitchell et al.: **limitations** are required)
- Pipeline named: NL → ODRL → Cedar → Rego
- No auto-attach to a PEP; fail closed on ambiguous NL
- Gold-set size, semantic match, constraint preservation
- Over-permission / under-permission bounds
- Cedar and Rego encodings must not widen ODRL
- Coverage of permission, prohibition, obligation, constraint
- Known-weak slices (e.g. ambiguous NL) cannot silently sit on the automated path
- Bundle attestations must match the card
- Verdict is **computed**; `metrics.json` must not be trusted if it says `PASS`

---

## Hybrid source (why three files, not one PDF)

| Source | File | Auditor question |
|---|---|---|
| Static context | `model_card.yaml` | What is it *for*? What must it never do? |
| Dynamic facts | `metrics.json` | How did *this* artifact score on the gold set? |
| Policy | `policies/governance.yaml` | What numbers are allowed to ship? |

If documentation cannot generate, the release does not exist. That is the Day 056 rule. The Day 060 rule: the auditor retrieves the same objects over HTTP, as JSON.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: rai_card` | `export PYTHONPATH=.` from repo root, venv active |
| `ERROR: rai_card is not importable` | Install `requirements.txt`, then `PYTHONPATH=.` |
| `ERROR: pyyaml not installed` | `pip install -r requirements.txt` |
| Card generate works, API 401 | Send `Authorization: Bearer auditor-demo-token` (or your `AUDIT_API_TOKEN`) |
| API looks at the wrong files | `export RAI_ROOT=/absolute/path/to/dummy-project` |
| Port in use | `AUDIT_API_PORT=8081 python3 serve_audit.py` |
| Recertification / review failures | Frozen “today” in tests is 2026-09-19; live runs use UTC today vs `recertification_due` |
| `odrl-host run` → `E_MODEL_API` (exit 5) | Live conversion needs a key: `export LLAMA_API_KEY=… LLAMA_BASE_URL=…/v1` (see LLM API key section). Offline paths need no key |
| `odrl-host optimize-prompts` refuses live probe | Same fix: it requires both `LLAMA_API_KEY` and `LLAMA_BASE_URL` |
