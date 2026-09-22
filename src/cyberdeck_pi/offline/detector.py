import asyncio
import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)

CHECK_INTERVAL_S = 1.0


class _MonitoredReader(Protocol):
    last_rx_monotonic: float | None
    offline_threshold_s: float


class OfflineDetector:
    """Watches each reader's last_rx_monotonic against its configured
    threshold. Per the protocol, "no data for N seconds" is the only
    offline signal that exists -- there's no kill-switch or graceful-
    shutdown message. Tracks online/offline state per toadlet, edge-
    triggered (only logs/updates on an actual state change).
    """

    def __init__(self, readers: dict[str, _MonitoredReader], check_interval_s: float = CHECK_INTERVAL_S):
        self._readers = readers
        self._check_interval_s = check_interval_s
        self.online: dict[str, bool] = {toadlet: False for toadlet in readers}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            for toadlet, reader in self._readers.items():
                self._check(toadlet, reader)
            await asyncio.sleep(self._check_interval_s)

    def _check(self, toadlet: str, reader: _MonitoredReader) -> None:
        now_online = self._is_online(reader)
        if now_online != self.online[toadlet]:
            self.online[toadlet] = now_online
            log.info("%s toadlet is now %s", toadlet, "online" if now_online else "offline")

    @staticmethod
    def _is_online(reader: _MonitoredReader) -> bool:
        if reader.last_rx_monotonic is None:
            return False
        return (time.monotonic() - reader.last_rx_monotonic) < reader.offline_threshold_s
