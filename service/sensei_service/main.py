import asyncio
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from .clients import authenticate
from .config import settings
from .feedback import FeedbackStore, arm_of
from .protocol import (ActionOutcomeMsg, ActionResultMsg, ApplyAckMsg, ApplyActionMsg, ApplyMsg, ContextFrameMsg,
                       FeedbackAckMsg, FeedbackMsg, HelloMsg, ProjectClosedMsg, checked_params)
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


def action_id_of(project_id: str, suggestion_id: str) -> str:
    # the same suggestion in the same project always gets the same id, so hosts can deduplicate
    return "act_" + hashlib.sha256(f"{project_id}/{suggestion_id}".encode()).hexdigest()[:12]


@dataclass
class Sent:
    # An action sent to a project's hosts: the results they still owe, and the Applies waiting for its outcome.
    project_id: str
    item: dict  # the suggestion as it was applied
    awaited: Counter = field(default_factory=Counter)  # host socket -> results it still owes (one per send)
    # one entry per Apply waiting for the outcome: the UI that sent it, and its client, whose Apply counts
    # once a host carries it out; None once that client has decided otherwise (its later decision stands)
    waiting: list[tuple[UiClient, str | None]] = field(default_factory=list)


class Hub:
    # Fans suggestions out to connected UIs, one stream per project, and routes applied
    # actions to the project's hosts. Every method is synchronous, so hub state never
    # changes halfway through another update.
    def __init__(self):
        self.clients: set[UiClient] = set()
        self.latest: dict[str, str] = {}               # project id -> last suggestions message
        self.hosts: dict[str, set[WebSocket]] = {}     # project id -> connected host sockets
        self.owners: dict[str, str] = {}               # project id -> client whose host opened it
        self.actions: dict[str, Sent] = {}              # action id -> what was sent, and to whom
        self.background: set[asyncio.Task] = set()     # sends and closes in flight, kept referenced

    def spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.finished)

    def finished(self, task: asyncio.Task):
        self.background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.info("background send failed: %r", task.exception())

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
            self.spawn(close_quietly(ui.ws))
            return
        ui.queue.put_nowait(message)

    def send_all(self, message: str):
        for ui in list(self.clients):
            self.send(ui, message)

    def publish(self, project_id: str, message: str):
        self.latest[project_id] = message
        self.send_all(message)

    def open(self, project_id: str, host: WebSocket, client: str) -> bool:
        # A project belongs to the client whose host opened it: further hosts of that client may
        # join, others are refused (they would receive its actions). Without per-client auth every
        # host is the same client.
        if self.owners.get(project_id, client) != client:
            return False
        self.owners[project_id] = client
        self.hosts.setdefault(project_id, set()).add(host)
        return True

    def close(self, project_id: str, host: WebSocket):
        hosts = self.hosts.get(project_id)
        if hosts is None:
            return
        hosts.discard(host)
        # results this host still owed for the project will never come: an Apply waiting only on them came to nothing
        for sent in self.actions.values():
            if sent.project_id == project_id and sent.awaited.pop(host, 0) and sum(sent.awaited.values()) == 0:
                self.answer(sent, False, "the host left the project before answering")
        if hosts:
            return
        del self.hosts[project_id]
        self.owners.pop(project_id, None)
        self.latest.pop(project_id, None)
        self.actions = {a: sent for a, sent in self.actions.items() if sent.project_id != project_id}
        self.send_all(ProjectClosedMsg(project_id=project_id).model_dump_json())

    def find(self, project_id: str | None, suggestion_id: str) -> dict | None:
        # A suggestion in the project's latest suggestions, or None if it isn't current.
        if project_id not in self.latest:
            return None
        return next((i for i in json.loads(self.latest[project_id])["items"] if i["id"] == suggestion_id), None)

    def apply(self, project_id: str, suggestion_id: str, ui: UiClient | None = None, client: str = "") -> str | None:
        # Sends a current suggestion's action to the project's hosts for `ui`, a UI of `client`. The UI
        # only names the suggestion: the action comes from what the service produced, re-checked against
        # the per-op bounds. Returns why it couldn't be sent, or None: `ui` then hears the outcome from answer().
        hosts = self.hosts.get(project_id)
        if not hosts:
            return "no host is connected for this project"
        item = self.find(project_id, suggestion_id)
        if item is None:
            return "this suggestion is no longer current"
        op = item["action"]["op"]
        try:
            params = checked_params(op, item["action"]["params"])
        except ValueError:
            return "the action is outside the allowed bounds"
        action_id = action_id_of(project_id, suggestion_id)
        sent = self.actions.setdefault(action_id, Sent(project_id, item))
        sent.awaited.update(hosts)
        if ui is not None:
            sent.waiting.append((ui, client))
        message = ApplyActionMsg(action_id=action_id, project_id=project_id, suggestion_id=suggestion_id,
                                 op=op, params=params).model_dump_json()
        for host in hosts:
            self.spawn(host.send_text(message))
        return None

    def answer(self, sent: Sent, ok: bool, detail: str | None = None, status: str | None = None) -> list[str]:
        # Tells each UI waiting on the action whether its Apply was carried out (ok: by at least one host).
        # Returns the clients whose Apply now counts as accepted: none unless ok, and none that decided otherwise since.
        if not sent.waiting:
            return []
        message = ApplyAckMsg(ok=ok, project_id=sent.project_id, suggestion_id=sent.item["id"], status=status,
                              detail=detail).model_dump_json()
        counted = sorted({client for _, client in sent.waiting if client is not None}) if ok else []
        for ui, _ in sent.waiting:
            self.send(ui, message)
        sent.waiting.clear()
        return counted

    def withdraw(self, project_id: str, suggestion_id: str, client: str):
        # The client decided again while its Apply was still waiting for a host: if a host carries the Apply
        # out afterwards, its UI still hears so, but the later decision is the one that counts.
        sent = self.actions.get(action_id_of(project_id, suggestion_id))
        if sent is not None:
            sent.waiting = [(ui, None if applier == client else applier) for ui, applier in sent.waiting]

    def report(self, host: WebSocket, result: ActionResultMsg) -> Sent | None:
        # Forwards a host's result to every UI: one result each time the action was sent to that host while
        # it hosted the project. Returns the action, or None if no result was owed.
        sent = self.actions.get(result.action_id)
        if sent is None or sent.awaited[host] <= 0:
            return None
        sent.awaited[host] -= 1
        self.send_all(ActionOutcomeMsg(project_id=sent.project_id, suggestion_id=sent.item["id"], action_id=result.action_id,
                                       status=result.status, detail=result.detail).model_dump_json())
        return sent


