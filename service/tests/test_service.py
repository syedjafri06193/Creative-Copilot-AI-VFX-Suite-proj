import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sensei_service.clients import Client, digest, shared_client
from sensei_service.config import settings
from sensei_service.engine.suggest import build_suggestions
from sensei_service import main
from sensei_service.feedback import FeedbackStore
from sensei_service.main import UI_QUEUE_SIZE, Hub, app, hub, serve
from sensei_service.protocol import ContextFrameMsg

HELLO = {"type": "hello", "host": {"name": "test", "version": "0"}, "project": {"id": "p", "fps": 24}}
ACK = {"type": "ack", "ok": True}
FRAME = {
    "type": "context_frame",
    "ts": 1,
    "playhead": 14.0,
    "scene": {"shot_id": "A012_C003", "emotion": "tense", "lighting": "low-key", "notes": "neon spill"},
    "timeline": {
        "clips": [{"id": "c1", "in": 10.0, "out": 14.2, "kind": "video"}],
        "cuts": [14.2],
        "audio": {"bpm": 92, "beats": [13.9, 14.55, 15.2]},
    },
}


def hello(project_id="p", host="test"):
    return {**HELLO, "host": {"name": host, "version": "0"}, "project": {"id": project_id, "fps": 24}}


def start_host(host, project_id="p", name="test"):
    host.send_json(hello(project_id, name))
    assert host.receive_json() == ACK


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "store", FeedbackStore(":memory:"))  # fresh learning state per test
    hub.clients.clear()
    hub.latest.clear()
    hub.hosts.clear()
    hub.owners.clear()
    hub.actions.clear()
    # As a context manager, every WebSocket session shares one event loop, as under uvicorn;
    # otherwise each session gets its own loop and cross-session wake-ups can be lost.
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_host_frame_is_broadcast_to_ui(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        ui.send_json(HELLO)
        assert ui.receive_json() == ACK  # ui is registered with the hub
        start_host(host)

        host.send_json(FRAME)
        to_host = host.receive_json()
        assert to_host["type"] == "suggestions"
        assert (to_host["project_id"], to_host["host"]) == ("p", "test")
        assert [i["kind"] for i in to_host["items"]] == ["color_grade", "vfx", "transition", "keyframe"]
        assert ui.receive_json() == to_host


def test_ui_gets_latest_suggestions_on_connect(client):
    with client.websocket_connect("/ws/host") as host:
        start_host(host)
        host.send_json(FRAME)
        latest = host.receive_json()
        with client.websocket_connect("/ws/ui") as ui:
            assert ui.receive_json() == latest


def test_frame_before_hello_is_rejected(client):
    with client.websocket_connect("/ws/host") as host:
        host.send_json(FRAME)
        assert "send hello before context_frame" in host.receive_json()["message"]


def test_projects_are_separate_streams(client):
    with client.websocket_connect("/ws/host") as a, client.websocket_connect("/ws/host") as b:
        start_host(a, "edit", "premiere")
        start_host(b, "grade", "resolve")
        a.send_json(FRAME)
        a.receive_json()
        b.send_json({**FRAME, "scene": {**FRAME["scene"], "emotion": "calm", "lighting": "day"}})
        b.receive_json()

        with client.websocket_connect("/ws/ui") as ui:
            streams = {m["project_id"]: m for m in (ui.receive_json(), ui.receive_json())}
        assert streams["edit"]["host"] == "premiere"
        assert streams["grade"]["host"] == "resolve"
        assert streams["edit"]["items"] != streams["grade"]["items"]


def test_ui_is_told_when_a_project_closes(client):
    with client.websocket_connect("/ws/ui") as ui:
        ui.send_json(HELLO)
        assert ui.receive_json() == ACK
        with client.websocket_connect("/ws/host") as host:
            start_host(host)
            host.send_json(FRAME)
            host.receive_json()
            assert ui.receive_json()["type"] == "suggestions"
        assert ui.receive_json() == {"type": "project_closed", "project_id": "p"}

    with client.websocket_connect("/ws/ui") as ui:
        ui.send_json(HELLO)
        assert ui.receive_json() == ACK  # closed project is no longer replayed on connect


def test_suggestion_ids_are_stable_and_content_based():
    ctx = ContextFrameMsg.model_validate(FRAME)
    first = {i.kind: i.id for i in build_suggestions(ctx).items}
    assert first == {i.kind: i.id for i in build_suggestions(ctx).items}
    assert len(set(first.values())) == len(first)

    later = {i.kind: i.id for i in build_suggestions(ContextFrameMsg.model_validate({**FRAME, "playhead": 14.6})).items}
    assert later["color_grade"] == first["color_grade"]
    assert later["keyframe"] != first["keyframe"]  # snaps to a different beat


@pytest.mark.parametrize("payload, expected", [
    ("not json", "invalid json"),
    ("[]", "must be a JSON object"),
    ('{"type": "context_frame", "ts": 1, "playhead": 1, "timeline": {}}', "scene: Field required"),
    ('{"type": "nope"}', "unknown msg type"),
])
def test_bad_input_gets_error_and_socket_stays_open(client, payload, expected):
    with client.websocket_connect("/ws/host") as host:
        host.send_text(payload)
        reply = host.receive_json()
        assert reply["type"] == "error"
        assert expected in reply["message"]

        host.send_json(HELLO)
        assert host.receive_json() == ACK


def test_feedback_is_validated(client):
    with client.websocket_connect("/ws/ui") as ui:
        ui.send_json({"type": "feedback", "suggestion_id": "sug_1234abcd", "verdict": "accepted", "project_id": "p"})
        assert ui.receive_json() == {"type": "ack_feedback", "ok": False, "project_id": "p",
                                     "suggestion_id": "sug_1234abcd", "detail": "this suggestion is no longer current"}

        ui.send_json({"type": "feedback", "verdict": "loved it"})
        reply = ui.receive_json()
        assert reply["type"] == "error"
        assert "invalid feedback" in reply["message"]


def test_ui_cannot_send_context_frames(client):
    with client.websocket_connect("/ws/ui") as ui:
        ui.send_json(FRAME)
        assert "only accepted on /ws/host" in ui.receive_json()["message"]


@pytest.mark.parametrize("path", ["/ws/host", "/ws/ui"])
def test_foreign_browser_origin_is_rejected(client, path):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, headers={"origin": "https://evil.example"}):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("path", ["/ws/host", "/ws/ui"])
def test_allowed_origin_is_accepted(client, path):
    with client.websocket_connect(path, headers={"origin": "http://127.0.0.1:5180"}) as ws:
        ws.send_json(HELLO)
        assert ws.receive_json() == ACK


