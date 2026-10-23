import json

import pytest

import resolve_adapter
from fake_resolve import FakeMediaPoolItem, FakeProject, FakeResolve, FakeTimeline, FakeTimelineItem, scene_timeline
from resolve_adapter import Reader, ResolveGone, apply_action, project_id, timecode_to_frame
from sensei_service.protocol import ActionResultMsg, ContextFrameMsg, HelloMsg


def reader_for(timeline, project_name="Night Shoot"):
    return Reader(FakeResolve(FakeProject(project_name, timeline)))


@pytest.mark.parametrize("timecode, fps, frame", [
    ("01:00:14:00", 24, 86736),
    ("00:00:01:12", 23.976, 36),     # counted at the nominal 24
    ("00:01:00;02", 29.97, 1800),    # drop frame: ;00 and ;01 don't exist in minute 1
    ("00:10:00;00", 29.97, 17982),   # ...but do every tenth minute
    ("00:01:00;04", 59.94, 3600),
])
def test_timecodes_become_frames(timecode, fps, frame):
    assert timecode_to_frame(timecode, fps) == frame


def test_the_open_timeline_becomes_a_valid_hello_and_context_frame():
    timeline = scene_timeline()
    hello, frame = reader_for(timeline).read()
    HelloMsg.model_validate(hello)  # the service's own models
    ContextFrameMsg.model_validate({**frame, "ts": 0})
    assert hello["project"]["fps"] == 24.0
    assert hello["project"]["id"].startswith("Night-Shoot.Edit-v3-")

    assert frame["playhead"] == 14.0
    # the shot is the clip's unique id, so a grade suggested for it can find it again
    assert frame["scene"] == {"shot_id": timeline.items[1].uid, "emotion": "tense", "lighting": "low-key",
                              "notes": "night interior, neon spill"}
    assert [c["label"] for c in frame["timeline"]["clips"]] == ["A012_C002", "A012_C003", "A012_C004"]  # within 30s
    assert frame["timeline"]["cuts"] == [10.0, 14.208]
    assert frame["timeline"]["audio"]["beats"] == [13.917, 14.583, 15.208]  # blue markers only


def test_sensei_markers_are_never_beats():
    timeline = scene_timeline()
    timeline.AddMarker(360, "Purple", "", "", 1)
    reader = Reader(FakeResolve(FakeProject("p", timeline)), beat_color="Purple")
    assert reader.read()[1]["timeline"]["audio"]["beats"] == [15.0]  # not the SENSEI marker at 352


def test_project_ids_are_unique_per_timeline_and_fit_the_service():
    a = reader_for(FakeTimeline("Edit v3", []), "Night Shoot").read()[0]["project"]["id"]
    b = reader_for(FakeTimeline("Edit v3", []), "Night Shoot").read()[0]["project"]["id"]
    long = reader_for(FakeTimeline("x" * 200, []), "Ünïcode / project: " * 10).read()[0]
    assert a != b  # same names, different Resolve timelines
    HelloMsg.model_validate(long)


def test_switching_timelines_is_seen_at_once():
    reader = reader_for(scene_timeline())
    first, _ = reader.read()
    other = FakeTimeline("Edit v4", [FakeTimelineItem("C1", 86400, 86880)], playhead=24)
    reader.resolve.manager.project.timeline = other
    second, frame = reader.read()
    assert second["project"]["id"] != first["project"]["id"]
    assert [c["label"] for c in frame["timeline"]["clips"]] == ["C1"]


def test_no_timeline_no_playhead_and_quitting():
    reader = reader_for(None)
    assert reader.read() is None

    timeline = scene_timeline()
    timeline.on_edit_page = False  # e.g. the Media page
    hello, frame = reader_for(timeline).read()
    assert hello is not None and frame is None

    reader = reader_for(scene_timeline())
    reader.resolve.quit()
    with pytest.raises(ResolveGone):
        reader.read()


def test_a_clip_without_metadata_still_has_a_scene():
    item = FakeTimelineItem("Plain", 86400, 86500, FakeMediaPoolItem("plain.mov"))
    _, frame = reader_for(FakeTimeline("t", [item], playhead=10)).read()
    assert frame["scene"] == {"shot_id": item.uid}
    _, frame = reader_for(FakeTimeline("t", [], playhead=10)).read()
    assert frame["scene"] == {"shot_id": "none"}


