from typing import Annotated, Literal, Optional, Any
from pydantic import BaseModel, Field, StringConstraints

# Client-supplied strings that end up in logs and the UI are bounded, and can't carry control
# characters (e.g. newlines that would forge log lines).
ProjectId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
SuggestionId = Annotated[str, StringConstraints(pattern=r"^sug_[0-9a-f]{8}$")]
Label = Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{1,64}$")]

class HostInfo(BaseModel):
    name: Label
    version: Label

class ProjectInfo(BaseModel):
    id: ProjectId
    fps: float

class HelloMsg(BaseModel):
    type: Literal["hello"]
    host: HostInfo
    project: ProjectInfo

class SceneCtx(BaseModel):
    shot_id: str
    emotion: Optional[str] = None
    lighting: Optional[str] = None
    notes: Optional[str] = None

class Clip(BaseModel):
    id: str
    in_: float = Field(alias="in")
    out: float
    kind: Literal["video", "audio"]
    label: Optional[str] = None

class AudioInfo(BaseModel):
    bpm: Optional[float] = None
    beats: list[float] = []

class TimelineCtx(BaseModel):
    clips: list[Clip] = []
    cuts: list[float] = []
    audio: Optional[AudioInfo] = None

class ContextFrameMsg(BaseModel):
    type: Literal["context_frame"]
    ts: int
    playhead: float
    scene: SceneCtx
    timeline: TimelineCtx

class FeedbackMsg(BaseModel):
    type: Literal["feedback"]
    suggestion_id: SuggestionId
    verdict: Literal["accepted", "rejected", "modified"]
    project_id: Optional[ProjectId] = None
    notes: Optional[Annotated[str, StringConstraints(max_length=1000)]] = None

class Action(BaseModel):
    op: str
    params: dict[str, Any] = {}

class SuggestionItem(BaseModel):
    id: str
    kind: Literal["color_grade", "transition", "keyframe", "vfx"]
    title: str
    confidence: float
    rationale: str
    action: Action

class SuggestionsMsg(BaseModel):
    type: Literal["suggestions"] = "suggestions"
    ts: int
    project_id: str = ""
    host: str = ""
    items: list[SuggestionItem]

class ProjectClosedMsg(BaseModel):
    type: Literal["project_closed"] = "project_closed"
    project_id: str
