import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from cyberdeck_pi.bus.event_bus import EventBus
from cyberdeck_pi.models.events import StampedEvent

log = logging.getLogger(__name__)


class EventLogger:
    """Persists every bus event to storage.root/logs/<toadlet>/<date>.jsonl.

    Keeps one file handle open per (toadlet, date) rather than reopening on
    every write -- durability comes from flush() after each line, not
    from a full close/reopen cycle on the hot path.
    """

    def __init__(self, bus: EventBus, root: Path):
        self._bus = bus
        self._root = root
        self._queue = bus.subscribe()
        self._task: asyncio.Task | None = None
        self._open_files: dict[tuple[str, str], IO[str]] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._bus.unsubscribe(self._queue)
        for f in self._open_files.values():
            f.close()
        self._open_files.clear()

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            self._append(event)

    def _append(self, event: StampedEvent) -> None:
        date_str = datetime.fromtimestamp(event.pi_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        key = (event.toadlet, date_str)
        f = self._open_files.get(key)
        if f is None:
            toadlet_dir = self._root / "logs" / event.toadlet
            toadlet_dir.mkdir(parents=True, exist_ok=True)
            f = (toadlet_dir / f"{date_str}.jsonl").open("a", encoding="utf-8")
            self._open_files[key] = f
        f.write(event.model_dump_json())
        f.write("\n")
        f.flush()
