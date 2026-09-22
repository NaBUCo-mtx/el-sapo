import json
import logging

from pydantic import TypeAdapter, ValidationError

from cyberdeck_pi.models.events import Event

log = logging.getLogger(__name__)

_event_adapter: TypeAdapter = TypeAdapter(Event)


def parse_line(raw: str) -> Event | None:
    """Parse one line of the newline-delimited JSON event protocol.

    Never raises: malformed JSON or a schema mismatch is logged and
    dropped rather than killing the reader loop, since the protocol
    hasn't been end-to-end tested against real firmware yet.
    """
    line = raw.strip()
    if not line:
        return None

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        log.warning("Dropping malformed JSON line: %r", raw)
        return None

    try:
        return _event_adapter.validate_python(data)
    except ValidationError as exc:
        log.warning("Dropping line that failed event validation: %r (%s)", raw, exc)
        return None
