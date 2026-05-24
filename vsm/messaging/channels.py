"""Channel definitions and the route allow-list for the VSM Message_Bus.

This module is the authoritative source of:

* :class:`ChannelId` -- the seven inter-system channels of the VSM PoC
  (REQ 2.1〜2.6, 9.5).
* :data:`ALLOWED_ROUTES` -- the immutable set of ``(sender_role, receiver_role,
  channel)`` triples that the :class:`vsm.messaging.bus.MessageBus` is allowed
  to deliver. Any tuple **not** in this set MUST be rejected (REQ 2.7).
* :func:`is_allowed` -- O(1) membership predicate used by both the bus and the
  property-based tests in :mod:`tests.property.test_message_bus`.

Design notes
------------
* The S3* (S3_Auditor) channels ``S3STAR_TO_S1`` and ``S3STAR_S5_AUDIT`` are
  **unidirectional** (REQ 2.6, 9.1, 9.5). The auditor speaks; nobody speaks
  back through these channels. This is encoded by listing the route in only
  one direction in :data:`ALLOWED_ROUTES`.
* All other inter-system channels are bilateral, so each appears as two tuples
  (one per direction).
* :data:`ALLOWED_ROUTES` is a :class:`frozenset` so that membership tests are
  O(1) and the table cannot be mutated at runtime.

References:
    - REQ 2.1: S1 ↔ S2 channel
    - REQ 2.2: S1 ↔ S3 channel
    - REQ 2.3: S3 ↔ S4 channel
    - REQ 2.4: S3 ↔ S5 channel
    - REQ 2.5: S4 ↔ S5 channel
    - REQ 2.6: S3* → S1 channel (unidirectional)
    - REQ 9.1: S3* operates independently of S3_Allocator
    - REQ 9.5: S3* → S5 audit channel (unidirectional)
"""

from __future__ import annotations

from enum import Enum

from vsm.roles import SystemRole


class ChannelId(str, Enum):
    """Identifiers for the seven inter-system channels of the VSM PoC.

    Values match the canonical wire-format strings from ``design.md`` so that
    the Event_Log and external tooling can compare channel identities by their
    string value alone.
    """

    S1_S2 = "S1-S2"  # REQ 2.1
    S1_S3 = "S1-S3"  # REQ 2.2
    S3_S4 = "S3-S4"  # REQ 2.3
    S3_S5 = "S3-S5"  # REQ 2.4
    S4_S5 = "S4-S5"  # REQ 2.5
    S3STAR_TO_S1 = "S3*->S1"  # REQ 2.6, 9.1 (unidirectional)
    S3STAR_S5_AUDIT = "S3*->S5(audit)"  # REQ 9.5 (unidirectional)


# Local aliases for readability of the route table below.
_S1 = SystemRole.S1_WORKER
_S2 = SystemRole.S2_COORDINATOR
_S3 = SystemRole.S3_ALLOCATOR
_S3STAR = SystemRole.S3STAR_AUDITOR
_S4 = SystemRole.S4_SCANNER
_S5 = SystemRole.S5_POLICY


ALLOWED_ROUTES: frozenset[tuple[SystemRole, SystemRole, ChannelId]] = frozenset(
    {
        # S1 ↔ S2 (REQ 2.1)
        (_S1, _S2, ChannelId.S1_S2),
        (_S2, _S1, ChannelId.S1_S2),
        # S1 ↔ S3 (REQ 2.2)
        (_S1, _S3, ChannelId.S1_S3),
        (_S3, _S1, ChannelId.S1_S3),
        # S3 ↔ S4 (REQ 2.3)
        (_S3, _S4, ChannelId.S3_S4),
        (_S4, _S3, ChannelId.S3_S4),
        # S3 ↔ S5 (REQ 2.4)
        (_S3, _S5, ChannelId.S3_S5),
        (_S5, _S3, ChannelId.S3_S5),
        # S4 ↔ S5 (REQ 2.5)
        (_S4, _S5, ChannelId.S4_S5),
        (_S5, _S4, ChannelId.S4_S5),
        # S3* → S1 (REQ 2.6, 9.1) -- unidirectional
        (_S3STAR, _S1, ChannelId.S3STAR_TO_S1),
        # S3* → S5 audit (REQ 9.5) -- unidirectional
        (_S3STAR, _S5, ChannelId.S3STAR_S5_AUDIT),
    }
)
"""Immutable allow-list of ``(sender_role, receiver_role, channel)`` triples.

Exactly 12 routes: 10 bilateral entries (5 channels × 2 directions) plus 2
unidirectional S3*-originated entries (REQ 2.6, 9.5)."""


def is_allowed(
    sender_role: SystemRole,
    receiver_role: SystemRole,
    channel: ChannelId,
) -> bool:
    """Return ``True`` iff the given route is in :data:`ALLOWED_ROUTES`.

    This is the single source of truth used by :class:`MessageBus.send` to
    enforce REQ 2.7 (channel rejection) and by property tests validating
    REQ 2.1〜2.6 / 9.5.

    The implementation is a plain ``in`` check against a :class:`frozenset`,
    so it runs in O(1) average time and is safe to call from hot paths.

    Args:
        sender_role: The role of the sending system.
        receiver_role: The role of the receiving system.
        channel: The channel the message is being sent on.

    Returns:
        ``True`` if the triple ``(sender_role, receiver_role, channel)`` is a
        member of :data:`ALLOWED_ROUTES`; ``False`` otherwise.
    """
    return (sender_role, receiver_role, channel) in ALLOWED_ROUTES


__all__ = ["ChannelId", "ALLOWED_ROUTES", "is_allowed"]
