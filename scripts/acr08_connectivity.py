#!/usr/bin/env python3
"""ACR-08 matrix/dry-run/evidence-verification entry point.

Examples:

    python scripts/acr08_connectivity.py matrix --output docs/acr08-e2e-matrix.json
    python scripts/acr08_connectivity.py checklist --output docs/acr08-owner-checklist.json
    python scripts/acr08_connectivity.py dry-run --output runs/acr08-dry-run.json
    python scripts/acr08_connectivity.py verify --results results.json --evidence evidence.json

The dry-run command only renders internal API request plans.  It has no
Discord/Slack adapter, no channel credentials, and performs no network write.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vsm.acr08 import main


if __name__ == "__main__":
    raise SystemExit(main())
