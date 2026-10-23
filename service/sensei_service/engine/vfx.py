from ..protocol import SuggestionItem, Action

def suggest_vfx(ctx):
    notes = (ctx.scene.notes or "").lower()
    items = []
    if "neon" in notes:
        items.append(SuggestionItem(
            id="",
            kind="vfx",
            title="Add subtle glow + bloom tuned for neon spill",
            confidence=0.74,
            rationale="Neon signage benefits from controlled bloom to sell intensity without clipping.",
            action=Action(op="apply_vfx_preset", params={
                "shot": ctx.scene.shot_id,
                "preset": "neon_bloom_02",
                "threshold": 0.78,
                "intensity": 0.35,
                "radius": 0.22
            })
        ))
    return items
