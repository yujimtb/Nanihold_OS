from __future__ import annotations

import os
from pathlib import Path


def pytest_configure(config):
    if config.option.basetemp is None:
        basetemp = Path(".pytest-tmp") / f"pid-{os.getpid()}"
        basetemp.parent.mkdir(exist_ok=True)
        config.option.basetemp = str(basetemp)
