"""``/settings`` — read-only environment report and on-demand reachability probes."""

import logging

from fastapi import APIRouter, HTTPException

from pka.api import settings_view as sv
from pka.api.schemas.settings import ProbeResult, SettingsReport

log = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsReport)
async def get_settings():
    return sv.build_settings_report()


@router.post("/probe/{capability}", response_model=ProbeResult)
def probe(capability: str):
    if capability not in sv.CAPABILITIES:
        raise HTTPException(400, f"Unknown capability: {capability}")
    return sv.probe_provider(capability)
