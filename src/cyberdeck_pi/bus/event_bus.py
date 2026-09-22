import asyncio
import logging

from cyberdeck_pi.models.events import StampedEvent

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 200


class EventBus:
    """In-process pub/sub for StampedEvents.

    Each subscriber gets its own bounded queue. A slow subscriber (a
    backgrounded dashboard tab, a stalled disk write) never blocks
    publish() -- on a full queue the oldest item for that subscriber
    only is dropped, since the UART reader's hot path must keep up with
    the wire in real time regardless of what any one consumer is doing.
    """

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE):
        self._queue_size = queue_size
        self._subscribers: dict[asyncio.Queue[StampedEvent], str | None] = {}

    def subscribe(self, toadlet: str | None = None) -> asyncio.Queue[StampedEvent]:
        queue: asyncio.Queue[StampedEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[queue] = toadlet
        return queue

    def unsubscribe(self, queue: asyncio.Queue[StampedEvent]) -> None:
        self._subscribers.pop(queue, None)

    def publish(self, event: StampedEvent) -> None:
        for queue, toadlet_filter in self._subscribers.items():
            if toadlet_filter is not None and toadlet_filter != event.toadlet:
                continue
            if queue.full():
                queue.get_nowait()
                log.debug("Subscriber queue full, dropped oldest event")
            queue.put_nowait(event)
