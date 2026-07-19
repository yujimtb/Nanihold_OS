from pathlib import Path

import pytest

from vsm.config import load_config
from vsm.errors import ConfigurationError


def test_example_benchmark_evidence_cannot_start_runtime():
    example = Path(__file__).parents[1] / "config" / "nanihold.example.toml"
    with pytest.raises(
        ConfigurationError,
        match="example benchmark evidence must be replaced",
    ):
        load_config(example)
