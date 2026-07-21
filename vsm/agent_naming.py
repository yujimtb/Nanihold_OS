from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from vsm.errors import InvariantViolation
from vsm.kernel.models import Identifier, NonBlank, StrictModel

if TYPE_CHECKING:
    from vsm.pilot.models import ModelCandidate


NameColumn = Literal["日", "英", "羅"]
DEFAULT_AGENT_NAME_CSV = Path(
    r"D:\userdata\docs\projects\_cutover_20260720_fable_activation\asset\Agent_name.csv"
)
RESERVED_AGENT_NAME = "Nagi"
RESERVED_AGENT_NAME_NODE = "node:owner-interface"


class AgentNameRow(StrictModel):
    category: NonBlank
    scale: Literal[1, 2, 3]
    semantic_coordinate: NonBlank
    japanese_name: NonBlank
    english_name: NonBlank
    latin_name: NonBlank
    likes: Literal["", "0", "1"]

    @property
    def is_eligible(self) -> bool:
        return self.likes != "0"

    @property
    def is_reserved(self) -> bool:
        return (
            self.japanese_name == RESERVED_AGENT_NAME
            or self.semantic_coordinate == "凪"
        )

    def name_for(self, column: NameColumn) -> str:
        return {
            "日": self.japanese_name,
            "英": self.english_name,
            "羅": self.latin_name,
        }[column]


class AgentNameAssignment(StrictModel):
    assignment_id: Identifier
    data_space_id: Identifier
    work_item_id: Identifier
    execution_id: Identifier
    node_id: Identifier
    pilot_id: Identifier
    agent_name: NonBlank
    base_name: NonBlank
    suffix: int = Field(ge=1)
    name_column: NameColumn
    scale: Literal[1, 2, 3]
    provider: NonBlank
    model_candidate_key: NonBlank

    @model_validator(mode="after")
    def suffix_matches_name(self) -> "AgentNameAssignment":
        expected = (
            self.base_name
            if self.suffix == 1
            else f"{self.base_name}{self.suffix}"
        )
        if self.agent_name != expected:
            raise ValueError("AgentNameAssignment agent_name does not match suffix")
        return self


class AgentIdentityRegistration(StrictModel):
    """A registry-issued identity for an agent that has no WorkItem yet."""

    registration_id: Identifier
    data_space_id: Identifier
    node_id: Identifier
    pilot_id: Identifier
    agent_name: NonBlank
    base_name: NonBlank
    suffix: int = Field(ge=1)
    name_column: NameColumn
    scale: Literal[1, 2, 3]
    provider: NonBlank
    model_candidate_key: NonBlank
    source: Literal["out_of_pipeline"]

    @model_validator(mode="after")
    def suffix_matches_name(self) -> "AgentIdentityRegistration":
        expected = (
            self.base_name
            if self.suffix == 1
            else f"{self.base_name}{self.suffix}"
        )
        if self.agent_name != expected:
            raise ValueError(
                "AgentIdentityRegistration agent_name does not match suffix"
            )
        return self


def _provider_family(candidate: ModelCandidate) -> Literal["claude", "gpt", "other"]:
    provider = candidate.provider.strip().lower()
    snapshot = (candidate.model_snapshot or "").strip().lower()
    if provider in {"anthropic", "claude"} or "claude" in snapshot:
        return "claude"
    if provider in {"openai", "gpt"} or snapshot.startswith("gpt-"):
        return "gpt"
    return "other"


def _scale_for(candidate: ModelCandidate) -> Literal[1, 2, 3]:
    model = (candidate.model_snapshot or "").strip().lower()
    if not model:
        raise InvariantViolation(
            "agent name assignment requires a model snapshot to determine model scale"
        )
    if "sol" in model or "fable" in model:
        return 3
    if "terra" in model or "opus" in model:
        return 2
    if "luna" in model or "sonnet" in model or "haiku" in model:
        return 1
    raise InvariantViolation(
        "agent name assignment cannot determine model scale: "
        f"{candidate.model_snapshot}"
    )


def _name_column(candidate: ModelCandidate) -> NameColumn:
    return {"claude": "日", "gpt": "英", "other": "羅"}[_provider_family(candidate)]


