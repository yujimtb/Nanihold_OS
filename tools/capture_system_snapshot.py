"""Capture a bounded, secret-free cutover snapshot from explicit sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from tools.history_source_export import HistorySourceExportError


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HistorySourceExportError(
            f"{label} requires exactly: {', '.join(sorted(fields))}"
        )
    return value


def _nonblank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistorySourceExportError(f"{label} must be a non-empty string")
    return value


def _parse_time(value: object) -> datetime:
    text = _nonblank(value, "captured_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistorySourceExportError("captured_at is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise HistorySourceExportError("captured_at must include a timezone")
    return parsed.astimezone(UTC)


def _run_git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        shell=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise HistorySourceExportError(
            f"git inventory failed for {path}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _git_state(item: dict[str, Any]) -> dict[str, object]:
    item = _exact_object(item, {"id", "path"}, "repository")
    identifier = _nonblank(item["id"], "repository id")
    path = Path(_nonblank(item["path"], "repository path"))
    if not path.is_absolute() or not path.is_dir():
        raise HistorySourceExportError(
            f"repository path must be an existing absolute directory: {path}"
        )
    head = _run_git(path, "rev-parse", "HEAD").strip()
    branch = _run_git(path, "branch", "--show-current").strip()
    porcelain = _run_git(path, "status", "--porcelain=v1", "-z")
    entries = tuple(part for part in porcelain.split("\0") if part)
    return {
        "state_key": f"git:{identifier}",
        "text": (
            f"{identifier}: {branch or '(detached)'} @ {head[:12]}, "
            f"変更 {len(entries)} 件"
        ),
        "value": {
            "repository_id": identifier,
            "path": str(path.resolve()),
            "head": head,
            "branch": branch or None,
            "status_entries": entries,
        },
    }


def _select_fields(document: object, fields: list[str], label: str) -> dict[str, object]:
    if not isinstance(document, dict):
        raise HistorySourceExportError(f"{label} response must be a JSON object")
    selected: dict[str, object] = {}
    for field in fields:
        if field not in document:
            raise HistorySourceExportError(
                f"{label} response is missing selected field: {field}"
            )
        selected[field] = document[field]
    return selected


def _endpoint_state(
    item: dict[str, Any],
    *,
    request: Callable[..., httpx.Response],
) -> dict[str, object]:
    item = _exact_object(
        item,
        {
            "state_key",
            "url",
            "bearer_token_env",
            "device_id",
            "selected_fields",
            "timeout_seconds",
        },
        "endpoint",
    )
    state_key = _nonblank(item["state_key"], "endpoint state_key")
    url = _nonblank(item["url"], "endpoint url")
    parsed_url = urlparse(url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise HistorySourceExportError(
            "endpoint url must be explicit HTTP(S) without credentials, query, or fragment"
        )
    token_env_value = item["bearer_token_env"]
    device_id_value = item["device_id"]
    headers: dict[str, str] = {}
    if token_env_value is not None:
        token_env = _nonblank(token_env_value, "endpoint bearer_token_env")
        token = os.environ.get(token_env)
        if token is None or not token:
            raise HistorySourceExportError(
                f"required endpoint credential environment variable is missing: {token_env}"
            )
        headers["Authorization"] = f"Bearer {token}"
    if device_id_value is not None:
        headers["X-Nanihold-Device-Id"] = _nonblank(
            device_id_value, "endpoint device_id"
        )
    selected_fields = item["selected_fields"]
    if (
        not isinstance(selected_fields, list)
        or not selected_fields
        or not all(isinstance(field, str) and field for field in selected_fields)
        or len(set(selected_fields)) != len(selected_fields)
    ):
        raise HistorySourceExportError(
            "endpoint selected_fields must be a non-empty unique string list"
        )
    timeout = item["timeout_seconds"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise HistorySourceExportError("endpoint timeout_seconds must be positive")
    try:
        response = request(url, headers=headers, timeout=float(timeout))
    except httpx.HTTPError as exc:
        raise HistorySourceExportError(f"endpoint request failed: {state_key}") from exc
    if response.status_code != 200:
        raise HistorySourceExportError(
            f"endpoint {state_key} returned HTTP {response.status_code}"
        )
    try:
        selected = _select_fields(response.json(), selected_fields, state_key)
    except ValueError as exc:
        raise HistorySourceExportError(
            f"endpoint {state_key} returned invalid JSON"
        ) from exc
    return {
        "state_key": state_key,
        "text": f"{state_key}: HTTP 200、{len(selected)}項目を確認",
        "value": {
            "url": url,
            "selected": selected,
            "response_sha256": _sha256(response.content),
        },
    }


def _file_state(item: dict[str, Any]) -> dict[str, object]:
    item = _exact_object(item, {"state_key", "path"}, "fingerprinted file")
    state_key = _nonblank(item["state_key"], "file state_key")
    path = Path(_nonblank(item["path"], "file path"))
    if not path.is_absolute() or not path.is_file():
        raise HistorySourceExportError(
            f"fingerprinted file must be an existing absolute file: {path}"
        )
    payload = path.read_bytes()
    return {
        "state_key": state_key,
        "text": f"{state_key}: {len(payload)} bytes、digest確認済み",
        "value": {
            "path": str(path.resolve()),
            "byte_count": len(payload),
            "sha256": _sha256(payload),
        },
    }


def capture_snapshot(
    spec_path: Path,
    *,
    request: Callable[..., httpx.Response] = httpx.get,
) -> dict[str, object]:
    if not spec_path.is_absolute() or not spec_path.is_file():
        raise HistorySourceExportError("snapshot spec must be an existing absolute file")
    try:
        spec = json.loads(spec_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistorySourceExportError("snapshot spec is invalid JSON") from exc
    spec = _exact_object(
        spec,
        {
            "captured_at",
            "source_instance_id",
            "repositories",
            "endpoints",
            "fingerprinted_files",
        },
        "snapshot spec",
    )
    captured_at = _parse_time(spec["captured_at"])
    instance = _nonblank(spec["source_instance_id"], "source_instance_id")
    repositories = spec["repositories"]
    endpoints = spec["endpoints"]
    files = spec["fingerprinted_files"]
    if not all(isinstance(items, list) for items in (repositories, endpoints, files)):
        raise HistorySourceExportError(
            "repositories, endpoints, and fingerprinted_files must be lists"
        )
    states = [
        *(_git_state(item) for item in repositories),
        *(_endpoint_state(item, request=request) for item in endpoints),
        *(_file_state(item) for item in files),
    ]
    state_keys = [str(item["state_key"]) for item in states]
    if len(state_keys) != len(set(state_keys)):
        raise HistorySourceExportError("snapshot state_key values must be unique")
    return {
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "source_instance_id": instance,
        "states": states,
    }


def write_snapshot(snapshot: dict[str, object], output: Path) -> None:
    if not output.is_absolute():
        raise HistorySourceExportError("snapshot output path must be absolute")
    if output.exists():
        raise HistorySourceExportError("snapshot output path must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical(snapshot) + b"\n"
    with output.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture an explicit secret-free system snapshot"
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        snapshot = capture_snapshot(arguments.spec.resolve())
        write_snapshot(snapshot, arguments.output.resolve())
    except (HistorySourceExportError, OSError) as exc:
        print(f"system snapshot failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "captured_at": snapshot["captured_at"],
                "source_instance_id": snapshot["source_instance_id"],
                "state_count": len(snapshot["states"]),  # type: ignore[arg-type]
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
