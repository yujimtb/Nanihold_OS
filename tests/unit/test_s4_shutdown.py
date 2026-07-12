"""S4 のキャンセル時に待機用子 task を残さないことを検証する。"""

from __future__ import annotations

import asyncio

import pytest

from vsm.llm.fake import FakeLLMProvider
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import start_run


@pytest.mark.asyncio
async def test_s4_shutdown_cleans_queue_getter_tasks(tmp_path) -> None:
    platform = await start_run(
        runs_dir=tmp_path / "runs",
        llm_override=FakeLLMProvider(response="ok"),
    )
    try:
        s4 = platform.systems[SystemRole.S4_SCANNER][0]
        await asyncio.sleep(0)
        assert {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done()
        } >= {"s4_task_get", "s4_s5_get"}

        await s4.shutdown()

        assert not any(
            task.get_name() in {"s4_task_get", "s4_s5_get"}
            for task in asyncio.all_tasks()
            if not task.done()
        )
    finally:
        await platform.shutdown()
