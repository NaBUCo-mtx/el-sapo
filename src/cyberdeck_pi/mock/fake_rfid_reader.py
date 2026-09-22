import asyncio
import os
import pty
import random

from cyberdeck_pi.rfid import protocol

# Plausible-looking 96-bit Gen2 EPCs (SGTIN-96-style headers) for a handful
# of tags that stay "in range" for the life of the mock.
_DEFAULT_EPCS = [
    "E28011700000021BC1A1E97C",
    "E28011700000021BC1A1E98D",
    "E28011700000021BC1A1E99E",
]


class FakeRfidReader:
    """Simulates an M7E Hecto module on the other end of a USB-serial link.

    Answers Mercury API commands (see rfid/protocol.py) and, once a
    continuous-read start command arrives, emits realistic-cadence
    keepalive and tag-found binary frames -- mirroring FakeToadletProcess's
    role for the WiFi/BT toadlets, adapted for a binary request/response +
    streaming protocol instead of newline-JSON.
    """

    def __init__(self, tag_epcs: list[str] | None = None, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._epcs = tag_epcs or _DEFAULT_EPCS
        self._reading = False
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        # Non-blocking for the same reason as FakeToadletProcess: add_reader
        # only promises "readable now", not "read() won't block".
        os.set_blocking(master_fd, False)
        self.device_path = os.ttyname(slave_fd)
        os.close(slave_fd)
        self._cmd_buf = b""
        self._emit_task: asyncio.Task | None = None

    @property
    def reading(self) -> bool:
        return self._reading

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

    async def __aenter__(self) -> "FakeRfidReader":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.stop()

    def _on_readable(self) -> None:
        try:
            chunk = os.read(self._master_fd, 4096)
        except (BlockingIOError, OSError):
            return
        if not chunk:
            return
        self._cmd_buf += chunk
        while True:
            result = protocol.parse_command_bytes(self._cmd_buf)
            if result is None:
                break
            command, consumed = result
            self._cmd_buf = self._cmd_buf[consumed:]
            self._handle_command(command)

    def _handle_command(self, command: protocol.CommandFrame) -> None:
        if command.opcode == protocol.OPCODE_MULTI_PROTOCOL_TAG_OP:
            # Sub-option byte: 0x01 = start continuous read, 0x02 = stop.
            # Real RfidReader.set_mode() is fire-and-forget for both (see
            # its docstring), so no response is required here either.
            if len(command.data) >= 3:
                if command.data[2] == 0x01:
                    self._reading = True
                elif command.data[2] == 0x02:
                    self._reading = False
            return

        # Every other command used by RfidReader.initialize() (getVersion,
        # setTagProtocol, setAntennaPort, setRegion, setReadPower,
        # disableReadFilter) just gets a generic "all good, same opcode,
        # no payload" ack -- enough for initialize()'s opcode-match check
        # without modeling each command's real reply shape.
        self._write_response(command.opcode, status=0x0000, payload=b"")

    def _write_response(self, opcode: int, status: int, payload: bytes) -> None:
        length = len(payload)
        body = bytes([length, opcode, (status >> 8) & 0xFF, status & 0xFF]) + payload
        crc = protocol.calculate_crc(body)
        frame = bytes([protocol.HEADER]) + body + crc.to_bytes(2, "big")
        try:
            os.write(self._master_fd, frame)
        except BlockingIOError:
            pass  # reader hasn't drained fast enough -- drop it, same as the real toadlets

    async def _emit_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_keepalive = 0.0
        while True:
            if self._reading:
                now = loop.time()
                if now >= next_keepalive:
                    self._write_response(
                        protocol.OPCODE_READ_TAG_ID_MULTIPLE, status=protocol.STATUS_KEEPALIVE, payload=b""
                    )
                    next_keepalive = now + 1.0
                self._write_response(protocol.OPCODE_READ_TAG_ID_MULTIPLE, status=0x0000, payload=self._make_tag_payload())
                await asyncio.sleep(self._rng.uniform(0.1, 0.4))
            else:
                await asyncio.sleep(0.2)

    def _make_tag_payload(self) -> bytes:
        epc = bytes.fromhex(self._rng.choice(self._epcs))
        rssi_byte = self._rng.randint(180, 230)  # -76..-26 dBm via the byte-256 convention
        antenna = 0x11  # TX port 1, RX port 1 (4 MSB / 4 LSB)
        frequency_khz = self._rng.choice([902750, 908750, 915250, 920750])  # plausible NA channels
        timestamp_ms = self._rng.randint(0, 999_999)
        epc_len_bits = (len(epc) + 4) * 8  # PC(2) + EPC + EPC-CRC(2), matches parse_tag_found's -4 convention
        pc_word = (len(epc) // 2) << 11  # standard Gen2 PC word: top 5 bits = EPC length in 16-bit words

        payload = bytearray()
        payload += bytes(7)  # [0:7] RFU
        payload.append(rssi_byte)  # [7] RSSI
        payload.append(antenna)  # [8] antenna
        payload += frequency_khz.to_bytes(3, "big")  # [9:12] frequency
        payload += timestamp_ms.to_bytes(4, "big")  # [12:16] timestamp
        payload += bytes(2)  # [16:18] phase
        payload.append(protocol.TAG_PROTOCOL_GEN2)  # [18] protocol
        payload += (0).to_bytes(2, "big")  # [19:21] embedded tag data length (bits) = 0
        payload.append(0x0F)  # [21] RFU
        payload += epc_len_bits.to_bytes(2, "big")  # [22:24] EPC length (bits)
        payload += pc_word.to_bytes(2, "big")  # [24:26] PC bits
        payload += epc  # [26:26+len(epc)] EPC
        payload += b"\x00\x00"  # EPC CRC (placeholder -- not verified downstream)
        return bytes(payload)
