from typing import Annotated, Literal, Optional, Any
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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
    shot_id: Label  # the clip under the playhead, as the host can find it again (grades and effects target it)
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
    project_id: ProjectId
    notes: Optional[Annotated[str, StringConstraints(max_length=1000)]] = None

class FeedbackAckMsg(BaseModel):  # service -> UI; names the suggestion so the UI can undo a refused verdict
    type: Literal["ack_feedback"] = "ack_feedback"
    ok: bool
    project_id: str
    suggestion_id: str
    detail: Optional[str] = None

class Action(BaseModel):
    op: str
    params: dict[str, Any] = {}

class Adjustment(BaseModel):  # how the user's stored decisions moved a suggestion's confidence
    prior: float  # the rule's own confidence
    applied: int
    dismissed: int

class SuggestionItem(BaseModel):
    id: str
    kind: Literal["color_grade", "transition", "keyframe", "vfx"]
    title: str
    confidence: float
    rationale: str
    action: Action
    adjusted: Optional[Adjustment] = None  # None while there is no feedback on this kind of suggestion

class SuggestionsMsg(BaseModel):
    type: Literal["suggestions"] = "suggestions"
    ts: int
    project_id: str = ""
    host: str = ""
    items: list[SuggestionItem]

class ProjectClosedMsg(BaseModel):
    type: Literal["project_closed"] = "project_closed"
    project_id: str

# --- Applying actions -------------------------------------------------------------------------

ActionId = Annotated[str, StringConstraints(pattern=r"^act_[0-9a-f]{12}$")]
PresetName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,64}$")]
Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Seconds = Annotated[float, Field(ge=0.0)]

class BoundedParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ColorGradeParams(BoundedParams):
    shot: Label  # the scene's shot_id: the clip it was suggested for, not whichever is under the playhead later
    lut: PresetName
    contrast: Annotated[float, Field(ge=0.5, le=2.0)]
    sat: Annotated[float, Field(ge=0.0, le=2.0)]
    shadow_tint: Optional[Literal["teal", "warm"]] = None
    highlight_tint: Optional[Literal["teal", "warm"]] = None

class TransitionParams(BoundedParams):
    type: Literal["dip_to_black", "cross_dissolve"]
    frames: Annotated[int, Field(ge=1, le=48)]
    at: Seconds

class KeyframeParams(BoundedParams):
    time: Seconds
    target: Literal["next_keyframe"]

class VfxParams(BoundedParams):
    shot: Label
    preset: PresetName
    threshold: Unit
    intensity: Unit
    radius: Unit

# The only operations a host is ever asked to perform, and the bounds on their params.
ACTION_PARAMS: dict[str, type[BoundedParams]] = {
    "apply_color_grade": ColorGradeParams,
    "apply_transition_at_cut": TransitionParams,
    "snap_keyframe_to_time": KeyframeParams,
    "apply_vfx_preset": VfxParams,
}

def checked_params(op: str, params: dict[str, Any]) -> dict[str, Any]:
    if op not in ACTION_PARAMS:
        raise ValueError(f"unknown action op {op!r}")
    return ACTION_PARAMS[op].model_validate(params).model_dump(exclude_none=True)

class ApplyMsg(BaseModel):  # UI -> service: names a suggestion; the action comes from the service
    type: Literal["apply"]
    project_id: ProjectId
    suggestion_id: SuggestionId

class ApplyActionMsg(BaseModel):  # service -> host; a host rejects it unless project_id is what it has open
    type: Literal["apply_action"] = "apply_action"
    action_id: ActionId
    project_id: ProjectId
    suggestion_id: SuggestionId
    op: str
    params: dict[str, Any]

class ActionResultMsg(BaseModel):  # host -> service
    type: Literal["action_result"]
    action_id: ActionId
    status: Literal["applied", "marked", "duplicate", "rejected"]
    detail: Optional[Annotated[str, StringConstraints(pattern=r"^[^\x00-\x1f\x7f]{0,200}$")]] = None  # shown on every UI

class ActionOutcomeMsg(BaseModel):  # service -> UI: one host's result, forwarded to every UI
    type: Literal["action_result"] = "action_result"
    project_id: str
    suggestion_id: str
    action_id: str
    status: Literal["applied", "marked", "duplicate", "rejected"]
    detail: Optional[str] = None

class ApplyAckMsg(BaseModel):  # service -> the UI that applied: the outcome of its Apply, once known
    type: Literal["ack_apply"] = "ack_apply"
    ok: bool  # a host carried it out (and the Apply counts), or none did: then `detail` says why
    project_id: str
    suggestion_id: str
    # when ok, how: the result of the host that carried it out ("marked": left for the editor to finish)
    status: Optional[Literal["applied", "marked", "duplicate"]] = None
    detail: Optional[str] = None
