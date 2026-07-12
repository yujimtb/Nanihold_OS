"""自己開発 controller の lock と起動時 reconcile。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vsm.selfdev.artifacts import SelfDevArtifactLayout, sha256_file
from vsm.selfdev.models import ProposalManifest
from vsm.selfdev.state_machine import TERMINAL_PHASES
from vsm.selfdev.store import SelfDevEventStore

try:  # Linux/WSL は設計上 fcntl lock を使う。
    import fcntl
except ImportError:  # pragma: no cover - Windows import safety
    fcntl = None  # type: ignore[assignment]

try:  # Windows ホスト(実CLI配備先)は msvcrt の region lock で同等の排他を取る。
    import msvcrt
except ImportError:  # pragma: no cover - POSIX import safety
    msvcrt = None  # type: ignore[assignment]


class RecoveryError(RuntimeError):
    """Event Log または immutable artifact の復元に失敗した。"""


class ControllerLease:
    """process lifetime の controller.lock。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    def acquire(self) -> None:
        if fcntl is None and msvcrt is None:
            raise RuntimeError("selfdev controller は OS のファイルロック(fcntl/msvcrt)が必要です")
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError(f"controller.lock を取得できません: {self.path}") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        if fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "ControllerLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    projections: dict[str, Any]
    active_proposal_id: str | None
    in_doubt_effects: tuple[tuple[str, str], ...]


class ControllerRecovery:
    """strict Event Log / manifest / effect journal の起動検証。"""

    def __init__(self, store: SelfDevEventStore) -> None:
        self.store = store
        self.layout: SelfDevArtifactLayout = store.layout

    def reconcile(self) -> RecoverySnapshot:
        try:
            events = self.store.read_events()
            projections = self.store.replay()
        except Exception as exc:
            raise RecoveryError(f"selfdev Event Log の strict recovery に失敗しました: {exc}") from exc

        active = [
            projection
            for projection in projections.values()
            if projection.aggregate.phase not in TERMINAL_PHASES
        ]
        if len(active) > 1:
            raise RecoveryError("非terminal Proposal は同時に1件だけ許可されます")

        for proposal_id, projection in projections.items():
            manifest_path = self.layout.proposal_manifest_path(proposal_id)
            if not manifest_path.exists():
                raise RecoveryError(f"ProposalManifest がありません: {proposal_id}")
            try:
                manifest = ProposalManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RecoveryError(f"ProposalManifest が不正です: {manifest_path}") from exc
            if manifest.id != proposal_id:
                raise RecoveryError(f"ProposalManifest の id が不一致です: {proposal_id}")
            expected = manifest.sha256()
            if sha256_file(manifest_path) != expected:
                raise RecoveryError(f"ProposalManifest の hash が不一致です: {manifest_path}")

        for event in events:
            if event.event_type != "artifact_created":
                continue
            proposal_id = str(event.payload["proposal_id"])
            path = self.layout.proposal_dir(proposal_id) / str(event.payload["ref"])
            if not path.is_file():
                raise RecoveryError(f"immutable artifact がありません: {path}")
            if sha256_file(path) != event.payload["sha256"]:
                raise RecoveryError(f"immutable artifact の hash が不一致です: {path}")

        starts: dict[tuple[str, str], bool] = {}
        completed: set[tuple[str, str]] = set()
        for event in events:
            payload = event.payload
            proposal_id = payload.get("proposal_id")
            effect_id = payload.get("effect_id")
            if not proposal_id or not effect_id:
                continue
            key = (str(proposal_id), str(effect_id))
            if event.event_type == "tool_invoked":
                starts[key] = True
            elif event.event_type == "tool_completed":
                completed.add(key)
        in_doubt = tuple(sorted(key for key in starts if key not in completed))
        return RecoverySnapshot(
            projections=projections,
            active_proposal_id=active[0].proposal_id if active else None,
            in_doubt_effects=in_doubt,
        )


__all__ = ["ControllerLease", "ControllerRecovery", "RecoveryError", "RecoverySnapshot"]
