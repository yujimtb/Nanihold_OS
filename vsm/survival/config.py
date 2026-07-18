"""Wave 0 の安全境界と経済計測設定。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SafetyBoundary:
    """対外副作用を明示的に閉じる設定。

    Human 認証は owner 承認前の placeholder であり、この Wave では実装しない。
    """

    bind_host: str = "127.0.0.1"
    external_sending_enabled: bool = False
    external_billing_enabled: bool = False
    human_auth_required: bool = False
    human_auth_status: str = "TODO_OWNER_APPROVAL"

    def __post_init__(self) -> None:
        if self.bind_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("survival bind_host は loopback でなければなりません")
        for name in (
            "external_sending_enabled",
            "external_billing_enabled",
            "human_auth_required",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} は boolean でなければなりません")
        if self.external_sending_enabled or self.external_billing_enabled:
            raise ValueError("対外送信・実請求は Wave 0〜2 の対象外で無効固定です")
        if not self.human_auth_status.strip():
            raise ValueError("human_auth_status は空にできません")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bind_host": self.bind_host,
            "external_sending_enabled": self.external_sending_enabled,
            "external_billing_enabled": self.external_billing_enabled,
            "human_auth_required": self.human_auth_required,
            "human_auth_status": self.human_auth_status,
        }


@dataclass(frozen=True, slots=True)
class SurvivalConfig:
    """Economic ledger/report のファイルと運用時刻。"""

    ledger_path: Path = Path("runs/survival/ledger.jsonl")
    usage_path: Path = Path("runs/survival/usage.jsonl")
    report_path: Path = Path("runs/survival/reports.jsonl")
    pricing_path: Path = Path("config/survival-pricing.toml")
    daily_report_time: str = "06:30"
    timezone_name: str = "Asia/Tokyo"
    safety: SafetyBoundary = field(default_factory=SafetyBoundary)

    def __post_init__(self) -> None:
        for name in ("ledger_path", "usage_path", "report_path"):
            if not isinstance(getattr(self, name), Path):
                raise ValueError(f"{name} は Path でなければなりません")
        if not isinstance(self.pricing_path, Path):
            raise ValueError("pricing_path は Path でなければなりません")
        if len(self.daily_report_time) != 5 or self.daily_report_time[2] != ":":
            raise ValueError("daily_report_time は HH:MM 形式でなければなりません")
        hour, minute = (int(part) for part in self.daily_report_time.split(":", 1))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("daily_report_time が不正です")
        if not self.timezone_name.strip():
            raise ValueError("timezone_name は空にできません")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_path": str(self.ledger_path),
            "usage_path": str(self.usage_path),
            "report_path": str(self.report_path),
            "pricing_path": str(self.pricing_path),
            "daily_report_time": self.daily_report_time,
            "timezone_name": self.timezone_name,
            "safety": self.safety.to_dict(),
        }


def _string(section: Mapping[str, Any], name: str, default: str, path: Path) -> str:
    value = section.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[survival] {name} in {path} は空でない文字列が必要です")
    return value.strip()


def load_survival_config(path: Path | None = None) -> SurvivalConfig:
    """``vsm.toml`` の ``[survival]`` を読み込む。

    設定ファイルがない場合は、安全境界を持つコード上の Wave 0 設定を使う。
    指定された path が存在しない場合は入力誤りとして失敗する。
    """

    target = path or Path("vsm.toml")
    if not target.exists():
        if path is not None:
            raise FileNotFoundError(target)
        return SurvivalConfig()
    try:
        with target.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {target}: {exc}") from exc
    section = raw.get("survival", {})
    if not isinstance(section, Mapping):
        raise ValueError(f"[survival] in {target} must be a table")
    allowed = {
        "ledger_path",
        "usage_path",
        "report_path",
        "pricing_path",
        "daily_report_time",
        "timezone_name",
        "bind_host",
        "external_sending_enabled",
        "external_billing_enabled",
        "human_auth_required",
        "human_auth_status",
    }
    unknown = set(section) - allowed
    if unknown:
        raise ValueError(f"unknown [survival] fields: {sorted(unknown)}")
    base = SurvivalConfig()
    config_dir = target.parent.resolve(strict=False)

    def configured_path(name: str, default: Path) -> Path:
        raw_value = _string(section, name, str(default), target)
        candidate = Path(raw_value)
        return candidate if candidate.is_absolute() else config_dir / candidate

    safety = SafetyBoundary(
        bind_host=_string(section, "bind_host", base.safety.bind_host, target),
        external_sending_enabled=section.get(
            "external_sending_enabled", base.safety.external_sending_enabled
        ),
        external_billing_enabled=section.get(
            "external_billing_enabled", base.safety.external_billing_enabled
        ),
        human_auth_required=section.get(
            "human_auth_required", base.safety.human_auth_required
        ),
        human_auth_status=_string(
            section, "human_auth_status", base.safety.human_auth_status, target
        ),
    )
    for name in (
        "external_sending_enabled",
        "external_billing_enabled",
        "human_auth_required",
    ):
        if not isinstance(getattr(safety, name), bool):
            raise ValueError(f"[survival] {name} must be boolean")
    return SurvivalConfig(
        ledger_path=configured_path("ledger_path", base.ledger_path),
        usage_path=configured_path("usage_path", base.usage_path),
        report_path=configured_path("report_path", base.report_path),
        pricing_path=configured_path("pricing_path", base.pricing_path),
        daily_report_time=_string(
            section, "daily_report_time", base.daily_report_time, target
        ),
        timezone_name=_string(section, "timezone_name", base.timezone_name, target),
        safety=safety,
    )
