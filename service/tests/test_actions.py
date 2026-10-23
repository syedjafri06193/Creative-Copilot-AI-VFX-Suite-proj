import re

import pytest
from fastapi.testclient import TestClient

from sensei_service.clients import Client, digest
from sensei_service.config import settings
from sensei_service.engine.suggest import build_suggestions
from sensei_service import main
from sensei_service.feedback import FeedbackStore
from sensei_service.main import app, hub
from sensei_service.protocol import ContextFrameMsg, checked_params

ACK = {"type": "ack", "ok": True}
FRAME = {
    "type": "context_frame",
    "ts": 1,
    "playhead": 14.0,
    "scene": {"shot_id": "A012_C003", "emotion": "tense", "lighting": "low-key", "notes": "neon spill"},
    "timeline": {"clips": [], "cuts": [14.2], "audio": {"beats": [13.9, 14.55, 15.2]}},
}


def hello(project_id="p"):
    return {"type": "hello", "host": {"name": "test", "version": "0"}, "project": {"id": project_id, "fps": 24}}


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


def start(host, project_id="p"):
    host.send_json(hello(project_id))
    assert host.receive_json() == ACK
    host.send_json(FRAME)
    return host.receive_json()


def item_of(suggestions, kind):
    return next(i for i in suggestions["items"] if i["kind"] == kind)


def test_apply_reaches_the_host_and_the_result_reaches_the_ui(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        assert ui.receive_json() == suggestions
        item = item_of(suggestions, "color_grade")

        ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item["id"]})
        action = host.receive_json()
        assert action["type"] == "apply_action" and re.fullmatch(r"act_[0-9a-f]{12}", action["action_id"])
        assert action["project_id"] == "p"  # so the host can refuse it if it has switched since
        assert (action["suggestion_id"], action["op"], action["params"]) == (item["id"], item["action"]["op"], item["action"]["params"])

        host.send_json({"type": "action_result", "action_id": action["action_id"], "status": "applied"})
        assert ui.receive_json() == {"type": "action_result", "project_id": "p", "suggestion_id": item["id"],
                                     "action_id": action["action_id"], "status": "applied", "detail": None}


def test_applying_twice_reuses_the_action_id(client):
    # so a host can recognise the repeat and not apply it twice
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        ui.receive_json()
        request = {"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]}
        ui.send_json(request)
        first = host.receive_json()
        ui.send_json(request)
        assert host.receive_json()["action_id"] == first["action_id"]


def test_the_ui_cannot_supply_its_own_action(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        ui.receive_json()
        item = item_of(suggestions, "vfx")
        ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item["id"],
                      "op": "delete_timeline", "params": {"everything": True}})
        action = host.receive_json()
        assert (action["op"], action["params"]) == (item["action"]["op"], item["action"]["params"])


@pytest.mark.parametrize("with_host, suggestion_id, detail", [
    (False, "sug_1234abcd", "no host is connected"),
    (True, "sug_1234abcd", "no longer current"),
])
def test_apply_failures_are_reported_to_the_ui(client, with_host, suggestion_id, detail):
    with client.websocket_connect("/ws/ui") as ui:
        if with_host:
            with client.websocket_connect("/ws/host") as host:
                start(host)
                ui.receive_json()
                ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": suggestion_id})
                outcome = ui.receive_json()
        else:
            ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": suggestion_id})
            outcome = ui.receive_json()
    assert outcome["type"] == "ack_apply" and outcome["ok"] is False and detail in outcome["detail"]
    assert (outcome["project_id"], outcome["suggestion_id"]) == ("p", suggestion_id)


def test_messages_are_only_accepted_on_their_endpoint(client):
    with client.websocket_connect("/ws/host") as host:
        host.send_json({"type": "apply", "project_id": "p", "suggestion_id": "sug_1234abcd"})
        assert "only accepted on /ws/ui" in host.receive_json()["message"]
    with client.websocket_connect("/ws/ui") as ui:
        ui.send_json({"type": "action_result", "action_id": "act_0123456789ab", "status": "applied"})
        assert "only accepted on /ws/host" in ui.receive_json()["message"]


