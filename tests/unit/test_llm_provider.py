"""Unit tests for LLM_Provider_Abstraction. Validates Requirements: 3.4, 3.5, 3.6.

These tests pin the behavioural contract of :class:`vsm.llm.fake.FakeLLMProvider`
and the helper constructors :func:`make_timeout_provider` /
:func:`make_error_provider` (Task 9.2) at the level the rest of the codebase
relies on:

* **REQ 3.4 (60-second timeout)** — the FakeLLMProvider does not enforce a
  timeout itself; instead the *caller* wraps ``invoke`` in
  ``asyncio.wait_for`` with the 60 s deadline. We reproduce the cancellation
  contract by constructing a provider whose ``latency`` is greater than the
  ``wait_for`` deadline; the test uses a small (0.1 s) deadline against a
  2 s latency so the suite stays fast while still exercising the same
  ``asyncio.TimeoutError`` path that the production 60 s deadline triggers.
* **REQ 3.5 (typed error within 1 s of cancellation)** — covered upstream in
  ``test_subagent.py`` where ``SubAgent.respond`` converts the
  ``asyncio.TimeoutError`` into a typed :class:`LLMTimeoutError`. Here we
  only assert the *raw* asyncio cancellation surface that ``SubAgent.respond``
  layers on top of.
* **REQ 3.6 (typed provider error)** — when a caller injects a
  :class:`LLMProviderError`, ``invoke`` MUST raise that exact instance with
  ``code`` / ``message`` preserved, so ``SubAgent.respond`` can transcribe
  them onto the ``llm_error`` Event_Log payload.

The tests intentionally avoid touching ``litellm`` so they run deterministically
in offline environments and do not depend on the live provider stack.
"""

from __future__ import annotations

import asyncio

import pytest

from vsm.errors import LLMProviderError
from vsm.llm.fake import (
    FakeLLMProvider,
    make_error_provider,
    make_timeout_provider,
)
from vsm.llm.types import LLMResponse


# ---------------------------------------------------------------------------
# Happy path: response shape (REQ 3.3 surface used by 3.4/3.5/3.6 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_provider_returns_llm_response_with_configured_text() -> None:
    """A fixed-string ``response`` is wrapped verbatim in :class:`LLMResponse`.

    ``latency=0.05`` is small enough to keep the test fast while still
    exercising the ``asyncio.sleep`` branch of ``FakeLLMProvider.invoke``.
    """
    provider = FakeLLMProvider(response="hi", latency=0.05)

    resp = await provider.invoke("prompt")

    assert isinstance(resp, LLMResponse)
    assert resp.text == "hi"
    # latency_ms mirrors the configured latency (50 ms here) so SLA tests
    # downstream can assert against the Event_Log payload deterministically.
    assert resp.latency_ms == 50
    # Default model name from the dataclass; not under test here, but pinned
    # so accidental changes to the default surface immediately.
    assert resp.model == "fake/test-model"


@pytest.mark.asyncio
async def test_fake_provider_supports_callable_response() -> None:
    """A callable ``response`` receives ``(prompt, model)`` and is stringified.

    This branch is what enables pattern-style fakes in the integration tests
    (e.g. ``response=lambda p, _m: f"echo:{p}"``).
    """
    provider = FakeLLMProvider(response=lambda prompt, _model: f"echo:{prompt}")

    resp = await provider.invoke("hello")

    assert resp.text == "echo:hello"


# ---------------------------------------------------------------------------
# Invocation history (used by every Sub_Agent / SLA test that introspects)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_provider_records_each_prompt_in_invocations() -> None:
    """``invocations`` captures prompt + resolved model in arrival order.

    Tests that introspect Sub_Agent → LLM wiring rely on this list to assert
    the exact prompt text reached the provider. The list MUST be append-only
    in call order (FIFO).
    """
    provider = FakeLLMProvider(response="x", latency=0.0)

    await provider.invoke("first")
    await provider.invoke("second", model="custom/model")

    assert len(provider.invocations) == 2
    assert provider.invocations[0]["prompt"] == "first"
    # Default model substituted when caller passes ``None`` (REQ 3.7 path).
    assert provider.invocations[0]["model"] == "fake/test-model"
    assert provider.invocations[1]["prompt"] == "second"
    # Caller-supplied model wins over the dataclass default.
    assert provider.invocations[1]["model"] == "custom/model"


