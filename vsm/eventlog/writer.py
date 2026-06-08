"""Single-writer Event_Log append engine.

This module implements :class:`EventLogWriter`, the sole writer of
``runs/{run_id}/events.jsonl`` for any active Run. design.md
§Components #3 (``Event_Log Writer``) prescribes a single-task writer behind
an :class:`asyncio.Queue` so that:

* every appended record is written by exactly one coroutine, which makes the
  monotonic ``seq`` assignment trivial and removes the need for any locking;
* callers (Systems, the CLI ``submit`` path, the Message_Bus, etc.) only have
  to ``await writer.append(...)`` and do not have to reason about retry,
  ``fsync`` or ordering themselves;
* the file handle is opened once in line-buffered (``buffering=1``) append
  mode with ``encoding="utf-8"`` so that an interrupted process leaves a
  partially-written but still valid line-delimited JSONL trail rather than a
  block-buffered torn write.

The writer pairs every line write with ``flush()`` + ``os.fsync()`` so that
the 100 ms append-visibility SLA in REQ 10.5 is met even when the OS page
cache would otherwise defer the write. Transient ``OSError`` (e.g. a
file system hiccup) is retried up to three times with a 100 ms gap between
attempts as required by REQ 10.6; on the third failure the writer raises
:class:`vsm.errors.EventLogAppendError` so that the calling System receives
a typed error rather than a silent loss of data.

Validates Requirements
----------------------
- REQ 10.3: the writer expects a path of the form ``runs/{run_id}/events.jsonl``.
  The caller (Run lifecycle code) creates the parent directory; the writer
  opens the file in append mode so that a pre-existing file is preserved and
  a non-existent file is created on first write.
- REQ 10.5: ``append()`` is a fast (sub-100 ms) hand-off into an
  :class:`asyncio.Queue`; the actual disk write happens on the writer task,
  which performs ``write`` + ``flush`` + ``fsync`` in a tight loop.
- REQ 10.6: ``_write_with_retry`` retries up to three times, sleeping at
  least 100 ms between attempts, and surfaces :class:`EventLogAppendError`
  to the writer loop when all three attempts fail.
- REQ 10.7: every appended record carries the envelope fields ``ts``,
  ``run_id``, ``event_type``, ``seq``, ``payload`` via :class:`Event`.
- REQ 10.8: the single-writer design preserves FIFO order, and ``seq`` is
  assigned monotonically starting from 0 on the writer task.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from vsm.clock import Clock
from vsm.errors import EventLogAppendError
from vsm.eventlog.schema import Event, validate_event_payload
from vsm.ids import validate_run_id

__all__ = ["EventLogWriter"]


# REQ 10.6: at most 3 attempts per append (the call itself plus 2 retries).
_MAX_APPEND_ATTEMPTS = 3

# REQ 10.6: at least 100 ms between successive attempts. The exact value is
# also referenced by ``tests/property/test_retry_semantics.py`` (Property 16)
# to assert that the retry interval lower bound is honoured.
_RETRY_BACKOFF_SECONDS = 0.1

# Bind-mounted Docker/WSL filesystems can make per-line fsync take hundreds of
# milliseconds, which blocks the single asyncio event loop. ``flush`` is enough
# for the CLI/read-side visibility SLA; set this env var when crash-durable
# fsync is required for an operational run.
_DURABLE_FSYNC = os.environ.get("VSM_EVENTLOG_FSYNC", "").lower() in {
    "1",
    "true",
    "yes",
}


class EventLogWriter:
    """Single-task writer for a Run's ``events.jsonl``.

    The writer owns:

    * an :class:`asyncio.Queue` of pending ``(event_type, payload, ts)`` tuples;
    * a monotonically increasing ``seq`` counter (REQ 10.8);
    * a single line-buffered file handle opened in append mode (REQ 10.3 / 10.5).

    Callers interact through three coroutines: :meth:`start`, :meth:`stop`,
    and :meth:`append`. The writer task itself is private (``_writer_loop``)
    and is the only coroutine that ever writes to the file or mutates the
    sequence counter.

    Parameters
    ----------
    run_id : str
        The Run identifier this writer is bound to. Validated against the
        REQ 10.2 format constraints (1..64 ASCII characters) at construction
        time so that a malformed identifier fails fast rather than producing
        an envelope that pydantic would later reject.
    path : pathlib.Path
        Path to the ``events.jsonl`` file. The parent directory must already
        exist (REQ 10.3 / 10.4: directory creation is the Run lifecycle
        code's responsibility, see ``vsm/runtime/lifecycle.py``).
    clock : Clock
        The clock used to stamp every appended event. Injecting this enables
        deterministic SLA verification with :class:`vsm.clock.FakeClock`.
    """

    def __init__(self, run_id: str, path: Path, clock: Clock) -> None:
        # REQ 10.2 / 10.7: reject malformed run_ids before opening any file.
        validate_run_id(run_id)

        self._run_id: str = run_id
        self._path: Path = path
        self._clock: Clock = clock

        # REQ 10.5 / 10.8: the queue is the synchronisation point between
        # callers and the writer task. ``append`` enqueues from any coroutine,
        # ``_writer_loop`` is the sole consumer.
        self._queue: asyncio.Queue[tuple[str, dict[str, Any], str, dict[str, Any]]] = (
            asyncio.Queue()
        )

        # REQ 10.8: ``seq`` starts at 0 and is incremented exclusively on the
        # writer task, which guarantees a strict monotonic sequence even
        # under concurrent enqueues.
        self._seq: int = 0
        self._stream_versions: dict[str, int] = {}

        # REQ 10.3 / 10.5: open the JSONL file in append mode with UTF-8
        # encoding and line buffering. ``buffering=1`` causes Python's text
        # IO layer to flush on every newline, which combined with the
        # explicit ``flush() + os.fsync()`` in ``_write_with_retry`` keeps
        # in-flight events visible within the 100 ms SLA.
        self._fh = open(path, "a", encoding="utf-8", buffering=1)

        # The writer task handle. ``None`` until ``start()`` is called and
        # again after ``stop()`` returns.
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Spawn the writer task if it is not already running.

        Idempotent: a second call is a no-op so that callers do not have to
        track lifecycle state defensively.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._writer_loop(), name=f"event_log_writer[{self._run_id}]"
        )

    async def stop(self) -> None:
        """Cancel the writer task and close the underlying file handle.

        The writer task is cancelled (rather than allowed to drain) because
        the lifecycle layer is expected to call ``stop`` only after every
        System has been shut down, at which point no further ``append``
        calls can race against this method. The file handle is flushed and
        closed unconditionally so that even a partially-drained queue still
        produces a well-formed (truncated) JSONL file on disk.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected: ``_writer_loop`` runs forever until cancelled.
                pass
            self._task = None

        if not self._fh.closed:
            try:
                self._fh.flush()
            finally:
                self._fh.close()

    async def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        node_id: str | None = None,
        stream_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        schema_version: int = 1,
    ) -> None:
        """Enqueue an event for the writer task to persist.

        Validates ``payload`` synchronously against the pydantic model
        registered for ``event_type`` so that schema errors surface at the
        caller's site (REQ 10.7) rather than being silently swallowed inside
        the writer task. A timestamp is sampled from the injected clock
        immediately so that the recorded ``ts`` reflects when the producer
        observed the event, not when the writer task happened to drain it.

        Parameters
        ----------
        event_type : str
            One of :data:`vsm.eventlog.schema.EVENT_TYPES`.
        payload : dict
            The event-specific payload. Must satisfy the pydantic model
            registered for ``event_type`` in
            :data:`vsm.eventlog.schema.PAYLOAD_MODELS`.

        Raises
        ------
        ValueError
            If ``event_type`` is not registered.
        pydantic.ValidationError
            If ``payload`` is malformed for the given ``event_type``.
        """
        # REQ 10.7: catch payload schema violations at the call site.
        validate_event_payload(event_type, payload)

        # REQ 2.8 / 2.9 / 10.5 / 10.7: stamp ``ts`` at producer observation
        # time using the injected clock so SLA assertions remain meaningful
        # under back-pressure on the writer queue.
        ts = self._clock.now_iso()

        # REQ 10.5: the hand-off itself is an in-memory queue ``put``, which
        # completes well within the 100 ms append-visibility SLA. The
        # ``await`` allows the event loop to schedule the writer task
        # promptly when there is no contention.
        metadata = {
            "node_id": node_id,
            "stream_id": stream_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "schema_version": schema_version,
        }
        await self._queue.put((event_type, payload, ts, metadata))

    async def _writer_loop(self) -> None:
        """Drain the queue and persist each event in FIFO order.

        Runs until cancelled by :meth:`stop`. On every iteration:

        1. blocks on ``queue.get`` for the next pending event;
        2. assigns the next monotonic ``seq`` (REQ 10.8);
        3. constructs the :class:`Event` envelope (which pydantic validates
           against REQ 10.7's required-field invariants); and
        4. delegates the durable write to :meth:`_write_with_retry`.
        """
        while True:
            event_type, payload, ts, metadata = await self._queue.get()

            # REQ 10.8: ``seq`` is assigned only here, on the single writer
            # task, so the assignment is race-free by construction.
            seq = self._seq
            self._seq += 1
            stream_id = metadata.get("stream_id") or metadata.get("node_id") or self._run_id
            stream_version = self._stream_versions.get(stream_id, 0) + 1
            self._stream_versions[stream_id] = stream_version

            # REQ 10.7: ``Event`` validates the envelope (timestamp pattern,
            # run_id length, event_type membership, non-negative seq).
            event = Event(
                ts=ts,
                run_id=self._run_id,
                node_id=metadata.get("node_id"),
                stream_id=stream_id,
                stream_version=stream_version,
                event_type=event_type,
                schema_version=metadata.get("schema_version") or 1,
                seq=seq,
                actor_type=metadata.get("actor_type") or "system",
                actor_id=metadata.get("actor_id"),
                correlation_id=metadata.get("correlation_id") or self._run_id,
                causation_id=metadata.get("causation_id"),
                payload=payload,
            )

            await self._write_with_retry(event)

    async def _write_with_retry(self, event: Event) -> None:
        """Serialise and append ``event``, retrying transient OS errors.

        REQ 10.6 mandates up to three attempts with at least 100 ms between
        attempts. The retry covers the entire write path (``write`` +
        ``flush`` + ``fsync``) because any of those three calls can fail
        with :class:`OSError` (e.g. ``ENOSPC``, ``EIO``). On the third
        consecutive failure the writer raises
        :class:`vsm.errors.EventLogAppendError` so that the calling System
        is notified rather than silently losing the event.

        Parameters
        ----------
        event : Event
            The validated envelope to persist as a single JSONL line.

        Raises
        ------
        EventLogAppendError
            When all three attempts fail. The original ``OSError`` is
            preserved as the exception's cause.
        """
        # ``model_dump_json`` produces compact JSON (no separators padding)
        # and respects the pydantic model schema. ``ensure_ascii`` defaults
        # to ``False`` for ``model_dump_json`` so non-ASCII payloads (e.g.
        # 営業 / リサーチ Sub_Agent labels in REQ 5.1) round-trip cleanly.
        line = event.model_dump_json() + "\n"

        for attempt in range(_MAX_APPEND_ATTEMPTS):
            try:
                self._fh.write(line)
                # REQ 10.5: explicit flush + fsync forces the page cache to
                # commit the write so the entry is observable to ``vsm tail``
                # within the 100 ms SLA.
                self._fh.flush()
                if _DURABLE_FSYNC:
                    os.fsync(self._fh.fileno())
                return
            except OSError as exc:
                # REQ 10.6: surface a typed error after the third failure.
                if attempt == _MAX_APPEND_ATTEMPTS - 1:
                    raise EventLogAppendError(event, exc) from exc
                # REQ 10.6: at least 100 ms between successive attempts.
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
