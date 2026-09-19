# Compliance Bundle: Natural Language to ODRL Policy Assistant

> Auto-generated on 2026-09-19 11:39 UTC. This is the **release envelope**, not the converter.
> Bundle spec: `nl2odrl-compliance-v1` · policy `nl2odrl-rai-v1`.

**Gate: `PASS`**

## Purpose

Prove that a Natural Language → ODRL → Cedar / OPA-Rego **assistant** is documented, measured, and human-gated before anyone treats its output as enforceable policy.

This repository does **not** ship a compiler. The bundle records what must be true of whatever compiler you plug in later.

## Inventory (required for a release)

- `model_card.yaml`
- `metrics.json`
- `policies/governance.yaml`
- `MODEL_CARD.md`
- `COMPLIANCE_BUNDLE.md`
- `rai-gate.json`
- `exhibits/gold_set/`

## Frozen exhibits (illustrative gold pairs, not generated here)

- `exhibits/gold_set/README.md`
- `exhibits/gold_set/01.nl.txt`
- `exhibits/gold_set/01.odrl.jsonld`
- `exhibits/gold_set/01.cedar`
- `exhibits/gold_set/01.rego`

These files exist so a reviewer can see *what* evaluation means. They are not produced by this CI step.

## Attestations

| Claim | Value |
|---|---|
| Not legal advice | `true` |
| No auto-attach to a PEP | `true` |
| Fail closed on ambiguous NL | `true` |
| Gold set frozen (hashed) | `true` |
| Gold set hash | `sha256:9f3c1a7e2b44d8c0e15a6b7d4c3e2f1908a7b6c5d4e3f2019283a4b5c6d7e8f9` |
| System ID | `nl2odrl-v1` |
| Owner | Policy Language Team — Responsible AI |

## Human sign-off checklist (do not automate)

- [ ] Policy author reviewed ODRL against the source NL (permissions, prohibitions, obligations, constraints).
- [ ] Cedar and Rego encodings do not widen ODRL permissions (spot-check over-permit cases).
- [ ] Ambiguous sentences were rejected or sent to fail-closed, not guessed into an allow.
- [ ] No PEP / OPA / Cedar store was updated without a named reviewer on `policy-authors@example.org`.
- [ ] Model card recertification date `2027-03-01` is still in the future.

## Residual issues from this gate

- None from this gate (residual product risk still exists; see the model card).

## How to use this dummy in a talk

1. Show `model_card.yaml` as the human-owned narrative (intended use, limitations, HITL).
2. Show `metrics.json` as eval-harness numbers — especially **over-permission**.
3. Run `python3 generate_card.py` and point at this bundle + `MODEL_CARD.md`.
4. Comment out `limitations` (or swap `model_card_BROKEN_demo.yaml`) and show the non-zero exit.
