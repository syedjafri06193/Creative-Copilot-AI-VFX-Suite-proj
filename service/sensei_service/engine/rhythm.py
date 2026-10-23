from ..protocol import SuggestionItem, Action

def suggest_rhythm_keyframes(ctx):
    audio = ctx.timeline.audio
    if not audio or not audio.beats:
        return []

    # Snap the next keyframe to the first beat at or after the playhead
    upcoming = [b for b in audio.beats if b >= ctx.playhead]
    if not upcoming:
        return []
    beat = round(min(upcoming), 3)
    return [SuggestionItem(
        id="",
        kind="keyframe",
        title=f"Snap next keyframe to the next beat @ {beat:.2f}s",
        confidence=0.70,
        rationale="Rhythmic alignment increases perceived polish (especially for motion text / speed ramps).",
        action=Action(op="snap_keyframe_to_time", params={
            "time": beat,
            "target": "next_keyframe"
        })
    )]
