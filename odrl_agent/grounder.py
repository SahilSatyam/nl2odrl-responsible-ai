"""Entity grounding and the least-privilege gate (Phase 2.1).

Replaces the ``"attacker-client"`` substring check with three deterministic
checks that hold for any principal name:

(a) **grounded** - every ``assignee``/``assigner``/``target`` of a rule maps to
    an :class:`EntityCatalog` entry, or (with no catalog) is mentioned in the
    source text, and every rule cites a source span;
(b) **least privilege** - no wildcard, public or unknown-external grant;
(c) **authorship** - a permission whose only support is an untrusted or
    injected span (imperative "ignore previous instructions", role overrides,
    JSON/markup breakout) cannot create a grant.

Each failure is a :class:`GroundingIssue` with a stable ``code``. Any issue
blocks auto-publication; none of them is a warning. The grounder never calls a
model and never widens a policy: it only resolves names to catalog IRIs.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from odrl_agent.ir import ConstraintIR, LogicalConstraintIR, PolicyIR, RuleIR

UNGROUNDED_PRINCIPAL = "UNGROUNDED_PRINCIPAL"
UNGROUNDED_TARGET = "UNGROUNDED_TARGET"
UNGROUNDED_RULE = "UNGROUNDED_RULE"
BROAD_GRANT = "BROAD_GRANT"
EXTERNAL_PRINCIPAL = "EXTERNAL_PRINCIPAL"
INJECTED_GRANT = "INJECTED_GRANT"
SUSPICIOUS_NAME = "SUSPICIOUS_NAME"

ISSUE_CODES = (
    UNGROUNDED_PRINCIPAL,
    UNGROUNDED_TARGET,
    UNGROUNDED_RULE,
    BROAD_GRANT,
    EXTERNAL_PRINCIPAL,
    INJECTED_GRANT,
    SUSPICIOUS_NAME,
)

#: Names that grant to everybody. Compared after normalization.
WILDCARD_NAMES = frozenset(
    {
        "*", "all", "any", "anyone", "anybody", "everyone", "everybody", "public",
        "the public", "general public", "world", "all users", "any user", "anonymous",
        "guest", "guests", "all parties", "any party", "third parties", "third party",
        "external", "externals", "internet", "unknown",
    }
)  # fmt: skip

#: Phrases that mark text written to steer the model rather than state policy.
_INJECTION_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bignore\b.{0,40}\b(instructions?|rules?|prompts?|above|previous|prior)\b",
        r"\bdisregard\b.{0,40}\b(instructions?|rules?|above|previous|prior)\b",
        r"\bforget\b.{0,30}\b(instructions?|rules?|everything)\b",
        r"\b(new|updated|real|actual)\s+(system\s+)?instructions?\b",
        r"\byou\s+are\s+now\b",
        r"\bact\s+as\b",
        r"\b(override|bypass)\b.{0,30}\b(polic(y|ies)|rules?|restrictions?|safety)\b",
        r"^\s*(system|assistant|developer|user)\s*:",
        r"<\s*/?\s*(system|assistant|im_start|im_end|instructions?)\b",
        r"\[\s*(system|inst)\s*\]",
        r"```",
        r"[{\[]\s*\"(permission|assignee|action|target|rules?|modality)\"\s*:",
        r"\b(admin|root|superuser)\s+(mode|access|override)\b",
        r"\b(instructions?|directive|command|note\s+to\s+(the\s+)?(ai|assistant|model))\s*:",
        r"\b(obey|follow|execute|comply\s+with)\b.{0,25}\b(instructions?|directive|command)\b",
        r"\b(skip|bypass|without|no)\b.{0,15}\b(review|approval|validation|verification)\b",
    )
)

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?|\n")


def normalize_name(text: str) -> str:
    """NFKC-fold, casefold and collapse whitespace/punctuation separators."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\s_\-]+", " ", folded).strip()


def _suspicious(name: str) -> str | None:
    """Explain why ``name`` looks like a homoglyph/spoof, else ``None``."""
    if any(unicodedata.category(ch) in ("Cf", "Cc") for ch in name):
        return "contains invisible or control characters"
    if unicodedata.normalize("NFKC", name) != name:
        return "changes under Unicode NFKC normalization"
    scripts = set()
    for ch in name:
        if ch.isalpha():
            try:
                scripts.add(unicodedata.name(ch).split(" ")[0])
            except ValueError:
                return "contains an unnamed character"
    if len(scripts) > 1:
        return f"mixes scripts {sorted(scripts)}"
    return None


@dataclass(frozen=True)
class CatalogEntry:
    """One known party or asset."""

    iri: str
    kind: str  # "party" | "asset"
    names: tuple[str, ...] = ()
    external: bool = False