def test_a_host_cannot_report_for_another_project(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as a, \
            client.websocket_connect("/ws/host") as b:
        suggestions = start(a, "edit")
        ui.receive_json()
        start(b, "grade")
        ui.receive_json()
        ui.send_json({"type": "apply", "project_id": "edit", "suggestion_id": item_of(suggestions, "vfx")["id"]})
        action = a.receive_json()

        b.send_json({"type": "action_result", "action_id": action["action_id"], "status": "applied"})
        assert "no result is expected" in b.receive_json()["message"]


def test_result_details_cannot_carry_control_characters(client):
    # a detail is shown on every UI and logged
    with client.websocket_connect("/ws/host") as host:
        host.send_json({"type": "action_result", "action_id": "act_0123456789ab", "status": "rejected",
                        "detail": "no clip\nINFO: forged log line"})
        assert host.receive_json()["message"].startswith("invalid action_result: detail")


def test_a_host_answers_each_send_once(client):
    # so a stray or repeated result can't overwrite what UIs show, while applying again still gets an answer
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        ui.receive_json()
        apply = {"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]}
        ui.send_json(apply)
        action = host.receive_json()

        def report(status):
            host.send_json({"type": "action_result", "action_id": action["action_id"], "status": status})

        report("applied")
        assert ui.receive_json()["status"] == "applied"
        assert ui.receive_json()["type"] == "ack_apply"
        report("duplicate")
        assert "no result is expected" in host.receive_json()["message"]

        ui.send_json(apply)  # sent again: one more answer is owed
        host.receive_json()
        report("duplicate")
        assert ui.receive_json()["status"] == "duplicate"


def apply_on_two_hosts(ui, a, b) -> dict:
    # Hosts a and b both have project "p" open, and the UI applies its vfx suggestion. Returns the action.
    suggestions = start(a)
    ui.receive_json()
    b.send_json(hello("p"))
    assert b.receive_json() == ACK
    ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]})
    action = a.receive_json()
    assert b.receive_json() == action
    return action


def result(action, status, detail=None):
    return {"type": "action_result", "action_id": action["action_id"], "status": status, "detail": detail}


def test_every_apply_gets_one_answer(client):
    # also when the same UI applies again before the host has answered the first
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        ui.receive_json()
        apply = {"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]}
        for _ in range(2):
            ui.send_json(apply)
            action = host.receive_json()
        host.send_json(result(action, "applied"))
        assert ui.receive_json()["type"] == "action_result"
        assert [ui.receive_json()["type"] for _ in range(2)] == ["ack_apply", "ack_apply"]
        host.send_json(result(action, "duplicate"))  # the second send's answer: nothing more is waiting
        assert ui.receive_json()["status"] == "duplicate"


def test_with_two_hosts_one_carrying_it_out_is_enough(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as a, \
            client.websocket_connect("/ws/host") as b:
        action = apply_on_two_hosts(ui, a, b)
        a.send_json(result(action, "rejected", "no clip at the cut"))
        assert ui.receive_json()["status"] == "rejected"  # shown, but not the outcome: b still owes an answer
        b.send_json(result(action, "applied"))
        assert ui.receive_json()["status"] == "applied"
        assert ui.receive_json() == {"type": "ack_apply", "ok": True, "project_id": "p",
                                     "suggestion_id": action["suggestion_id"], "status": "applied", "detail": None}


def test_with_two_hosts_it_comes_to_nothing_only_when_both_refuse(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as a, \
            client.websocket_connect("/ws/host") as b:
        action = apply_on_two_hosts(ui, a, b)
        a.send_json(result(action, "rejected", "no clip at the cut"))
        assert ui.receive_json()["status"] == "rejected"
        b.send_json(result(action, "rejected", "the timeline is locked"))
        assert ui.receive_json()["status"] == "rejected"
        ack = ui.receive_json()
        assert ack["type"] == "ack_apply" and ack["ok"] is False and ack["detail"] == "the timeline is locked"


def test_a_host_leaving_before_it_answers_settles_the_apply(client):
    # its answer will never come: once nothing else is owed, the Apply came to nothing
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as a:
        with client.websocket_connect("/ws/host") as b:
            action = apply_on_two_hosts(ui, a, b)
            a.send_json(result(action, "rejected"))
            assert ui.receive_json()["status"] == "rejected"
        assert ui.receive_json() == {"type": "ack_apply", "ok": False, "project_id": "p",
                                     "suggestion_id": action["suggestion_id"], "status": None,
                                     "detail": "the host left the project before answering"}


@pytest.mark.parametrize("status", ["applied", "marked", "duplicate"])
def test_the_answer_says_how_it_was_carried_out(client, status):
    # "marked" means the host left it for the editor to finish: the UI must not call that "Applied"
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        ui.receive_json()
        ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "transition")["id"]})
        action = host.receive_json()
        host.send_json(result(action, status))
        assert ui.receive_json()["status"] == status  # the host's result
        ack = ui.receive_json()
        assert (ack["type"], ack["ok"], ack["status"]) == ("ack_apply", True, status)


