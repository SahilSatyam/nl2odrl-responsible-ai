# NL → ODRL agentic converter

Reads natural-language access rules, drafts **W3C ODRL 2.2**, and
evaluates the conversion against frozen goldens before anything
attaches to a policy enforcement point. Cedar/Rego compilation and
live authorization are out of scope.

Built on [`agentcrash-core`](https://pypi.org/project/agentcrash-core/)
(reliability framework: SDK instrumentation, fault injection,
deterministic verification, control plane).

The ethical red line is **over-permission**: the conversion grants a
right the source sentence did not. Fluency is not a control.

## Requirements

- Python **3.11+** (`python3 --version`)
- `pip`

## Install

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .
```

Every command below assumes the venv is active and:

```bash
export PYTHONPATH=.
```

## LLM API key (only needed for live model calls)

Seeded fixtures, checks, diagnosis, the Studio, and the full test
suite work **with no key**. Only live model calls need one:

| Needs `LLAMA_API_KEY` + `LLAMA_BASE_URL` | Works offline, no key |
|---|---|
| `odrl-host run` (live ReAct conversion) | `odrl-host seed` (fixture-backed runs into `agentcrash.db`) |
| `odrl-host optimize-prompts` (live DSPy probe; refuses without a key) | `odrl-host check` / `diagnose` (fixture reference patch; `diagnose` only tries the live patch path when `LLAMA_BASE_URL` is set) |
| Live metrics (`scripts/metrics.py`) | `agentcrash check --plugin odrl_agent.verifiers`, PII scan, full pytest suite |

Any OpenAI-compatible `{LLAMA_BASE_URL}/chat/completions` endpoint
works (default model is `muse-spark-1.3`):

```bash
export LLAMA_API_KEY=<your-key>      # header only, never logged or traced
export LLAMA_BASE_URL=<your-endpoint>/v1
# optional tuning (defaults shown):
# export LLAMA_TIMEOUT_S=60
# export LLAMA_MAX_TOKENS=8000
```

Without a key, `odrl-host run` fails closed with `E_MODEL_API`
(exit 5) naming the missing variables — intentional, not an install
problem.

## Use it

```bash
export PYTHONPATH=.
odrl-host seed --help
odrl-host run --scenario odrl_convert_timeout --seed 42   # needs key
odrl-host check --trace traces/<run>.jsonl --invariant odrl_valid
odrl-host diagnose --trace traces/<run>.jsonl             # fixture patch
odrl-host serve                                           # ODRL/Policy Studio
agentcrash check --trace <run>.jsonl --plugin odrl_agent.verifiers \
  --invariant odrl_valid --invariant no_attacker_grant \
  --invariant exactly_once_save --scan-pii
```

## Test

```bash
export PYTHONPATH=.
python3 -m pytest
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: odrl_agent` | `export PYTHONPATH=.` from repo root, venv active |
| `odrl-host run` → `E_MODEL_API` (exit 5) | Live conversion needs a key: `export LLAMA_API_KEY=… LLAMA_BASE_URL=…/v1` (see above); offline paths need no key |
| `odrl-host optimize-prompts` refuses live probe | Same fix: it requires both `LLAMA_API_KEY` and `LLAMA_BASE_URL` |
| `odrl-host serve` → `ui/index.html not found` | Run from the repo root |

## Repository map

| Path | Role |
|---|---|
| `odrl_agent/` | Converter: NL parsing, ODRL 2.2 generation, scenario runner, ODRL invariants, `odrl-host` CLI |
| `scenarios/` | `odrl_convert*` run scenarios (naive vs fixed policies, faults) |
| `policies/` | Default + fixed converter policies |
| `schemas/` | Trace, scenario, env-state, ODRL-subset SHACL shapes |
| `prompts/` | DSPy probe instruction pack |
| `scripts/metrics.py` | Live-metrics harness (needs key) |
| `tests/` + `tests/fixtures/` | Converter tests + frozen golden traces |
| `examples/` | Second-app sample (`odrl_host`), end-to-end Lab demo |
| `ui/` | ODRL/Policy Studio assets served by `odrl-host serve` |
| `.github/workflows/ci.yml` | Converter tests + PII guardrail on 3.11–3.13 |