class EntityCatalog:
    """Pluggable lookup of known parties and assets.

    Subclass and override :meth:`lookup` to back this by a directory or data
    catalogue; the in-memory form is enough for tests and small deployments.
    """

    def __init__(self, entries: Iterable[CatalogEntry] = ()) -> None:
        self._by_name: dict[tuple[str, str], CatalogEntry] = {}
        for entry in entries:
            self.add(entry)

    def add(self, entry: CatalogEntry) -> None:
        for name in (entry.iri, *entry.names):
            self._by_name[(entry.kind, normalize_name(name))] = entry

    def lookup(self, name: str, kind: str) -> CatalogEntry | None:
        return self._by_name.get((kind, normalize_name(name)))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityCatalog:
        """Build from ``{"parties": [...], "assets": [...]}``.

        Each entry is ``{"iri": ..., "names": [...], "external": bool}``.
        """
        entries: list[CatalogEntry] = []
        for kind, key in (("party", "parties"), ("asset", "assets")):
            for raw in data.get(key, []) or []:
                iri = str(raw["iri"])
                names = tuple(str(n) for n in raw.get("names", []) or [])
                entries.append(CatalogEntry(iri, kind, names, bool(raw.get("external", False))))
        return cls(entries)

    @classmethod
    def from_file(cls, path: str | Path) -> EntityCatalog:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass
class GroundingIssue:
    code: str
    detail: str
    rule_index: int | None = None
    field: str | None = None
    value: str | None = None


@dataclass
class GroundingReport:
    issues: list[GroundingIssue] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)
    untrusted_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def grounded(self) -> bool:
        """True when no issue blocks auto-publication."""
        return not self.issues

    def codes(self) -> list[str]:
        return sorted({issue.code for issue in self.issues})

    def as_dict(self) -> dict[str, Any]:
        return {
            "grounded": self.grounded,
            "issues": [asdict(issue) for issue in self.issues],
            "resolved": dict(self.resolved),
            "untrusted_ranges": [list(r) for r in self.untrusted_ranges],
        }

    def messages(self) -> list[str]:
        out = []
        for issue in self.issues:
            where = f"rules.{issue.rule_index}." if issue.rule_index is not None else ""
            out.append(f"grounding: {issue.code} {where}{issue.field or ''}: {issue.detail}")
        return out


TRUSTED_PARTIES_ENV = "NL2ODRL_TRUSTED_PARTIES"


def trusted_parties_from_env() -> list[str]:
    """Parties the deployment vouches for (comma-separated env value).

    These come from deployment context (e.g. the data steward that issues
    offers), never from the text itself, so they are grounded without a
    mention. With no catalog, every other party must be named in the source.
    """
    return [p.strip() for p in os.environ.get(TRUSTED_PARTIES_ENV, "").split(",") if p.strip()]


def injected_ranges(source: str) -> list[tuple[int, int]]:
    """Character ranges of sentences that read as model-steering injections."""
    ranges: list[tuple[int, int]] = []
    for match in _SENTENCE.finditer(source):
        sentence = match.group(0)
        if sentence.strip() and any(p.search(sentence) for p in _INJECTION_PATTERNS):
            ranges.append((match.start(), match.end()))
    return ranges


def _overlaps(span: tuple[int, int], ranges: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < r_end and r_start < end for r_start, r_end in ranges)


def _mentions(name: str, source: str) -> list[tuple[int, int]]:
    """Where ``name`` occurs in ``source`` (case/whitespace-insensitive)."""
    needle = normalize_name(name)
    if not needle:
        return []
    pattern = r"[\s_\-]+".join(re.escape(part) for part in needle.split(" "))
    folded = unicodedata.normalize("NFKC", source)
    if len(folded) != len(source):
        folded = source  # offsets must stay aligned with the original text
    return [(m.start(), m.end()) for m in re.finditer(pattern, folded, re.IGNORECASE)]


_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")


def _aliases(name: str) -> list[str]:
    """``name`` plus, for an absolute IRI, its last path/URN segment.

    The extractor may already mint ``urn:data:dataset-7`` for text that says
    "dataset-7"; the mention check then looks for the local name.
    """
    out = [name]
    if _IRI.match(name):
        tail = re.split(r"[:/#]", name.rstrip("/#"))[-1]
        if tail and tail != name:
            out.append(tail)
    return out


def _constraint_values(nodes: list) -> Iterable[ConstraintIR]:
    for node in nodes:
        if isinstance(node, LogicalConstraintIR):
            yield from _constraint_values(node.constraints)
        else:
            yield node


