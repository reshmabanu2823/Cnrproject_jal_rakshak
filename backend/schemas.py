"""Request models. Validation lives here so bad data is rejected with a clear 422
before it reaches the database."""
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

MAX_CLOCK_SKEW = timedelta(minutes=5)


def to_local_naive(ts: datetime) -> datetime:
    """Store everything as local wall-clock time (the schema's convention)."""
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return ts.replace(microsecond=0)


def check_not_future(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    ts = to_local_naive(ts)
    if ts > datetime.now() + MAX_CLOCK_SKEW:
        raise ValueError("timestamp is in the future (check the device clock / NTP)")
    return ts


class ReadingIn(BaseModel):
    zone_id: int
    liters: float = Field(ge=0, le=5000)
    ts: datetime | None = None

    _ts = field_validator("ts")(check_not_future)


class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    icon: str = Field(default="💧", max_length=8)


class Slab(BaseModel):
    upto_kl: float | None = Field(default=None, gt=0)
    rate: float = Field(ge=0)


class SettingsIn(BaseModel):
    """All fields optional: send only what you want to change."""
    household_size: int | None = Field(default=None, ge=1, le=100)
    lpcd_target: float | None = Field(default=None, gt=0, le=1000)
    tariff_slabs: list[Slab] | None = Field(default=None, min_length=1, max_length=20)
    fixed_charge: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @field_validator("tariff_slabs")
    @classmethod
    def _slabs_ordered(cls, v):
        if v is None:
            return v
        if v[-1].upto_kl is not None:
            raise ValueError("last slab must have upto_kl = null (no upper limit)")
        bounds = [s.upto_kl for s in v[:-1]]
        if any(b is None for b in bounds) or bounds != sorted(bounds) or len(set(bounds)) != len(bounds):
            raise ValueError("slab upper limits must be strictly increasing")
        return v


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    zone_id: int
    pulses_per_liter: float = Field(default=450.0, gt=0, le=100_000)


class DevicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    zone_id: int | None = None
    pulses_per_liter: float | None = Field(default=None, gt=0, le=100_000)
    active: bool | None = None


class IngestItem(BaseModel):
    seq: int | None = Field(default=None, ge=0, lt=2**63, description="Monotonic counter from the device; makes retries safe")
    ts: datetime | None = Field(default=None, description="When the water flowed; defaults to server receive time")
    liters: float | None = Field(default=None, ge=0, le=5000)
    pulses: int | None = Field(default=None, ge=0, le=10_000_000, description="Raw flow-sensor pulses; converted with the device's pulses_per_liter")

    @model_validator(mode="after")
    def _one_quantity(self):
        if (self.liters is None) == (self.pulses is None):
            raise ValueError("send exactly one of 'liters' or 'pulses'")
        return self


class IngestIn(BaseModel):
    readings: list[IngestItem] = Field(min_length=1, max_length=500)
    firmware: str | None = Field(default=None, max_length=40)


class AlertAction(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class WebhookIn(BaseModel):
    url: HttpUrl
    secret: str | None = Field(default=None, min_length=8, max_length=200)
    min_severity: Literal["low", "medium", "high"] = "medium"
