"""Provider-specific payload adapters.

Adapters only translate provider payloads. They do not decide whether a value is
urgent or anomalous; shared monitoring policy belongs downstream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

PayModel = Literal["prepaid_balance", "credits_package", "postpaid", "spend_report"]
ADAPTER_VERSION = "1"


class AdapterError(ValueError):
    """Base class for a payload that cannot become an observation."""


class SchemaError(AdapterError):
    """The payload does not match the provider's documented shape."""


class SemanticError(AdapterError):
    """The shape is known but its values violate the provider contract."""


@dataclass(frozen=True)
class ProviderDefinition:
    provider: str
    name: str
    pay_model: PayModel
    unit: str


@dataclass(frozen=True)
class CanonicalSample:
    metric_name: str
    value: float
    unit: str
    capacity: float | None = None
    refresh_at: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


PROVIDERS = {
    item.provider: item
    for item in (
        ProviderDefinition("brightdata", "Oxylabs", "prepaid_balance", "usd"),
        ProviderDefinition("evomi", "Smartproxy", "prepaid_balance", "usd"),
        ProviderDefinition("scrapfly", "ScraperAPI", "credits_package", "credits"),
        ProviderDefinition("twocaptcha", "Anti-Captcha", "prepaid_balance", "usd"),
        ProviderDefinition("zerobounce", "NeverBounce", "credits_package", "credits"),
        ProviderDefinition("findymail", "Hunter", "credits_package", "credits"),
        ProviderDefinition("bounceban", "Kickbox", "credits_package", "credits"),
        ProviderDefinition("openai", "OpenAI", "prepaid_balance", "usd"),
        ProviderDefinition("openrouter", "Groq", "prepaid_balance", "usd"),
        ProviderDefinition("anthropic", "Anthropic", "spend_report", "usd"),
        ProviderDefinition("elevenlabs", "Deepgram", "credits_package", "credits"),
        ProviderDefinition("tremendous", "Tango Card", "prepaid_balance", "gbp"),
        ProviderDefinition("vastai", "RunPod", "postpaid", "usd"),
        ProviderDefinition("meta_ads", "Google Ads", "spend_report", "usd"),
        ProviderDefinition("resend", "Resend", "credits_package", "credits"),
    )
}

_SIMPLE_BALANCE = {"brightdata", "twocaptcha", "openai", "openrouter"}
_CREDIT_PACKAGES = {
    "scrapfly",
    "zerobounce",
    "findymail",
    "bounceban",
    "elevenlabs",
    "resend",
}


def _mapping(value: Any, field_name: str = "payload") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{field_name} must be an object")
    return value


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SchemaError(f"{key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise SemanticError(f"{key} must be finite")
    return result


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(f"{key} must be a non-empty string")
    return value


def _expect_unit(actual: str, expected: str, field_name: str) -> None:
    if actual.lower() != expected:
        raise SemanticError(f"{field_name} must be {expected!r}, got {actual!r}")


def _balance(payload: dict[str, Any]) -> list[CanonicalSample]:
    currency = _text(payload, "currency")
    _expect_unit(currency, "usd", "currency")
    return [CanonicalSample("provider_balance", _number(payload, "balance"), "usd")]


def _credits(payload: dict[str, Any]) -> list[CanonicalSample]:
    remaining = _number(payload, "remaining")
    capacity = _number(payload, "package")
    refresh = _text(payload, "refresh")
    try:
        date.fromisoformat(refresh)
    except ValueError as exc:
        raise SemanticError("refresh must be an ISO date") from exc
    if capacity <= 0:
        raise SemanticError("package must be positive")
    if not 0 <= remaining <= capacity:
        raise SemanticError("remaining must be between zero and package")
    return [
        CanonicalSample(
            "provider_credits_remaining",
            remaining,
            "credits",
            capacity=capacity,
            refresh_at=refresh,
        )
    ]


def normalize_payload(provider: str, payload: Any) -> list[CanonicalSample]:
    """Translate one known provider payload into one or more canonical samples."""

    if provider not in PROVIDERS:
        raise SchemaError(f"no adapter for provider {provider!r}")
    data = _mapping(payload)

    if provider in _SIMPLE_BALANCE:
        return _balance(data)
    if provider in _CREDIT_PACKAGES:
        return _credits(data)
    if provider == "evomi":
        nested = _mapping(data.get("data"), "data")
        wallet = _mapping(nested.get("wallet"), "data.wallet")
        _expect_unit(_text(wallet, "ccy"), "usd", "ccy")
        return [CanonicalSample("provider_balance", _number(wallet, "amount"), "usd")]
    if provider == "anthropic":
        if _text(data, "object") != "cost_report":
            raise SemanticError("object must be 'cost_report'")
        window = _text(data, "window")
        if window != "trailing_24h":
            raise SemanticError("unsupported Anthropic spend window")
        return [
            CanonicalSample(
                "provider_spend",
                _number(data, "amount_cents") / 100,
                "usd",
                labels={"window": window},
            )
        ]
    if provider == "tremendous":
        return [CanonicalSample("provider_balance", _number(data, "gbp"), "gbp")]
    if provider == "vastai":
        _expect_unit(_text(data, "unit"), "usd", "unit")
        return [CanonicalSample("provider_credit", _number(data, "credit"), "usd")]
    if provider == "meta_ads":
        return [
            CanonicalSample(
                "provider_spend",
                _number(data, "spend_usd_24h"),
                "usd",
                labels={"window": "trailing_24h"},
            ),
            CanonicalSample(
                "provider_spend",
                _number(data, "spend_usd_30d"),
                "usd",
                labels={"window": "trailing_30d"},
            ),
        ]
    raise AssertionError(f"adapter registry is incomplete for {provider}")
