"""The adapter, driving a fake Resolve, against the real service on a local port, with a UI."""
import json
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from websockets.sync.client import connect

import resolve_adapter
from fake_resolve import FakeProject, FakeResolve, FakeTimeline, FakeTimelineItem, scene_timeline
from sensei_service import main as service
from sensei_service.clients import Client, digest, shared_client

TOKEN = "s3cret"


@pytest.fixture
def service_url(monkeypatch):
    monkeypatch.setattr(service.settings, "clients", [shared_client(TOKEN)])  # auth on, as in a real setup
    server = uvicorn.Server(uvicorn.Config(service.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def resolve(service_url):
    app = FakeResolve(FakeProject("Night Shoot", scene_timeline()))
    stop = threading.Event()
    thread = threading.Thread(target=resolve_adapter.run, args=(app,), daemon=True,
                              kwargs={"url": f"{service_url}/ws/host", "token": TOKEN, "should_stop": stop.is_set})
    thread.start()
    yield SimpleNamespace(app=app, thread=thread, timeline=app.manager.project.timeline)
    stop.set()
    thread.join(5)


def ui(service_url):
    return connect(f"{service_url}/ws/ui", subprotocols=[f"sensei.token.{TOKEN}"])


def next_message(ws, wanted):
    while True:
        message = json.loads(ws.recv(timeout=10))
        if wanted(message):
            return message


def suggestions(message):
    return message["type"] == "suggestions"


def outcome_of(suggestion_id):
    return lambda message: message["type"] == "action_result" and message["suggestion_id"] == suggestion_id


def test_suggestions_from_resolve_reach_a_ui_and_applying_them_edits_the_timeline(service_url, resolve):
    with ui(service_url) as ws:
        message = next_message(ws, suggestions)
        assert message["host"] == "davinci-resolve" and message["project_id"].startswith("Night-Shoot.Edit-v3-")
        items = {item["kind"]: item for item in message["items"]}
        assert set(items) == {"color_grade", "transition", "keyframe", "vfx"}

        def apply(kind):
            ws.send(json.dumps({"type": "apply", "project_id": message["project_id"], "suggestion_id": items[kind]["id"]}))
            return next_message(ws, outcome_of(items[kind]["id"]))

        assert apply("color_grade")["status"] == "applied"
        assert resolve.timeline.items[1].cdl["Saturation"] == "0.9200"  # the clip under the playhead

        transition = apply("transition")
        assert transition["status"] == "marked" and "dip to black" in transition["detail"]
        assert resolve.timeline.markers[341.0]["customData"] == f"sensei:{transition['action_id']}"

        assert apply("transition")["status"] == "duplicate"  # applying again changes nothing


def test_moving_the_playhead_or_switching_timelines_follows_the_editor(service_url, resolve):
    with ui(service_url) as ws:
        first = next_message(ws, suggestions)
        resolve.timeline.playhead = 24 * 20  # 20s: past the beats, into the third shot
        moved = next_message(ws, suggestions)
        assert moved["project_id"] == first["project_id"]
        assert {i["kind"] for i in moved["items"]} == {"color_grade"}

        resolve.app.manager.project.timeline = FakeTimeline("Edit v4", [FakeTimelineItem("C1", 86400, 86880)], playhead=24)
        assert next_message(ws, lambda m: m["type"] == "project_closed")["project_id"] == first["project_id"]
        assert next_message(ws, suggestions)["project_id"].startswith("Night-Shoot.Edit-v4-")


@pytest.fixture
def two_clients(service_url, monkeypatch):
    # an edit bay (host and UI) and a laptop (host): each adapter's own client. Yields start(resolve, token).
    monkeypatch.setattr(resolve_adapter, "HELLO_RETRY_SECONDS", 0.5)
    monkeypatch.setattr(service.settings, "clients", [Client("edit-bay", ("host", "ui"), digest("bay")),
                                                      Client("laptop", ("host",), digest("laptop"))])
    adapters = []

    def start(app, token):
        stop = threading.Event()
        thread = threading.Thread(target=resolve_adapter.run, args=(app,), daemon=True,
                                  kwargs={"url": f"{service_url}/ws/host", "token": token, "should_stop": stop.is_set})
        thread.start()
        adapters.append((stop, thread))
        return stop, thread

    yield start
    for stop, thread in adapters:
        stop.set()
        thread.join(5)


def test_a_project_held_by_another_client_waits_its_turn(service_url, two_clients, capsys):
    project = FakeProject("Night Shoot", scene_timeline())  # both adapters see the same project
    with connect(f"{service_url}/ws/ui", subprotocols=["sensei.token.bay"]) as ws:
        stop, thread = two_clients(FakeResolve(project), "bay")
        project_id = next_message(ws, suggestions)["project_id"]
        two_clients(FakeResolve(project), "laptop")
        while "is held by another client" not in capsys.readouterr().out:
            time.sleep(0.05)
        assert service.hub.owners[project_id] == "edit-bay"

        stop.set()  # the edit bay closes its adapter: the laptop takes over
        thread.join(5)
        assert next_message(ws, lambda m: m["type"] == "project_closed")["project_id"] == project_id
        assert next_message(ws, suggestions)["project_id"] == project_id
        assert service.hub.owners[project_id] == "laptop"


def test_switching_to_a_project_another_client_holds_releases_the_old_one(service_url, two_clients, capsys):
    # The laptop's editor opens the edit bay's project: the laptop is refused it, and its old project closes,
    # so that project's suggestions can no longer be applied to whatever the laptop now has open.
    shared = FakeProject("Night Shoot", scene_timeline())
    laptop = FakeResolve(FakeProject("Laptop Cut", FakeTimeline("Rough", [FakeTimelineItem("C1", 86400, 86880)], playhead=24)))
    with connect(f"{service_url}/ws/ui", subprotocols=["sensei.token.bay"]) as ws:
        two_clients(FakeResolve(shared), "bay")
        held = next_message(ws, suggestions)["project_id"]
        two_clients(laptop, "laptop")
        own = next_message(ws, lambda m: suggestions(m) and m["project_id"] != held)

        laptop.manager.project = shared
        assert next_message(ws, lambda m: m["type"] == "project_closed")["project_id"] == own["project_id"]
        while "is held by another client" not in capsys.readouterr().out:
            time.sleep(0.05)
        assert own["project_id"] not in service.hub.hosts and service.hub.owners[held] == "edit-bay"

        ws.send(json.dumps({"type": "apply", "project_id": own["project_id"], "suggestion_id": own["items"][0]["id"]}))
        ack = next_message(ws, lambda m: m["type"] == "ack_apply")
        assert ack["suggestion_id"] == own["items"][0]["id"] and ack["ok"] is False
        assert all(item.cdl is None for item in shared.timeline.items)


def test_the_adapter_stops_when_resolve_quits(resolve):
    resolve.app.quit()
    resolve.thread.join(5)
    assert not resolve.thread.is_alive()