class AgentNameRegistry:
    """CSV-backed, deterministic, task-scoped agent-name allocator."""

    _required_columns = ("カテゴリ", "規模", "意味座標", "日", "英", "羅", "いいね")

    def __init__(self, rows: Iterable[AgentNameRow]) -> None:
        materialized = tuple(rows)
        if not materialized:
            raise InvariantViolation("Agent_name.csv must contain at least one row")
        self._rows = materialized
        self._next_index: dict[tuple[int, NameColumn], int] = defaultdict(int)
        self._used_names: set[str] = set()
        self._assignments: dict[str, AgentNameAssignment] = {}
        self._registrations: dict[str, AgentIdentityRegistration] = {}
        self._lock = RLock()

    @classmethod
    def from_csv(cls, path: Path) -> "AgentNameRegistry":
        if not path.is_file():
            raise InvariantViolation(f"Agent_name.csv not found: {path}")
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if tuple(reader.fieldnames or ()) != cls._required_columns:
                    raise InvariantViolation(
                        "Agent_name.csv columns must be exactly: "
                        + ",".join(cls._required_columns)
                    )
                raw_rows = list(reader)
        except UnicodeError as exc:
            raise InvariantViolation("Agent_name.csv must be UTF-8 encoded") from exc
        rows: list[AgentNameRow] = []
        for index, raw in enumerate(raw_rows, start=2):
            try:
                rows.append(
                    AgentNameRow(
                        category=(raw["カテゴリ"] or "").strip(),
                        scale=int((raw["規模"] or "").strip()),
                        semantic_coordinate=(raw["意味座標"] or "").strip(),
                        japanese_name=(raw["日"] or "").strip(),
                        english_name=(raw["英"] or "").strip(),
                        latin_name=(raw["羅"] or "").strip(),
                        likes=(raw["いいね"] or "").strip(),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise InvariantViolation(
                    f"Agent_name.csv row {index} is invalid"
                ) from exc
        return cls(rows)

    @classmethod
    def from_csv_default(cls) -> "AgentNameRegistry":
        return cls.from_csv(DEFAULT_AGENT_NAME_CSV)

    @property
    def rows(self) -> tuple[AgentNameRow, ...]:
        return self._rows

    @property
    def assignments(self) -> tuple[AgentNameAssignment, ...]:
        with self._lock:
            return tuple(self._assignments.values())

    @property
    def registrations(self) -> tuple[AgentIdentityRegistration, ...]:
        with self._lock:
            return tuple(self._registrations.values())

    def restore(self, assignments: Iterable[AgentNameAssignment]) -> None:
        """Restore names already recorded in Ledger before accepting dispatches."""
        restored = tuple(assignments)
        with self._lock:
            for assignment in restored:
                if assignment.assignment_id in self._assignments:
                    if self._assignments[assignment.assignment_id] != assignment:
                        raise InvariantViolation(
                            "agent-name assignment collision: "
                            f"{assignment.assignment_id}"
                        )
                    continue
                if assignment.agent_name in self._used_names:
                    raise InvariantViolation(
                        "agent name is assigned more than once: "
                        f"{assignment.agent_name}"
                    )
                matching_rows = [
                    row
                    for row in self._rows
                    if row.scale == assignment.scale
                    and row.name_for(assignment.name_column) == assignment.base_name
                ]
                if len(matching_rows) != 1:
                    raise InvariantViolation(
                        f"assignment base name is not present in Agent_name.csv: "
                        f"{assignment.base_name}"
                    )
                row = matching_rows[0]
                if not row.is_eligible or row.is_reserved:
                    raise InvariantViolation(
                        "assignment uses a forbidden Agent_name.csv row: "
                        f"{assignment.base_name}"
                    )
                self._assignments[assignment.assignment_id] = assignment
                self._used_names.add(assignment.agent_name)

    def restore_registrations(
        self, registrations: Iterable[AgentIdentityRegistration]
    ) -> None:
        """Restore identities recorded for agents outside WorkItem dispatch."""

        restored = tuple(registrations)
        with self._lock:
            for registration in restored:
                if registration.registration_id in self._registrations:
                    if self._registrations[registration.registration_id] != registration:
                        raise InvariantViolation(
                            "agent identity registration collision: "
                            f"{registration.registration_id}"
                        )
                    continue
                if registration.agent_name in self._used_names:
                    raise InvariantViolation(
                        "agent name is assigned more than once: "
                        f"{registration.agent_name}"
                    )
                matching_rows = [
                    row
                    for row in self._rows
                    if row.scale == registration.scale
                    and row.name_for(registration.name_column)
                    == registration.base_name
                ]
                if len(matching_rows) != 1:
                    raise InvariantViolation(
                        "registration base name is not present in Agent_name.csv: "
                        f"{registration.base_name}"
                    )
                row = matching_rows[0]
                if not row.is_eligible or row.is_reserved:
                    raise InvariantViolation(
                        "registration uses a forbidden Agent_name.csv row: "
                        f"{registration.base_name}"
                    )
                self._registrations[registration.registration_id] = registration
                self._used_names.add(registration.agent_name)

    def allocate(
        self,
        *,
        assignment_id: str,
        data_space_id: str,
        work_item_id: str,
        execution_id: str,
        node_id: str,
        pilot_id: str,
        candidate: ModelCandidate,
    ) -> AgentNameAssignment:
        scale = _scale_for(candidate)
        column = _name_column(candidate)
        pool = tuple(
            row
            for row in self._rows
            if row.scale == scale and row.is_eligible and not row.is_reserved
        )
        if not pool:
            raise InvariantViolation(
                "Agent_name.csv has no eligible names for scale "
                f"{scale} and column {column}"
            )
        with self._lock:
            if assignment_id in self._assignments:
                raise InvariantViolation(
                    "agent-name assignment already exists: "
                    f"{assignment_id}"
                )
            base_name, suffix, agent_name = self._next_name(
                scale=scale,
                column=column,
                pool=pool,
            )
            assignment = AgentNameAssignment(
                assignment_id=assignment_id,
                data_space_id=data_space_id,
                work_item_id=work_item_id,
                execution_id=execution_id,
                node_id=node_id,
                pilot_id=pilot_id,
                agent_name=agent_name,
                base_name=base_name,
                suffix=suffix,
                name_column=column,
                scale=scale,
                provider=candidate.provider,
                model_candidate_key=candidate.key,
            )
            self._assignments[assignment_id] = assignment
            self._used_names.add(agent_name)
            return assignment

    def allocate_out_of_pipeline(
        self,
        *,
        registration_id: str,
        data_space_id: str,
        node_id: str,
        pilot_id: str,
        candidate: ModelCandidate,
    ) -> AgentIdentityRegistration:
        """Issue a collision-free name before an agent enters WorkItem flow."""

        scale = _scale_for(candidate)
        column = _name_column(candidate)
        pool = tuple(
            row
            for row in self._rows
            if row.scale == scale and row.is_eligible and not row.is_reserved
        )
        if not pool:
            raise InvariantViolation(
                "Agent_name.csv has no eligible names for scale "
                f"{scale} and column {column}"
            )
        with self._lock:
            if registration_id in self._registrations:
                raise InvariantViolation(
                    "agent identity registration already exists: "
                    f"{registration_id}"
                )
            if registration_id in self._assignments:
                raise InvariantViolation(
                    "registration id collides with an agent-name assignment: "
                    f"{registration_id}"
                )
            base_name, suffix, agent_name = self._next_name(
                scale=scale,
                column=column,
                pool=pool,
            )
            registration = AgentIdentityRegistration(
                registration_id=registration_id,
                data_space_id=data_space_id,
                node_id=node_id,
                pilot_id=pilot_id,
                agent_name=agent_name,
                base_name=base_name,
                suffix=suffix,
                name_column=column,
                scale=scale,
                provider=candidate.provider,
                model_candidate_key=candidate.key,
                source="out_of_pipeline",
            )
            self._registrations[registration_id] = registration
            self._used_names.add(agent_name)
            return registration

    def _next_name(
        self,
        *,
        scale: Literal[1, 2, 3],
        column: NameColumn,
        pool: tuple[AgentNameRow, ...],
    ) -> tuple[str, int, str]:
        start = self._next_index[(scale, column)] % len(pool)
        selected: AgentNameRow | None = None
        selected_index = start
        for offset in range(len(pool)):
            index = (start + offset) % len(pool)
            row = pool[index]
            if row.name_for(column) not in self._used_names:
                selected = row
                selected_index = index
                break
        if selected is None:
            selected_index = start
            selected = pool[selected_index]
        base_name = selected.name_for(column)
        suffix = 1
        agent_name = base_name
        while agent_name in self._used_names:
            suffix += 1
            agent_name = f"{base_name}{suffix}"
        self._next_index[(scale, column)] = selected_index + 1
        return base_name, suffix, agent_name

    def assignment_for_name(self, agent_name: str) -> AgentNameAssignment:
        with self._lock:
            matches = [
                assignment
                for assignment in self._assignments.values()
                if assignment.agent_name == agent_name
            ]
        if len(matches) != 1:
            raise InvariantViolation(f"agent name is not assigned: {agent_name}")
        return matches[0]

    def assignment_for_id(self, assignment_id: str) -> AgentNameAssignment:
        with self._lock:
            assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise InvariantViolation(
                f"agent-name assignment is not present: {assignment_id}"
            )
        return assignment

    def registration_for_id(self, registration_id: str) -> AgentIdentityRegistration:
        with self._lock:
            registration = self._registrations.get(registration_id)
        if registration is None:
            raise InvariantViolation(
                f"agent identity registration is not present: {registration_id}"
            )
        return registration

    def is_name_registered(self, agent_name: str) -> bool:
        with self._lock:
            return agent_name in self._used_names


def assignment_from_payload(payload: Mapping[str, object]) -> AgentNameAssignment:
    raw = payload.get("assignment")
    if not isinstance(raw, Mapping):
        raise InvariantViolation("agent-name assignment event payload is invalid")
    return AgentNameAssignment.model_validate(raw)