def test_closing_a_project_settles_and_forgets_its_actions(client):
    with client.websocket_connect("/ws/ui") as ui:
        with client.websocket_connect("/ws/host") as host:
            suggestions = start(host)
            ui.receive_json()
            ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]})
            host.receive_json()
            assert len(hub.actions) == 1
        ack = ui.receive_json()
        assert ack["type"] == "ack_apply" and ack["ok"] is False and "left the project" in ack["detail"]
        assert ui.receive_json()["type"] == "project_closed"
    assert hub.actions == {}


@pytest.mark.parametrize("playhead", [x / 4 for x in range(40, 82)])
@pytest.mark.parametrize("emotion, lighting, notes", [("tense", "low-key", "neon"), ("calm", "day", ""), (None, "noir", "neon")])
def test_engine_actions_are_within_bounds(playhead, emotion, lighting, notes):
    frame = {**FRAME, "playhead": playhead,
             "scene": {"shot_id": "X", "emotion": emotion, "lighting": lighting, "notes": notes},
             "timeline": {"cuts": [14.2], "audio": {"beats": [10 + i * 0.65 for i in range(16)]}}}
    for item in build_suggestions(ContextFrameMsg.model_validate(frame)).items:
        checked_params(item.action.op, item.action.params)


@pytest.mark.parametrize("op, params", [
    ("apply_vfx_preset", {"preset": "neon_bloom_02", "threshold": 0.78, "intensity": 5, "radius": 0.22}),
    ("apply_color_grade", {"shot": "A012_C003", "lut": "../../etc/passwd", "contrast": 1.1, "sat": 0.9}),
    ("apply_color_grade", {"lut": "teal_orange_07", "contrast": 1.1, "sat": 0.9}),  # no target clip
    ("apply_color_grade", {"shot": "A012\nC003", "lut": "teal_orange_07", "contrast": 1.1, "sat": 0.9}),
    ("apply_transition_at_cut", {"type": "cross_dissolve", "frames": 8, "at": 14.2, "extra": 1}),
    ("delete_timeline", {}),
])
def test_out_of_bounds_actions_are_refused(op, params):
    with pytest.raises(ValueError):
        checked_params(op, params)


def test_the_hub_rechecks_bounds_before_sending():
    # even an action the service itself produced is re-checked before it reaches a host
    from sensei_service.main import Hub

    class Host:
        sent = []

        async def send_text(self, message):
            self.sent.append(message)

    local = Hub()
    host = Host()
    local.open("p", host, "")
    local.latest["p"] = ('{"type": "suggestions", "ts": 1, "project_id": "p", "host": "t", "items": [{"id": "sug_1234abcd", '
                         '"kind": "vfx", "title": "t", "confidence": 0.5, "rationale": "r", "action": {"op": "apply_vfx_preset", '
                         '"params": {"preset": "x", "threshold": 9, "intensity": 0.1, "radius": 0.1}}}]}')
    assert local.apply("p", "sug_1234abcd") == "the action is outside the allowed bounds"
    assert host.sent == [] and local.actions == {}


