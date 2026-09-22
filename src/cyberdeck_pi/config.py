import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel


class WebConfig(BaseModel):
    host: str
    port: int


class StorageConfig(BaseModel):
    root: str


class ToadletConfig(BaseModel):
    source: Literal["real", "mock"]
    baud: int
    offline_threshold_s: float
    device: str | None = None


class RfidToadletConfig(BaseModel):
    source: Literal["real", "mock"]
    baud: int
    offline_threshold_s: float
    device: str | None = None
    region: str = "NORTHAMERICA"
    read_power_cdbm: int = 500  # centi-dBm; 500 = 5.00 dBm. Conservative
    # default given the battery/buck-regulator power budget -- max is 2700
    # (27.00 dBm) but that draws over 720mA and needs external 5V, not USB.


class ToadletsConfig(BaseModel):
    wifi: ToadletConfig
    bt: ToadletConfig
    rfid: RfidToadletConfig


class Settings(BaseModel):
    web: WebConfig
    storage: StorageConfig
    toadlets: ToadletsConfig


def load_settings(path: Path) -> Settings:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Settings.model_validate(data)


def save_settings(path: Path, settings: Settings) -> None:
    with path.open("wb") as f:
        tomli_w.dump(settings.model_dump(exclude_none=True), f)
