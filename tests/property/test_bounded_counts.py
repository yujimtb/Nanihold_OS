"""Property 11 (Bounded counts). Validates Requirements: 1.3, 1.4, 13.4, 13.5, 13.6."""
from __future__ import annotations
import pytest
from hypothesis import given, settings, strategies as st
from vsm.config import RunConfig
from vsm.errors import ConfigError
from vsm.roles import SystemRole, MANDATORY_ROLES


def _build_counts(s2=1, s3=1, s3star=1, s4=1, s5=1, s1=0):
    return {
        SystemRole.S2_COORDINATOR: s2,
        SystemRole.S3_ALLOCATOR: s3,
        SystemRole.S3STAR_AUDITOR: s3star,
        SystemRole.S4_SCANNER: s4,
        SystemRole.S5_POLICY: s5,
        SystemRole.S1_WORKER: s1,
    }


@given(n=st.integers(min_value=1, max_value=16))
@settings(max_examples=100)
def test_mandatory_count_in_range_accepted(n):
    """REQ 13.4: any count in [1, 16] is accepted for mandatory roles."""
    cfg = RunConfig(sub_agents_per_role=_build_counts(s2=n, s3=n, s3star=n, s4=n, s5=n))
    for role in MANDATORY_ROLES:
        assert cfg.count(role) == n


@given(n=st.integers(max_value=0))
@settings(max_examples=100)
def test_mandatory_count_zero_or_negative_rejected(n):
    """REQ 13.4: count <= 0 for mandatory roles is rejected."""
    with pytest.raises(ConfigError):
        RunConfig(sub_agents_per_role=_build_counts(s2=n))


@given(n=st.integers(min_value=17, max_value=200))
@settings(max_examples=100)
def test_mandatory_count_over_16_rejected(n):
    """REQ 13.4: count > 16 for mandatory roles is rejected."""
    with pytest.raises(ConfigError):
        RunConfig(sub_agents_per_role=_build_counts(s4=n))


@given(n=st.integers(min_value=0, max_value=64))
@settings(max_examples=100)
def test_s1_count_in_range_accepted(n):
    """REQ 13.5 + 1.4: S1 count in [0, 64] is accepted."""
    cfg = RunConfig(sub_agents_per_role=_build_counts(s1=n))
    assert cfg.count(SystemRole.S1_WORKER) == n


@given(n=st.integers(min_value=65, max_value=200))
@settings(max_examples=100)
def test_s1_count_over_64_rejected(n):
    """REQ 1.4: per-System S1 cap is 64."""
    with pytest.raises(ConfigError):
        RunConfig(sub_agents_per_role=_build_counts(s1=n))


@given(n=st.integers(min_value=0, max_value=1024))
@settings(max_examples=100)
def test_s1_max_in_range_accepted(n):
    """REQ 1.3: s1_max in [0, 1024] is accepted."""
    cfg = RunConfig(s1_max=n, s1_dynamic_max=min(n, 64))
    assert cfg.s1_max == n


@given(n=st.integers(min_value=1025, max_value=10000))
@settings(max_examples=100)
def test_s1_max_over_1024_rejected(n):
    """REQ 1.3: s1_max > 1024 rejected."""
    with pytest.raises(ConfigError):
        RunConfig(s1_max=n)


@given(n=st.integers(min_value=0, max_value=64))
@settings(max_examples=100)
def test_s1_dynamic_max_in_range_accepted(n):
    """REQ 13.6: s1_dynamic_max in [0, 64] is accepted."""
    cfg = RunConfig(s1_dynamic_max=n)
    assert cfg.s1_dynamic_max == n


@given(n=st.integers(min_value=65, max_value=10000))
@settings(max_examples=100)
def test_s1_dynamic_max_over_64_rejected(n):
    """REQ 13.6: s1_dynamic_max > 64 rejected."""
    with pytest.raises(ConfigError):
        RunConfig(s1_dynamic_max=n)
