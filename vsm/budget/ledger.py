"""Hierarchical budget accounting and conservative invocation admission."""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class BudgetContext:
    token: float = 0.0
    wall_clock_time: float = 0.0
    external_api_cost: float = 0.0
    human_time: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "token": self.token,
            "wall_clock_time": self.wall_clock_time,
            "external_api_cost": self.external_api_cost,
            "human_time": self.human_time,
        }


@dataclass
class BudgetLedger:
    remaining: BudgetContext
    consumed: BudgetContext = field(default_factory=BudgetContext)

    def consume(self, **amounts: float) -> None:
        for key, amount in amounts.items():
            if amount < 0:
                raise ValueError("budget consumption must be non-negative")
            current = getattr(self.remaining, key)
            if amount > current:
                raise RuntimeError(f"budget exceeded for {key}")
            setattr(self.remaining, key, current - amount)
            setattr(self.consumed, key, getattr(self.consumed, key) + amount)


@dataclass(frozen=True, slots=True)
class InvocationEstimate:
    """単一 LLM invocation を開始する前に予約する保守的な利用量。"""

    tokens: int
    wall_clock_seconds: float


@dataclass(slots=True)
class InvocationBudgetGuard:
    """直近実績と設定済み初期値から invocation の admission を判定する。

    Node ごとの直近1回の実績と初期見積の大きい方へ安全倍率を掛ける。
    履歴が無い最初の呼び出しも、設定済み初期値を予約できない限り開始しない。
    """

    initial_tokens: int
    initial_wall_clock_seconds: float
    safety_multiplier: float
    _recent: dict[str, tuple[int, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.initial_tokens, int)
            or isinstance(self.initial_tokens, bool)
            or self.initial_tokens <= 0
        ):
            raise ValueError("initial_tokens must be a positive integer")
        if (
            not isinstance(self.initial_wall_clock_seconds, (int, float))
            or isinstance(self.initial_wall_clock_seconds, bool)
            or self.initial_wall_clock_seconds <= 0
        ):
            raise ValueError("initial_wall_clock_seconds must be positive")
        if (
            not isinstance(self.safety_multiplier, (int, float))
            or isinstance(self.safety_multiplier, bool)
            or self.safety_multiplier < 1
        ):
            raise ValueError("safety_multiplier must be at least 1")

    def estimate(self, node_id: str) -> InvocationEstimate:
        recent_tokens, recent_wall_seconds = self._recent.get(node_id, (0, 0.0))
        return InvocationEstimate(
            tokens=math.ceil(
                max(self.initial_tokens, recent_tokens) * self.safety_multiplier
            ),
            wall_clock_seconds=(
                max(self.initial_wall_clock_seconds, recent_wall_seconds)
                * self.safety_multiplier
            ),
        )

    def record(self, node_id: str, *, tokens: int, wall_clock_seconds: float) -> None:
        if tokens < 0 or wall_clock_seconds < 0:
            raise ValueError("invocation usage must be non-negative")
        self._recent[node_id] = (tokens, wall_clock_seconds)

    @staticmethod
    def rejection_reasons(
        estimate: InvocationEstimate,
        *,
        node_remaining_tokens: float,
        node_remaining_wall_clock_seconds: float,
        run_remaining_tokens: float,
        run_remaining_wall_clock_seconds: float,
    ) -> list[str]:
        """見積量を予約できない budget scope 名を返す。"""

        reasons: list[str] = []
        if node_remaining_tokens < estimate.tokens:
            reasons.append("node_tokens")
        if node_remaining_wall_clock_seconds < estimate.wall_clock_seconds:
            reasons.append("node_wall_clock")
        if run_remaining_tokens < estimate.tokens:
            reasons.append("run_tokens")
        if run_remaining_wall_clock_seconds < estimate.wall_clock_seconds:
            reasons.append("run_wall_clock")
        return reasons
