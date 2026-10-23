import asyncio
import hmac
import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from .config import settings
from .protocol import HelloMsg, ContextFrameMsg, FeedbackMsg, ProjectClosedMsg
from .engine.suggest import build_suggestions

log = logging.getLogger("uvicorn.error")

app = FastAPI(title="SENSEI Service", version="0.1.0")

UI_QUEUE_SIZE = 256  # messages a UI may fall behind by before it is disconnected


class UiClient:
    # Everything sent to one UI goes through its queue and a single sender task,
    # so a UI that stops reading only ever blocks itself.
    def __init__(self, ws: WebSocket, backlog: int):
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=UI_QUEUE_SIZE + backlog)
        self.sender: asyncio.Task | None = None
        self.dropped = False  # set once; nothing is queued for this UI after that


class Hub:
    # Fans suggestions out to connected UIs, one stream per project. Every method is
    # synchronous, so hub state never changes halfway through another update.
    def __init__(self):
        self.clients: set[UiClient] = set()
        self.latest: dict[str, str] = {}  # project id -> last suggestions message
        self.hosts: dict[str, int] = {}   # project id -> connected host count
        self.background: set[asyncio.Task] = set()  # close tasks, kept referenced until they finish

    def subscribe(self, ws: WebSocket) -> UiClient:
        # The snapshot is queued in the same step as registration, so no publish or close can
        # land in between: a UI sees each open project's latest state, then every later message.
        ui = UiClient(ws, backlog=len(self.latest))
        for message in self.latest.values():
            ui.queue.put_nowait(message)
        self.clients.add(ui)
        return ui

    def start(self, ui: UiClient):
        ui.sender = asyncio.get_running_loop().create_task(self.pump(ui))

    async def pump(self, ui: UiClient):
        try:
            while True:
                await ui.ws.send_text(await ui.queue.get())
        except Exception as exc:  # cancellation is a BaseException and still propagates
            # The socket failed under us: stop delivering. The connection's handler sees the
            # disconnect and finishes cleanup; the task ends normally, with nothing left unretrieved.
            log.info("stopped sending to a UI whose connection failed: %r", exc)
            ui.dropped = True
            self.clients.discard(ui)

    def unsubscribe(self, ui: UiClient):
        ui.dropped = True
        self.clients.discard(ui)
        if ui.sender is not None:
            ui.sender.cancel()

    def send(self, ui: UiClient, message: str):
        if ui.dropped:
            return
        if ui.queue.full():
            log.warning("disconnecting a UI that stopped reading (%d messages behind)", ui.queue.qsize())
            self.unsubscribe(ui)
            closing = asyncio.get_running_loop().create_task(close_quietly(ui.ws))
            self.background.add(closing)
            closing.add_done_callback(self.background.discard)
            return
        ui.queue.put_nowait(message)

    def send_all(self, message: str):
        for ui in list(self.clients):
            self.send(ui, message)

    def publish(self, project_id: str, message: str):
        self.latest[project_id] = message
        self.send_all(message)

    def open(self, project_id: str):
        self.hosts[project_id] = self.hosts.get(project_id, 0) + 1

    def close(self, project_id: str):
        remaining = self.hosts.get(project_id, 0) - 1
        if remaining > 0:
            self.hosts[project_id] = remaining
            return
        self.hosts.pop(project_id, None)
        self.latest.pop(project_id, None)
        self.send_all(ProjectClosedMsg(project_id=project_id).model_dump_json())


hub = Hub()


async def close_quietly(ws: WebSocket):
    # No timeout: the close frame has to wait until the socket drains. A slow client that resumes
    # reading then gets it (1013: try again later) and reconnects with a fresh snapshot; a client
    # that never reads again is cut off by uvicorn's keepalive ping. The hub has dropped it already.
    try:
        await ws.close(code=1013)
    except Exception:
        pass


TOKEN_PROTOCOL = "sensei.token."


