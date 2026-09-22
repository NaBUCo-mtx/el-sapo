import asyncio
import struct
from typing import AsyncIterator

# DEEP_CAPTURE framing: [2-byte big-endian length][raw 802.11 frame bytes].
# The length field counts only the frame bytes, not itself.
_FRAME_LENGTH = struct.Struct(">H")

# A generous upper bound on a real 802.11 frame (max MSDU is ~2304 bytes;
# this leaves headroom). Used only to catch framing misalignment -- e.g.
# right after the JSON-to-binary mode switch, where a stray newline byte
# inside earlier binary content can fool the JSON reader into stopping
# mid-frame, leaving the next read starting at a bogus offset. Trusting
# a wildly large length there would mean blocking for data that's never
# coming; treating it as corrupt and resyncing keeps this bounded.
_MAX_PLAUSIBLE_FRAME_LEN = 4096


async def read_frames(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Read length-prefixed binary frames until the stream ends.

    Ends quietly on EOF, including a truncated trailing frame (the
    connection dropping mid-frame is expected to happen sometimes --
    there's no retry/replay in this protocol, so a partial last frame
    is simply discarded rather than treated as an error).
    """
    while True:
        try:
            header = await reader.readexactly(_FRAME_LENGTH.size)
        except asyncio.IncompleteReadError:
            return
        (length,) = _FRAME_LENGTH.unpack(header)
        if length > _MAX_PLAUSIBLE_FRAME_LEN:
            continue
        try:
            frame = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            return
        yield frame
