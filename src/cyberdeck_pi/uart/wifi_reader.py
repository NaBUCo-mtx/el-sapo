import time
from typing import AsyncIterator

from cyberdeck_pi.capture.frame_parser import read_frames
from cyberdeck_pi.uart.base_reader import ToadletReader


class WifiReader(ToadletReader):
    toadlet = "wifi"
    valid_modes = frozenset(
        {
            "SLEEP",
            "PASSIVE_SCAN",
            "PROBE_SNIFF",
            "CHANNEL_MONITOR",
            "DEAUTH_DETECT",
            "EAPOL_CAPTURE",
            "DEEP_CAPTURE",
        }
    )
    default_mode = "PASSIVE_SCAN"
    binary_mode = "DEEP_CAPTURE"

    async def deep_capture_frames(self) -> AsyncIterator[bytes]:
        """Yield raw 802.11 frames while in DEEP_CAPTURE mode.

        Ends as soon as the mode switches away (or the connection
        closes) -- callers alternate between this and events() based
        on current_mode, same as the firmware alternates its framing.
        """
        if self._reader is None:
            raise RuntimeError("call connect() before deep_capture_frames()")
        async for frame in read_frames(self._reader):
            if self.current_mode != self.binary_mode:
                return
            self.last_rx_monotonic = time.monotonic()
            yield frame
