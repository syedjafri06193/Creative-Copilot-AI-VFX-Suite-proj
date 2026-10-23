import hashlib
import json
import time
from .color import suggest_color
from .rhythm import suggest_rhythm_keyframes
from .vfx import suggest_vfx
from .transitions import suggest_transitions
from ..protocol import SuggestionsMsg

def build_suggestions(context) -> SuggestionsMsg:
    items = []
    items += suggest_color(context)
    items += suggest_transitions(context)
    items += suggest_rhythm_keyframes(context)
    items += suggest_vfx(context)
    # ranked: most confident first (stable, so ties keep rule order)
    items.sort(key=lambda it: it.confidence, reverse=True)

    # ids derive from content, so a suggestion keeps its id across frames
    for it in items:
        if not it.id:
            key = json.dumps([it.kind, it.action.op, it.action.params], sort_keys=True)
            it.id = f"sug_{hashlib.sha256(key.encode()).hexdigest()[:8]}"
    return SuggestionsMsg(ts=int(time.time()), items=items)