@pytest.fixture
def token(monkeypatch):
    # the single shared-token mode (SENSEI_TOKEN)
    monkeypatch.setattr(settings, "clients", [shared_client("s3cret")])
    return "s3cret"


@pytest.fixture
def per_client(monkeypatch):
    # per-client mode (SENSEI_CLIENTS_FILE): a host-only and a UI-only client
    tokens = {"resolve": "host-token", "desktop": "ui-token"}
    monkeypatch.setattr(settings, "clients", [
        Client("resolve", ("host",), digest(tokens["resolve"])),
        Client("desktop", ("ui",), digest(tokens["desktop"])),
    ])
    return tokens


@pytest.mark.parametrize("path, name, allowed", [
    ("/ws/host", "resolve", True),
    ("/ws/ui", "resolve", False),
    ("/ws/ui", "desktop", True),
    ("/ws/host", "desktop", False),
])
def test_clients_may_only_use_their_roles(client, per_client, path, name, allowed):
    subprotocols = [f"sensei.token.{per_client[name]}"]
    if allowed:
        with client.websocket_connect(path, subprotocols=subprotocols) as ws:
            ws.send_json(HELLO)
            assert ws.receive_json() == ACK
    else:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path, subprotocols=subprotocols):
                pass
        assert exc.value.code == 1008


@pytest.mark.parametrize("path", ["/ws/host", "/ws/ui"])
def test_unknown_client_token_is_rejected(client, per_client, path):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, subprotocols=["sensei.token.not-a-client"]):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("path", ["/ws/host", "/ws/ui"])
@pytest.mark.parametrize("subprotocols", [None, ["sensei.token.wrong"]])
def test_token_is_required_when_configured(client, token, path, subprotocols):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, subprotocols=subprotocols):
            pass
    assert exc.value.code == 1008


def test_valid_token_is_accepted_and_echoed(client, token):
    with client.websocket_connect("/ws/host", subprotocols=[f"sensei.token.{token}"]) as host:
        assert host.accepted_subprotocol == f"sensei.token.{token}"
        start_host(host)


def test_token_subprotocol_is_echoed_when_auth_is_off(client):
    # browsers drop the connection if an offered subprotocol is not echoed
    with client.websocket_connect("/ws/ui", subprotocols=["sensei.token.anything"]) as ui:
        assert ui.accepted_subprotocol == "sensei.token.anything"


class FakeWs:
    def __init__(self, stall=False, fail=False):
        self.stall = stall
        self.fail = fail
        self.sent = []
        self.closed = False
        self.close_calls = 0

    async def send_text(self, message):
        if self.fail:
            raise ConnectionResetError("peer reset the connection")
        if self.stall:
            await asyncio.Event().wait()  # a client that stopped reading: the send never completes
        self.sent.append(message)

    async def close(self, code=1000):
        self.close_calls += 1
        self.closed = True


