"""Headless controller task の process-local service wrapper。

FastAPI lifespan への接続は Wave 4 の責務とし、この module は task の
開始・停止と controller health だけを提供する。
"""

from __future__ import annotations

import asyncio

from vsm.selfdev.controller import SelfDevController


class SelfDevService:
    def __init__(self, controller: SelfDevController, *, idle_seconds: float = 0.1) -> None:
        if idle_seconds <= 0:
            raise ValueError("idle_seconds は正数でなければなりません")
        self.controller = controller
        self.idle_seconds = idle_seconds
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._fatal: BaseException | None = None

    @property
    def healthy(self) -> bool:
        return self._task is not None and not self._task.done() and self._fatal is None

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.controller.start()
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="selfdev-controller",
        )

    async def _run(self) -> None:
        try:
            await self.controller.run_forever(stop_event=self._stop_event, idle_seconds=self.idle_seconds)
        except BaseException as exc:
            self._fatal = exc
            raise

    async def stop(self) -> None:
        self._stop_event.set()
        task_error: BaseException | None = None
        if self._task is not None:
            try:
                await self._task
            except BaseException as exc:
                task_error = exc
            finally:
                self._task = None
        await self.controller.stop()
        if task_error is not None:
            raise task_error


__all__ = ["SelfDevService"]