def grade(timeline, item=1):
    # the grade suggested while the playhead was on timeline.items[item]
    return {"action_id": "act_aaaaaaaaaaaa", "suggestion_id": "sug_00000001", "op": "apply_color_grade",
            "params": {"shot": timeline.items[item].uid, "lut": "teal_orange_07", "contrast": 1.1, "sat": 0.92,
                       "shadow_tint": "teal", "highlight_tint": "warm"}}


def apply(reader, action, applied=None):
    opened = reader.open_timeline()
    if opened is not None and "project_id" not in action:  # sent for the open timeline unless a test says otherwise
        action = {**action, "project_id": project_id(*opened[:2])}
    result = apply_action(reader, action, set() if applied is None else applied)
    ActionResultMsg.model_validate(result)
    return result


def test_a_grade_sets_a_cdl_on_the_clip_and_marks_it():
    timeline = scene_timeline()
    result = apply(reader_for(timeline), grade(timeline))
    assert result["status"] == "applied" and "A012_C003" in result["detail"]
    assert timeline.items[1].cdl == {"NodeIndex": "1", "Slope": "1.1440 1.1110 1.0670",
                                     "Offset": "-0.0800 -0.0400 -0.0100", "Power": "1.0000 1.0000 1.0000",
                                     "Saturation": "0.9200"}
    assert timeline.markers[240.0]["customData"] == "sensei:act_aaaaaaaaaaaa"  # at the clip's start


def test_an_action_is_carried_out_once_even_after_a_restart():
    timeline = scene_timeline()
    reader, applied, action = reader_for(timeline), set(), grade(timeline)
    assert apply(reader, action, applied)["status"] == "applied"
    assert apply(reader, action, applied)["status"] == "duplicate"
    assert apply(reader, action, set())["status"] == "duplicate"  # a new run finds the marker
    assert len([m for m in timeline.markers.values() if m["customData"] == "sensei:act_aaaaaaaaaaaa"]) == 1


def test_transitions_keyframes_and_effects_leave_markers():
    timeline = scene_timeline()
    reader = reader_for(timeline)
    transition = apply(reader, {"action_id": "act_bbbbbbbbbbbb", "suggestion_id": "sug_00000002",
                                "op": "apply_transition_at_cut", "params": {"type": "dip_to_black", "frames": 3, "at": 14.208}})
    assert transition["status"] == "marked"
    assert timeline.markers[341.0]["note"] == "Add a 3-frame dip to black at this cut"

    keyframe = apply(reader, {"action_id": "act_cccccccccccc", "suggestion_id": "sug_00000003",
                              "op": "snap_keyframe_to_time", "params": {"time": 14.583, "target": "next_keyframe"}})
    assert keyframe["status"] == "marked"
    assert timeline.markers[351.0]["customData"] == "sensei:act_cccccccccccc"  # the beat marker holds frame 350

    vfx = apply(reader, {"action_id": "act_dddddddddddd", "suggestion_id": "sug_00000004", "op": "apply_vfx_preset",
                         "params": {"shot": timeline.items[1].uid, "preset": "neon_bloom_02", "threshold": 0.78,
                                    "intensity": 0.35, "radius": 0.22}})
    assert vfx["status"] == "marked"
    assert timeline.markers[240.0]["note"] == "Apply neon_bloom_02: threshold 0.78, intensity 0.35, radius 0.22"


def test_a_grade_goes_to_the_clip_it_was_suggested_for():
    # the editor has moved on to the next shot by the time Apply arrives
    timeline = scene_timeline()
    action = grade(timeline)
    timeline.playhead = 400
    assert apply(reader_for(timeline), action)["status"] == "applied"
    assert timeline.items[1].cdl is not None and timeline.items[2].cdl is None
    assert timeline.markers[240.0]["customData"] == "sensei:act_aaaaaaaaaaaa"


