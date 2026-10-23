from sensei_service.engine.suggest import build_suggestions
from sensei_service.protocol import ContextFrameMsg


def frame(playhead=14.0, emotion="tense", lighting="low-key", notes="neon spill", cuts=(14.2,), beats=(13.9, 14.55)):
    return ContextFrameMsg.model_validate({
        "type": "context_frame",
        "ts": 1,
        "playhead": playhead,
        "scene": {"shot_id": "A012_C003", "emotion": emotion, "lighting": lighting, "notes": notes},
        "timeline": {"clips": [], "cuts": list(cuts), "audio": {"beats": list(beats)}},
    })


def by_kind(ctx):
    return {i.kind: i for i in build_suggestions(ctx).items}


def test_keyframe_snaps_to_the_next_beat_not_the_nearest():
    # 13.9 is nearer, but it is already behind the playhead
    assert by_kind(frame(playhead=14.0))["keyframe"].action.params["time"] == 14.55


def test_no_keyframe_after_the_last_beat():
    assert "keyframe" not in by_kind(frame(playhead=15.0))


def test_times_are_rounded():
    beats = [10 + i * 0.65 for i in range(16)]  # 10 + 9 * 0.65 == 15.850000000000001
    assert by_kind(frame(playhead=15.8, beats=beats))["keyframe"].action.params["time"] == 15.85


def test_color_grade_weighs_emotion_and_lighting():
    both = by_kind(frame(emotion="tense", lighting="low-key"))["color_grade"]
    lighting_only = by_kind(frame(emotion="neutral", lighting="low-key"))["color_grade"]
    neither = by_kind(frame(emotion="neutral", lighting="day"))["color_grade"]

    assert both.confidence > lighting_only.confidence
    assert lighting_only.rationale.startswith("Low-key lighting")
    assert "tense" not in lighting_only.rationale.lower()
    assert neither.action.params["lut"] == "filmic_neutral_03"


def test_transition_names_its_cut():
    assert by_kind(frame(playhead=14.0, cuts=(5.0, 14.2)))["transition"].action.params["at"] == 14.2


def test_suggestions_are_ranked_by_confidence():
    confidences = [i.confidence for i in build_suggestions(frame()).items]
    assert confidences == sorted(confidences, reverse=True)
