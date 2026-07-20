from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None,
    reason="Windows PowerShell launcher contract",
)

SECRET_VALUES = {
    "LETHE_NANIHOLD_TOKEN": "a" * 64,
    "NANIHOLD_API_BEARER_TOKEN": "b" * 64,
    "PILOT_HOST_BEARER_TOKEN": "c" * 64,
}
CERTIFICATE = "d" * 64


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _ps_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _write_fake_repository(root: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "production_pilot_host.py").write_text(
        """
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

args = sys.argv[1:]
config = json.loads(Path(args[args.index("--config") + 1]).read_text("utf-8"))
assert os.environ["NANIHOLD_PARENT_SENTINEL"] == "parent-preserved"
assert os.path.normcase(os.environ["PATH"].split(os.pathsep)[0]) == os.path.normcase(
    os.environ["NANIHOLD_SENTINEL_PATH"]
)
for name, expected in config["expected_secrets"].items():
    assert os.environ[name] == expected

if config.get("startup_mode") == "fail":
    sys.stderr.write(os.environ["PILOT_HOST_BEARER_TOKEN"] * 200)
    raise SystemExit(19)

token = os.environ["PILOT_HOST_BEARER_TOKEN"]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        authorized = (
            self.headers.get("Authorization") == f"Bearer {token}"
            and self.headers.get("X-Nanihold-Pilot-Host-Id")
            == config["pilot_host_id"]
            and self.headers.get("X-Nanihold-Device-Id") == config["device_id"]
            and self.headers.get("X-Nanihold-Device-Certificate-Sha256")
            == config["device_certificate_sha256"]
        )
        if self.path != "/health" or not authorized:
            self.send_response(401)
            self.end_headers()
            return
        encoded = json.dumps(
            {
                "status": "ready",
                "identity": {
                    "pilot_host_id": config["pilot_host_id"],
                    "device_id": config["device_id"],
                    "certificate_sha256": config[
                        "device_certificate_sha256"
                    ],
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return

ThreadingHTTPServer(
    (config["bind_host"], config["bind_port"]), Handler
).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )


def _launcher_fixture(
    tmp_path: Path,
    launcher: Path,
    *,
    startup_mode: str = "serve",
) -> tuple[list[str], dict[str, str], Path, Path, Path]:
    repository = tmp_path / "repository"
    _write_fake_repository(repository)
    port = _free_port()
    config = {
        "pilot_host_id": "pilot-host:test",
        "device_id": "device:test",
        "device_certificate_sha256": CERTIFICATE,
        "bearer_token_env": "PILOT_HOST_BEARER_TOKEN",
        "bind_host": "127.0.0.1",
        "bind_port": port,
        "expected_secrets": SECRET_VALUES,
        "startup_mode": startup_mode,
    }
    config_path = tmp_path / "pilot-host.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env_path = tmp_path / "runtime.env"
    env_path.write_text(
        "".join(f"{name}={value}\n" for name, value in SECRET_VALUES.items()),
        encoding="utf-8",
    )
    log_path = tmp_path / "pilot-host.log"
    pid_path = tmp_path / "pilot-host.pid"
    sentinel_path = tmp_path / "parent-path-first"
    sentinel_path.mkdir()
    wrapper = tmp_path / "invoke-launcher.ps1"
    wrapper.write_text(
        (
            "$ErrorActionPreference = 'Stop'\n"
            f"$env:PATH = {_ps_quote(str(sentinel_path) + ';')} + $env:PATH\n"
            "$launcherArguments = @{\n"
            f"  PythonExecutable = {_ps_quote(sys.executable)}\n"
            f"  RepositoryRoot = {_ps_quote(repository)}\n"
            f"  ConfigFile = {_ps_quote(config_path)}\n"
            f"  RuntimeEnvFile = {_ps_quote(env_path)}\n"
            f"  LogFile = {_ps_quote(log_path)}\n"
            f"  PidFile = {_ps_quote(pid_path)}\n"
            "  ReadyTimeoutSeconds = 10\n"
            "}\n"
            f"& {_ps_quote(launcher)} @launcherArguments\n"
        ),
        encoding="utf-8",
    )
    command = ["pwsh", "-NoProfile", "-File", str(wrapper)]
    environment = os.environ.copy()
    environment["NANIHOLD_PARENT_SENTINEL"] = "parent-preserved"
    environment["NANIHOLD_SENTINEL_PATH"] = str(sentinel_path)
    return command, environment, pid_path, log_path, config_path


def _run_launcher(
    command: list[str],
    *,
    environment: dict[str, str],
    output_directory: Path,
    timeout: float,
) -> tuple[int, str, str]:
    stdout_path = output_directory / "launcher.stdout"
    stderr_path = output_directory / "launcher.stderr"
    with stdout_path.open("w", encoding="utf-8") as stdout:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                command,
                check=False,
                stdout=stdout,
                stderr=stderr,
                text=True,
                env=environment,
                timeout=timeout,
            )
    return (
        completed.returncode,
        stdout_path.read_text("utf-8"),
        stderr_path.read_text("utf-8"),
    )


def _terminate(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _process_is_alive(pid: int) -> bool:
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def test_launcher_preserves_parent_environment_and_waits_until_ready(
    tmp_path: Path,
) -> None:
    launcher = Path(__file__).parents[1] / "scripts/start_activation_pilot_host.ps1"
    command, environment, pid_path, _, config_path = _launcher_fixture(
        tmp_path, launcher
    )

    returncode, stdout, stderr = _run_launcher(
        command,
        environment=environment,
        output_directory=tmp_path,
        timeout=20,
    )

    assert returncode == 0, stderr
    assert stdout.strip() == "PilotHost ready; PID receipt written."
    assert all(value not in stdout for value in SECRET_VALUES.values())
    pid = int(pid_path.read_text("utf-8").strip())
    try:
        time.sleep(2)
        assert _process_is_alive(pid)
        config = json.loads(config_path.read_text("utf-8"))
        request = urllib.request.Request(
            f"http://127.0.0.1:{config['bind_port']}/health",
            headers={
                "Authorization": (
                    f"Bearer {SECRET_VALUES['PILOT_HOST_BEARER_TOKEN']}"
                ),
                "X-Nanihold-Pilot-Host-Id": config["pilot_host_id"],
                "X-Nanihold-Device-Id": config["device_id"],
                "X-Nanihold-Device-Certificate-Sha256": CERTIFICATE,
            },
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["status"] == "ready"
    finally:
        _terminate(pid)


def test_launcher_does_not_overwrite_existing_pid_receipt(tmp_path: Path) -> None:
    launcher = Path(__file__).parents[1] / "scripts/start_activation_pilot_host.ps1"
    command, environment, pid_path, _, _ = _launcher_fixture(tmp_path, launcher)
    pid_path.write_text("owner-receipt\n", encoding="utf-8")

    returncode, _, _ = _run_launcher(
        command,
        environment=environment,
        output_directory=tmp_path,
        timeout=10,
    )

    assert returncode != 0
    assert pid_path.read_text("utf-8") == "owner-receipt\n"


def test_launcher_keeps_long_child_stderr_out_of_parent_stdio(
    tmp_path: Path,
) -> None:
    launcher = Path(__file__).parents[1] / "scripts/start_activation_pilot_host.ps1"
    command, environment, pid_path, log_path, _ = _launcher_fixture(
        tmp_path,
        launcher,
        startup_mode="fail",
    )

    returncode, stdout, stderr = _run_launcher(
        command,
        environment=environment,
        output_directory=tmp_path,
        timeout=20,
    )

    assert returncode != 0
    combined = stdout + stderr
    assert SECRET_VALUES["PILOT_HOST_BEARER_TOKEN"] not in combined
    assert "stderr_bytes=" in combined
    assert "stderr_sha256=" in combined
    assert len(combined) < 2_000
    assert not pid_path.exists()
    assert (Path(f"{log_path}.stderr")).stat().st_size > 10_000