@pytest.mark.parametrize("change, detail", [
    (lambda t, a: a["params"].update(shot="removed-clip"), "the clip it was suggested for is no longer on the timeline"),
    (lambda t, a: a.update(op="delete_timeline"), "unsupported op delete_timeline"),
    (lambda t, a: a["params"].update(shadow_tint="purple"), "invalid params"),
    (lambda t, a: a["params"].pop("lut"), "invalid params"),
    (lambda t, a: setattr(t.items[1], "nodes", 0), "Resolve refused the grade"),  # its marker is taken back
    # no grade without the marker that recognises it as a duplicate after a restart
    (lambda t, a: [t.AddMarker(frame, "Green", "", "", 1) for frame in range(240, 246)], "no free frame for a marker"),
])
def test_actions_that_cant_be_carried_out_are_rejected_untouched(change, detail):
    timeline = scene_timeline()
    action = grade(timeline)
    change(timeline, action)
    before = dict(timeline.markers)
    result = apply(reader_for(timeline), action)
    assert result["status"] == "rejected" and result["detail"].startswith(detail)
    assert timeline.items[1].cdl is None and timeline.markers == before


def test_an_action_for_another_timeline_is_rejected_untouched():
    # the editor switched timelines after the suggestion was made: it must not land on this one
    timeline = scene_timeline()
    before = dict(timeline.markers)
    result = apply(reader_for(timeline), {**grade(timeline), "project_id": "Night-Shoot.Edit-v2-abcdef"})
    assert result["status"] == "rejected" and result["detail"].startswith("this action is for Night-Shoot.Edit-v2-abcdef")
    assert timeline.items[1].cdl is None and timeline.markers == before


def test_actions_need_an_open_timeline():
    assert apply(reader_for(None), grade(scene_timeline())) == {"type": "action_result", "action_id": "act_aaaaaaaaaaaa",
                                              "status": "rejected", "detail": "no timeline is open"}


def test_details_never_carry_control_characters():
    timeline = scene_timeline()
    timeline.items[1].name = "Night\x07shot"
    result = apply(reader_for(timeline), grade(timeline))  # apply() validates it as the service would
    assert result["status"] == "applied" and "Night shot" in result["detail"]


class FakeSocket:
    # The service's side of one connection: each hello gets the next of `answers`, behind any
    # `earlier` messages still on their way.
    def __init__(self, answers, earlier=()):
        self.answers, self.inbox, self.sent = list(answers), list(earlier), []

    def send(self, message):
        self.sent.append(json.loads(message))
        if self.sent[-1]["type"] == "hello":
            self.inbox.append(self.answers.pop(0))

    def recv(self, timeout=None):
        if not self.inbox:
            raise TimeoutError()
        message = self.inbox.pop(0)
        return message if isinstance(message, str) else json.dumps(message)


def run_session(ws):
    # until the first context frame is sent; returns the types of the messages sent
    resolve_adapter.session(ws, reader_for(scene_timeline()), set(),
                            should_stop=lambda: any(m["type"] == "context_frame" for m in ws.sent))
    return [m["type"] for m in ws.sent]


def test_only_an_ack_answers_hello():
    # an error about an earlier message, read after hello was sent, isn't hello's answer
    ws = FakeSocket([{"type": "ack", "ok": True}],
                    earlier=[{"type": "error", "message": "no result is expected from this host for that action_id"}])
    assert run_session(ws) == ["hello", "context_frame"]


def test_messages_that_arent_json_objects_are_ignored():
    ws = FakeSocket([{"type": "ack", "ok": True}], earlier=["<html>502 Bad Gateway</html>", "[1, 2]"])
    assert run_session(ws) == ["hello", "context_frame"]


def test_a_refused_hello_is_asked_again_later(monkeypatch, capsys):
    monkeypatch.setattr(resolve_adapter, "HELLO_RETRY_SECONDS", 0.2)
    refused = {"type": "ack", "ok": False, "detail": "project 'p' is held by another client"}
    assert run_session(FakeSocket([refused, {"type": "ack", "ok": True}])) == ["hello", "hello", "context_frame"]
    assert "is held by another client" in capsys.readouterr().out


def test_settings_come_from_the_environment_then_the_file(tmp_path):
    path = tmp_path / "resolve.json"
    assert resolve_adapter.settings({"SENSEI_RESOLVE_CONFIG": str(path)}) == \
           {"url": "ws://127.0.0.1:8000/ws/host", "token": "", "beat_color": "Blue"}
    path.write_text(json.dumps({"url": "ws://studio:8000/ws/host", "token": "from-file", "beat_color": "Green"}))
    env = {"SENSEI_RESOLVE_CONFIG": str(path), "SENSEI_TOKEN": "from-env"}
    assert resolve_adapter.settings(env) == {"url": "ws://studio:8000/ws/host", "token": "from-env", "beat_color": "Green"}
