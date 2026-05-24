"""Event_Log read-only readers for ``vsm tail`` / ``vsm status`` / ``vsm replay``.

This module is the read-side counterpart to
:class:`vsm.eventlog.writer.EventLogWriter`. It deliberately does **not**
hold any file descriptor open across calls or maintain background state:
each function opens ``events.jsonl``, reads what it needs, and (for the
async tailing case) closes the descriptor when the consumer cancels the
iterator.

Two entry points are exposed:

* :func:`read_all` — synchronous, one-shot drain of every event in the
  file. Used by ``vsm status`` and ``vsm replay`` (one-shot scans).
* :func:`iter_appended` — asynchronous generator that yields every event
  already in the file then continues to yield events that are appended
  afterwards. Used by ``vsm tail`` (continuous tail).

REQ 11.2 sets the SLA for ``vsm tail`` at 1 second between an append and
its emission. We poll with :data:`_TAIL_POLL_INTERVAL_SECONDS` (200 ms),
which leaves a 5x margin against the 1-second budget while keeping idle
CPU usage negligible.

REQ 11.7 requires the CLI observation subcommands to surface a missing
``events.jsonl`` as an exit-code-2 error; this module raises
:class:`vsm.errors.CLIError` with ``exit_code=2`` so the CLI layer can
catch and translate it into the user-facing ``Event_Log not found for
run <id>`` message without further branching.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from vsm.errors import CLIError

# REQ 11.2: tail latency budget is 1 s; a 200 ms poll interval leaves a
# 5x margin while keeping idle CPU usage negligible. Exposed as a module
# constant so tests can override via the keyword-only ``poll_interval``
# parameter on :func:`iter_appended`.
_TAIL_POLL_INTERVAL_SECONDS = 0.2


__all__ = ["read_all", "iter_appended"]


def read_all(path: Path) -> list[dict[str, Any]]:
    """Read every JSONL event from ``path``, returning a list of parsed dicts.

    Used by ``vsm status`` and ``vsm replay`` for one-shot reads where the
    caller wants the entire log materialised in memory. The events are
    returned in the order they appear in the file (which, by REQ 10.8, is
    also the writer's append / ``seq`` order).

    REQ 11.7: the caller must verify path existence beforehand if it wants
    to surface the standard ``Event_Log not found`` error. This function
    intentionally does not handle missing-file errors so the CLI layer can
    decide whether the absence is fatal (``status`` / ``tail`` / ``replay``)
    or recoverable (e.g. a fresh run that has not yet appended).

    Parameters
    ----------
    path : pathlib.Path
        Path to the ``events.jsonl`` file.

    Returns
    -------
    list[dict[str, Any]]
        Every parsed event envelope, in file order. Blank lines (which can
        appear if the writer was killed mid-flush) are skipped silently.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist (propagated from ``Path.open``).
    json.JSONDecodeError
        If any non-blank line is not valid JSON. Mirrors
        :func:`vsm.eventlog.replay.replay`'s strict parsing posture: a
        corrupt line indicates a writer bug or external tampering and is
        not silently swallowed.
    """
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            events.append(json.loads(stripped))
    return events


async def iter_appended(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool] | None = None,
    *,
    poll_interval: float = _TAIL_POLL_INTERVAL_SECONDS,
) -> AsyncIterator[dict[str, Any]]:
    """Yield each event in ``events.jsonl`` then continue tailing for new appends.

    The generator first drains every event already present in the file,
    then enters a polling loop that re-issues ``readline`` every
    ``poll_interval`` seconds; new lines appended by
    :class:`vsm.eventlog.writer.EventLogWriter` after the initial drain are
    yielded as soon as the next poll observes them.

    Only events for which ``predicate`` returns truthy are yielded. The
    default predicate accepts everything, which is what ``vsm tail``
    without ``--system`` / ``--channel`` filters expects (REQ 11.4). The
    CLI layer composes its ``--system`` / ``--channel`` filters into a
    single callable and passes it here.

    REQ 11.2: each appended event is yielded within 1 second of the
    append. The poll interval is 200 ms by default, so the worst-case
    detection latency is 200 ms (plus the consumer's ``async for`` step),
    well inside the 1-second SLA.

    REQ 11.7: if ``path`` does not exist at the moment the consumer first
    iterates, raises :class:`vsm.errors.CLIError` with ``exit_code=2`` so
    the CLI layer can translate it into the ``Event_Log not found for
    run <id>`` message. Note: because this function is an ``async``
    generator, the existence check and the resulting ``CLIError`` are
    *deferred* until the consumer's first ``__anext__`` — calling
    ``iter_appended(missing_path)`` does not raise on its own; the error
    surfaces when the first ``async for`` step is taken. This matches the
    natural laziness of generators and keeps the CLI's error handling
    centred on the iteration site.

    Parameters
    ----------
    path : pathlib.Path
        Path to the ``events.jsonl`` file.
    predicate : Callable[[dict[str, Any]], bool] | None, optional
        Filter applied to every parsed event. ``None`` (default) means
        "accept everything" (REQ 11.4).
    poll_interval : float, keyword-only, optional
        Seconds to ``asyncio.sleep`` between empty-readline polls.
        Defaults to :data:`_TAIL_POLL_INTERVAL_SECONDS` (200 ms). Exposed
        primarily for tests that want to drive the loop deterministically;
        production callers should rely on the default.

    Yields
    ------
    dict[str, Any]
        Each parsed event envelope that passes ``predicate``, in file
        order.

    Raises
    ------
    vsm.errors.CLIError
        With ``exit_code=2`` (REQ 11.7) if ``path`` does not exist.
    json.JSONDecodeError
        If any non-blank line is not valid JSON. Symmetric with
        :func:`read_all` and :func:`vsm.eventlog.replay.replay`: a corrupt
        JSONL line is a hard error, not silently dropped.
    """
    if not path.exists():
        # REQ 11.7: surface the missing Event_Log to the CLI as exit
        # code 2. The message intentionally mirrors design.md's tail
        # pseudocode so log-greppers can match either source.
        raise CLIError(
            f"Event_Log not found for run path {path}",
            exit_code=2,
        )

    pred: Callable[[dict[str, Any]], bool] = (
        predicate if predicate is not None else (lambda _evt: True)
    )

    with path.open("r", encoding="utf-8") as fh:
        # First pass: drain events that were already on disk when the
        # tail began. ``vsm tail`` consumers expect the historical prefix
        # so they can establish context before live events arrive.
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            evt = json.loads(stripped)
            if pred(evt):
                yield evt

        # Second phase: poll for new appends. ``readline`` returns an
        # empty string when the descriptor is at EOF; we sleep for
        # ``poll_interval`` and try again. This loop is intentionally
        # unbounded — the consumer terminates the tail by cancelling the
        # ``async for`` (e.g. SIGINT in the CLI), which closes the
        # generator and, by way of the ``with`` block, the file handle.
        while True:
            line = fh.readline()
            if not line:
                await asyncio.sleep(poll_interval)
                continue
            stripped = line.strip()
            if not stripped:
                continue
            evt = json.loads(stripped)
            if pred(evt):
                yield evt
