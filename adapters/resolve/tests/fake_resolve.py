"""A stand-in for the DaVinci Resolve scripting API, for tests.

Implements only the calls the adapter makes, as Resolve's scripting README (v20) documents them:
settings and timecodes are strings; GetStartFrame and item positions are absolute frame numbers;
marker dicts are keyed by float frame offsets from the timeline's start and hold color, duration,
note, name and customData; AddMarker refuses an occupied frame, an unknown color or a frame
outside the timeline; SetCDL takes string values and a NodeIndex within the clip's nodes.
"""
import uuid

MARKER_COLORS = {"Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose",
                 "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"}
CDL_KEYS = {"NodeIndex", "Slope", "Offset", "Power", "Saturation"}


class FakeMediaPoolItem:
    def __init__(self, name, metadata=None):
        self.name = name
        self.metadata = dict(metadata or {})

    def GetName(self):
        return self.name

    def GetMetadata(self, metadataType=None):
        if metadataType is None:
            return dict(self.metadata)
        return self.metadata.get(metadataType, "")


class FakeTimelineItem:
    def __init__(self, name, start, end, media=None, nodes=1):
        self.name, self.start, self.end, self.media, self.nodes = name, start, end, media, nodes
        self.uid = str(uuid.uuid4())
        self.cdl = None

    def GetName(self):
        return self.name

    def GetStart(self, subframe_precision=False):
        return self.start

    def GetEnd(self, subframe_precision=False):
        return self.end

    def GetDuration(self, subframe_precision=False):
        return self.end - self.start

    def GetUniqueId(self):
        return self.uid

    def GetMediaPoolItem(self):
        return self.media

    def SetCDL(self, cdl):
        if not isinstance(cdl, dict) or not set(cdl) <= CDL_KEYS or "NodeIndex" not in cdl:
            return False
        if not all(isinstance(value, str) for value in cdl.values()):
            return False
        try:
            if not 1 <= int(cdl["NodeIndex"]) <= self.nodes:
                return False
            for key in ("Slope", "Offset", "Power"):
                if key in cdl and len([float(v) for v in cdl[key].split()]) != 3:
                    return False
            if "Saturation" in cdl:
                float(cdl["Saturation"])
        except ValueError:
            return False
        self.cdl = dict(cdl)
        return True


class FakeTimeline:
    def __init__(self, name, items, fps=24, start_frame=86400, length=24 * 120, playhead=0):
        # `playhead` is a frame offset from the start; fps an integer rate (non-drop timecode)
        self.name, self.items, self.fps = name, list(items), fps
        self.start_frame, self.length, self.playhead = start_frame, length, playhead
        self.settings = {"timelineFrameRate": str(fps)}
        self.markers = {}
        self.uid = str(uuid.uuid4())
        self.on_edit_page = True

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetSetting(self, settingName=None):
        if settingName is None:
            return dict(self.settings)
        return self.settings.get(settingName, "")

    def GetStartFrame(self):
        return self.start_frame

    def GetEndFrame(self):
        return self.start_frame + self.length

    def GetCurrentTimecode(self):
        if not self.on_edit_page:  # only answered on the Cut, Edit, Color and Deliver pages
            return ""
        frame = self.start_frame + self.playhead
        seconds, frames = divmod(frame, self.fps)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"

    def GetTrackCount(self, trackType):
        return 1 if trackType == "video" else 0

    def GetItemListInTrack(self, trackType, index):
        return list(self.items) if (trackType, index) == ("video", 1) else []

    def GetCurrentVideoItem(self):
        frame = self.start_frame + self.playhead
        return next((item for item in self.items if item.start <= frame < item.end), None)

    def GetMarkers(self):
        return {frame: dict(info) for frame, info in self.markers.items()}

    def AddMarker(self, frameId, color, name, note, duration, customData=""):
        frame = float(frameId)
        if frame in self.markers or color not in MARKER_COLORS or not 0 <= frame < self.length:
            return False
        self.markers[frame] = {"color": color, "duration": float(duration), "note": note, "name": name,
                               "customData": customData}
        return True

    def GetMarkerByCustomData(self, customData):
        for frame, info in self.markers.items():
            if info["customData"] == customData:
                return {frame: dict(info)}
        return {}

    def DeleteMarkerByCustomData(self, customData):
        # deletes the first marker with that customData
        for frame, info in self.markers.items():
            if info["customData"] == customData:
                del self.markers[frame]
                return True
        return False


class FakeProject:
    def __init__(self, name, timeline=None):
        self.name, self.timeline = name, timeline
        self.uid = str(uuid.uuid4())
        self.settings = {"timelineFrameRate": "24"}

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetCurrentTimeline(self):
        return self.timeline

    def GetSetting(self, settingName=None):
        return self.settings.get(settingName, "")


class FakeProjectManager:
    def __init__(self, project=None):
        self.project = project

    def GetCurrentProject(self):
        return self.project


class FakeResolve:
    def __init__(self, project=None):
        self.manager = FakeProjectManager(project)

    def GetProjectManager(self):
        return self.manager  # None once Resolve has quit

    def quit(self):
        self.manager = None


def scene_timeline():
    # Three shots at 24 fps from 01:00:00:00; the playhead sits on the second, 0.208s before a cut.
    # Blue markers are beats; a red note marker and a SENSEI marker are not.
    start = 86400
    night = FakeMediaPoolItem("A012_C003.mov", {"Keywords": "emotion:tense, lighting:low-key, night",
                                                 "Comments": "night interior, neon spill"})
    items = [
        FakeTimelineItem("A012_C002", start, start + 240, FakeMediaPoolItem("A012_C002.mov")),
        FakeTimelineItem("A012_C003", start + 240, start + 341, night),
        FakeTimelineItem("A012_C004", start + 341, start + 600, FakeMediaPoolItem("A012_C004.mov")),
        FakeTimelineItem("B003_C001", start + 2400, start + 2500, FakeMediaPoolItem("B003_C001.mov")),
    ]
    timeline = FakeTimeline("Edit v3", items, playhead=336)  # 14.0s
    for frame in (334, 350, 365):
        timeline.AddMarker(frame, "Blue", "", "", 1)
    timeline.AddMarker(338, "Red", "Note", "fix the boom", 1)
    timeline.AddMarker(352, "Purple", "SENSEI keyframe", "", 1, "sensei:act_000000000000")
    return timeline
