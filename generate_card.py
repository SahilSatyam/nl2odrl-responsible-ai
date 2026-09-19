#!/usr/bin/env python3
"""Stage entrypoint. Emits MODEL_CARD.md + COMPLIANCE_BUNDLE.md or fails CI.

Does not convert NL to ODRL/Cedar/Rego.
"""

from __future__ import annotations

import sys

try:
    from rai_card.cli import main
except ImportError:
    sys.stderr.write(
        "ERROR: rai_card is not importable. From the repo root run:\n"
        "  python3 -m pip install -r requirements.txt\n"
        "  PYTHONPATH=. python3 generate_card.py\n"
    )
    raise SystemExit(2) from None

if __name__ == "__main__":
    raise SystemExit(main())
