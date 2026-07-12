"""Gate 入力、scope、protected approval を検証する trusted 境界。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

from vsm.errors import GateError
from vsm.selfdev.models import is_protected_path

REQUIRED_GATES: tuple[str, ...] = ("g1", "g2", "g3", "g4")

__all__ = [
    "ProtectedApproval",
    "REQUIRED_GATES",
    "ScopeCheckResult",
    "canonical_scope",
    "scope_sha256",
    "verify_protected_approval",
    "verify_scope",
]


def canonical_scope(scope: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    normalized: list[dict[str, str]] = []
    for rule in scope:
        if set(rule) != {"path", "kind"}:
            raise GateError("scope の PathRule は path/kind のみを持たなければなりません")
        if not isinstance(rule["path"], str) or not isinstance(rule["kind"], str):
            raise GateError("scope の path/kind は文字列でなければなりません")
        path = rule["path"].replace("\\", "/")
        if not path or path.startswith("/") or "\x00" in path:
            raise GateError(f"scope path が不正です: {path!r}")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise GateError(f"scope path が不正です: {path!r}")
        kind = rule["kind"]
        if kind not in {"file", "tree"}:
            raise GateError(f"scope kind が不正です: {kind!r}")
        normalized.append({"path": path.rstrip("/"), "kind": kind})
    if not normalized:
        raise GateError("scope は1件以上必要です")
    return tuple(normalized)


def scope_sha256(scope: Sequence[Mapping[str, Any]]) -> str:
    data = json.dumps(
        list(canonical_scope(scope)), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _in_scope(path: str, scope: Sequence[Mapping[str, Any]]) -> bool:
    value = path.replace("\\", "/")
    if not value or PurePosixPath(value).is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise GateError(f"changed path が不正です: {path!r}")
    for rule in canonical_scope(scope):
        if value == rule["path"] or (rule["kind"] == "tree" and value.startswith(rule["path"] + "/")):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ProtectedApproval:
    event_id: str
    proposal_manifest_sha256: str
    protected_scope_sha256: str

    def __post_init__(self) -> None:
        if not self.event_id:
            raise GateError("protected approval の event_id は必須です")
        for name in ("proposal_manifest_sha256", "protected_scope_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", getattr(self, name)):
                raise GateError(f"protected approval の {name} が不正です")

    @classmethod
    def from_value(cls, value: "ProtectedApproval | Mapping[str, Any] | None") -> "ProtectedApproval | None":
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise GateError("protected approval は object でなければなりません")
        if set(value) != {"event_id", "proposal_manifest_sha256", "protected_scope_sha256"}:
            raise GateError("protected approval の field が不正です")
        return cls(
            event_id=str(value["event_id"]),
            proposal_manifest_sha256=str(value["proposal_manifest_sha256"]),
            protected_scope_sha256=str(value["protected_scope_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class ScopeCheckResult:
    changed_paths: tuple[str, ...]
    outside_scope_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.outside_scope_paths and not self.protected_paths


def verify_scope(
    changed_paths: Sequence[str],
    scope: Sequence[Mapping[str, Any]],
    *,
    protected_approval: ProtectedApproval | Mapping[str, Any] | None = None,
    proposal_manifest_sha256: str | None = None,
    protected_scope_sha256: str | None = None,
    risk_class: str | None = None,
) -> ScopeCheckResult:
    normalized_scope = canonical_scope(scope)
    if any(not isinstance(path, str) for path in changed_paths):
        raise GateError("changed path は文字列でなければなりません")
    paths = tuple(sorted(set(path.replace("\\", "/") for path in changed_paths)))
    outside = tuple(path for path in paths if not _in_scope(path, normalized_scope))
    protected = tuple(path for path in paths if is_protected_path(path))
    approval = ProtectedApproval.from_value(protected_approval)
    authorized = not protected
    if protected:
        if risk_class != "protected":
            authorized = False
        elif approval is None:
            authorized = False
        elif not proposal_manifest_sha256 or not protected_scope_sha256:
            authorized = False
        else:
            authorized = (
                approval.proposal_manifest_sha256 == proposal_manifest_sha256
                and approval.protected_scope_sha256 == protected_scope_sha256
                and bool(approval.event_id)
            )
    return ScopeCheckResult(
        changed_paths=paths,
        outside_scope_paths=outside,
        protected_paths=() if authorized else protected,
    )


def verify_protected_approval(
    *,
    changed_paths: Sequence[str],
    scope: Sequence[Mapping[str, Any]],
    risk_class: str,
    proposal_manifest_sha256: str,
    protected_scope_sha256: str,
    approval: ProtectedApproval | Mapping[str, Any] | None,
) -> str | None:
    """変更された protected path に対する event id を返す。"""

    result = verify_scope(
        changed_paths,
        scope,
        protected_approval=approval,
        proposal_manifest_sha256=proposal_manifest_sha256,
        protected_scope_sha256=protected_scope_sha256,
        risk_class=risk_class,
    )
    if result.outside_scope_paths:
        raise GateError("scope 外の変更があります: " + ", ".join(result.outside_scope_paths))
    if result.protected_paths:
        raise GateError("protected approval が不正または不足しています")
    parsed = ProtectedApproval.from_value(approval)
    return parsed.event_id if parsed is not None and any(is_protected_path(path) for path in result.changed_paths) else None
