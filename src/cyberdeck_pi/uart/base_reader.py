import asyncio
import logging
import time
from typing import AsyncIterator, ClassVar

import serial_asyncio_fast

from cyberdeck_pi.config import ToadletConfig
from cyberdeck_pi.models.events import StampedEvent
from cyberdeck_pi.uart.line_protocol import parse_line

log = logging.getLogger(__name__)

# Real documented JSON events are all well under a few hundred bytes.
# Bounding how far readline() will scan for a newline keeps a stray
# 0x0A inside binary frame content (e.g. right after switching to
# DEEP_CAPTURE) from letting the JSON reader silently eat an unbounded
# amount of binary data before it finds one.
_MAX_LINE_LENGTH = 4096

# Firmware bench testing (see Docs/pi-handoff-update-bench-testing.md) found
# that a MODE: command sent immediately after opening a fresh serial
# connection can get dropped or corrupted -- a USB-serial settling issue,
# worse right after a physical reconnect. Real fix: give the link a moment
# after connecting before writing anything, and send mode commands twice.
# Only real hardware has this settling behavior -- skip the wait against
# the mock (a pty), which has no such delay and would just slow tests down.
_CONNECT_SETTLE_S = 1.0
_MODE_RESEND_GAP_S = 0.2


class ToadletReader:
    toadlet: ClassVar[str]
    valid_modes: ClassVar[frozenset[str]]
    default_mode: ClassVar[str]
    # A mode name that switches the wire from newline-JSON to raw binary
    # framing (DEEP_CAPTURE, WiFi-only). None means every mode is JSON.
    binary_mode: ClassVar[str | None] = None

    def __init__(self, config: ToadletConfig, device_override: str | None = None):
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
            raise ValueError(f"no device configured for the {self.toadlet} toadlet")
        self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
            url=self._device, baudrate=self._config.baud, limit=_MAX_LINE_LENGTH
        )
        if self._config.source == "real":
            await asyncio.sleep(_CONNECT_SETTLE_S)

    async def events(self) -> AsyncIterator[StampedEvent]:
        """Yield parsed JSON events until the mode switches to binary
        framing (if this reader has one) or the connection closes."""
        if self._reader is None:
            raise RuntimeError("call connect() before events()")
        while True:
            if self.current_mode == self.binary_mode:
                return
            try:
                raw = await self._reader.readline()
            except asyncio.LimitOverrunError as exc:
                # No newline within a sane line length -- either genuinely
                # malformed data, or (more likely, right after a mode
                # switch) we've started reading binary frame bytes.
                # Discard what was scanned; exc.consumed bytes are left
                # in the buffer on this error and must be drained or the
                # next readline() hits the exact same wall immediately.
                await self._reader.read(exc.consumed)
                continue
            if not raw:
                return
            self.last_rx_monotonic = time.monotonic()
            event = parse_line(raw.decode("utf-8", errors="replace"))
            if event is None:
                continue
            yield StampedEvent(toadlet=self.toadlet, pi_ts=time.time(), event=event)

    async def set_mode(self, name: str, param: str | None = None) -> None:
        if name not in self.valid_modes:
            raise ValueError(f"{name!r} is not a valid mode for the {self.toadlet} toadlet")
        if self._writer is None:
            raise RuntimeError("call connect() before set_mode()")
        command = f"MODE:{name}" + (f":{param}" if param else "") + "\n"
        # Sent twice, a beat apart: bench testing found single commands can
        # get dropped or corrupted, and the protocol has no ack to tell us
        # whether the first one landed. Re-entering the same mode twice is
        # a no-op on the firmware side, so resending is always safe.
        self._writer.write(command.encode())
        await self._writer.drain()
        await asyncio.sleep(_MODE_RESEND_GAP_S)
        self._writer.write(command.encode())
        await self._writer.drain()
        self.current_mode = name
        self.current_param = param

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
