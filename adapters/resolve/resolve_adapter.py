"""DaVinci Resolve host adapter for SENSEI.

Streams the open timeline's context to the SENSEI service (/ws/host) and carries out the
suggestions a UI applies. Run it from Resolve's Workspace > Scripts menu, or as a standalone
process where your Resolve edition allows external scripting. See README.md.

Needs Python 3.9+ with the `websockets` package (13 or later) installed for the Python that
runs it. Settings come from the environment (SENSEI_WS_URL, SENSEI_TOKEN, SENSEI_BEAT_COLOR)
or, for a script started from Resolve's menu, from ~/.sensei/resolve.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from typing import Callable, Optional

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

HOST = {"name": "davinci-resolve", "version": "0.2"}
DEFAULT_URL = "ws://127.0.0.1:8000/ws/host"
POLL_SECONDS = 0.25       # how often the playhead is read
STRUCTURE_SECONDS = 2.0   # how often clips, cuts and beats are re-read
WINDOW_SECONDS = 30.0     # clips, cuts and beats sent around the playhead
HELLO_RETRY_SECONDS = 10  # before asking again for a project the service refused
MARKER_COLOR = "Purple"   # markers SENSEI leaves on the timeline...
MARKER_TAG = "sensei:"    # ...carry this customData prefix and the action id
MARKER_SEARCH = 6         # frames tried from the target when a marker already sits there
TINTS = {"teal": (-0.03, 0.01, 0.04), "warm": (0.04, 0.01, -0.03)}  # RGB shifts


class ResolveGone(Exception):
    """Resolve has quit: its scripting API no longer answers."""


def log(message: str):
    print(f"[sensei] {message}", flush=True)


def settings(env=os.environ) -> dict:
    # A script started from Resolve's menu inherits Resolve's environment, so the same settings
    # can also live in a file.
    path = env.get("SENSEI_RESOLVE_CONFIG") or os.path.join(os.path.expanduser("~"), ".sensei", "resolve.json")
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        saved = {}
    return {
        "url": env.get("SENSEI_WS_URL") or saved.get("url") or DEFAULT_URL,
        "token": env.get("SENSEI_TOKEN") or saved.get("token") or "",
        "beat_color": env.get("SENSEI_BEAT_COLOR") or saved.get("beat_color") or "Blue",
    }


# --- Reading the timeline --------------------------------------------------------------------

def timecode_to_frame(timecode: str, fps: float) -> int:
    # "01:00:14:00" -> frames since 00:00:00:00; drop-frame timecode (29.97/59.94) uses ";"
    hours, minutes, seconds, frames = (int(part) for part in re.split(r"[:;.]", timecode))
    nominal = round(fps)
    total = ((hours * 60 + minutes) * 60 + seconds) * nominal + frames
    if ";" in timecode:
        dropped = 2 * (nominal // 30)  # frame numbers skipped each minute, except every tenth
        all_minutes = hours * 60 + minutes
        total -= dropped * (all_minutes - all_minutes // 10)
    return total


def frame_rate(project, timeline) -> float:
    # "24", "23.976", "29.97"...: the timeline's own setting, else the project's
    rate = timeline.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate")
    return float(str(rate).split()[0])


def slug(text: str, limit: int) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:limit] or "untitled"


def project_id(project, timeline) -> str:
    # Readable, unique per Resolve project and timeline, and within the service's id pattern.
    digest = hashlib.sha256(f"{project.GetUniqueId()}/{timeline.GetUniqueId()}".encode()).hexdigest()[:6]
    return f"{slug(project.GetName(), 24)}.{slug(timeline.GetName(), 24)}-{digest}"


def text(value, limit: int) -> str:
    return " ".join(str(value).split())[:limit]  # one line, bounded


def scene_of(item) -> dict:
    # The clip under the playhead: its unique id (so a grade or effect suggested for it can find it
    # again), and what its metadata says. Keywords such as "emotion:tense" and "lighting:low-key"
    # set the mood; Description and Comments are the notes.
    if item is None:
        return {"shot_id": "none"}
    media = item.GetMediaPoolItem()
    metadata = (media.GetMetadata() or {}) if media is not None else {}
    scene = {"shot_id": text(item.GetUniqueId(), 64) or "none"}
    for keyword in re.split(r"[,;]", metadata.get("Keywords", "")):
        key, sep, value = keyword.partition(":")
        if sep and key.strip().lower() in ("emotion", "lighting") and value.strip():
            scene[key.strip().lower()] = text(value, 64)
    notes = " ".join(metadata.get(field, "") for field in ("Description", "Comments")).strip()
    if notes:
        scene["notes"] = text(notes, 500)
    return scene


def structure(timeline, fps: float, beat_color: str) -> tuple[list, list, list]:
    # Clips and cuts on video track 1, and beats: markers of the beat colour (not SENSEI's own).
    start = timeline.GetStartFrame()
    items = sorted(timeline.GetItemListInTrack("video", 1) or [], key=lambda item: item.GetStart())
    clips = [{"id": text(item.GetUniqueId(), 64), "in": round((item.GetStart() - start) / fps, 3),
              "out": round((item.GetEnd() - start) / fps, 3), "kind": "video", "label": text(item.GetName(), 64)}
             for item in items]
    cuts = [clip["in"] for clip in clips[1:]]  # every clip after the first starts with a cut
    beats = sorted(round(float(frame) / fps, 3) for frame, info in (timeline.GetMarkers() or {}).items()
                   if info.get("color") == beat_color and not str(info.get("customData", "")).startswith(MARKER_TAG))
    return clips, cuts, beats


class Reader:
    """Reads the open timeline as the editor works: the playhead on every poll, its clips,
    cuts and beats every few seconds."""

    def __init__(self, resolve, beat_color: str = "Blue"):
        self.resolve = resolve
        self.beat_color = beat_color
        self.cached: Optional[tuple] = None  # (project id, clips, cuts, beats)
        self.read_at = 0.0

    def open_timeline(self):
        # (project, timeline, fps), or None when no timeline is open
        manager = self.resolve.GetProjectManager()
        if manager is None:
            raise ResolveGone()
        project = manager.GetCurrentProject()
        timeline = project.GetCurrentTimeline() if project is not None else None
        if timeline is None:
            return None
        return project, timeline, frame_rate(project, timeline)

    def playhead(self, timeline, fps: float) -> Optional[float]:
        # seconds from the timeline's start; None on pages without a playhead (e.g. Media)
        timecode = timeline.GetCurrentTimecode()
        if not timecode:
            return None
        return round((timecode_to_frame(timecode, fps) - timeline.GetStartFrame()) / fps, 3)

    def read(self) -> Optional[tuple[dict, Optional[dict]]]:
        # (hello, context frame without ts) for the open timeline, or None
        opened = self.open_timeline()
        if opened is None:
            return None
        project, timeline, fps = opened
        pid = project_id(project, timeline)
        hello = {"type": "hello", "host": HOST, "project": {"id": pid, "fps": fps}}
        now = time.monotonic()
        if self.cached is None or self.cached[0] != pid or now - self.read_at >= STRUCTURE_SECONDS:
            self.cached = (pid, *structure(timeline, fps, self.beat_color))
            self.read_at = now
        playhead = self.playhead(timeline, fps)
        if playhead is None:
            return hello, None
        _, clips, cuts, beats = self.cached
        low, high = playhead - WINDOW_SECONDS, playhead + WINDOW_SECONDS
        return hello, {
            "type": "context_frame",
            "playhead": playhead,
            "scene": scene_of(timeline.GetCurrentVideoItem()),
            "timeline": {
                "clips": [clip for clip in clips if clip["out"] >= low and clip["in"] <= high],
                "cuts": [cut for cut in cuts if low <= cut <= high],
                "audio": {"beats": [beat for beat in beats if low <= beat <= high]},
            },
        }


# --- Applying actions ------------------------------------------------------------------------

def leave_marker(timeline, frame: int, name: str, note: str, tag: str) -> Optional[int]:
    # Markers can't share a frame: takes the first free frame from `frame` on. Returns it, or None.
    taken = timeline.GetMarkers() or {}
    for candidate in range(max(frame, 0), max(frame, 0) + MARKER_SEARCH):
        if float(candidate) not in taken and timeline.AddMarker(candidate, MARKER_COLOR, name, note, 1, tag):
            return candidate
    return None


def cdl_for(params: dict) -> dict:
    # An ASC CDL approximating the grade on the clip's first node: contrast pivots around mid grey
    # (slope with a balancing offset), a shadow tint shifts the offset (shows most in the shadows),
    # a highlight tint scales the slope (shows most in the highlights).
    contrast = float(params["contrast"])
    slope = [contrast] * 3
    offset = [0.5 * (1 - contrast)] * 3
    if params.get("shadow_tint"):
        offset = [o + t for o, t in zip(offset, TINTS[params["shadow_tint"]])]
    if params.get("highlight_tint"):
        slope = [s * (1 + t) for s, t in zip(slope, TINTS[params["highlight_tint"]])]

    def triple(values):
        return " ".join(f"{v:.4f}" for v in values)

    return {"NodeIndex": "1", "Slope": triple(slope), "Offset": triple(offset), "Power": triple([1.0] * 3),
            "Saturation": f"{float(params['sat']):.4f}"}


def suggested_clip(timeline, shot: str):
    # The clip a grade or effect was suggested for (the scene's shot_id is its unique id), wherever
    # the playhead is now; None once it has left the timeline.
    for track in range(1, int(timeline.GetTrackCount("video") or 0) + 1):
        for item in timeline.GetItemListInTrack("video", track) or []:
            if text(item.GetUniqueId(), 64) == shot:
                return item
    return None


def apply_color_grade(timeline, fps, params, tag):
    look, cdl = params["lut"], cdl_for(params)  # bad params are rejected before anything changes
    item = suggested_clip(timeline, params["shot"])
    if item is None:
        return "rejected", "the clip it was suggested for is no longer on the timeline"
    # The marker first: it is what recognises a repeat after a restart, so there is no grade without one.
    if leave_marker(timeline, item.GetStart() - timeline.GetStartFrame(), "SENSEI grade",
                    f"Approximates the {look} look with a CDL on node 1", tag) is None:
        return "rejected", "no free frame for a marker there"
    if not item.SetCDL(cdl):
        timeline.DeleteMarkerByCustomData(tag)
        return "rejected", "Resolve refused the grade"
    return "applied", f"CDL on node 1 of {text(item.GetName(), 64)} (approximates {look})"


def apply_transition_at_cut(timeline, fps, params, tag):
    # The scripting API can't add transitions: leave the editor a marker on the cut.
    kind = {"dip_to_black": "dip to black", "cross_dissolve": "cross dissolve"}[params["type"]]
    note = f"Add a {int(params['frames'])}-frame {kind} at this cut"
    return marked(timeline, round(float(params["at"]) * fps), "SENSEI transition", note, tag, fps)


def snap_keyframe_to_time(timeline, fps, params, tag):
    # Nor keyframes: mark the beat the next keyframe should move to.
    return marked(timeline, round(float(params["time"]) * fps), "SENSEI keyframe",
                  "Move the next keyframe onto this beat", tag, fps)


def apply_vfx_preset(timeline, fps, params, tag):
    # Nor effects: mark the clip with the preset and its settings.
    item = suggested_clip(timeline, params["shot"])
    if item is None:
        return "rejected", "the clip it was suggested for is no longer on the timeline"
    note = (f"Apply {params['preset']}: threshold {float(params['threshold']):.2f}, "
            f"intensity {float(params['intensity']):.2f}, radius {float(params['radius']):.2f}")
    return marked(timeline, item.GetStart() - timeline.GetStartFrame(), "SENSEI effect", note, tag, fps)


def marked(timeline, frame, name, note, tag, fps):
    placed = leave_marker(timeline, frame, name, note, tag)
    if placed is None:
        return "rejected", "no free frame for a marker there"
    return "marked", f"left a marker at {placed / fps:.2f}s: {note}"


OPS = {
    "apply_color_grade": apply_color_grade,
    "apply_transition_at_cut": apply_transition_at_cut,
    "snap_keyframe_to_time": snap_keyframe_to_time,
    "apply_vfx_preset": apply_vfx_preset,
}


def apply_action(reader: Reader, action: dict, applied: set) -> dict:
    # Carries out an apply_action on the open timeline; returns the action_result. An action id is
    # carried out at most once: `applied` covers this run, the marker each action leaves covers earlier ones.
    action_id = action["action_id"]

    def result(status: str, detail: Optional[str] = None) -> dict:
        # the service refuses a detail over 200 characters or with control characters
        return {"type": "action_result", "action_id": action_id, "status": status,
                "detail": None if detail is None else re.sub(r"[\x00-\x1f\x7f]+", " ", detail)[:200]}

    opened = reader.open_timeline()
    if opened is None:
        return result("rejected", "no timeline is open")
    project, timeline, fps = opened
    open_id = project_id(project, timeline)
    if action.get("project_id") != open_id:
        # sent for a timeline the editor has since left: carrying it out would edit the wrong one
        return result("rejected", f"this action is for {action.get('project_id')}, but {open_id} is open")
    tag = MARKER_TAG + action_id
    if action_id in applied or timeline.GetMarkerByCustomData(tag):
        return result("duplicate")
    handler = OPS.get(action.get("op"))
    if handler is None:
        return result("rejected", f"unsupported op {action.get('op')}")
    try:
        status, detail = handler(timeline, fps, action.get("params") or {}, tag)
    except (KeyError, TypeError, ValueError) as exc:
        return result("rejected", f"invalid params ({exc!r})")
    if status != "rejected":
        applied.add(action_id)
    log(f"{action['op']} {status}: {detail}")
    return result(status, detail)


# --- Talking to the service ------------------------------------------------------------------

def session(ws, reader: Reader, applied: set, should_stop: Callable[[], bool]):
    # One connection: announce the open timeline, stream its context when it changes, carry out
    # actions as they arrive. Returns when asked to stop; connection errors propagate.
    def on_message(raw) -> dict:
        try:
            message = json.loads(raw)
        except ValueError:
            message = None
        if not isinstance(message, dict):  # never sent by the service; not worth dropping the connection over
            log(f"ignored a message that isn't a JSON object: {text(raw, 80)}")
            return {}
        if message.get("type") == "apply_action":
            ws.send(json.dumps(apply_action(reader, message, applied)))
        elif message.get("type") == "error":
            log(f"service: {message.get('message')}")
        return message

    def pump(seconds: float, until: Optional[set] = None) -> Optional[dict]:
        # Handles messages for `seconds`, or until one of the `until` types arrives (returned).
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                message = on_message(ws.recv(timeout=remaining))
            except TimeoutError:
                break
            if until and message.get("type") in until:
                return message
        if until:
            raise TimeoutError("the service didn't answer hello")
        return None

    announced: Optional[dict] = None  # the hello the service accepted
    retry_at = 0.0
    last_frame: Optional[dict] = None
    while not should_stop():
        state = reader.read()
        if state is not None:
            hello, frame = state
            if hello != announced and time.monotonic() >= retry_at:
                ws.send(json.dumps(hello))
                # only an ack answers hello: an error meanwhile is about an earlier message
                reply = pump(5.0, until={"ack"})
                if reply.get("ok"):
                    announced, last_frame = hello, None
                    log(f"streaming timeline {hello['project']['id']}")
                else:  # e.g. another client's host holds this project
                    log(f"the service refused timeline {hello['project']['id']}: {reply.get('detail')}")
                    announced, retry_at = None, time.monotonic() + HELLO_RETRY_SECONDS
            if hello == announced and frame is not None and frame != last_frame:
                ws.send(json.dumps({**frame, "ts": int(time.time())}))
                last_frame = frame
        pump(POLL_SECONDS)


def run(resolve, url: str = DEFAULT_URL, token: str = "", beat_color: str = "Blue",
        should_stop: Callable[[], bool] = lambda: False):
    # Connects to the service and keeps reconnecting until asked to stop or Resolve quits.
    reader = Reader(resolve, beat_color)
    applied: set = set()  # kept across reconnects
    delay = 1.0
    while not should_stop():
        try:
            with connect(url, subprotocols=[f"sensei.token.{token}"] if token else None, open_timeout=5) as ws:
                log(f"connected to {url}")
                delay = 1.0
                session(ws, reader, applied, should_stop)
        except ResolveGone:
            log("DaVinci Resolve has closed; stopping")
            return
        except (OSError, InvalidHandshake, ConnectionClosed, TimeoutError) as exc:
            log(f"not connected to the service ({exc}); retrying in {delay:.0f}s")
        deadline = time.monotonic() + delay
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(0.1)
        delay = min(delay * 2, 10.0)


SCRIPTING_API = {  # where Resolve installs its scripting module (README "Using a script")
    "win32": os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                          "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting"),
    "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
    "linux": "/opt/resolve/Developer/Scripting",
}


def find_resolve(names: dict):
    # Inside Resolve (Workspace > Scripts) the app object is already defined; outside it, Resolve's
    # scripting module finds the running app.
    if names.get("resolve") is not None:
        return names["resolve"]
    if names.get("bmd") is not None:
        return names["bmd"].scriptapp("Resolve")
    api = os.environ.get("RESOLVE_SCRIPT_API") or SCRIPTING_API.get(sys.platform, SCRIPTING_API["linux"])
    sys.path.append(os.path.join(api, "Modules"))
    import DaVinciResolveScript
    return DaVinciResolveScript.scriptapp("Resolve")


def main(names: dict):
    app = find_resolve(names)
    if app is None:
        log("couldn't reach DaVinci Resolve: is it running, and does it allow external scripting "
            "(Preferences > System > General)? Or start this from Workspace > Scripts.")
        sys.exit(1)
    try:
        run(app, **settings())
    except KeyboardInterrupt:
        pass


# Run as a script, or from Resolve's Scripts menu (which defines `resolve`); importing does nothing.
if __name__ == "__main__" or "resolve" in globals():
    main(globals())
