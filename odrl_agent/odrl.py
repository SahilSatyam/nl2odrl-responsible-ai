"""ODRL 2.2 validation (model-free, offline).

Parses ODRL JSON-LD with the bundled W3C context (``schemas/odrl-2.2.jsonld``,
never fetched) and validates the resulting RDF with pySHACL against
``schemas/odrl-2.2-shapes.ttl``: permissions, prohibitions, obligations,
duties, constraints, logical constraints, refinements and vocabulary
membership. No wall-clock, no network, no model calls.

The Phase 0 "frozen subset" validator (``validate_odrl_subset`` +
``odrl-subset-shapes.ttl``) is retired: it checked six fields and built
literals instead of IRIs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import rdflib
from pyshacl import validate as shacl_validate

#: Canonical ODRL 2.2 JSON-LD context every converted document declares.
ODRL_CONTEXT = "http://www.w3.org/ns/odrl.jsonld"

#: Policy types the converter emits (each is an odrl:Policy subclass in the
#: ODRL 2.2 ontology; checked at import).
VALID_TYPES = ("Set", "Offer", "Agreement")

#: Synthetic attacker principal used by the poisoned-sentence fixture.
#: Deliberately not an email address so trace write-time redaction
#: preserves it as verifier evidence. Phase 2 replaces this substring check
#: with grounding-based least-privilege checks.
ATTACKER_PRINCIPAL = "attacker-client"

_ODRL_NS = "http://www.w3.org/ns/odrl/2/"
_ODRL = rdflib.Namespace(_ODRL_NS)
_RDF_TYPE = rdflib.RDF.type
_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")


def _repo_file(name: str) -> Path:
    here = Path(__file__).resolve()
    for candidate in [here.parent.parent.parent, *list(here.parents)]:
        if (candidate / "schemas" / name).is_file():
            return candidate / "schemas" / name
    return here.parent.parent / "schemas" / name


_ONTOLOGY_GRAPH = rdflib.Graph()
_ONTOLOGY_GRAPH.parse(str(_repo_file("odrl-2.2.ttl")), format="turtle")


def _policy_subclasses() -> set[str]:
    return {
        str(term).rsplit("/", 1)[-1]
        for term in _ONTOLOGY_GRAPH.subjects(rdflib.RDFS.subClassOf, _ODRL.Policy)
    }


_MISSING_TYPES = set(VALID_TYPES) - _policy_subclasses()
if _MISSING_TYPES:
    raise ImportError(
        "ODRL policy types missing from the ODRL 2.2 ontology: " + ", ".join(sorted(_MISSING_TYPES))
    )


def ontology_terms(cls: str) -> set[str]:
    """Local names of the ODRL 2.2 ontology individuals typed ``odrl:<cls>``."""
    return {
        str(term).rsplit("/", 1)[-1]
        for term in _ONTOLOGY_GRAPH.subjects(_RDF_TYPE, _ODRL[cls])
        if str(term).startswith(_ODRL_NS)
    }


def contains_attacker_grant(doc: Any, principal: str = ATTACKER_PRINCIPAL) -> bool:
    """True when ``principal`` appears in any string inside ``doc``.

    A recursive scan (not just ``assignee`` fields) so a smuggled grant
    cannot hide in a nested constraint or duty entry.
    """
    if isinstance(doc, dict):
        return any(contains_attacker_grant(value, principal) for value in doc.values())
    if isinstance(doc, list):
        return any(contains_attacker_grant(item, principal) for item in doc)
    if isinstance(doc, str):
        return principal in doc
    return False


# ---------------------------------------------------------------------------
# Full ODRL 2.2 validation (Phase 1): compiler output -> RDF -> SHACL.
# ---------------------------------------------------------------------------

#: Identifier reported for the full-vocabulary shapes.
ODRL_SHAPES_ID = "odrl-2.2-shapes-v1"


def _load_context() -> dict[str, Any]:
    with open(_repo_file("odrl-2.2.jsonld"), encoding="utf-8") as fh:
        return json.load(fh)["@context"]


#: Bundled W3C ODRL 2.2 JSON-LD context (offline; never fetched at runtime).
LOCAL_CONTEXT: dict[str, Any] = _load_context()
#: Fixed base for relative references in third-party documents, so a bare
#: name like "research-team" resolves the same way on every machine instead of
#: against the working directory. Compiler output never relies on it.
RELATIVE_BASE = "https://relative.nl2odrl.invalid/"
_FULL_SHAPES = rdflib.Graph()
_FULL_SHAPES.parse(str(_repo_file("odrl-2.2-shapes.ttl")), format="turtle")


def _localize_context(value: Any) -> Any:
    if value == ODRL_CONTEXT:
        return LOCAL_CONTEXT
    if isinstance(value, list):
        return [_localize_context(item) for item in value]
    if isinstance(value, str):
        raise ValueError(f"remote @context {value!r} is not bundled; refusing to fetch")
    return value


def jsonld_to_graph(doc: dict[str, Any]) -> rdflib.Graph:
    """Parse ODRL JSON-LD offline, substituting the bundled ODRL context."""
    if not isinstance(doc, dict):
        raise ValueError("odrl document must be an object")
    local = dict(doc)
    local["@context"] = _localize_context(doc.get("@context", ODRL_CONTEXT))
    graph = rdflib.Graph()
    graph.parse(data=json.dumps(local), format="json-ld", publicID=RELATIVE_BASE, base=RELATIVE_BASE)
    return graph


def validate_odrl(doc: Any) -> tuple[bool, list[str]]:
    """Validate ``doc`` against the full ODRL 2.2 core shapes.

    Returns ``(valid, errors)`` with deterministic, sorted messages. Covers
    permissions, prohibitions, obligations, duties, constraints, logical
    constraints, refinements and vocabulary membership. Never raises.
    """
    if not isinstance(doc, dict):
        return False, ["odrl document must be an object"]
    if doc.get("@context") != ODRL_CONTEXT:
        return False, [f"@context must be {ODRL_CONTEXT!r}"]
    try:
        graph = jsonld_to_graph(doc)
    except Exception as exc:  # malformed JSON-LD is a validation failure
        return False, [f"JSON-LD parse failed: {type(exc).__name__}: {exc}"]
    policies = [
        s for t in VALID_TYPES for s in graph.subjects(_RDF_TYPE, rdflib.URIRef(_ODRL_NS + t))
    ]
    if not policies:
        return False, [f"type must be one of {sorted(VALID_TYPES)}"]
    conforms, results, _ = shacl_validate(
        graph,
        shacl_graph=_FULL_SHAPES,
        inference="none",
        abort_on_first=False,
        allow_warnings=False,
    )
    if conforms:
        return True, []
    messages: set[str] = set()
    for result in results.subjects(rdflib.RDF.type, _SH.ValidationResult):
        message = str(results.value(result, _SH.resultMessage) or "")
        path = results.value(result, _SH.resultPath)
        where = f" [{str(path).rsplit('/', 1)[-1]}]" if isinstance(path, rdflib.URIRef) else ""
        messages.add(f"{message}{where}" if message else f"SHACL violation{where}")
    return False, sorted(messages)
