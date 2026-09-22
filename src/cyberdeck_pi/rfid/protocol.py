import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ThingMagic Mercury API serial protocol, as spoken by the M7E Hecto module.
# Byte layout and CRC verified against SparkFun's open-source (MIT) Arduino
# library, which documents the wire format precisely in code comments:
# https://github.com/sparkfun/SparkFun_Simultaneous_RFID_Tag_Reader_Library
#   src/SparkFun_UHF_RFID_Reader.h / .cpp

HEADER = 0xFF

# Opcodes actually used here (subset of the full Mercury API).
OPCODE_VERSION = 0x03
OPCODE_MULTI_PROTOCOL_TAG_OP = 0x2F
OPCODE_SET_ANTENNA_PORT = 0x91
OPCODE_SET_READ_TX_POWER = 0x92
OPCODE_SET_TAG_PROTOCOL = 0x93
OPCODE_SET_REGION = 0x97
OPCODE_SET_READER_OPTIONAL_PARAMS = 0x9A

OPCODE_READ_TAG_ID_MULTIPLE = 0x22  # both the "start reading" sub-op and every
# continuous-read response (tag found / keepalive / etc.) come back tagged
# with this opcode.

TAG_PROTOCOL_GEN2 = 0x05

REGION_NORTHAMERICA = 0x01
REGION_INDIA = 0x04
REGION_JAPAN = 0x05
REGION_CHINA = 0x06
REGION_EUROPE = 0x08
REGION_KOREA = 0x09
REGION_AUSTRALIA = 0x0B
REGION_NEWZEALAND = 0x0C
REGION_NORTHAMERICA2 = 0x0D
REGION_NORTHAMERICA3 = 0x0E
REGION_OPEN = 0xFF

REGION_BY_NAME = {
    "NORTHAMERICA": REGION_NORTHAMERICA,
    "INDIA": REGION_INDIA,
    "JAPAN": REGION_JAPAN,
    "CHINA": REGION_CHINA,
    "EUROPE": REGION_EUROPE,
    "KOREA": REGION_KOREA,
    "AUSTRALIA": REGION_AUSTRALIA,
    "NEWZEALAND": REGION_NEWZEALAND,
    "NORTHAMERICA2": REGION_NORTHAMERICA2,
    "NORTHAMERICA3": REGION_NORTHAMERICA3,
    "OPEN": REGION_OPEN,
}

# Continuous-read (opcode 0x22) status-word special cases, only meaningful
# when the response carries zero bytes of payload.
STATUS_KEEPALIVE = 0x0400
STATUS_TEMPTHROTTLE = 0x0504
STATUS_HIGHRETURNLOSS = 0x0505

# configBlob bytes are opaque, undocumented-by-ThingMagic option blobs
# reverse-engineered by SparkFun from the Universal Reader Assistant's
# transport logs -- not worth decoding further than "this is what starts/
# stops continuous GEN2 reads."
_START_READING_BLOB = bytes(
    [0x00, 0x00, 0x01, 0x22, 0x00, 0x00, 0x05, 0x07, 0x22, 0x10, 0x00, 0x1B, 0x03, 0xE8, 0x01, 0xFF]
)
_STOP_READING_BLOB = bytes([0x00, 0x00, 0x02])
_DISABLE_READ_FILTER_BLOB = bytes([0x01, 0x0C, 0x00])  # key-value form, option 0x0C=off

# ThingMagic's CRC-16 variant: nibble-at-a-time, table-driven, init 0xFFFF.
# Close to but not the same as CRC-CCITT (different init value).
_CRC_TABLE = (
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
    0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
)


