"""
DaVinci Resolve adapter: placeholder, not implemented yet.

Resolve exposes scripting via Python/Lua.
An adapter would:
- Connect to Resolve scripting API
- Read current timeline playhead, clip metadata
- Connect to the SENSEI service's /ws/host, send `hello`, then context frames (see docs/PROTOCOL.md)
- Apply actions: LUT, OFX, transitions, keyframe adjustments
"""

def get_timeline_context():
    # TODO: implement Resolve API calls
    return {}

def apply_action(action: dict):
    # TODO: map SENSEI action ops -> Resolve operations
    return True
