import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
log = logging.getLogger(__name__)

STATUS_INTERVAL_S = 1.0


@router.websocket("/ws/{toadlet}")
async def toadlet_events_ws(websocket: WebSocket, toadlet: str) -> None:
    state = websocket.app.state.cyberdeck
    if state.get_reader(toadlet) is None:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    queue = state.bus.subscribe(toadlet=toadlet)
    tasks = {
        asyncio.create_task(_watch_disconnect(websocket)),
        asyncio.create_task(_event_loop(websocket, queue)),
        asyncio.create_task(_status_loop(websocket, state, toadlet)),
    }
    try:
        # A pure send-only loop can't detect a client disconnect on its
        # own -- it never calls receive(), so a dead socket just spins
        # forever leaking the task and the bus subscription. Racing a
        # dedicated receive() watcher against the senders is what makes
        # the disconnect actually observable.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
        state.bus.unsubscribe(queue)


async def _watch_disconnect(websocket: WebSocket) -> None:
    # Raw receive() returns a "websocket.disconnect" message rather than
    # raising -- only the receive_text()/receive_json() wrappers translate
    # that into WebSocketDisconnect. Looping past it would re-call
    # receive() on an already-disconnected socket, which raises RuntimeError.
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _event_loop(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await websocket.send_json({"kind": "event", "data": event.model_dump(mode="json")})


async def _status_loop(websocket: WebSocket, state, toadlet: str) -> None:
    while True:
        # Looked up fresh each tick, not captured once at connect -- a
        # reconfigure_toadlet() call can swap in a new reader object for this
        # toadlet mid-connection, and a stale reference would freeze this
        # socket's status on the old reader's last mode forever.
        reader = state.get_reader(toadlet)
        payload = {"toadlet": toadlet, "mode": reader.current_mode, "online": state.detector.online[toadlet]}
        if toadlet == "wifi":
            payload["capture_active"] = state.capture_active
            payload["capture_bytes"] = state.capture_bytes
        await websocket.send_json({"kind": "status", "data": payload})
        await asyncio.sleep(STATUS_INTERVAL_S)
