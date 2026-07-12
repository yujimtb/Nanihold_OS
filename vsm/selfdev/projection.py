"""Event Log だけから Proposal projection を再構築する reducer。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable

from vsm.eventlog.schema import Event, validate_event_payload
from vsm.selfdev.state_machine import PauseCause, PauseKind, ProposalAggregate, ProposalPhase


@dataclass(frozen=True, slots=True)
class ProposalProjection:
    proposal_id: str
    aggregate: ProposalAggregate
    event_ids: tuple[str, ...] = ()
    transition_event_ids: tuple[str, ...] = ()
    run_links: tuple[dict[str, Any], ...] = ()
    last_seq: int = -1

    def apply(self, event: Event | dict[str, Any]) -> "ProposalProjection":
        envelope = event if isinstance(event, Event) else Event.model_validate(event)
        validate_event_payload(
            envelope.event_type, envelope.payload, schema_version=envelope.schema_version
        )
        if envelope.correlation_id != self.proposal_id and envelope.payload.get("proposal_id") != self.proposal_id:
            raise ValueError("Proposal stream の correlation_id が一致しません")
        if envelope.seq <= self.last_seq:
            raise ValueError("Proposal projection の seq が逆行しています")
        aggregate = self.aggregate
        transition_ids = self.transition_event_ids
        run_links = self.run_links
        payload = envelope.payload
        if envelope.event_type == "proposal_state_changed":
            from_state = payload["from_state"]
            if (from_state is None and aggregate.state_version != 1) or (
                from_state is not None and ProposalPhase(from_state) is not aggregate.phase
            ):
                raise ValueError("proposal_state_changed の from_state が projection と一致しません")
            target = ProposalPhase(payload["to_state"])
            # Initial creation is the only event without a predecessor.
            if from_state is None:
                if target is not ProposalPhase.PROPOSED or aggregate.state_version != 1:
                    raise ValueError("Proposal の初期 state event が不正です")
            else:
                from vsm.selfdev.state_machine import ProposalStateMachine

                machine = ProposalStateMachine(aggregate)
                machine.transition(target, allow_while_paused=target is ProposalPhase.ABORTED)
                aggregate = machine.aggregate
            transition_ids = (*transition_ids, envelope.event_id)
        elif envelope.event_type == "proposal_pause_changed":
            if payload["action"] == "added":
                reset_at = payload.get("reset_at")
                parsed_reset = (
                    datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                    if reset_at is not None
                    else None
                )
                aggregate = aggregate.pause(
                    PauseCause(
                        pause_id=payload["pause_id"],
                        kind=PauseKind(payload["cause"]),
                        actor_type=payload.get("actor_type") or "controller",
                        actor_id=payload.get("actor_id") or envelope.actor_id or "controller",
                        pool_id=payload.get("pool_id"),
                        reset_at=parsed_reset,
                        source_event_id=payload.get("source_event_id") or envelope.event_id,
                        reason=payload["reason"],
                    )
                )
            else:
                aggregate = aggregate.resume(payload["pause_id"])
        elif envelope.event_type == "proposal_run_linked":
            run_id = payload["run_id"]
            if run_id in aggregate.implementation_run_ids:
                raise ValueError("同じ Run を二重 link できません")
            run_links = (*run_links, dict(payload))
            aggregate = replace(
                aggregate,
                active_run_id=run_id,
                implementation_run_ids=(*aggregate.implementation_run_ids, run_id),
                repair_used=aggregate.repair_used or payload["run_kind"] == "repair",
                state_version=aggregate.state_version + 1,
            )
        return replace(
            self,
            aggregate=aggregate,
            event_ids=(*self.event_ids, envelope.event_id),
            transition_event_ids=transition_ids,
            run_links=run_links,
            last_seq=envelope.seq,
        )


def replay_proposal_events(events: Iterable[Event | dict[str, Any]], proposal_id: str) -> ProposalProjection:
    projection = ProposalProjection(proposal_id=proposal_id, aggregate=ProposalAggregate())
    ordered = list(events)
    ordered.sort(key=lambda event: event.seq if isinstance(event, Event) else event["seq"])
    for event in ordered:
        projection = projection.apply(event)
    return projection


def replay_projections(events: Iterable[Event | dict[str, Any]]) -> dict[str, ProposalProjection]:
    projections: dict[str, ProposalProjection] = {}
    for event in sorted(events, key=lambda item: item.seq if isinstance(item, Event) else item["seq"]):
        payload = event.payload if isinstance(event, Event) else event["payload"]
        proposal_id = payload.get("proposal_id")
        if not proposal_id:
            continue
        projection = projections.get(proposal_id)
        if projection is None:
            projection = ProposalProjection(proposal_id, ProposalAggregate())
        projections[proposal_id] = projection.apply(event)
    return projections


__all__ = ["ProposalProjection", "replay_proposal_events", "replay_projections"]