def queued(ui):
    items = []
    while not ui.queue.empty():
        items.append(ui.queue.get_nowait())
    return items


def test_a_stalled_ui_is_dropped_without_blocking_others():
    async def scenario():
        hub = Hub()
        stalled, healthy = FakeWs(stall=True), FakeWs()
        uis = [hub.subscribe(stalled), hub.subscribe(healthy)]
        for ui in uis:
            hub.start(ui)
        messages = [f"m{i}" for i in range(UI_QUEUE_SIZE + 10)]
        for message in messages:
            hub.publish("p", message)  # synchronous: never waits on a UI
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert uis[0] not in hub.clients and stalled.closed
        assert uis[1] in hub.clients and healthy.sent == messages
        hub.unsubscribe(uis[1])

    asyncio.run(scenario())


def test_a_dropped_ui_is_dropped_once(caplog):
    # its own replies (acks, errors) keep going through hub.send until the socket closes
    async def scenario():
        hub = Hub()
        stalled = FakeWs(stall=True)
        ui = hub.subscribe(stalled)
        hub.start(ui)
        for i in range(UI_QUEUE_SIZE + 2):
            hub.publish("p", f"m{i}")
            await asyncio.sleep(0)
        for _ in range(20):
            hub.send(ui, "ack")
        assert len(hub.background) == 1  # the close task is referenced while it runs
        await asyncio.sleep(0.05)
        assert hub.background == set()
        assert stalled.close_calls == 1

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        asyncio.run(scenario())
    assert sum("stopped reading" in r.getMessage() for r in caplog.records) == 1


def test_a_failed_send_ends_the_sender_cleanly():
    async def scenario():
        hub = Hub()
        ui = hub.subscribe(FakeWs(fail=True))
        hub.start(ui)
        hub.publish("p", "m0")
        await asyncio.sleep(0.01)
        assert ui.sender.done() and not ui.sender.cancelled()
        assert ui.sender.exception() is None  # nothing left for asyncio to report as unretrieved
        assert ui not in hub.clients and ui.dropped
        hub.publish("p", "m1")
        assert ui.queue.empty()  # nothing more is queued for a dead UI

    asyncio.run(scenario())


def test_snapshot_is_queued_before_later_updates():
    # a project closing right after a UI connects must not be resurrected by the replay
    hub = Hub()
    host = FakeWs()
    hub.open("p", host, "")
    hub.publish("p", "latest")
    ui = hub.subscribe(FakeWs())
    hub.close("p", host)
    assert queued(ui) == ["latest", '{"type":"project_closed","project_id":"p"}']


def test_host_switching_projects_keeps_counts_right(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        start_host(host, "a")
        start_host(host, "b")
        assert ui.receive_json() == {"type": "project_closed", "project_id": "a"}
        start_host(host, "b")  # re-sending the same project must not close it
        host.send_json(FRAME)
        host.receive_json()
        assert ui.receive_json()["type"] == "suggestions"  # no project_closed for "b" in between
        assert {project: len(hosts) for project, hosts in hub.hosts.items()} == {"b": 1}
    assert hub.hosts == {}


def test_closing_an_unknown_project_is_harmless():
    hub = Hub()
    hub.close("never-opened", FakeWs())
    assert hub.hosts == {}


FEEDBACK = {"type": "feedback", "suggestion_id": "sug_1234abcd", "verdict": "accepted", "project_id": "p"}


@pytest.mark.parametrize("path, message", [
    ("/ws/ui", {**FEEDBACK, "suggestion_id": "sug_1234abcd\nINFO: forged log line"}),
    ("/ws/ui", {**FEEDBACK, "project_id": "p\n1"}),
    ("/ws/ui", {**FEEDBACK, "notes": "x" * 1001}),
    ("/ws/host", {**HELLO, "project": {"id": "a" * 65, "fps": 24}}),
    ("/ws/host", {**HELLO, "host": {"name": "evil\r\nhost", "version": "0"}}),
])
def test_client_strings_are_bounded(client, path, message):
    with client.websocket_connect(path) as ws:
        ws.send_json(message)
        reply = ws.receive_json()
        assert reply["type"] == "error" and reply["message"].startswith("invalid ")


def test_serve_stops_once_the_client_is_dropped():
    # after a drop Starlette refuses receive_text(), so serve() must not read again
    class Inbox:
        def __init__(self, messages):
            self.messages = list(messages)

        async def receive_text(self):
            return self.messages.pop(0)

    replies, state = [], {"active": True}

    async def reply(message):
        replies.append(message)
        state["active"] = False  # e.g. the UI is dropped while its message is handled

    inbox = Inbox(['{"type": "nope"}'] * 3)
    asyncio.run(serve(inbox, reply, active=lambda: state["active"]))
    assert len(replies) == 1 and len(inbox.messages) == 2
