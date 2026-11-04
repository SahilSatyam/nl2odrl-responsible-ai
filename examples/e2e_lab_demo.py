"""End-to-end Lab demo: fault run -> control plane -> verdict -> Lab URL.

Usage: python examples/e2e_lab_demo.py
Then open the printed URL in a browser (standalone artifact; the user
checks it there — this script does not open anything itself).
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "odrl_host"))

import app as host

from agentcrash import (
    AgentCrash,
    RequiredStep,
    WorkflowConfig,
    evaluate_trace,
)
from agentcrash.server import register_project, serve


def post(base: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def main() -> int:
    register_project("odrl-policy-engine", "development")
    ac = AgentCrash(WorkflowConfig(project="odrl-policy-engine", fault_injection=True))
    ac.configure_faults({"generate_odrl": {"kind": "malformed_output", "occurrence": 1}})
    result = host.convert_policy("Allow analysts to read dataset-1", ac=ac)
    report = evaluate_trace(
        list(ac.traces[result["run_id"]]), [RequiredStep("validate-odrl")], run_id=result["run_id"]
    )
    server, port = serve(port=0)
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    post(base, "/v1/projects", {"project": "odrl-policy-engine"})
    post(base, "/v1/events", {"events": list(ac.traces[result["run_id"]])})
    verdict = post(
        base, "/v1/evaluations", {"run_id": result["run_id"], "results": report["results"]}
    )
    print(f"AgentCrash Lab: {base}/  (project odrl-policy-engine)")
    print(f"run {result['run_id']}: faults={result['faults']} " f"verdict={verdict['verdict']}")
    print("Serving until Ctrl-C ...")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
