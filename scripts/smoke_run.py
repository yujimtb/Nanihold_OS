"""Smoke run: drive the representative scenario end-to-end with FakeLLMProvider.

Usage:
    python scripts/smoke_run.py
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

from vsm.clock import SystemClock
from vsm.config import RunConfig
from vsm.eventlog.reader import read_all
from vsm.ids import generate_uuid
from vsm.llm.fake import FakeLLMProvider
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import start_run


async def main() -> None:
    fake_llm = FakeLLMProvider(response="ok", latency=0.05)
    clock = SystemClock()
    platform = await start_run(
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=clock,
    )

    try:
        task_payload = {
            "task_id": generate_uuid(),
            "run_id": platform.run_id,
            "description": "smoke test: representative VSM event flow",
            "file_paths": [],
            "submitted_at": clock.now_iso(),
        }
        await platform.eventlog.append("task_submitted", task_payload)
        s4 = platform.systems[SystemRole.S4_SCANNER][0]
        await s4.trigger(task_payload)

        events_path = platform.run_dir / "events.jsonl"
        deadline = SystemClock().monotonic() + 30.0
        last_count = -1
        while SystemClock().monotonic() < deadline:
            await asyncio.sleep(0.5)
            if not events_path.exists():
                continue
            events = read_all(events_path)
            if len(events) != last_count:
                print(
                    f"  [t+{int((SystemClock().monotonic() - (deadline - 30.0)))}s] "
                    f"{len(events)} events; latest types: "
                    f"{[e['event_type'] for e in events[-5:]]}"
                )
                last_count = len(events)
            event_types = {e["event_type"] for e in events}
            if "s1_completion" in event_types:
                print("\n✓ s1_completion observed")
                break
        else:
            print("\n✗ no s1_completion within 30 s")
    finally:
        events = read_all(events_path) if events_path.exists() else []
        print(f"\n--- Run summary: {platform.run_id} ---")
        print(f"  total events: {len(events)}")
        print("  by event_type:")
        for et, n in Counter(e["event_type"] for e in events).most_common():
            print(f"    {et:35s} {n}")
        print("\n--- last 20 events ---")
        for evt in events[-20:]:
            payload_keys = list(evt.get("payload", {}).keys())
            print(
                f"  seq={evt['seq']:3d} {evt['event_type']:30s} "
                f"keys={payload_keys}"
            )
        await platform.shutdown()
        print(f"\nrun_dir: {platform.run_dir}")


if __name__ == "__main__":
    asyncio.run(main())