@pytest.fixture
def host_clients(monkeypatch):
    # per-client mode with two different host clients and a UI client
    tokens = {"resolve": "resolve-token", "intruder": "intruder-token", "desktop": "ui-token"}
    monkeypatch.setattr(settings, "clients", [
        Client("resolve", ("host",), digest(tokens["resolve"])),
        Client("intruder", ("host",), digest(tokens["intruder"])),
        Client("desktop", ("ui",), digest(tokens["desktop"])),
    ])
    return {name: [f"sensei.token.{token}"] for name, token in tokens.items()}


def test_another_client_cannot_take_over_a_project(client, host_clients):
    with client.websocket_connect("/ws/ui", subprotocols=host_clients["desktop"]) as ui, \
            client.websocket_connect("/ws/host", subprotocols=host_clients["resolve"]) as owner, \
            client.websocket_connect("/ws/host", subprotocols=host_clients["intruder"]) as intruder:
        suggestions = start(owner)
        ui.receive_json()
        intruder.send_json(hello("p"))
        assert intruder.receive_json() == {"type": "ack", "ok": False, "detail": "project 'p' is held by another client"}

        ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]})
        action = owner.receive_json()
        assert action["type"] == "apply_action"
        # the intruder received no action (its next message is the error below) and can't report on it
        intruder.send_json({"type": "action_result", "action_id": action["action_id"], "status": "applied"})
        assert "no result is expected" in intruder.receive_json()["message"]


def test_a_host_refused_another_project_no_longer_hosts_its_old_one(client, host_clients):
    # its editor has left the old project, so that project's actions must not reach it
    with client.websocket_connect("/ws/ui", subprotocols=host_clients["desktop"]) as ui, \
            client.websocket_connect("/ws/host", subprotocols=host_clients["resolve"]) as owner, \
            client.websocket_connect("/ws/host", subprotocols=host_clients["intruder"]) as switcher:
        own = start(switcher, "q")
        ui.receive_json()
        start(owner, "p")
        ui.receive_json()

        switcher.send_json(hello("p"))
        assert switcher.receive_json()["ok"] is False
        assert ui.receive_json() == {"type": "project_closed", "project_id": "q"}
        assert "q" not in hub.hosts and "q" not in hub.owners

        ui.send_json({"type": "apply", "project_id": "q", "suggestion_id": item_of(own, "vfx")["id"]})
        assert "no host is connected" in ui.receive_json()["detail"]


def test_another_host_of_the_same_client_may_join(client, host_clients):
    with client.websocket_connect("/ws/host", subprotocols=host_clients["resolve"]) as first, \
            client.websocket_connect("/ws/host", subprotocols=host_clients["resolve"]) as second:
        start(first)
        second.send_json(hello("p"))
        assert second.receive_json() == ACK
        assert len(hub.hosts["p"]) == 2


def test_only_the_hosts_an_action_was_sent_to_may_report_it(client):
    with client.websocket_connect("/ws/ui") as ui, client.websocket_connect("/ws/host") as host:
        suggestions = start(host)
        ui.receive_json()
        ui.send_json({"type": "apply", "project_id": "p", "suggestion_id": item_of(suggestions, "vfx")["id"]})
        action = host.receive_json()
        with client.websocket_connect("/ws/host") as late:  # joins the project after the action was sent
            late.send_json(hello("p"))
            assert late.receive_json() == ACK
            late.send_json({"type": "action_result", "action_id": action["action_id"], "status": "applied"})
            assert "no result is expected" in late.receive_json()["message"]
