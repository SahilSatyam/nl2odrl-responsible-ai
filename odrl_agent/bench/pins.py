"""Pins for everything a benchmark number depends on (Baseline v1 identity).

``benchmarks/v1/pins.json`` records the default model id, the extractor prompt
pack hash and the IR schema hash. A prompt or model change moves a hash, an
offline test fails until the pins are rewritten, and the rewrite is a visible
diff that the live gate workflow (path-filtered on the same files) must clear.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from odrl_agent import extractor
from odrl_agent.ir import IR_VERSION, ir_json_schema, wire_schema

PINS_PATH = Path(__file__).resolve().parents[2] / "benchmarks" / "v1" / "pins.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def current() -> dict[str, Any]:
    pack = extractor.SYSTEM_PROMPT + "\n--\n" + extractor.DECOMPOSE_PROMPT
    schema = json.dumps(ir_json_schema(), sort_keys=True)
    return {
        "model": extractor.DEFAULT_MODEL,
        "extractor_prompt_pack_sha": _sha(pack),
        "ir_schema_sha": _sha(schema),
        "wire_schema_sha": _sha(json.dumps(wire_schema(), sort_keys=True)),
        "ir_version": IR_VERSION,
    }


def load(path: Path = PINS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text("utf-8"))


def write(path: Path = PINS_PATH) -> dict[str, Any]:
    pins = current()
    path.write_text(json.dumps(pins, indent=1) + "\n", "utf-8")
    return pins


def drift(path: Path = PINS_PATH) -> dict[str, tuple[Any, Any]]:
    """``{key: (pinned, current)}`` for every pin that moved."""
    now, pinned = current(), load(path)
    return {k: (pinned.get(k), v) for k, v in now.items() if pinned.get(k) != v}
