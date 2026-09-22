import struct
import time
from pathlib import Path

PCAP_MAGIC = 0xA1B2C3D4
PCAP_VERSION_MAJOR = 2
PCAP_VERSION_MINOR = 4
PCAP_SNAPLEN = 65535
LINKTYPE_IEEE802_11 = 105  # raw 802.11 frames, no radiotap header

_GLOBAL_HEADER = struct.Struct("<IHHIIII")
_RECORD_HEADER = struct.Struct("<IIII")


class PcapWriter:
    """Writes the classic libpcap format for one DEEP_CAPTURE session.

    Firmware sends raw 802.11 frames only, no PCAP framing of its own
    -- the global header (written once) and per-packet headers (one
    per frame) are added here.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("wb")
        self._file.write(
            _GLOBAL_HEADER.pack(
                PCAP_MAGIC,
                PCAP_VERSION_MAJOR,
                PCAP_VERSION_MINOR,
                0,  # thiszone
                0,  # sigfigs
                PCAP_SNAPLEN,
                LINKTYPE_IEEE802_11,
            )
        )
        self._file.flush()
        self.bytes_written = _GLOBAL_HEADER.size

    def write_frame(self, frame: bytes, ts: float) -> None:
        ts_sec = int(ts)
        ts_usec = int((ts - ts_sec) * 1_000_000)
        self._file.write(_RECORD_HEADER.pack(ts_sec, ts_usec, len(frame), len(frame)))
        self._file.write(frame)
        self._file.flush()
        self.bytes_written += _RECORD_HEADER.size + len(frame)

    def close(self) -> None:
        self._file.close()


def capture_file_path(root: Path, channel_param: str | None) -> Path:
    channel = (channel_param or "unknown").removeprefix("ch")
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return root / "captures" / f"deep_capture_ch{channel}_{timestamp}.pcap"