# ---------------------------------------------------------------------------
# REQ 3.6: provider error injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_provider_raises_injected_provider_error() -> None:
    """Injected :class:`LLMProviderError` is raised verbatim.

    Validates REQ 3.6: ``code`` and ``message`` are preserved so the
    upstream ``SubAgent.respond`` can copy them onto the ``llm_error``
    Event_Log payload (``provider_code`` / ``provider_message``).
    """
    provider = FakeLLMProvider(error=LLMProviderError(code=500, message="boom"))

    with pytest.raises(LLMProviderError) as excinfo:
        await provider.invoke("p")

    assert excinfo.value.code == 500
    assert excinfo.value.message == "boom"
    # Even on the error path the call MUST be observable in ``invocations``;
    # otherwise SLA tests cannot detect "did the provider get called at all?".
    assert len(provider.invocations) == 1
    assert provider.invocations[0]["prompt"] == "p"


# ---------------------------------------------------------------------------
# REQ 3.4 / 3.5: latency vs. caller-side asyncio.wait_for cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_provider_latency_is_cancellable_via_wait_for() -> None:
    """Caller-side ``asyncio.wait_for`` cancels a long-running ``invoke``.

    Validates the *cancellation surface* underpinning REQ 3.5. The production
    deadline is 60 s, but using a 60 s deadline in the test would make the
    suite punitively slow without changing the contract under test: a
    provider whose ``latency`` exceeds the deadline MUST be cancellable, and
    the cancellation MUST surface as :class:`asyncio.TimeoutError` (which
    ``SubAgent.respond`` then converts into a typed
    :class:`vsm.errors.LLMTimeoutError`).

    We pair ``latency=2.0`` with ``timeout=0.1`` so the same control flow
    fires deterministically in well under a second.
    """
    provider = FakeLLMProvider(response="never", latency=2.0)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(provider.invoke("x"), timeout=0.1)


# ---------------------------------------------------------------------------
# Helper constructors
# ---------------------------------------------------------------------------


def test_make_timeout_provider_uses_latency_above_60s_default() -> None:
    """``make_timeout_provider()`` defaults to a latency strictly greater than 60 s.

    Validates the helper contract used by REQ 3.4 / 3.5 SLA tests: any caller
    wrapping the returned provider in ``asyncio.wait_for(..., 60)`` MUST hit
    the deadline. The helper carries a margin (default 70 s) so the timeout
    fires reliably even if the provider's internal sleep resolution is
    coarse.
    """
    provider = make_timeout_provider()

    assert isinstance(provider, FakeLLMProvider)
    assert provider.latency >= 60.0
    # No injected error: the timeout, not a synthetic raise, is what the
    # caller is meant to observe.
    assert provider.error is None


def test_make_timeout_provider_accepts_custom_latency() -> None:
    """An explicit ``timeout_seconds`` is forwarded to ``latency`` verbatim."""
    provider = make_timeout_provider(timeout_seconds=120.0)

    assert provider.latency == 120.0


@pytest.mark.asyncio
async def test_make_error_provider_raises_with_supplied_code_and_message() -> None:
    """``make_error_provider(code, message)`` builds a provider that raises that error.

    Validates REQ 3.6: callers can synthesise any ``(code, message)`` pair —
    here ``(429, "rate")`` modelling a rate-limit response — and the helper
    SHALL propagate both values onto the raised :class:`LLMProviderError`.
    """
    provider = make_error_provider(code=429, message="rate")

    with pytest.raises(LLMProviderError) as excinfo:
        await provider.invoke("x")

    assert excinfo.value.code == 429
    assert excinfo.value.message == "rate"


def test_make_error_provider_accepts_string_code() -> None:
    """String error codes (e.g. ``"RATE_LIMIT"``) are preserved as-is.

    Some providers report error codes as opaque strings rather than HTTP
    status integers; the helper MUST not coerce the ``code`` type so the
    Event_Log payload mirrors the upstream exactly (REQ 3.6).
    """
    provider = make_error_provider(code="RATE_LIMIT", message="slow down")

    assert isinstance(provider.error, LLMProviderError)
    assert provider.error.code == "RATE_LIMIT"
    assert provider.error.message == "slow down"
