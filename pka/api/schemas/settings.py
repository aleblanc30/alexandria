"""Read-only settings report and provider-reachability probe models."""

from typing import Any

from pydantic import BaseModel


class SettingField(BaseModel):
    name: str
    value: Any | None
    is_secret: bool
    is_set: bool | None = None  # only meaningful when is_secret
    is_default: bool
    tier: str


class SettingGroup(BaseModel):
    name: str
    fields: list[SettingField]


class CapabilityStatus(BaseModel):
    capability: str
    provider: str
    model: str
    base_url: str
    credential_present: bool


class SettingsReport(BaseModel):
    groups: list[SettingGroup]
    capabilities: list[CapabilityStatus]


class ProbeResult(BaseModel):
    reachable: bool
    detail: str
