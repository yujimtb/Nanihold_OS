"""自己開発 Proposal の artifact layout と immutable hash 操作。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_raw_response(path: Path, text: str) -> Path:
    """LLM の生応答を不変 artifact として保存し、衝突時は別名にする。"""

    if not isinstance(text, str):
        raise TypeError("raw response は str でなければなりません")
    candidate = path
    retry_number = 0
    while candidate.exists():
        if candidate.read_text(encoding="utf-8") == text:
            return candidate
        retry_number += 1
        suffix = "-retry" if retry_number == 1 else f"-retry-{retry_number}"
        candidate = path.with_name(f"{path.stem}{suffix}{path.suffix}")
    _atomic_write(candidate, text.encode("utf-8"), immutable=True)
    return candidate


def _atomic_write(path: Path, data: bytes, *, immutable: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256_bytes(data)
    if path.exists():
        if not immutable:
            raise FileExistsError(f"immutable artifact は既に存在します: {path}")
        if sha256_file(path) != digest:
            raise ValueError(f"immutable artifact の内容が衝突しました: {path}")
        return digest
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest


class SelfDevArtifactLayout:
    """``runs/selfdev`` 以下の正規 layout。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=False)
        self.controller_dir = self.root / "controller"
        self.proposals_dir = self.root / "proposals"
        self.reports_dir = self.root / "reports"

    @property
    def events_path(self) -> Path:
        return self.controller_dir / "events.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.controller_dir / "controller.lock"

    def proposal_dir(self, proposal_id: str) -> Path:
        if not proposal_id or "/" in proposal_id or "\\" in proposal_id:
            raise ValueError("proposal_id は単一の識別子でなければなりません")
        return self.proposals_dir / proposal_id

    def proposal_manifest_path(self, proposal_id: str) -> Path:
        return self.proposal_dir(proposal_id) / "proposal.json"

    def projection_path(self, proposal_id: str) -> Path:
        return self.proposal_dir(proposal_id) / "projection.json"

    def artifacts_dir(self, proposal_id: str) -> Path:
        return self.proposal_dir(proposal_id) / "artifacts"

    def gates_dir(self, proposal_id: str) -> Path:
        return self.proposal_dir(proposal_id) / "gates"

    def audit_dir(self, proposal_id: str) -> Path:
        return self.proposal_dir(proposal_id) / "audit"

    def pr_description_path(self, proposal_id: str) -> Path:
        return self.proposal_dir(proposal_id) / "pr-description.md"

    def write_json(self, path: Path, payload: Any, *, immutable: bool = True) -> str:
        data = (canonical_json(payload) + "\n").encode("utf-8")
        return _atomic_write(path, data, immutable=immutable)

    def write_text(self, path: Path, text: str, *, immutable: bool = True) -> str:
        if not isinstance(text, str):
            raise TypeError("artifact text は str でなければなりません")
        return _atomic_write(path, text.encode("utf-8"), immutable=immutable)

    def write_proposal_manifest(self, manifest: Any) -> tuple[Path, str]:
        path = self.proposal_manifest_path(manifest.id)
        # ProposalManifest.sha256() is defined over canonical JSON without a
        # transport newline.  Keep that immutable hash identical to the
        # on-disk bytes so recovery and audit can compare one contract.
        digest = self.write_text(path, manifest.canonical_json(), immutable=True)
        if digest != manifest.sha256():
            raise ValueError("ProposalManifest の canonical hash と保存結果が一致しません")
        return path, digest

    def verify(self, path: Path, expected_sha256: str) -> None:
        actual = sha256_file(path)
        if actual != expected_sha256:
            raise ValueError(f"artifact hash mismatch: {path}")


__all__ = [
    "SelfDevArtifactLayout",
    "canonical_json",
    "sha256_bytes",
    "sha256_file",
    "write_raw_response",
]
