import pytest
from pydantic import ValidationError

from sensei_service.engine.suggest import build_suggestions
from sensei_service.protocol import ContextFrameMsg


def frame(playhead=14.0, emotion="tense", lighting="low-key", notes="neon spill", cuts=(14.2,), beats=(13.9, 14.55),
          shot="A012_C003"):
    return ContextFrameMsg.model_validate({
        "type": "context_frame",
        "ts": 1,
        "playhead": playhead,
        "scene": {"shot_id": shot, "emotion": emotion, "lighting": lighting, "notes": notes},
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


def test_grades_and_effects_name_their_shot():
    # so a host applies them to that clip even after the playhead has moved on, and the same look on
    # another shot is another suggestion (and action), not a duplicate of the first
    first, second = by_kind(frame(shot="A012_C003")), by_kind(frame(shot="A012_C004"))
    for kind in ("color_grade", "vfx"):
        assert first[kind].action.params["shot"] == "A012_C003"
        assert first[kind].id != second[kind].id


@pytest.mark.parametrize("shot", ["", "x" * 65, "A012\nC003"])
def test_shot_ids_are_bounded(shot):
    with pytest.raises(ValidationError):
        frame(shot=shot)


def test_suggestions_are_ranked_by_confidence():
    confidences = [i.confidence for i in build_suggestions(frame()).items]
    assert confidences == sorted(confidences, reverse=True)
