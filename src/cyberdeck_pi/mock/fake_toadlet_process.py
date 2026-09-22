import asyncio
import json
import os
import pty
import random
import struct
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _ModeSpec:
    interval_s: float
    make_payload: Callable[[], dict | bytes | None]
    binary: bool = False


class FakeToadletProcess:
    """Simulates an ESP32 toadlet on the other end of a UART.

    Opens a pty pair and exposes the slave side's device path so a
    ToadletReader can connect to it exactly as it would a real /dev/ttyAMA*.
    Accepts `MODE:<NAME>[:<param>]\\n` commands on that link and emits
    newline-delimited JSON events at a cadence appropriate to the active
    mode, mirroring the real wire protocol.
    """

    def __init__(self, mode_specs: dict[str, _ModeSpec], default_mode: str):
        self._mode_specs = mode_specs
        self._mode = default_mode
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        # Non-blocking: add_reader only means "readable now", not
        # "read() won't block" -- a pty can report readable spuriously
        # (e.g. around slave open/close), and a blocking read() there
        # would freeze the whole event loop with no way to recover,
        # since nothing else can run on that loop to unblock it.
        os.set_blocking(master_fd, False)
        self.device_path = os.ttyname(slave_fd)
        os.close(slave_fd)
        self._cmd_buf = b""
        self._emit_task: asyncio.Task | None = None

    @property
    def mode(self) -> str:
        return self._mode

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_reader(self._master_fd, self._on_readable)
        self._emit_task = asyncio.create_task(self._emit_loop())

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        loop.remove_reader(self._master_fd)
        if self._emit_task is not None:
            self._emit_task.cancel()
            try:
                await self._emit_task
            except asyncio.CancelledError:
                pass
        os.close(self._master_fd)

    async def __aenter__(self) -> "FakeToadletProcess":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self._master_fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        self._cmd_buf += chunk
        while b"\n" in self._cmd_buf:
            raw, self._cmd_buf = self._cmd_buf.split(b"\n", 1)
            self._handle_command(raw.decode("utf-8", errors="replace").strip())

    def _handle_command(self, line: str) -> None:
        if not line.startswith("MODE:"):
            return
        mode_name = line[len("MODE:") :].split(":", 1)[0]
        if mode_name in self._mode_specs:
            self._mode = mode_name

    async def _emit_loop(self) -> None:
        while True:
            spec = self._mode_specs.get(self._mode)
            if spec is None:
                await asyncio.sleep(0.2)
                continue
            payload = spec.make_payload()
            if payload is not None:
                data = (
                    struct.pack(">H", len(payload)) + payload
                    if spec.binary
                    else (json.dumps(payload) + "\n").encode()
                )
                try:
                    os.write(self._master_fd, data)
                except BlockingIOError:
                    # Non-blocking fd and the reader hasn't drained fast
                    # enough -- drop it, same as the real toadlets' "no
                    # retry/replay, small in-flight buffer only" behavior.
                    pass
            await asyncio.sleep(spec.interval_s)


def _random_mac(rng: random.Random) -> str:
    return ":".join(f"{rng.randint(0, 255):02X}" for _ in range(6))


def _random_hex(rng: random.Random, nbytes: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(nbytes * 2))


