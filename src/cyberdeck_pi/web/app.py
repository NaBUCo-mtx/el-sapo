import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cyberdeck_pi.bus.event_bus import EventBus
from cyberdeck_pi.capture.pcap_writer import PcapWriter, capture_file_path
from cyberdeck_pi.config import ToadletConfig, RfidToadletConfig, Settings, save_settings
from cyberdeck_pi.mock.fake_toadlet_process import FakeToadletProcess, make_bt_fake_toadlet, make_wifi_fake_toadlet
from cyberdeck_pi.mock.fake_rfid_reader import FakeRfidReader
from cyberdeck_pi.offline.detector import OfflineDetector
from cyberdeck_pi.rfid.reader import RfidReader
from cyberdeck_pi.storage.event_logger import EventLogger
from cyberdeck_pi.uart.base_reader import ToadletReader
from cyberdeck_pi.uart.bt_reader import BtReader
from cyberdeck_pi.uart.wifi_reader import WifiReader
from cyberdeck_pi.web import routes, ws

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class CyberdeckState:
    def __init__(self, settings: Settings, config_path: Path | None = None):
        self.settings = settings
        self._config_path = config_path
        self.bus = EventBus()
        self._readers: dict[str, ToadletReader | RfidReader] = {}
        self.logger: EventLogger | None = None
        self.detector: OfflineDetector | None = None
        self.capture_active = False
        self.capture_bytes = 0
        self._mock_toadlets: dict[str, FakeToadletProcess | FakeRfidReader] = {}
        self._pump_tasks: dict[str, asyncio.Task] = {}
        # Serializes reconfigure_toadlet() calls against each other and against
        # stop() -- both tear down and rebuild reader/pump/mock state, and
        # interleaving two of them would corrupt _readers/_mock_toadlets.
        self._reconfig_lock = asyncio.Lock()

    @property
    def wifi_reader(self) -> WifiReader | None:
        return self._readers.get("wifi")  # type: ignore[return-value]

    @property
    def bt_reader(self) -> BtReader | None:
        return self._readers.get("bt")  # type: ignore[return-value]

    @property
    def rfid_reader(self) -> RfidReader | None:
        return self._readers.get("rfid")  # type: ignore[return-value]

    def get_reader(self, toadlet: str) -> ToadletReader | RfidReader | None:
        return self._readers.get(toadlet)

    async def start(self) -> None:
        self._readers["wifi"] = await self._build_reader(WifiReader, self.settings.toadlets.wifi, make_wifi_fake_toadlet)
        self._readers["bt"] = await self._build_reader(BtReader, self.settings.toadlets.bt, make_bt_fake_toadlet)
        self._readers["rfid"] = await self._build_rfid_reader(self.settings.toadlets.rfid)

        self.logger = EventLogger(self.bus, Path(self.settings.storage.root))
        self.logger.start()

        # Handed the live dict, not a copy -- reconfigure_toadlet() swapping a
        # value in self._readers is immediately visible to the detector too.
        self.detector = OfflineDetector(self._readers)
        self.detector.start()

        for toadlet in ("wifi", "bt", "rfid"):
            self._start_pump(toadlet)

    async def stop(self) -> None:
        for toadlet in list(self._pump_tasks):
            await self._stop_pump(toadlet)
        for reader in self._readers.values():
            await reader.close()
        for mock_toadlet in self._mock_toadlets.values():
            await mock_toadlet.stop()
        if self.logger is not None:
            await self.logger.stop()
        if self.detector is not None:
            await self.detector.stop()

    async def reconfigure_toadlet(self, toadlet: str, config: ToadletConfig | RfidToadletConfig) -> None:
        """Swap a toadlet's device/baud/source live, without restarting the
        process. Builds and connects the replacement reader *before*
        touching the old one, so a bad device path or unreachable port
        leaves the toadlet running on its previous config instead of dead."""
        if toadlet not in self._readers:
            raise ValueError(f"unknown toadlet {toadlet!r}")
        async with self._reconfig_lock:
            old_reader = self._readers[toadlet]
            old_mock = self._mock_toadlets.pop(toadlet, None)
            try:
                if toadlet == "wifi":
                    new_reader = await self._build_reader(WifiReader, config, make_wifi_fake_toadlet)
                elif toadlet == "bt":
                    new_reader = await self._build_reader(BtReader, config, make_bt_fake_toadlet)
                else:
                    new_reader = await self._build_rfid_reader(config)
            except Exception:
                # _build_* may have already registered a new mock under this
                # key before failing later (e.g. RFID initialize() timeout)
                # -- stop it so its pty subprocess doesn't leak, then restore
                # the still-live old mock's dict entry.
                leaked_mock = self._mock_toadlets.get(toadlet)
                if leaked_mock is not None and leaked_mock is not old_mock:
                    await leaked_mock.stop()
                if old_mock is not None:
                    self._mock_toadlets[toadlet] = old_mock
                else:
                    self._mock_toadlets.pop(toadlet, None)
                raise

            await self._stop_pump(toadlet)
            self._readers[toadlet] = new_reader
            if toadlet == "wifi":
                self.capture_active = False
            self._start_pump(toadlet)

            await old_reader.close()
            if old_mock is not None:
                await old_mock.stop()

            self.settings = self.settings.model_copy(
                update={"toadlets": self.settings.toadlets.model_copy(update={toadlet: config})}
            )
            if self._config_path is not None:
                save_settings(self._config_path, self.settings)

    def _start_pump(self, toadlet: str) -> None:
        reader = self._readers[toadlet]
        coro = self._pump_wifi(reader) if toadlet == "wifi" else self._pump(reader)
        self._pump_tasks[toadlet] = asyncio.create_task(coro)

    async def _stop_pump(self, toadlet: str) -> None:
        task = self._pump_tasks.pop(toadlet, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _build_reader(self, reader_cls: type[ToadletReader], config: ToadletConfig, mock_factory) -> ToadletReader:
        device_override = None
        if config.source == "mock":
            mock_toadlet = mock_factory()
            await mock_toadlet.start()
            device_override = mock_toadlet.device_path
        reader = reader_cls(config, device_override=device_override)
        if device_override is not None:
            self._mock_toadlets[reader.toadlet] = mock_toadlet
        await reader.connect()
        log.info("%s reader connected (source=%s device=%s)", reader.toadlet, config.source, device_override or config.device)
        return reader

    async def _build_rfid_reader(self, config: RfidToadletConfig) -> RfidReader:
        device_override = None
        if config.source == "mock":
            mock_toadlet = FakeRfidReader()
            await mock_toadlet.start()
            device_override = mock_toadlet.device_path
        reader = RfidReader(config, device_override=device_override)
        if device_override is not None:
            self._mock_toadlets["rfid"] = mock_toadlet
        await reader.connect()
        await reader.initialize()  # must complete before the pump task starts reading -- see RfidReader docstring
        log.info("rfid reader connected (source=%s device=%s)", config.source, device_override or config.device)
        return reader

    async def _pump(self, reader: ToadletReader | RfidReader) -> None:
        async for event in reader.events():
            self.bus.publish(event)

    async def _pump_wifi(self, reader: WifiReader) -> None:
        # events() ends either because the connection closed for good,
        # or because the mode switched to binary framing -- current_mode
        # is what tells those two apart, since both look like "the
        # generator returned" from here.
        while True:
            async for event in reader.events():
                self.bus.publish(event)
            if reader.current_mode != reader.binary_mode:
                return
            await self._run_deep_capture(reader)

    async def _run_deep_capture(self, reader: WifiReader) -> None:
        path = capture_file_path(Path(self.settings.storage.root), reader.current_param)
        writer = PcapWriter(path)
        self.capture_active = True
        self.capture_bytes = writer.bytes_written
        log.info("DEEP_CAPTURE started -> %s", path)
        try:
            async for frame in reader.deep_capture_frames():
                writer.write_frame(frame, time.time())
                self.capture_bytes = writer.bytes_written
        finally:
            writer.close()
            self.capture_active = False
            log.info("DEEP_CAPTURE stopped (%d bytes written)", self.capture_bytes)


def create_app(settings: Settings, config_path: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = CyberdeckState(settings, config_path=config_path)
        await state.start()
        app.state.cyberdeck = state
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes.router)
    app.include_router(ws.router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