hub = Hub()
store = FeedbackStore(settings.feedback_db)


def record_vote(client: str, project_id: str, suggestion_id: str, verdict: str) -> str | None:
    # A UI's verdict on a suggestion of an open project's latest suggestions (its content tells us its
    # type); anything else isn't stored. Returns why it wasn't recorded, or None.
    item = hub.find(project_id, suggestion_id)
    if item is None:
        return "this suggestion is no longer current"
    store.vote(client, project_id, suggestion_id, arm_of(item["kind"], item["action"]["op"], item["action"]["params"]), verdict)
    log.info("feedback %r %s (project %r, client %r)", suggestion_id, verdict, project_id, client or None)
    return None


CONFIRMED = {"applied", "marked", "duplicate"}  # the host carried the action out, or already had


def settle(sent: Sent, result: ActionResultMsg):
    # The outcome of the Applies waiting on an action, decided here and only here: carried out as soon as
    # one host confirms it (and then applying counts as accepting), not carried out once every host it was
    # sent to has answered without confirming. A refused action teaches nothing.
    item = sent.item
    if result.status in CONFIRMED:
        for client in hub.answer(sent, True, status=result.status):
            store.vote(client, sent.project_id, item["id"],
                       arm_of(item["kind"], item["action"]["op"], item["action"]["params"]), "accepted")
            log.info("feedback %r accepted (applied; project %r, client %r)", item["id"], sent.project_id, client or None)
    elif sum(sent.awaited.values()) == 0:
        hub.answer(sent, False, result.detail or "the host refused it")


async def close_quietly(ws: WebSocket):
    # No timeout: the close frame has to wait until the socket drains. A slow client that resumes
    # reading then gets it (1013: try again later) and reconnects with a fresh snapshot; a client
    # that never reads again is cut off by uvicorn's keepalive ping. The hub has dropped it already.
    try:
        await ws.close(code=1013)
    except Exception:
        pass


TOKEN_PROTOCOL = "sensei.token."


async def admit(ws: WebSocket, role: str) -> tuple[str, str] | None:
    # Checks the handshake before accepting it. Returns (subprotocol to echo or "", client name or
    # "" when auth is off), or None after rejecting the connection (closing before accept -> HTTP 403).
    # Browsers always send Origin, so this blocks other web pages.
    origin = ws.headers.get("origin")
    if origin is not None and origin not in settings.allowed_origins:
        log.warning("rejected websocket from origin %r", origin)
        await ws.close(code=1008)
        return None

    offered = [p for p in ws.scope.get("subprotocols", []) if p.startswith(TOKEN_PROTOCOL)]
    name = ""
    if settings.clients is not None:
        client = authenticate(settings.clients, offered[0][len(TOKEN_PROTOCOL):]) if offered else None
        if client is None or role not in client.roles:
            reason = "no valid token" if client is None else f"client {client.name!r} lacks the {role!r} role"
            log.warning("rejected websocket on %s: %s", ws.url.path, reason)
            await ws.close(code=1008)
            return None
        name = client.name

    # a browser drops the connection unless the subprotocol it offered is echoed back
    return (offered[0] if offered else ""), name


def error(message: str) -> str:
    return json.dumps({"type": "error", "message": message})


def describe(err: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors())


ONLY_ON = {"context_frame": "/ws/host", "action_result": "/ws/host", "apply": "/ws/ui", "feedback": "/ws/ui"}


