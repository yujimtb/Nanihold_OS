"""Price Profile / FX の厳密解決と円換算。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import tomllib
from typing import Iterable

from vsm.survival.models import UsageRecord


class CostingError(ValueError):
    """価格、適用期間、為替を解決できない計測。"""


def _d(value: Decimal | str | int) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("単価・為替は有限な0以上のDecimalでなければなりません")
    return result


def _jpy(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class PriceProfile:
    profile_id: str
    provider: str
    billing_mode: str
    model: str
    currency: str
    input_rate_per_million: Decimal
    output_rate_per_million: Decimal
    cache_read_rate_per_million: Decimal
    cache_write_rate_per_million: Decimal
    tool_rate: Decimal
    valid_from: date
    valid_until: date | None = None
    source_url: str | None = None
    approved_by: str | None = None
    approval_status: str = "TODO_OWNER_APPROVAL"
    wall_clock_rate_per_hour: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("profile_id", "provider", "billing_mode", "model", "currency"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} は空にできません")
        if len(self.currency) != 3:
            raise ValueError("currency は3文字でなければなりません")
        for name in (
            "input_rate_per_million",
            "output_rate_per_million",
            "cache_read_rate_per_million",
            "cache_write_rate_per_million",
            "tool_rate",
            "wall_clock_rate_per_hour",
        ):
            _d(getattr(self, name))
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("valid_until は valid_from 以後でなければなりません")

    def active_on(self, day: date) -> bool:
        return self.valid_from <= day and (
            self.valid_until is None or day <= self.valid_until
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "profile_id": self.profile_id,
            "provider": self.provider,
            "billing_mode": self.billing_mode,
            "model": self.model,
            "currency": self.currency.upper(),
            "input_rate_per_million": str(_d(self.input_rate_per_million)),
            "output_rate_per_million": str(_d(self.output_rate_per_million)),
            "cache_read_rate_per_million": str(_d(self.cache_read_rate_per_million)),
            "cache_write_rate_per_million": str(_d(self.cache_write_rate_per_million)),
            "tool_rate": str(_d(self.tool_rate)),
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "source_url": self.source_url,
            "approved_by": self.approved_by,
            "approval_status": self.approval_status,
            "wall_clock_rate_per_hour": str(_d(self.wall_clock_rate_per_hour)),
        }


@dataclass(frozen=True, slots=True)
class FxRate:
    rate_id: str
    base_currency: str
    quote_currency: str
    rate: Decimal
    effective_on: date
    source: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if not self.rate_id.strip() or not self.source.strip():
            raise ValueError("rate_id/source は空にできません")
        if len(self.base_currency) != 3 or len(self.quote_currency) != 3:
            raise ValueError("通貨コードは3文字でなければなりません")
        if _d(self.rate) <= 0:
            raise ValueError("FX rate は正数でなければなりません")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at は timezone-aware でなければなりません")

    def to_dict(self) -> dict[str, str]:
        return {
            "rate_id": self.rate_id,
            "base_currency": self.base_currency.upper(),
            "quote_currency": self.quote_currency.upper(),
            "rate": str(_d(self.rate)),
            "effective_on": self.effective_on.isoformat(),
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat(),
        }


class PriceBook:
    """完全一致した PriceProfile と FX のみを返す。"""

    def __init__(self, profiles: Iterable[PriceProfile] = (), fx_rates: Iterable[FxRate] = ()) -> None:
        self._profiles: dict[str, PriceProfile] = {}
        self._fx_rates: dict[str, FxRate] = {}
        for profile in profiles:
            self.add_profile(profile)
        for fx_rate in fx_rates:
            self.add_fx_rate(fx_rate)

    def add_profile(self, profile: PriceProfile) -> None:
        for existing in self._profiles.values():
            if (
                existing.provider == profile.provider
                and existing.billing_mode == profile.billing_mode
                and existing.model == profile.model
                and existing.valid_from <= (profile.valid_until or profile.valid_from)
                and profile.valid_from <= (existing.valid_until or existing.valid_from)
            ):
                raise CostingError(
                    "価格プロファイルの適用期間が重複しています: "
                    f"{existing.profile_id} / {profile.profile_id}"
                )
        if profile.profile_id in self._profiles:
            raise CostingError(f"price profile already exists: {profile.profile_id}")
        self._profiles[profile.profile_id] = profile

    def add_fx_rate(self, fx_rate: FxRate) -> None:
        if fx_rate.rate_id in self._fx_rates:
            raise CostingError(f"FX rate already exists: {fx_rate.rate_id}")
        self._fx_rates[fx_rate.rate_id] = fx_rate

    def resolve_profile(
        self, *, provider: str, billing_mode: str, model: str, on: date
    ) -> PriceProfile:
        candidates = [
            profile
            for profile in self._profiles.values()
            if profile.provider == provider
            and profile.billing_mode == billing_mode
            and profile.model == model
            and profile.active_on(on)
        ]
        if len(candidates) != 1:
            raise CostingError(
                "完全一致する価格プロファイルが1件必要です: "
                f"provider={provider}, billing_mode={billing_mode}, model={model}, on={on}"
            )
        return candidates[0]

    def resolve_fx(self, *, currency: str, on: date) -> FxRate | None:
        currency = currency.upper()
        if currency == "JPY":
            return None
        candidates = [
            fx
            for fx in self._fx_rates.values()
            if fx.base_currency == currency
            and fx.quote_currency == "JPY"
            and fx.effective_on == on
        ]
        if len(candidates) != 1:
            raise CostingError(
                f"{currency}/JPY の為替レートが {on} に1件必要です"
            )
        return candidates[0]

    def cost_usage(self, usage: UsageRecord) -> tuple[int, PriceProfile, FxRate | None]:
        day = usage.started_at.date()
        profile = self.resolve_profile(
            provider=usage.provider,
            billing_mode=usage.billing_mode,
            model=usage.model,
            on=day,
        )
        currency = profile.currency.upper()
        fx = self.resolve_fx(currency=currency, on=day)
        foreign = (
            Decimal(usage.input_tokens) * _d(profile.input_rate_per_million)
            + Decimal(usage.output_tokens) * _d(profile.output_rate_per_million)
            + Decimal(usage.cache_read_tokens) * _d(profile.cache_read_rate_per_million)
            + Decimal(usage.cache_write_tokens) * _d(profile.cache_write_rate_per_million)
        ) / Decimal(1_000_000)
        foreign += _d(usage.tool_units) * _d(profile.tool_rate)
        foreign += (
            Decimal(usage.wall_clock_ms)
            / Decimal(3_600_000)
            * _d(profile.wall_clock_rate_per_hour)
        )
        if currency == "JPY":
            return _jpy(foreign), profile, fx
        if fx is None:
            raise CostingError(f"{currency}/JPY の為替レートがありません")
        return _jpy(foreign * _d(fx.rate)), profile, fx


def load_price_book(path: Path) -> PriceBook:
    """owner が確定した TOML だけを PriceBook に取り込む。"""

    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    raw_profiles = raw.get("profiles")
    raw_fx_rates = raw.get("fx_rates")
    if not isinstance(raw_profiles, dict) or not isinstance(raw_fx_rates, dict):
        raise ValueError("pricing TOML は [profiles.*] と [fx_rates.*] を必須とします")
    profiles: list[PriceProfile] = []
    for profile_id, value in raw_profiles.items():
        if not isinstance(value, dict):
            raise ValueError(f"profiles.{profile_id} must be a table")
        profiles.append(
            PriceProfile(
                profile_id=str(profile_id),
                provider=str(value["provider"]),
                billing_mode=str(value["billing_mode"]),
                model=str(value["model"]),
                currency=str(value["currency"]),
                input_rate_per_million=Decimal(str(value["input_rate_per_million"])),
                output_rate_per_million=Decimal(str(value["output_rate_per_million"])),
                cache_read_rate_per_million=Decimal(str(value["cache_read_rate_per_million"])),
                cache_write_rate_per_million=Decimal(str(value["cache_write_rate_per_million"])),
                tool_rate=Decimal(str(value["tool_rate"])),
                valid_from=date.fromisoformat(str(value["valid_from"])),
                valid_until=(
                    date.fromisoformat(str(value["valid_until"]))
                    if value.get("valid_until") is not None
                    else None
                ),
                source_url=value.get("source_url"),
                approved_by=value.get("approved_by"),
                approval_status=str(value.get("approval_status", "TODO_OWNER_APPROVAL")),
                wall_clock_rate_per_hour=Decimal(str(value.get("wall_clock_rate_per_hour", "0"))),
            )
        )
    fx_rates: list[FxRate] = []
    for rate_id, value in raw_fx_rates.items():
        if not isinstance(value, dict):
            raise ValueError(f"fx_rates.{rate_id} must be a table")
        retrieved_at = value["retrieved_at"]
        if not isinstance(retrieved_at, datetime):
            raise ValueError(f"fx_rates.{rate_id}.retrieved_at must be datetime")
        fx_rates.append(
            FxRate(
                rate_id=str(rate_id),
                base_currency=str(value["base_currency"]),
                quote_currency=str(value["quote_currency"]),
                rate=Decimal(str(value["rate"])),
                effective_on=date.fromisoformat(str(value["effective_on"])),
                source=str(value["source"]),
                retrieved_at=retrieved_at,
            )
        )
    return PriceBook(profiles, fx_rates)