def calculate_crc(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        index = crc >> 12
        crc = (((crc << 4) & 0xFFFF) | (byte >> 4)) ^ _CRC_TABLE[index]
        index = crc >> 12
        crc = (((crc << 4) & 0xFFFF) | (byte & 0x0F)) ^ _CRC_TABLE[index]
    return crc


def encode_command(opcode: int, data: bytes = b"") -> bytes:
    """[0xFF][LEN][OPCODE][DATA...][CRC_HI][CRC_LO]. CRC covers LEN+OPCODE+DATA."""
    body = bytes([len(data), opcode]) + data
    crc = calculate_crc(body)
    return bytes([HEADER]) + body + crc.to_bytes(2, "big")


@dataclass
class RfidFrame:
    opcode: int
    status: int
    payload: bytes  # everything between status and the frame's trailing CRC


async def read_frame(reader: asyncio.StreamReader) -> RfidFrame | None:
    """Read one CRC-valid frame, resyncing past anything that doesn't parse.

    Response layout: [0xFF][LEN][OPCODE][STATUS_HI][STATUS_LO][...LEN bytes
    of payload...][CRC_HI][CRC_LO] -- total LEN+7 bytes. Ends quietly (None)
    on EOF. A bad CRC or a truncated read both fall back to re-hunting for
    the next 0xFF header rather than trusting misaligned data -- the same
    "bound the damage, don't trust a corrupt length blindly" approach used
    for DEEP_CAPTURE's binary framing.
    """
    while True:
        try:
            header = await reader.readexactly(1)
        except asyncio.IncompleteReadError:
            return None
        if header[0] != HEADER:
            continue

        try:
            length_byte = await reader.readexactly(1)
            rest = await reader.readexactly(length_byte[0] + 5)  # opcode+status+payload+crc
        except asyncio.IncompleteReadError:
            return None

        length = length_byte[0]
        opcode = rest[0]
        status = (rest[1] << 8) | rest[2]
        payload = rest[3 : 3 + length]
        crc_received = (rest[3 + length] << 8) | rest[4 + length]
        crc_computed = calculate_crc(length_byte + rest[: 3 + length])
        if crc_received != crc_computed:
            log.debug("Dropping RFID frame with bad CRC, resyncing")
            continue

        return RfidFrame(opcode=opcode, status=status, payload=payload)


@dataclass
class CommandFrame:
    opcode: int
    data: bytes


def parse_command_bytes(buffer: bytes) -> tuple[CommandFrame, int] | None:
    """Pull one command frame (the Pi->module direction -- no status field,
    see encode_command()) off the front of `buffer`, discarding any leading
    noise before the next 0xFF. Returns (frame, bytes_consumed), or None if
    there isn't a complete frame buffered yet.

    Only used by the mock module simulator to decode incoming commands; a
    real RfidReader only ever builds these via encode_command() and never
    needs to parse its own command shape back out. Unlike read_frame(),
    this doesn't verify CRC -- it only ever needs to understand commands
    this same codebase generated.
    """
    header_index = buffer.find(bytes([HEADER]))
    if header_index == -1 or header_index + 2 > len(buffer):
        return None
    length = buffer[header_index + 1]
    end = header_index + length + 5
    if end > len(buffer):
        return None
    opcode = buffer[header_index + 2]
    data = buffer[header_index + 3 : header_index + 3 + length]
    return CommandFrame(opcode=opcode, data=data), end


@dataclass
class TagFoundFields:
    rssi: int
    tx_antenna: int
    rx_antenna: int
    frequency_khz: int
    timestamp_ms: int
    epc: bytes


def parse_tag_found(payload: bytes) -> TagFoundFields:
    """Pull the fields out of a full tag-record payload (opcode 0x22,
    LEN not one of the special 0x00/0x08/0x0A short forms). Offsets below
    are relative to the start of `payload` (i.e. absolute-frame-offset - 5,
    since payload starts right after the 2-byte status field) -- verified
    by hand against the reference library's own annotated example frame.
    """
    tag_data_bits = (payload[19] << 8) | payload[20]
    m = -(-tag_data_bits // 8)  # ceiling division: bits -> bytes

    rssi = payload[7] - 256
    antenna = payload[8]
    frequency_khz = (payload[9] << 16) | (payload[10] << 8) | payload[11]
    timestamp_ms = int.from_bytes(payload[12:16], "big")

    epc_len_bits = (payload[22 + m] << 8) | payload[23 + m]
    epc_bytes_count = epc_len_bits // 8 - 4  # subtract PC (2 bytes) + EPC CRC (2 bytes)
    epc_start = 26 + m
    epc = payload[epc_start : epc_start + epc_bytes_count]

    return TagFoundFields(
        rssi=rssi,
        tx_antenna=antenna >> 4,
        rx_antenna=antenna & 0x0F,
        frequency_khz=frequency_khz,
        timestamp_ms=timestamp_ms,
        epc=epc,
    )


# High-level command encoders -- these are the module's only public,
# ready-to-write interface; the raw blobs above are private implementation
# detail.


def encode_get_version() -> bytes:
    return encode_command(OPCODE_VERSION)


def encode_set_tag_protocol(protocol_id: int = TAG_PROTOCOL_GEN2) -> bytes:
    return encode_command(OPCODE_SET_TAG_PROTOCOL, bytes([0x00, protocol_id]))


def encode_set_antenna_port(tx_port: int = 1, rx_port: int = 1) -> bytes:
    return encode_command(OPCODE_SET_ANTENNA_PORT, bytes([tx_port, rx_port]))


def encode_set_region(region: int) -> bytes:
    return encode_command(OPCODE_SET_REGION, bytes([region]))


def encode_set_read_power(centi_dbm: int) -> bytes:
    """centi_dbm: hundredths of a dBm, e.g. 500 = 5.00 dBm. Max 2700 (27 dBm)."""
    return encode_command(OPCODE_SET_READ_TX_POWER, centi_dbm.to_bytes(2, "big", signed=True))


def encode_disable_read_filter() -> bytes:
    return encode_command(OPCODE_SET_READER_OPTIONAL_PARAMS, _DISABLE_READ_FILTER_BLOB)


def encode_start_reading() -> bytes:
    return encode_command(OPCODE_MULTI_PROTOCOL_TAG_OP, _START_READING_BLOB)


def encode_stop_reading() -> bytes:
    return encode_command(OPCODE_MULTI_PROTOCOL_TAG_OP, _STOP_READING_BLOB)
