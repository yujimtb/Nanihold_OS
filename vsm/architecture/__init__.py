"""Architecture-layer primitives for Event_Log and projections."""

from vsm.architecture.events import EventEnvelope
from vsm.architecture.projections import ProjectionCheckpoint

__all__ = ["EventEnvelope", "ProjectionCheckpoint"]
