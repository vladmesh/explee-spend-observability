from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PayModel = Literal["prepaid_balance", "credits_package", "postpaid", "spend_report"]
ProcessingOutcome = Literal[
    "success",
    "transport_error",
    "http_error",
    "throttled",
    "empty_payload",
    "invalid_json",
    "schema_error",
    "semantic_error",
]


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str
    name: str
    pay_model: PayModel
    unit: str
    endpoint: str
    note: str = ""


class Observation(BaseModel):
    """Canonical metric sample linked to its immutable source response."""

    raw_response_id: int
    observed_at: datetime
    provider: str
    pay_model: PayModel
    metric_name: str
    value: float
    capacity: float | None = None
    unit: str
    refresh_at: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    adapter_version: str
