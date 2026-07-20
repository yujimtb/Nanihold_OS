from vsm.pilot.models import ModelCandidate


def test_model_candidate_can_explicitly_disable_all_tools():
    candidate = ModelCandidate(
        adapter="claude-code",
        adapter_version="2.1.215",
        provider="anthropic",
        selection="provider_configured",
        model_snapshot=None,
        effort="high",
        toolset=(),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )

    assert candidate.toolset == ()
