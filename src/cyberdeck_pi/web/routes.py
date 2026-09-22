from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from serial.tools import list_ports

from cyberdeck_pi.config import ToadletConfig, RfidToadletConfig

router = APIRouter()

_TOADLETS = ("wifi", "bt", "rfid")


class ModeRequest(BaseModel):
    mode: str
    param: str | None = None


class ToadletConfigRequest(BaseModel):
    source: Literal["real", "mock"]
    device: str | None = None
    baud: int
    offline_threshold_s: float
    region: str = "NORTHAMERICA"
    read_power_cdbm: int = 500


def _get_reader(request: Request, toadlet: str):
    reader = request.app.state.cyberdeck.get_reader(toadlet)
    if reader is None:
        raise HTTPException(status_code=404, detail=f"unknown toadlet {toadlet!r}")
    return reader


@router.get("/api/{toadlet}/modes")
async def get_toadlet_modes(toadlet: str, request: Request):
    reader = _get_reader(request, toadlet)
    return {
        "toadlet": toadlet,
        "modes": sorted(reader.valid_modes),
        "current_mode": reader.current_mode,
    }


@router.post("/api/{toadlet}/mode")
async def set_toadlet_mode(toadlet: str, body: ModeRequest, request: Request):
    reader = _get_reader(request, toadlet)
    try:
        await reader.set_mode(body.mode, body.param)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"toadlet": toadlet, "mode": reader.current_mode}


@router.get("/api/ports")
async def list_serial_ports():
    """Serial ports currently visible to the OS -- populates the device
    dropdown in the settings UI. Ports the OS can't see (e.g. a real ESP32
    wired to a dedicated GPIO UART with no USB bridge) won't show up here;
    those are entered as free text instead."""
    return {
        "ports": [
            {"device": p.device, "description": p.description}
            for p in sorted(list_ports.comports(), key=lambda p: p.device)
        ]
    }


@router.get("/api/{toadlet}/config")
async def get_toadlet_config(toadlet: str, request: Request):
    if toadlet not in _TOADLETS:
        raise HTTPException(status_code=404, detail=f"unknown toadlet {toadlet!r}")
    config = getattr(request.app.state.cyberdeck.settings.toadlets, toadlet)
    return config.model_dump()


@router.post("/api/{toadlet}/config")
async def set_toadlet_config(toadlet: str, body: ToadletConfigRequest, request: Request):
    if toadlet not in _TOADLETS:
        raise HTTPException(status_code=404, detail=f"unknown toadlet {toadlet!r}")
    if toadlet == "rfid":
        config = RfidToadletConfig(**body.model_dump())
    else:
        config = ToadletConfig(**body.model_dump(exclude={"region", "read_power_cdbm"}))
    try:
        await request.app.state.cyberdeck.reconfigure_toadlet(toadlet, config)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return config.model_dump()
