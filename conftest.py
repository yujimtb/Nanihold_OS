from __future__ import annotations

import os


def pytest_configure(config):
    if config.option.basetemp is None:
        config.option.basetemp = f".pytest-tmp/pid-{os.getpid()}"
