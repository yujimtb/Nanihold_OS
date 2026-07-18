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
import json
import os
from pathlib import Path
from typing import Any, Literal

from vsm.clock import Clock
from vsm.errors import EventLogAppendError
from vsm.eventlog.schema import Event, validate_event_payload
from vsm.ids import validate_run_id

__all__ = ["EventLogWriter"]


# REQ 10.6: at most 3 attempts per append (the call itself plus 2 retries).
_MAX_APPEND_ATTEMPTS = 3

# REQ 10.6: at least 100 ms between successive attempts. The implementation
# asks the event loop for 110 ms so the observed gap remains above 100 ms on
# Windows timers that occasionally resume a few milliseconds early.
_RETRY_BACKOFF_SECONDS = 0.11

# Bind-mounted Docker/WSL filesystems can make per-line fsync take hundreds of
# milliseconds, which blocks the single asyncio event loop. ``flush`` is enough
# for the CLI/read-side visibility SLA; set this env var when crash-durable
# fsync is required for an operational run.
Durability = Literal["buffered", "durable"]
WriterState = Literal["created", "running", "stopping", "stopped"]


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

    def __init__(
        self,
        run_id: str,
        path: Path,
        clock: Clock,
        *,
        durability: Durability = "buffered",
        strict_recovery: bool = False,
    ) -> None:
        # REQ 10.2 / 10.7: reject malformed run_ids before opening any file.
        validate_run_id(run_id)

        self._run_id: str = run_id
        self._path: Path = path
        self._clock: Clock = clock
        if durability not in {"buffered", "durable"}:
            raise ValueError("durability は buffered または durable でなければなりません")
        self._durability = durability
        self._strict_recovery = strict_recovery or durability == "durable"

        # REQ 10.5 / 10.8: the queue is the synchronisation point between
        # callers and the writer task. ``append`` enqueues from any coroutine,
        # ``_writer_loop`` is the sole consumer.
        self._queue: asyncio.Queue[
            tuple[str, dict[str, Any], str, dict[str, Any], int | None, asyncio.Future[Event] | None]
            | None
        ] = asyncio.Queue()

        # REQ 10.8: ``seq`` starts at 0 and is incremented exclusively on the
        # writer task, which guarantees a strict monotonic sequence even
        # under concurrent enqueues.
        self._seq: int = 0
        self._stream_versions: dict[str, int] = {}
        if path.exists() and path.stat().st_size:
            with path.open("r", encoding="utf-8") as existing:
                expected_seq = 0
                expected_stream_versions: dict[str, int] = {}
                for line in existing:
                    if not line.strip():
                        if self._strict_recovery:
                            raise ValueError("Event Log に空行があります")
                        continue
                    try:
                        record = json.loads(line)
                        if self._strict_recovery:
                            envelope = Event.model_validate(record)
                            validate_event_payload(
                                envelope.event_type,
                                envelope.payload,
                                schema_version=envelope.schema_version,
                            )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                        raise ValueError("Event Log の既存行を strict 検証できません") from exc
                    if self._strict_recovery:
                        stream = record.get("stream_id") or record.get("node_id") or run_id
                        if int(record["seq"]) != expected_seq:
                            raise ValueError("Event Log の seq が連続していません")
                        expected_seq += 1
                        expected_version = expected_stream_versions.get(stream, 0) + 1
                        if int(record.get("stream_version", 0)) != expected_version:
                            raise ValueError("Event Log の stream_version が連続していません")
                        expected_stream_versions[stream] = expected_version
                    self._seq = max(self._seq, int(record["seq"]) + 1)
                    stream_id = record.get("stream_id") or record.get("node_id") or run_id
                    self._stream_versions[stream_id] = max(
                        self._stream_versions.get(stream_id, 0),
                        int(record.get("stream_version", 0)),
                    )

        # REQ 10.3 / 10.5: open the JSONL file in append mode with UTF-8
        # encoding and line buffering. ``buffering=1`` causes Python's text
        # IO layer to flush on every newline, which combined with the
        # explicit ``flush() + os.fsync()`` in ``_write_with_retry`` keeps
        # in-flight events visible within the 100 ms SLA.
        self._fh = open(path, "a", encoding="utf-8", buffering=1)

        # The writer task handle. ``None`` until ``start()`` is called and
        # again after ``stop()`` returns. 受付状態と sentinel 投入は同じ
        # lock の中で遷移させる。これにより ``append`` が sentinel の後ろへ
        # enqueue され、成功を返したまま失われる shutdown race を防ぐ。
        self._task: asyncio.Task[None] | None = None
        self._state: WriterState = "created"
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the writer task if it is not already running.

        Idempotent: a second call is a no-op so that callers do not have to
        track lifecycle state defensively.
        """
        async with self._lifecycle_lock:
            if self._state == "running":
                return
            if self._state != "created":
                raise RuntimeError(
                    f"EventLogWriter cannot start from state {self._state}: {self._run_id}"
                )
            self._task = asyncio.create_task(
                self._writer_loop(), name=f"event_log_writer[{self._run_id}]"
            )
            self._state = "running"

    async def stop(self) -> None:
        """Drain the writer queue and close the underlying file handle.

        A sentinel is queued after every event already accepted by
        :meth:`append`, then the writer task is awaited. FIFO ordering therefore
        guarantees that shutdown does not discard the tail of the Event_Log.
        受付終了への状態遷移と sentinel 投入は ``append`` と同じ lock 内で
        行うため、競合した ``append`` は sentinel より前に受理されるか、
        明示的に失敗するかのどちらかであり、後ろへ紛れ込まない。
        """
        async with self._lifecycle_lock:
            if self._state == "stopped":
                return
            if self._state == "created":
                self._state = "stopped"
                self._close_file()
                return
            task = self._task
            if task is None:
                raise RuntimeError(
                    f"EventLogWriter has no task in state {self._state}: {self._run_id}"
                )
            if self._state == "running":
                # ``append`` も同じ lock を取るため、state 変更後に受理される
                # event はなく、sentinel より前の全 event が排水対象になる。
                self._state = "stopping"
                self._queue.put_nowait(None)

        try:
            # stop 呼び出し元の cancel を writer task へ伝播させない。
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # shutdown 自体が cancel されても sentinel 排水と file close は
            # 完遂してから上位へ cancel を返す。
            try:
                await asyncio.shield(task)
            finally:
                await self._finish_stop(task)
            raise
        finally:
            if task.done():
                await self._finish_stop(task)

    async def _finish_stop(self, task: asyncio.Task[None]) -> None:
        """完了した writer task を一度だけ回収して file を閉じる。"""

        async with self._lifecycle_lock:
            if self._state == "stopped":
                return
            if self._task is not task:
                raise RuntimeError("EventLogWriter task changed during stop")
            self._task = None
            self._state = "stopped"
            self._close_file()

    def _close_file(self) -> None:
        if self._fh.closed:
            return
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
        expected_stream_version: int | None = None,
    ) -> Event | None:
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
        validate_event_payload(event_type, payload, schema_version=schema_version)

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
        future: asyncio.Future[Event] | None = None
        if self._durability == "durable":
            future = asyncio.get_running_loop().create_future()
        async with self._lifecycle_lock:
            task = self._task
            if self._state != "running" or task is None or task.done():
                raise RuntimeError(
                    "EventLogWriter is not accepting events "
                    f"(state={self._state}, run_id={self._run_id})"
                )
            self._queue.put_nowait(
                (event_type, payload, ts, metadata, expected_stream_version, future)
            )
        if future is not None:
            return await future
        return None

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
            queued = await self._queue.get()
            if queued is None:
                return
            event_type, payload, ts, metadata, expected_stream_version, future = queued

            # REQ 10.8: ``seq`` is assigned only here, on the single writer
            # task, so the assignment is race-free by construction.
            seq = self._seq
            self._seq += 1
            stream_id = metadata.get("stream_id") or metadata.get("node_id") or self._run_id
            current_stream_version = self._stream_versions.get(stream_id, 0)
            if (
                expected_stream_version is not None
                and expected_stream_version != current_stream_version
            ):
                error = ValueError(
                    f"stale stream version for {stream_id!r}: "
                    f"expected {expected_stream_version}, current {current_stream_version}"
                )
                if future is not None and not future.done():
                    future.set_exception(error)
                raise error
            stream_version = current_stream_version + 1

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

            try:
                await self._write_with_retry(event)
            except Exception as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                raise
            self._stream_versions[stream_id] = stream_version
            if future is not None and not future.done():
                future.set_result(event)

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
                if self._durability == "durable":
                    os.fsync(self._fh.fileno())
                return
            except OSError as exc:
                # REQ 10.6: surface a typed error after the third failure.
                if attempt == _MAX_APPEND_ATTEMPTS - 1:
                    raise EventLogAppendError(event, exc) from exc
                # REQ 10.6: at least 100 ms between successive attempts.
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