class Grounder:
    """Resolve IR names and apply the least-privilege gate."""

    def __init__(
        self,
        catalog: EntityCatalog | None = None,
        *,
        allow_external: bool = False,
        trusted_parties: Iterable[str] | None = None,
    ) -> None:
        self.catalog = catalog
        self.allow_external = allow_external
        if trusted_parties is None:
            trusted_parties = trusted_parties_from_env()
        self.trusted = {normalize_name(name) for name in trusted_parties if name.strip()}

    def _resolve(
        self,
        name: str,
        kind: str,
        source: str,
        injected: list[tuple[int, int]],
        report: GroundingReport,
        index: int | None,
        field_name: str,
        is_grant: bool,
    ) -> str:
        code = UNGROUNDED_PRINCIPAL if kind == "party" else UNGROUNDED_TARGET

        def issue(code_: str, detail: str) -> None:
            report.issues.append(GroundingIssue(code_, detail, index, field_name, name))

        if kind == "party" and is_grant and normalize_name(name) in WILDCARD_NAMES:
            issue(BROAD_GRANT, f"grant to wildcard/public principal {name!r}")
            return name
        why = _suspicious(name)
        if why:
            issue(SUSPICIOUS_NAME, f"{name!r} {why}")
        if self.catalog is not None:
            entry = self.catalog.lookup(name, kind)
            if entry is None:
                issue(code, f"{name!r} is not in the entity catalog")
                return name
            if entry.external and is_grant and not self.allow_external:
                issue(EXTERNAL_PRINCIPAL, f"grant to external principal {entry.iri}")
            report.resolved[name] = entry.iri
            return entry.iri
        if kind == "party" and normalize_name(name) in self.trusted:
            return name
        mentions = [m for alias in _aliases(name) for m in _mentions(alias, source)]
        if not mentions:
            issue(code, f"{name!r} does not appear in the source text")
        elif is_grant and kind == "party" and all(_overlaps(m, injected) for m in mentions):
            issue(INJECTED_GRANT, f"{name!r} is named only inside injected text")
        return name

    def ground(
        self,
        ir: PolicyIR,
        source: str,
        *,
        untrusted_ranges: list[tuple[int, int]] | None = None,
    ) -> tuple[PolicyIR, GroundingReport]:
        """Return the IR with catalog IRIs substituted, and the gate's report.

        ``untrusted_ranges`` are extra source ranges the caller knows were not
        written by the policy author (e.g. quoted third-party content); they
        are treated like detected injections.
        """
        injected = injected_ranges(source) + list(untrusted_ranges or [])
        report = GroundingReport(untrusted_ranges=injected)
        out = ir.model_copy(deep=True)

        def party(name: str | None, index: int | None, fname: str, grant: bool) -> str | None:
            if name is None:
                return None
            return self._resolve(name, "party", source, injected, report, index, fname, grant)

        out.assigner = party(out.assigner, None, "assigner", False)
        policy_grants = any(r.modality == "permission" for r in out.rules)
        out.assignee = party(out.assignee, None, "assignee", policy_grants)
        for index, rule in enumerate(out.rules):
            self._ground_rule(rule, index, source, injected, report, party)
        return out, report

    def _ground_rule(
        self,
        rule: RuleIR,
        index: int,
        source: str,
        injected: list[tuple[int, int]],
        report: GroundingReport,
        party: Any,
    ) -> None:
        grant = rule.modality == "permission"
        spans = [(s.start, s.end) for s in rule.source_spans if s.start is not None]
        if not spans:
            report.issues.append(
                GroundingIssue(UNGROUNDED_RULE, "rule cites no source span", index, "source_spans")
            )
        elif grant and all(_overlaps(span, injected) for span in spans):
            report.issues.append(
                GroundingIssue(
                    INJECTED_GRANT,
                    "permission is supported only by injected/untrusted text",
                    index,
                    "source_spans",
                )
            )
        rule.assignee = party(rule.assignee, index, "assignee", grant)
        rule.assigner = party(rule.assigner, index, "assigner", False)
        if rule.target is not None:
            rule.target = self._resolve(
                rule.target, "asset", source, injected, report, index, "target", grant
            )
        elif grant:
            report.issues.append(
                GroundingIssue(BROAD_GRANT, "permission has no target (applies to anything)",
                               index, "target")
            )  # fmt: skip
        for duty in rule.duties:
            duty.assignee = party(duty.assignee, index, "duties.assignee", False)
        for constraint in _constraint_values(rule.constraints):
            if constraint.left_operand == "recipient" and grant:
                name = constraint.right_operand
                if normalize_name(name) in WILDCARD_NAMES:
                    report.issues.append(
                        GroundingIssue(BROAD_GRANT, f"recipient constraint {name!r} is a wildcard",
                                       index, "constraints", name)
                    )  # fmt: skip


def ground_entities(
    ir: PolicyIR,
    source: str,
    catalog: EntityCatalog | None = None,
    **kw: Any,
) -> tuple[PolicyIR, GroundingReport]:
    """Convenience wrapper around :meth:`Grounder.ground`."""
    return Grounder(catalog, allow_external=kw.pop("allow_external", False)).ground(
        ir, source, **kw
    )
