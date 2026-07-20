from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


STRUCTURED_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "display_text": {"type": "string", "minLength": 1},
        "actions": {"type": "array", "items": {"type": "object"}},
    },
    "required": [
        "display_text",
        "actions",
    ],
}


def _candidate_key(candidate: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "adapter": candidate["adapter"],
            "adapter_version": candidate["adapter_version"],
            "provider": candidate["provider"],
            "model_snapshot": candidate["model_snapshot"],
            "effort": candidate["effort"],
            "toolset": sorted(candidate["toolset"]),
            "sandbox_fingerprint": candidate["sandbox_fingerprint"],
            "environment_fingerprint": candidate["environment_fingerprint"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{candidate['adapter']}@{candidate['adapter_version']}:{digest}"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


class PilotHost:
    def __init__(
        self, config_path: Path, working_directory: Path, log_path: Path
    ) -> None:
        raw = json.loads(config_path.read_text("utf-8"))
        required = {
            "candidate",
            "cli_executable",
            "cli_version",
            "bearer_token_env",
            "max_budget_usd",
            "timeout_seconds",
            "bind_host",
            "bind_port",
        }
        if set(raw) != required:
            raise RuntimeError(
                "PilotHost config fields differ from the required exact contract"
            )
        candidate = raw["candidate"]
        if not isinstance(candidate, dict):
            raise RuntimeError("PilotHost candidate must be an object")
        if candidate.get("adapter") != "claude-code":
            raise RuntimeError("local PilotHost requires the claude-code adapter")
        if candidate.get("provider") != "anthropic":
            raise RuntimeError("local PilotHost requires the anthropic provider")
        if candidate.get("effort") != "low":
            raise RuntimeError("local PilotHost requires effort low")
        model = candidate.get("model_snapshot")
        if not isinstance(model, str) or not model:
            raise RuntimeError("local PilotHost model snapshot is required")
        if "opus" in model.lower():
            raise RuntimeError("local PilotHost forbids Opus")
        if candidate.get("toolset") != ["conversation-only"]:
            raise RuntimeError(
                "local PilotHost requires the conversation-only toolset"
            )
        if not working_directory.is_dir():
            raise RuntimeError("PilotHost working directory does not exist")
        self.candidate = candidate
        self.candidate_key = _candidate_key(candidate)
        self.cli_executable = str(raw["cli_executable"])
        self.cli_version = str(raw["cli_version"])
        self.bearer_token = _required_env(str(raw["bearer_token_env"]))
        self.max_budget_usd = float(raw["max_budget_usd"])
        self.timeout_seconds = float(raw["timeout_seconds"])
        self.bind_host = str(raw["bind_host"])
        self.bind_port = int(raw["bind_port"])
        self.working_directory = working_directory.resolve()
        self.log_path = log_path.resolve()
        if self.max_budget_usd <= 0 or self.timeout_seconds <= 0:
            raise RuntimeError("PilotHost budget and timeout must be positive")
        self._validate_cli()

    def log(self, message: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{message}\n")

    def _validate_cli(self) -> None:
        completed = subprocess.run(
            [self.cli_executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Claude Code version check failed: {completed.stderr.strip()}"
            )
        reported = completed.stdout.strip()
        if not reported.startswith(f"{self.cli_version} "):
            raise RuntimeError(
                f"Claude Code version mismatch: configured={self.cli_version}, "
                f"reported={reported}"
            )
        if self.candidate["adapter_version"] != self.cli_version:
            raise RuntimeError(
                "candidate adapter_version differs from the installed CLI version"
            )

    def invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) != {"candidate", "owner_text", "context"}:
            raise RuntimeError("Interface request fields differ from the exact contract")
        if request["candidate"] != self.candidate:
            raise RuntimeError("Interface request candidate differs from PilotHost")
        owner_text = request["owner_text"]
        context = request["context"]
        if not isinstance(owner_text, str) or not owner_text.strip():
            raise RuntimeError("owner_text must not be blank")
        if not isinstance(context, dict):
            raise RuntimeError("context must be an object")
        prompt = json.dumps(
            {
                "instruction": (
                    "You are the Interface Pilot for a local Nanihold verification. "
                    "Answer the owner in Japanese. Return the requested structured "
                    "object in one pass. Do not claim to execute work or side effects. "
                    "actions may describe proposed typed InterfaceAction values only."
                ),
                "owner_text": owner_text,
                "context": context,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        argv = [
            self.cli_executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(STRUCTURED_RESPONSE_SCHEMA, separators=(",", ":")),
            "--model",
            self.candidate["model_snapshot"],
            "--effort",
            self.candidate["effort"],
            "--tools",
            "",
            "--safe-mode",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--max-budget-usd",
            str(self.max_budget_usd),
        ]
        provider_session_id = context.get("provider_session_id")
        if provider_session_id is not None:
            if not isinstance(provider_session_id, str) or not provider_session_id:
                raise RuntimeError("provider_session_id must be a non-empty string")
            argv.extend(("--resume", provider_session_id))
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
            cwd=self.working_directory,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"Claude Code failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        try:
            outer = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Claude Code returned invalid JSON") from exc
        model_usage = outer.get("modelUsage")
        if not isinstance(model_usage, dict) or len(model_usage) != 1:
            raise RuntimeError("Claude Code did not report one actual model snapshot")
        actual_model = next(iter(model_usage))
        if actual_model != self.candidate["model_snapshot"]:
            raise RuntimeError(
                "RequestedActualModelMismatch: "
                f"requested={self.candidate['model_snapshot']}, actual={actual_model}"
            )
        actual_usage = model_usage[actual_model]
        if not isinstance(actual_usage, dict):
            raise RuntimeError("Claude Code modelUsage entry is malformed")
        usage_fields = {
            "input_tokens": "inputTokens",
            "cache_creation_input_tokens": "cacheCreationInputTokens",
            "cache_read_input_tokens": "cacheReadInputTokens",
            "output_tokens": "outputTokens",
            "cost_usd": "costUSD",
        }
        pilot_usage: dict[str, object] = {
            "candidate_key": self.candidate_key,
            "actual_provider": "anthropic",
            "actual_model_snapshot": actual_model,
        }
        for target, source in usage_fields.items():
            value = actual_usage.get(source)
            if not isinstance(value, (int, float)) or value < 0:
                raise RuntimeError(
                    f"Claude Code modelUsage.{source} is missing or invalid"
                )
            pilot_usage[target] = value
        duration_ms = outer.get("duration_ms")
        if not isinstance(duration_ms, int) or duration_ms < 0:
            raise RuntimeError("Claude Code duration_ms is missing or invalid")
        pilot_usage["duration_ms"] = duration_ms
        pilot_usage.update(
            {
                "classifier_triggered": False,
                "model_substitution": False,
                "full_history_resent": False,
                "polling_call": False,
                "false_complete": False,
                "reedited_tokens": 0,
            }
        )
        structured = outer.get("structured_output")
        if not isinstance(structured, dict):
            raise RuntimeError("Claude Code structured_output is missing")
        session_id = outer.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Claude Code session_id is missing")
        structured["provider_session_id"] = session_id
        structured["pilot_usage"] = pilot_usage
        return {
            "requested_candidate_key": self.candidate_key,
            "actual_provider": "anthropic",
            "actual_model_snapshot": actual_model,
            "structured_response": structured,
        }


def _handler(host: PilotHost):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NaniholdLocalPilotHost/1"

        def _authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {host.bearer_token}"

        def _send(self, status: HTTPStatus, body: dict[str, Any]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Bearer token required"})
                return
            if self.path != "/health":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._send(
                HTTPStatus.OK,
                {
                    "status": "ready",
                    "candidate_key": host.candidate_key,
                    "model_snapshot": host.candidate["model_snapshot"],
                    "effort": host.candidate["effort"],
                    "tools": "disabled",
                },
            )

        def do_POST(self) -> None:
            if not self._authorized():
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "Bearer token required"})
                return
            if self.path != "/v1/interface-turn":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2_000_000:
                    raise RuntimeError("invalid request size")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise RuntimeError("request must be an object")
                result = host.invoke(request)
            except Exception as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, result)

        def log_message(self, format: str, *args: object) -> None:
            host.log(f"{self.address_string()} - {format % args}")

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--working-directory", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()
    host = PilotHost(
        args.config.resolve(),
        args.working_directory.resolve(),
        args.log_file.resolve(),
    )
    server = ThreadingHTTPServer(
        (host.bind_host, host.bind_port),
        _handler(host),
    )
    host.log(
        json.dumps(
            {
                "status": "ready",
                "bind": f"{host.bind_host}:{host.bind_port}",
                "candidate_key": host.candidate_key,
            }
        )
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
