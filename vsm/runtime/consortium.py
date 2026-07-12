"""階層に依存しないラウンド制 Consortium 実行プロトコル。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from vsm.agents import AgentRequest, AgentRuntimeProtocol, HumanAgent
from vsm.config import ConsortiumConfig
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.nodes import Node


class ConsortiumProtocolError(RuntimeError):
    """Consortium の応答が契約を満たさない場合に送出する。"""


class ConsortiumAborted(RuntimeError):
    """人間参加 timeout の abort policy により中止された。"""


@dataclass(frozen=True, slots=True)
class NodeParticipant:
    """Node 参照と、その Node 自身の AgentRuntime の組。"""

    node: Node
    runtime: AgentRuntimeProtocol | None


@dataclass(frozen=True, slots=True)
class ConsortiumStatement:
    participant_id: str
    participant_kind: str
    round_number: int | None
    statement: str


@dataclass(frozen=True, slots=True)
class ConsortiumDecision:
    consortium_id: str
    decision: str
    reason: str
    dissent_summary: str
    statements: tuple[ConsortiumStatement, ...]


class ContextViewHook(Protocol):
    def __call__(
        self,
        node_id: str,
        run_id: str,
        subject: str,
        recent_event_summary: str,
    ) -> str | Awaitable[str]: ...


HumanStatementWaiter = Callable[[str, float], Awaitable[str | None]]


class Consortium:
    """Node role を固定せずに合議を実行し、全遷移を Event Log に残す。"""

    def __init__(
        self,
        *,
        run_id: str,
        eventlog: EventLogWriter,
        config: ConsortiumConfig,
        context_view_hook: ContextViewHook | None = None,
        human_statement_waiter: HumanStatementWaiter | None = None,
    ) -> None:
        self._run_id = run_id
        self._eventlog = eventlog
        self._config = config
        self._context_view_hook = context_view_hook
        self._human_statement_waiter = human_statement_waiter
        self._human_futures: dict[str, asyncio.Future[str]] = {}

    async def convene(
        self,
        *,
        subject: str,
        participants: Sequence[NodeParticipant],
        convener_node_id: str,
        human: HumanAgent | None = None,
        rounds: int | None = None,
        trigger: str = "s5",
    ) -> ConsortiumDecision:
        if not subject.strip():
            raise ValueError("subject is required")
        if trigger not in {"s5", "algedonic", "human"}:
            raise ValueError("trigger must be s5, algedonic, or human")
        participant_by_id = {item.node.id: item for item in participants}
        if len(participant_by_id) != len(participants) or not participant_by_id:
            raise ValueError("participants must contain unique Node references")
        if convener_node_id not in participant_by_id:
            raise ValueError("convener_node_id must identify a participant")
        round_count = self._config.default_rounds if rounds is None else rounds
        if not isinstance(round_count, int) or isinstance(round_count, bool) or round_count < 1:
            raise ValueError("rounds must be a positive integer")
        human_id = human.human_id if human is not None else "human"

        consortium_id = generate_uuid()
        participant_ids = list(participant_by_id)
        if self._config.human_participation != "none":
            participant_ids.append(human_id)
        await self._eventlog.append(
            "consortium_convened",
            {
                "consortium_id": consortium_id,
                "subject": subject,
                "participants": participant_ids,
                "convener": convener_node_id,
                "rounds": round_count,
                "trigger": trigger,
            },
            node_id=convener_node_id,
            actor_id=convener_node_id,
        )

        statements: list[ConsortiumStatement] = []
        for round_number in range(1, round_count + 1):
            for participant in participants:
                recent_summary = self._recent_summary(statements)
                context_view = await self._build_context_view(
                    participant.node.id, subject, recent_summary
                )
                prompt = (
                    "あなたは Consortium の参加者です。件名について、判断案・根拠・"
                    "懸念を簡潔に述べてください。\n"
                    f"件名: {subject}\nラウンド: {round_number}/{round_count}"
                )
                if participant.runtime is None:
                    statement_text = (
                        f"{participant.node.vsm_position} の責務から検討する。"
                        f"件名は「{subject}」。現時点の文脈: {context_view}"
                    )
                else:
                    result = await asyncio.wait_for(
                        participant.runtime.invoke(
                            AgentRequest(prompt=prompt, context_view=context_view)
                        ),
                        timeout=participant.runtime.timeout_seconds,
                    )
                    statement_text = result.text
                if not statement_text.strip():
                    raise ConsortiumProtocolError(
                        f"participant {participant.node.id} returned an empty statement"
                    )
                statement = ConsortiumStatement(
                    participant_id=participant.node.id,
                    participant_kind="node",
                    round_number=round_number,
                    statement=statement_text,
                )
                statements.append(statement)
                await self._append_statement(consortium_id, statement)

        if self._config.human_participation != "none":
            await self._eventlog.append(
                "consortium_waiting",
                {
                    "consortium_id": consortium_id,
                    "participant_id": human_id,
                    "timeout_seconds": self._config.human_timeout_seconds,
                },
                node_id=convener_node_id,
                actor_id=convener_node_id,
            )
            human_text = await self._wait_for_human(consortium_id)
            if human_text is None:
                await self._eventlog.append(
                    "consortium_human_timeout",
                    {
                        "consortium_id": consortium_id,
                        "participant_id": human_id,
                        "policy": self._config.human_timeout_policy,
                    },
                    node_id=convener_node_id,
                    actor_id=convener_node_id,
                )
                if self._config.human_timeout_policy == "abort":
                    await self._eventlog.append(
                        "consortium_aborted",
                        {
                            "consortium_id": consortium_id,
                            "reason": "human statement timed out",
                        },
                        node_id=convener_node_id,
                        actor_id=convener_node_id,
                    )
                    raise ConsortiumAborted("human statement timed out")
            else:
                statement = ConsortiumStatement(
                    participant_id=human_id,
                    participant_kind="human",
                    round_number=None,
                    statement=human_text,
                )
                statements.append(statement)
                await self._append_statement(consortium_id, statement)

        convener = participant_by_id[convener_node_id]
        if convener.runtime is None:
            raise ConsortiumProtocolError("convener requires an AgentRuntime")
        synthesis_prompt = (
            "あなたは Consortium の招集者です。以下の発言を総合し、JSON object のみを"
            "返してください。必須キーは decision, reason, dissent_summary です。\n"
            f"件名: {subject}\n発言:\n{self._recent_summary(statements)}"
        )
        synthesis_context = await self._build_context_view(
            convener_node_id, subject, self._recent_summary(statements)
        )
        result = await asyncio.wait_for(
            convener.runtime.invoke(
                AgentRequest(prompt=synthesis_prompt, context_view=synthesis_context)
            ),
            timeout=convener.runtime.timeout_seconds,
        )
        payload = self._parse_decision(result.text)
        decision = ConsortiumDecision(
            consortium_id=consortium_id,
            decision=payload["decision"],
            reason=payload["reason"],
            dissent_summary=payload["dissent_summary"],
            statements=tuple(statements),
        )
        await self._eventlog.append(
            "consortium_decided",
            {
                "consortium_id": consortium_id,
                "decision": decision.decision,
                "reason": decision.reason,
                "dissent_summary": decision.dissent_summary,
            },
            node_id=convener_node_id,
            actor_id=convener_node_id,
        )
        return decision

    def submit_human_statement(self, consortium_id: str, statement: str) -> None:
        if not statement.strip():
            raise ValueError("statement is required")
        future = self._human_futures.get(consortium_id)
        if future is None or future.done():
            raise KeyError(f"consortium is not waiting for human: {consortium_id}")
        future.set_result(statement)

    async def _wait_for_human(self, consortium_id: str) -> str | None:
        if self._human_statement_waiter is not None:
            return await self._human_statement_waiter(
                consortium_id, self._config.human_timeout_seconds
            )
        future = asyncio.get_running_loop().create_future()
        self._human_futures[consortium_id] = future
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self._config.human_timeout_seconds
            )
        except asyncio.TimeoutError:
            return None
        finally:
            self._human_futures.pop(consortium_id, None)

    async def _build_context_view(
        self, node_id: str, subject: str, recent_event_summary: str
    ) -> str:
        if self._context_view_hook is None:
            return (
                f"Run: {self._run_id}\nNode: {node_id}\n件名: {subject}\n"
                f"直近イベント要約:\n{recent_event_summary}"
            )
        value = self._context_view_hook(
            node_id, self._run_id, subject, recent_event_summary
        )
        return await value if inspect.isawaitable(value) else value

    async def _append_statement(
        self, consortium_id: str, statement: ConsortiumStatement
    ) -> None:
        await self._eventlog.append(
            "consortium_statement",
            {
                "consortium_id": consortium_id,
                "participant_id": statement.participant_id,
                "participant_kind": statement.participant_kind,
                "round": statement.round_number,
                "statement": statement.statement,
            },
            node_id=(
                statement.participant_id
                if statement.participant_kind == "node"
                else None
            ),
            actor_type=statement.participant_kind,
            actor_id=statement.participant_id,
        )

    @staticmethod
    def _recent_summary(statements: Sequence[ConsortiumStatement]) -> str:
        if not statements:
            return "（まだ発言はありません）"
        return "\n".join(
            f"- {item.participant_id} (round={item.round_number}): {item.statement}"
            for item in statements[-12:]
        )

    @staticmethod
    def _parse_decision(text: str) -> dict[str, str]:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConsortiumProtocolError("convener response must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ConsortiumProtocolError("convener response must be a JSON object")
        result: dict[str, str] = {}
        for field in ("decision", "reason", "dissent_summary"):
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ConsortiumProtocolError(f"convener response requires non-empty {field}")
            result[field] = value
        return result


__all__ = [
    "Consortium",
    "ConsortiumAborted",
    "ConsortiumDecision",
    "ConsortiumProtocolError",
    "ConsortiumStatement",
    "ContextViewHook",
    "NodeParticipant",
]
