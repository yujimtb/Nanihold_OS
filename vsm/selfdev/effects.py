"""自己開発 controller の durable exactly-once effect journal。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from vsm.selfdev.reasons import exception_reason
from vsm.selfdev.store import SelfDevEventStore

EffectKind = Literal["workspace", "run", "gate", "commit", "cleanup", "audit", "report"]


def _digest(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    else:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class EffectCompletion:
    effect_id: str
    effect_kind: str
    result_sha256: str
    artifact_refs: tuple[str, ...]
    recovered: bool


class EffectInDoubt(RuntimeError):
    """副作用の開始だけが残り、再実行の安全性を証明できない。"""


class EffectJournal:
    """副作用の前後を Event Log に固定する単一入口。"""

    def __init__(self, store: SelfDevEventStore) -> None:
        self.store = store

    def _records(self, proposal_id: str, effect_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            event.payload
            for event in self.store.read_events()
            if event.payload.get("proposal_id") == proposal_id
            and event.payload.get("effect_id") == effect_id
            and event.event_type in {"tool_invoked", "tool_completed", "tool_failed"}
        )

    def completed(self, proposal_id: str, effect_id: str) -> EffectCompletion | None:
        completions = [item for item in self._records(proposal_id, effect_id) if "result_sha256" in item]
        if len(completions) > 1:
            raise ValueError(f"effect_id が二重完了しています: {effect_id}")
        if not completions:
            return None
        item = completions[0]
        return EffectCompletion(
            effect_id=effect_id,
            effect_kind=str(item["effect_kind"]),
            result_sha256=str(item["result_sha256"]),
            artifact_refs=tuple(item.get("artifact_refs", ())),
            recovered=bool(item.get("recovered", False)),
        )

    async def run(
        self,
        *,
        proposal_id: str,
        effect_id: str,
        effect_kind: EffectKind,
        input_value: Any,
        operation: Callable[[], Any | Awaitable[Any]],
        artifact_refs: tuple[str, ...] = (),
    ) -> tuple[bool, Any | EffectCompletion]:
        """効果を一度だけ実行し、``(executed, result)``を返す。

        `tool_invoked` が残った状態での再実行は、外部副作用の重複を避けるため
        明示的に停止する。外部事実を検証して復元する場合は completed を
        `recovered=True` で controller 側から記録する。
        """

        previous = self._records(proposal_id, effect_id)
        completion = self.completed(proposal_id, effect_id)
        if completion is not None:
            return False, completion
        if previous:
            raise EffectInDoubt(f"effect {effect_id} は開始済みだが完了を証明できません")

        input_sha256 = _digest(input_value)
        await self.store.append(
            "tool_invoked",
            {
                "proposal_id": proposal_id,
                "effect_id": effect_id,
                "effect_kind": effect_kind,
                "input_sha256": input_sha256,
            },
            proposal_id=proposal_id,
            actor_type="controller",
            schema_version=2,
        )
        try:
            value = operation()
            if hasattr(value, "__await__"):
                value = await value
        except Exception as exc:
            await self.store.append(
                "tool_failed",
                {
                    "proposal_id": proposal_id,
                    "effect_id": effect_id,
                    "effect_kind": effect_kind,
                    "error_type": type(exc).__name__,
                    "reason": exception_reason(
                        exc,
                        context=(
                            "implementation run"
                            if effect_kind == "run"
                            else f"{effect_kind} effect"
                        ),
                    ),
                },
                proposal_id=proposal_id,
                actor_type="controller",
                schema_version=2,
            )
            raise

        result_sha256 = _digest(value)
        await self.store.append(
            "tool_completed",
            {
                "proposal_id": proposal_id,
                "effect_id": effect_id,
                "effect_kind": effect_kind,
                "result_sha256": result_sha256,
                "artifact_refs": artifact_refs,
                "recovered": False,
            },
            proposal_id=proposal_id,
            actor_type="controller",
            schema_version=2,
        )
        return True, value

    async def record_recovered(
        self,
        *,
        proposal_id: str,
        effect_id: str,
        effect_kind: EffectKind,
        result: Any,
        artifact_refs: tuple[str, ...] = (),
    ) -> EffectCompletion:
        """外部事実を検証済みの場合だけ、再実行せず完了を追記する。"""

        records = self._records(proposal_id, effect_id)
        if not any("input_sha256" in item for item in records):
            raise EffectInDoubt(f"未開始 effect は recovered 完了にできません: {effect_id}")
        if self.completed(proposal_id, effect_id) is not None:
            raise ValueError(f"effect は既に完了しています: {effect_id}")
        digest = _digest(result)
        await self.store.append(
            "tool_completed",
            {
                "proposal_id": proposal_id,
                "effect_id": effect_id,
                "effect_kind": effect_kind,
                "result_sha256": digest,
                "artifact_refs": artifact_refs,
                "recovered": True,
            },
            proposal_id=proposal_id,
            actor_type="controller",
            schema_version=2,
        )
        return EffectCompletion(effect_id, effect_kind, digest, artifact_refs, True)


__all__ = ["EffectCompletion", "EffectInDoubt", "EffectJournal"]
