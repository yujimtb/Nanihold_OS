"""Unit tests for ``SubAgent.respond``.

Validates Requirements: 3.3, 3.4, 3.5, 3.6.

These tests pin the three Event_Log paths that ``SubAgent.respond`` MUST
exercise, driven through the public ``System.register_sub_agent`` entry
point rather than constructing :class:`SubAgent` directly. Going through
``register_sub_agent`` exercises the wiring that production code relies
on (UUID assignment, shared eventlog/llm/clock injection, Sub_Agent
count enforcement) so a regression in that wiring shows up here too.

* **Success path (REQ 3.3)** — a successful LLM invocation appends
  exactly one ``llm_invocation`` event whose payload mirrors the
  :class:`LLMResponse` (system_id / sub_agent_id / model / prompt /
  response / latency_ms / tokens_in / tokens_out).
* **Provider-error path (REQ 3.6)** — when the provider raises
  :class:`LLMProviderError`, ``SubAgent.respond`` appends an
  ``llm_error`` event (with ``provider_code`` stringified per the
  schema) and re-raises the original typed error.
* **Timeout path (REQ 3.4 / 3.5)** — when the provider exceeds the
  configured deadline, ``asyncio.wait_for`` cancels the inner
  coroutine, ``SubAgent.respond`` appends an ``llm_timeout`` event,
  and re-raises a typed :class:`LLMTimeoutError`. The 60-second SLA
  defined by REQ 3.4 is enforced via ``asyncio.wait_for`` at the
  SubAgent layer; we monkeypatch ``_LLM_TIMEOUT_SECONDS`` down to
  0.1 s so the test runs in well under a second instead of literally
  waiting 60+ seconds.

The tests use a real :class:`EventLogWriter` against a temporary file
so both the schema validation (pydantic) and the writer-task FIFO
drain are covered end-to-end. ``FakeLLMProvider`` is used to inject
deterministic latency and errors without touching ``litellm``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vsm.clock import SystemClock
from vsm.errors import LLMProviderError, LLMTimeoutError
from vsm.eventlog.writer import EventLogWriter
from vsm.llm.fake import FakeLLMProvider
from vsm.llm.types import LLMProviderProtocol
from vsm.roles import SystemRole
from vsm.systems.base import System


class _DummySystem(System):
    """Concrete :class:`System` subclass for testing :class:`SubAgent`.

    :class:`System.run` is abstract; tests do not need an actual main
    loop because they invoke ``SubAgent.respond`` directly. The body
    awaits forever so that if a test accidentally calls ``start()``
    the task simply parks instead of terminating early and confusing
    the assertions.
    """

    async def run(self) -> None:  # pragma: no cover - never invoked
        await asyncio.Event().wait()


async def _build_system(
    tmp_path: Path,
    llm: LLMProviderProtocol,
) -> tuple[_DummySystem, EventLogWriter, Path]:
    """Wire up a started :class:`EventLogWriter` and a :class:`_DummySystem`.

    Returns ``(system, writer, events_path)``. The caller is responsible
    for awaiting ``writer.stop()`` (typically inside ``try / finally``)
    so the writer task drains and the JSONL file is flushed before the
    test inspects it. A single ``"default"`` Sub_Agent is registered so
    tests can address it via ``system.sub_agents[0]``.
    """
    events_path = tmp_path / "events.jsonl"
    writer = EventLogWriter(
        run_id="run-sub",
        path=events_path,
        clock=SystemClock(),
    )
    await writer.start()

    system = _DummySystem(
        system_id="sys-1",
        role=SystemRole.S1_WORKER,
        eventlog=writer,
        llm=llm,
        clock=SystemClock(),
    )
    system.register_sub_agent(label="default")
    return system, writer, events_path


def _read_events(events_path: Path) -> list[dict]:
    """Read the JSONL file and return the parsed event envelopes."""
    return [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_respond_success_appends_invocation(tmp_path: Path) -> None:
    """REQ 3.3: success path appends exactly one ``llm_invocation`` event.

    A small ``latency=0.01`` is used so the writer task has time to
    drain (we still ``asyncio.sleep(0.2)`` afterwards to be safe). The
    payload is asserted to contain every field required by REQ 3.3 and
    by the ``llm_invocation`` schema (system_id, sub_agent_id, model,
    prompt, response, latency_ms, tokens_in, tokens_out).
    """
    llm = FakeLLMProvider(response="ok response", latency=0.01)
    system, writer, events_path = await _build_system(tmp_path, llm)
    try:
        sub_agent = system.sub_agents[0]
        resp = await sub_agent.respond("hello")
        # REQ 3.3: caller observes the LLMResponse payload directly.
        assert resp.text == "ok response"
        # Yield to the writer task so the queued event is flushed before
        # the test reads ``events.jsonl``. 200 ms is generous on CI; the
        # writer normally drains in well under 10 ms.
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    events = _read_events(events_path)
    invocations = [e for e in events if e["event_type"] == "llm_invocation"]
    assert len(invocations) == 1, (
        f"expected exactly one llm_invocation, got {len(invocations)} "
        f"(events={[e['event_type'] for e in events]})"
    )
    payload = invocations[0]["payload"]
    assert payload["system_id"] == "sys-1"
    assert payload["sub_agent_id"] == sub_agent.sub_agent_id
    assert payload["prompt"] == "hello"
    assert payload["response"] == "ok response"
    # REQ 3.3 also requires model / latency_ms / token usage to be
    # present. We do not pin specific numeric values because they are
    # determined by the FakeLLMProvider's defaults; presence and
    # non-negativity are sufficient.
    assert "model" in payload
    assert payload["latency_ms"] >= 0
    assert payload["tokens_in"] >= 0
    assert payload["tokens_out"] >= 0


@pytest.mark.asyncio
async def test_respond_provider_error(tmp_path: Path) -> None:
    """REQ 3.6: provider error appends ``llm_error`` and re-raises typed error.

    ``provider_code`` is stringified by the implementation so the
    schema's ``min_length=1`` constraint is satisfied for both ``int``
    and ``str`` code values. Here we pass ``code=500`` (an ``int``) to
    exercise the coercion path.
    """
    llm = FakeLLMProvider(error=LLMProviderError(code=500, message="boom"))
    system, writer, events_path = await _build_system(tmp_path, llm)
    try:
        with pytest.raises(LLMProviderError):
            await system.sub_agents[0].respond("x")
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    events = _read_events(events_path)
    errors = [e for e in events if e["event_type"] == "llm_error"]
    assert len(errors) == 1, (
        f"expected exactly one llm_error, got {len(errors)} "
        f"(events={[e['event_type'] for e in events]})"
    )
    payload = errors[0]["payload"]
    assert payload["system_id"] == "sys-1"
    assert payload["sub_agent_id"] == system.sub_agents[0].sub_agent_id
    # REQ 3.6: provider_code is the stringified upstream code.
    assert payload["provider_code"] == "500"
    assert payload["provider_message"] == "boom"
    # And no llm_invocation was emitted on the error path.
    assert not any(e["event_type"] == "llm_invocation" for e in events)


@pytest.mark.asyncio
async def test_respond_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ 3.4 / 3.5: timeout appends ``llm_timeout`` and raises typed error.

    The production deadline is 60 seconds (REQ 3.4), enforced via
    ``asyncio.wait_for`` at the SubAgent layer (see
    ``vsm/systems/base.py``). Waiting 60 actual seconds in a unit test
    is impractical, so we monkeypatch ``_LLM_TIMEOUT_SECONDS`` down to
    0.1 s and pair it with ``latency=2.0`` so ``asyncio.wait_for`` is
    guaranteed to fire. The behaviour under test (timeout → event +
    typed raise) is identical regardless of the absolute deadline.
    """
    monkeypatch.setattr("vsm.systems.base._LLM_TIMEOUT_SECONDS", 0.1)
    llm = FakeLLMProvider(response="never", latency=2.0)
    system, writer, events_path = await _build_system(tmp_path, llm)
    try:
        # REQ 3.5: caller receives a typed LLMTimeoutError, not asyncio's
        # raw TimeoutError. ``raise ... from None`` in the implementation
        # strips the asyncio cause chain.
        with pytest.raises(LLMTimeoutError):
            await system.sub_agents[0].respond("x")
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    events = _read_events(events_path)
    timeouts = [e for e in events if e["event_type"] == "llm_timeout"]
    assert len(timeouts) == 1, (
        f"expected exactly one llm_timeout, got {len(timeouts)} "
        f"(events={[e['event_type'] for e in events]})"
    )
    payload = timeouts[0]["payload"]
    assert payload["system_id"] == "sys-1"
    assert payload["sub_agent_id"] == system.sub_agents[0].sub_agent_id
    # ``elapsed_ms`` is bounded below by 0 (schema ge=0) and should be
    # a non-negative integer; we don't assert an upper bound because it
    # depends on the SystemClock-driven monotonic measurement.
    assert payload["elapsed_ms"] >= 0
    # Ensure no spurious llm_invocation was recorded for the timed-out call.
    assert not any(e["event_type"] == "llm_invocation" for e in events)