async def admit(ws: WebSocket) -> str | None:
    # Checks the handshake before accepting it. Returns the subprotocol to echo ("" for none),
    # or None after rejecting the connection (closing before accept -> HTTP 403).
    # Browsers always send Origin, so this blocks other web pages.
    origin = ws.headers.get("origin")
    if origin is not None and origin not in settings.allowed_origins:
        log.warning("rejected websocket from origin %r", origin)
        await ws.close(code=1008)
        return None

    offered = [p for p in ws.scope.get("subprotocols", []) if p.startswith(TOKEN_PROTOCOL)]
    if settings.token is not None:
        presented = offered[0][len(TOKEN_PROTOCOL):] if offered else ""
        if not hmac.compare_digest(presented.encode(), settings.token.encode()):
            log.warning("rejected websocket without a valid token")
            await ws.close(code=1008)
            return None

    # a browser drops the connection unless the subprotocol it offered is echoed back
    return offered[0] if offered else ""


def error(message: str) -> str:
    return json.dumps({"type": "error", "message": message})


def describe(err: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors())


async def serve(ws: WebSocket, reply, on_hello=None, on_frame=None, active=lambda: True):
    # Bad input gets an error reply; the socket stays open. Stops as soon as active() turns false.
    while active():
        raw = await ws.receive_text()
        if not active():
            return  # dropped while waiting: the message is ignored
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await reply(error("invalid json"))
            continue
        if not isinstance(data, dict):
            await reply(error("message must be a JSON object"))
            continue

        msg_type = data.get("type")
        try:
            if msg_type == "hello":
                hello = HelloMsg.model_validate(data)
                if on_hello is not None:
                    on_hello(hello)
                await reply(json.dumps({"type": "ack", "ok": True}))
            elif msg_type == "context_frame":
                if on_frame is None:
                    await reply(error("context_frame is only accepted on /ws/host"))
                else:
                    await on_frame(ContextFrameMsg.model_validate(data))
            elif msg_type == "feedback":
                fb = FeedbackMsg.model_validate(data)
                # accept feedback (store later)
                log.info("feedback %r %s (project %r)", fb.suggestion_id, fb.verdict, fb.project_id)
                await reply(json.dumps({"type": "ack_feedback", "ok": True}))
            else:
                await reply(error("unknown msg type"))
        except ValidationError as err:
            await reply(error(f"invalid {msg_type}: {describe(err)}"))


@app.get("/health")
def health():
    return {"ok": True}


@app.websocket("/ws/host")
async def ws_host(ws: WebSocket):
    subprotocol = await admit(ws)
    if subprotocol is None:
        return
    await ws.accept(subprotocol=subprotocol or None)

    hello: HelloMsg | None = None

    def on_hello(msg: HelloMsg):
        nonlocal hello
        previous, hello = hello, msg
        # register the new project before releasing the old one, so re-sending the same
        # project never closes it in between
        hub.open(msg.project.id)
        if previous is not None:
            hub.close(previous.project.id)

    async def on_frame(ctx: ContextFrameMsg):
        if hello is None:
            await ws.send_text(error("send hello before context_frame"))
            return
        msg = build_suggestions(ctx)
        msg.project_id, msg.host = hello.project.id, hello.host.name
        suggestions = msg.model_dump_json()
        hub.publish(hello.project.id, suggestions)  # never waits on a UI
        await ws.send_text(suggestions)

    try:
        await serve(ws, ws.send_text, on_hello, on_frame)
    except WebSocketDisconnect:
        return
    finally:
        if hello is not None:
            hub.close(hello.project.id)


@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket):
    subprotocol = await admit(ws)
    if subprotocol is None:
        return
    # subscribed before the handshake completes, so nothing published meanwhile is missed
    ui = hub.subscribe(ws)

    async def reply(message: str):
        hub.send(ui, message)

    try:
        await ws.accept(subprotocol=subprotocol or None)
        hub.start(ui)
        await serve(ws, reply, active=lambda: not ui.dropped)
        # Dropped (fell behind, or its socket failed). Our close may still be waiting for the socket
        # to drain, and Starlette refuses receive_text() once a close has begun, so read raw messages
        # (ignored) until the connection ends; uvicorn's keepalive still cuts a client that never reads.
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass
    except WebSocketDisconnect:
        return
    finally:
        hub.unsubscribe(ui)
