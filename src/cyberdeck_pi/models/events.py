from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class EventModel(BaseModel):
    # extra="allow": the wire protocol hasn't been end-to-end tested against
    # real firmware yet, so unexpected fields must not break parsing.
    model_config = ConfigDict(extra="allow")


class ApBeacon(EventModel):
    type: Literal["ap_beacon"]
    mac: str
    ssid: str
    channel: int
    rssi: int
    # Plain str, not a closed Literal: real hardware sends encryption-type
    # combinations beyond the handoff doc's enumerated list (e.g. the very
    # common "WPA/WPA2" mixed mode) -- a strict enum silently dropped every
    # beacon from those APs, whole event and all, during real-hardware
    # testing. Real-world AP encryption reporting isn't a fully closed set.
    enc: str


class ProbeReq(EventModel):
    type: Literal["probe_req"]
    mac: str
    rssi: int
    ssid: str


class ChannelStat(EventModel):
    type: Literal["channel_stat"]
    channel: int
    packet_count: int
    window_ms: int
    avg_rssi: int | None = None


class Deauth(EventModel):
    type: Literal["deauth"]
    mac: str
    target_mac: str
    channel: int
    rssi: int
    reason_code: int


class Eapol(EventModel):
    type: Literal["eapol"]
    mac: str
    client_mac: str
    channel: int
    rssi: int
    message_num: int
    pmkid: str | None = None


class BleAdv(EventModel):
    type: Literal["ble_adv"]
    mac: str
    name: str
    rssi: int
    mfg_id: int | None = None
    mfg_data: str | None = None


class SkimmerMatch(EventModel):
    type: Literal["skimmer_match"]
    mac: str
    name: str
    rssi: int
    mfg_id: int | None = None
    mfg_data: str | None = None
    matched_signature: str


class BtClassic(EventModel):
    type: Literal["bt_classic"]
    mac: str
    name: str
    rssi: int
    cod: str


class DwellUpdate(EventModel):
    type: Literal["dwell_update"]
    mac: str
    first_seen_ms: int
    last_seen_ms: int
    dwell_ms: int
    rssi: int


class TagRead(EventModel):
    # Not part of the ESP32 WiFi/BT protocol -- this is the M7E Hecto UHF
    # RFID toadlet's own event shape, parsed from ThingMagic's binary Mercury
    # API rather than JSON. epc is a hex string, matching the convention
    # already used for mfg_data/pmkid/cod above.
    type: Literal["tag_read"]
    epc: str
    rssi: int
    frequency_khz: int
    tx_antenna: int
    rx_antenna: int
    timestamp_ms: int


Event = Annotated[
    Union[
        ApBeacon,
        ProbeReq,
        ChannelStat,
        Deauth,
        Eapol,
        BleAdv,
        SkimmerMatch,
        BtClassic,
        DwellUpdate,
        TagRead,
    ],
    Field(discriminator="type"),
]


class StampedEvent(BaseModel):
    toadlet: Literal["wifi", "bt", "rfid"]
    pi_ts: float
    event: Event