async def serve(ws: WebSocket, reply, handlers=None, on_hello=None, active=lambda: True):
    # Bad input gets an error reply; the socket stays open. Stops as soon as active() turns false.
    # `handlers` maps the endpoint's own message types to async handlers that validate their input.
    handlers = handlers or {}
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
                problem = on_hello(hello) if on_hello is not None else None
                # a refusal is an ack too, so a host can tell it from a late error about an earlier message
                await reply(json.dumps({"type": "ack", "ok": False, "detail": problem} if problem else {"type": "ack", "ok": True}))
            elif msg_type in handlers:
                await handlers[msg_type](data)
            elif msg_type in ONLY_ON:
                await reply(error(f"{msg_type} is only accepted on {ONLY_ON[msg_type]}"))
            else:
                await reply(error("unknown msg type"))
        except ValidationError as err:
            await reply(error(f"invalid {msg_type}: {describe(err)}"))


@app.get("/health")
def health():
    return {"ok": True}


@app.websocket("/ws/host")
async def ws_host(ws: WebSocket):
    admitted = await admit(ws, "host")
    if admitted is None:
        return
    subprotocol, client = admitted
    await ws.accept(subprotocol=subprotocol or None)

    hello: HelloMsg | None = None

    def on_hello(msg: HelloMsg) -> str | None:
        nonlocal hello
        if hello is None or hello.project.id != msg.project.id:
            # register the new project before releasing the old one
            if not hub.open(msg.project.id, ws, client):
                log.warning("refused project %r to client %r: another client's host holds it", msg.project.id, client or None)
                if hello is not None:
                    # the host has moved on from its old project either way: it must get none of its actions
                    hub.close(hello.project.id, ws)
                    hello = None
                return f"project {msg.project.id!r} is held by another client"
            if hello is not None:
                hub.close(hello.project.id, ws)
        hello = msg
        log.info("project %r registered by host %r (client %r)", msg.project.id, msg.host.name, client or None)
        return None

    async def on_frame(data: dict):
        ctx = ContextFrameMsg.model_validate(data)
        if hello is None:
            await ws.send_text(error("send hello before context_frame"))
            return
        msg = build_suggestions(ctx, store.adjust)
        msg.project_id, msg.host = hello.project.id, hello.host.name
        suggestions = msg.model_dump_json()
        hub.publish(hello.project.id, suggestions)  # never waits on a UI
        await ws.send_text(suggestions)

    async def on_result(data: dict):
        result = ActionResultMsg.model_validate(data)
        sent = hub.report(ws, result)
        if sent is None:
            await ws.send_text(error("no result is expected from this host for that action_id"))
            return
        settle(sent, result)
        log.info("action %s %s for project %r (client %r)", result.action_id, result.status, sent.project_id, client or None)

    try:
        await serve(ws, ws.send_text, {"context_frame": on_frame, "action_result": on_result}, on_hello)
    except WebSocketDisconnect:
        return
    finally:
        if hello is not None:
            hub.close(hello.project.id, ws)


@app.websocket("/ws/ui")
async def ws_ui(ws: WebSocket):
    admitted = await admit(ws, "ui")
    if admitted is None:
        return
    subprotocol, client = admitted
    # subscribed before the handshake completes, so nothing published meanwhile is missed
    ui = hub.subscribe(ws)

    async def reply(message: str):
        hub.send(ui, message)

    async def on_apply(data: dict):
        request = ApplyMsg.model_validate(data)
        log.info("apply %r requested for project %r (client %r)", request.suggestion_id, request.project_id, client or None)
        # answered with exactly one ack_apply: now if it can't be sent, else once its outcome is known (settle)
        problem = hub.apply(request.project_id, request.suggestion_id, ui, client)
        if problem is not None:
            await reply(ApplyAckMsg(ok=False, project_id=request.project_id, suggestion_id=request.suggestion_id,
                                    detail=problem).model_dump_json())

    async def on_feedback(data: dict):
        fb = FeedbackMsg.model_validate(data)
        problem = record_vote(client, fb.project_id, fb.suggestion_id, fb.verdict)
        if problem is None:
            # a recorded decision replaces an Apply still waiting; a refused one changes nothing at all
            hub.withdraw(fb.project_id, fb.suggestion_id, client)
        await reply(FeedbackAckMsg(ok=problem is None, project_id=fb.project_id, suggestion_id=fb.suggestion_id,
                                   detail=problem).model_dump_json())

    try:
        await ws.accept(subprotocol=subprotocol or None)
        hub.start(ui)
        await serve(ws, reply, {"apply": on_apply, "feedback": on_feedback}, active=lambda: not ui.dropped)
        # Dropped (fell behind, or its socket failed). Our close may still be waiting for the socket
        # to drain, and Starlette refuses receive_text() once a close has begun, so read raw messages
        # (ignored) until the connection ends; uvicorn's keepalive still cuts a client that never reads.
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass
    except WebSocketDisconnect:
        return
    finally:
        hub.unsubscribe(ui)
