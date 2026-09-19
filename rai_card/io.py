from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from rai_card.models import (
    ComplianceBundleSpec,
    DynamicMetrics,
    StaticCard,
    parse_bundle,
    parse_dynamic,
    parse_static,
)


def load_yaml(path: str | Path) -> dict[str, Any]:
    loaded = yaml.safe_load(Path(path).read_text())
    return loaded if isinstance(loaded, dict) else {}


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def load_static_card(path: str | Path) -> tuple[dict[str, Any], StaticCard | None, str | None]:
    raw = load_yaml(path)
    try:
        return raw, parse_static(raw), None
    except ValidationError as exc:
        return raw, None, str(exc)


def load_dynamic_metrics(
    path: str | Path,
) -> tuple[dict[str, Any], DynamicMetrics | None, str | None]:
    raw = load_json(path)
    try:
        return raw, parse_dynamic(raw), None
    except ValidationError as exc:
        return raw, None, str(exc)


def load_bundle_spec(
    path: str | Path,
) -> tuple[dict[str, Any], ComplianceBundleSpec | None, str | None]:
    raw = load_yaml(path)
    try:
        return raw, parse_bundle(raw), None
    except ValidationError as exc:
        return raw, None, str(exc)
