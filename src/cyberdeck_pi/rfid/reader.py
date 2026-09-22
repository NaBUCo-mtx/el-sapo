import asyncio
import logging
import time
from typing import AsyncIterator

import serial_asyncio_fast

from cyberdeck_pi.config import RfidToadletConfig
from cyberdeck_pi.models.events import StampedEvent, TagRead
from cyberdeck_pi.rfid import protocol

log = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 2.0

# Same USB-serial settling issue the ESP32 toadlets' bench testing found (see
# Docs/pi-handoff-update-bench-testing.md) plausibly applies here too, since
# this is also a USB-serial link -- give it a moment before initialize()
# sends the first command. Skipped against the mock (a pty), which has no
# such delay.
_CONNECT_SETTLE_S = 1.0


class RfidReader:
    """Driver for the M7E Hecto UHF RFID module over its USB-serial link.

    Unlike WifiReader/BtReader, this isn't newline-JSON -- it's ThingMagic's
    binary Mercury API (see rfid/protocol.py). "Mode" here means "is
    continuous read running," which this reader implements as fire-and-
    forget commands (matching how set_mode() already behaves for the other
    two toadlets -- no ack is waited for there either). That also sidesteps a
    real hazard: initialize() and events() must never read the stream
    concurrently, since interleaved reads on one asyncio.StreamReader would
    corrupt framing for both. As long as start/stop stay write-only,
    events() can safely be the sole reader for the whole life of the
    connection after initialize() completes.
    """

    toadlet = "rfid"
    valid_modes = frozenset({"SLEEP", "CONTINUOUS_READ"})
    default_mode = "SLEEP"

    def __init__(self, config: RfidToadletConfig, device_override: str | None = None):
        self._config = config
        self._device = device_override or config.device
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.current_mode = self.default_mode
        self.current_param: str | None = None
        self.last_rx_monotonic: float | None = None
        self.offline_threshold_s = config.offline_threshold_s

    async def connect(self) -> None:
        if self._device is None:
            raise ValueError("no device configured for the rfid toadlet")
        self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
            url=self._device, baudrate=self._config.baud
        )
        if self._config.source == "real":
            await asyncio.sleep(_CONNECT_SETTLE_S)

    async def initialize(self) -> None:
        """One-time module setup: confirm it's alive, then set protocol,
        antenna, region, and power. Must run to completion before the
        pump task starts calling events() -- see class docstring."""
        region = protocol.REGION_BY_NAME.get(self._config.region.upper())
        if region is None:
            raise ValueError(f"unknown RFID region {self._config.region!r}")

        await self._command(protocol.encode_get_version(), protocol.OPCODE_VERSION)
        await self._command(protocol.encode_set_tag_protocol(), protocol.OPCODE_SET_TAG_PROTOCOL)
        await self._command(protocol.encode_set_antenna_port(), protocol.OPCODE_SET_ANTENNA_PORT)
        await self._command(protocol.encode_set_region(region), protocol.OPCODE_SET_REGION)
        await self._command(
            protocol.encode_set_read_power(self._config.read_power_cdbm), protocol.OPCODE_SET_READ_TX_POWER
        )
        await self._command(
            protocol.encode_disable_read_filter(), protocol.OPCODE_SET_READER_OPTIONAL_PARAMS
        )

    async def _command(self, wire_bytes: bytes, expected_opcode: int) -> protocol.RfidFrame:
        if self._reader is None or self._writer is None:
            raise RuntimeError("call connect() before sending commands")
        self._writer.write(wire_bytes)
        await self._writer.drain()
        frame = await asyncio.wait_for(protocol.read_frame(self._reader), timeout=COMMAND_TIMEOUT_S)
        if frame is None:
            raise RuntimeError("RFID module closed the connection during a command")
        if frame.opcode != expected_opcode:
            log.warning(
                "RFID module replied with opcode 0x%02X, expected 0x%02X", frame.opcode, expected_opcode
            )
        return frame

    async def set_mode(self, name: str, param: str | None = None) -> None:
        if name not in self.valid_modes:
            raise ValueError(f"{name!r} is not a valid mode for the rfid toadlet")
        if self._writer is None:
            raise RuntimeError("call connect() before set_mode()")
        wire_bytes = protocol.encode_start_reading() if name == "CONTINUOUS_READ" else protocol.encode_stop_reading()
        self._writer.write(wire_bytes)
        await self._writer.drain()
        self.current_mode = name
        self.current_param = param

    async def events(self) -> AsyncIterator[StampedEvent]:
        """Yield TagRead events forever, until the connection closes.

        Idles quietly while current_mode is SLEEP -- the module only sends
        anything unsolicited once CONTINUOUS_READ starts, so (unlike
        WifiReader's DEEP_CAPTURE handling) there's no framing ambiguity to
        resolve here: there's only ever one binary framing in play.
        """
        if self._reader is None:
            raise RuntimeError("call connect() before events()")
        while True:
            frame = await protocol.read_frame(self._reader)
            if frame is None:
                return
            self.last_rx_monotonic = time.monotonic()

            if frame.opcode != protocol.OPCODE_READ_TAG_ID_MULTIPLE:
                continue  # a stray response to some other opcode; not a tag stream event
            if len(frame.payload) in (0, 8, 10):
                # 0 = keepalive/temp-throttle/high-return-loss (connection
                # health only), 8/10 = unknown/temperature records this
                # reader doesn't decode yet.
                continue

            try:
                fields = protocol.parse_tag_found(frame.payload)
            except IndexError:
                log.warning("Dropping malformed RFID tag record (%d-byte payload)", len(frame.payload))
                continue

            event = TagRead(
                type="tag_read",
                epc=fields.epc.hex(),
                rssi=fields.rssi,
                frequency_khz=fields.frequency_khz,
                tx_antenna=fields.tx_antenna,
                rx_antenna=fields.rx_antenna,
                timestamp_ms=fields.timestamp_ms,
            )
            yield StampedEvent(toadlet=self.toadlet, pi_ts=time.time(), event=event)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
