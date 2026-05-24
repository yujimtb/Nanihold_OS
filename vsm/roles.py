"""VSM System role identifiers.

This module hosts the canonical :class:`SystemRole` enum that names every
System in the Viable System Model that the PoC platform represents
(REQ 1.1). It lives in a small, leaf-level module so that both
:mod:`vsm.config` and :mod:`vsm.messaging.channels` can import it without
creating a circular dependency between configuration loading and the
messaging / channel layer.

References
----------
- REQ 1.1: The VSM_Platform SHALL provide distinct software components,
  each with a unique role identifier, for each of S1_Worker,
  S2_Coordinator, S3_Allocator, S3Star_Auditor, S4_Scanner, and S5_Policy.
- design.md `## Components and Interfaces` §1: ``SystemRole`` is an Enum
  with members ``S1_WORKER``, ``S2_COORDINATOR``, ``S3_ALLOCATOR``,
  ``S3STAR_AUDITOR``, ``S4_SCANNER``, ``S5_POLICY``.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["SystemRole", "MANDATORY_ROLES"]


class SystemRole(str, Enum):
    """Identifier for each VSM System that the platform instantiates.

    Members
    -------
    S1_WORKER
        VSM System 1 — environment-facing worker. Multiple instances may
        be created dynamically by S3_Allocator (REQ 1.3, 13.6).
    S2_COORDINATOR
        VSM System 2 — coordinates S1_Worker instances to prevent
        oscillation / collision (REQ 8). Mandatory at Run start.
    S3_ALLOCATOR
        VSM System 3 — allocates resources to S1_Worker instances and
        decides their specialization / count (REQ 7). Mandatory at Run
        start.
    S3STAR_AUDITOR
        VSM System 3* (S3-star) — performs direct audit of S1_Worker
        instances bypassing S3_Allocator (REQ 9). Mandatory at Run start.
    S4_SCANNER
        VSM System 4 — scans the external environment for opportunities
        and threats (REQ 5). Mandatory at Run start.
    S5_POLICY
        VSM System 5 — produces top-level policy and balances S3 and S4
        (REQ 6). Mandatory at Run start.

    The string values are stable and human-readable so that they can be
    used directly as Event_Log payload fields and CLI ``--system`` filter
    arguments without further translation.
    """

    S1_WORKER = "S1_WORKER"
    S2_COORDINATOR = "S2_COORDINATOR"
    S3_ALLOCATOR = "S3_ALLOCATOR"
    S3STAR_AUDITOR = "S3STAR_AUDITOR"
    S4_SCANNER = "S4_SCANNER"
    S5_POLICY = "S5_POLICY"


#: The set of roles that MUST have at least one configured instance before
#: a Run is allowed to transition to the active state (REQ 1.2, 13.1).
#: S1_WORKER is intentionally excluded because REQ 13.5 permits an initial
#: S1 count of zero.
MANDATORY_ROLES: frozenset[SystemRole] = frozenset(
    {
        SystemRole.S2_COORDINATOR,
        SystemRole.S3_ALLOCATOR,
        SystemRole.S3STAR_AUDITOR,
        SystemRole.S4_SCANNER,
        SystemRole.S5_POLICY,
    }
)
