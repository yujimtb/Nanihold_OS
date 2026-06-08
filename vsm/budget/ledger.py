"""Hierarchical budget accounting."""

from __future__ import annotations

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