def make_wifi_fake_toadlet(rng: random.Random | None = None) -> FakeToadletProcess:
    rng = rng or random.Random()
    aps = [
        (_random_mac(rng), ssid, rng.choice([1, 6, 11]))
        for ssid in ["HomeNet-5G", "CafeWiFi", "", "Office-Guest", "Neighbor2G"]
    ]
    eapol_counter = {"n": 0}

    def ap_beacon() -> dict:
        mac, ssid, channel = rng.choice(aps)
        return {
            "type": "ap_beacon",
            "mac": mac,
            "ssid": ssid,
            "channel": channel,
            "rssi": rng.randint(-90, -40),
            # Includes WPA/WPA2 (a common real-world mixed mode found during
            # hardware testing, not in the original handoff doc's list) so
            # the mock exercises the same values real APs actually send.
            "enc": rng.choice(["OPEN", "WEP", "WPA", "WPA2", "WPA3", "WPA/WPA2", "WPA2/WPA3"]),
        }

    def probe_req() -> dict:
        return {
            "type": "probe_req",
            "mac": _random_mac(rng),
            "rssi": rng.randint(-90, -40),
            "ssid": rng.choice(["HomeWiFi", ""]),
        }

    def channel_stat() -> dict:
        return {
            "type": "channel_stat",
            "channel": rng.choice([1, 6, 11]),
            "packet_count": rng.randint(0, 200),
            "window_ms": 1000,
            "avg_rssi": rng.randint(-90, -40),
        }

    def deauth() -> dict:
        return {
            "type": "deauth",
            "mac": _random_mac(rng),
            "target_mac": rng.choice([_random_mac(rng), "FF:FF:FF:FF:FF:FF"]),
            "channel": rng.choice([1, 6, 11]),
            "rssi": rng.randint(-90, -40),
            "reason_code": rng.choice([1, 7, 15]),
        }

    def eapol() -> dict:
        message_num = (eapol_counter["n"] % 4) + 1
        eapol_counter["n"] += 1
        event = {
            "type": "eapol",
            "mac": _random_mac(rng),
            "client_mac": _random_mac(rng),
            "channel": rng.choice([1, 6, 11]),
            "rssi": rng.randint(-90, -40),
            "message_num": message_num,
        }
        if message_num == 1:
            event["pmkid"] = _random_hex(rng, 16)
        return event

    def deep_capture_frame() -> bytes:
        # Firmware sends raw 802.11 frames -- content doesn't need to be
        # a well-formed frame for exercising the length-prefix framing
        # and PCAP reconstruction, just realistically sized.
        length = rng.randint(20, 300)
        return bytes(rng.randint(0, 255) for _ in range(length))

    return FakeToadletProcess(
        mode_specs={
            "SLEEP": _ModeSpec(1.0, lambda: None),
            "PASSIVE_SCAN": _ModeSpec(0.15, ap_beacon),
            "PROBE_SNIFF": _ModeSpec(0.5, probe_req),
            "CHANNEL_MONITOR": _ModeSpec(1.0, channel_stat),
            "DEAUTH_DETECT": _ModeSpec(4.0, deauth),
            "EAPOL_CAPTURE": _ModeSpec(6.0, eapol),
            "DEEP_CAPTURE": _ModeSpec(0.05, deep_capture_frame, binary=True),
        },
        default_mode="PASSIVE_SCAN",
    )


def make_bt_fake_toadlet(rng: random.Random | None = None) -> FakeToadletProcess:
    rng = rng or random.Random()
    names = ["MyTag", "Old Phone", "", "Fitness Band", "Wireless Earbuds"]
    tracked_macs = [_random_mac(rng) for _ in range(3)]
    toadlet_start = time.monotonic()
    first_seen_ms = {mac: rng.uniform(0, 5000) for mac in tracked_macs}

    def ble_adv() -> dict:
        event = {
            "type": "ble_adv",
            "mac": _random_mac(rng),
            "name": rng.choice(names),
            "rssi": rng.randint(-90, -40),
        }
        if rng.random() < 0.6:
            event["mfg_id"] = rng.randint(0, 500)
            event["mfg_data"] = _random_hex(rng, 5)
        return event

    def skimmer_match() -> dict:
        return {
            "type": "skimmer_match",
            "mac": _random_mac(rng),
            "name": "",
            "rssi": rng.randint(-90, -40),
            "mfg_data": _random_hex(rng, 5),
            "matched_signature": "square-reader-v2",
        }

    def bt_classic() -> dict:
        return {
            "type": "bt_classic",
            "mac": _random_mac(rng),
            "name": rng.choice(names),
            "rssi": rng.randint(-90, -40),
            "cod": _random_hex(rng, 3),
        }

    def dwell_update() -> dict:
        mac = rng.choice(tracked_macs)
        fs = first_seen_ms[mac]
        now_ms = (time.monotonic() - toadlet_start) * 1000 + fs
        return {
            "type": "dwell_update",
            "mac": mac,
            "first_seen_ms": int(fs),
            "last_seen_ms": int(now_ms),
            "dwell_ms": int(now_ms - fs),
            "rssi": rng.randint(-90, -40),
        }

    return FakeToadletProcess(
        mode_specs={
            "SLEEP": _ModeSpec(1.0, lambda: None),
            "BLE_SCAN": _ModeSpec(0.25, ble_adv),
            "BLE_PROXIMITY": _ModeSpec(0.25, ble_adv),
            "SKIMMER_DETECT": _ModeSpec(3.0, skimmer_match),
            "BT_CLASSIC_SCAN": _ModeSpec(2.5, bt_classic),
            "DWELL_LOG": _ModeSpec(1.0, dwell_update),
        },
        default_mode="BLE_SCAN",
    )
