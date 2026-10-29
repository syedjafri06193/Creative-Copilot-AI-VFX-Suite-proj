# SENSEI Protocol (v0.1)

Transport: WebSocket JSON

## Endpoints
- `/ws/host` — editing hosts. Send `hello` first (it registers the host's project), then `context_frame` and `action_result`. Each `context_frame` is answered with `suggestions` on the same socket; a frame sent before `hello` gets an `error`. Hosts also receive `apply_action` (see *Applying a suggestion*).
- `/ws/ui` — copilot UIs. No handshake: UIs don't register a project (a `hello` here is acknowledged and ignored). Send `apply` and `feedback`; each is answered once, with `ack_apply` and `ack_feedback`, on the connection that sent it (if that connection closes first, the answer is lost and the outcome is unknown to the UI; sending the decision again is safe, since an action a host already carried out comes back as `duplicate` and a client's later vote replaces its earlier one). On connect, receive the latest `suggestions` of every open project; then every `suggestions` message produced from any host's frames, and `project_closed` when the last host of a project disconnects, in that order. A UI that falls 256 messages behind is disconnected (close code 1013) rather than slowing everyone else down.

Suggestions are scoped per project: each `suggestions` message carries the `project_id` and `host` name from that host's `hello`, so a UI can keep one stream per project. With per-client tokens, a project belongs to the client whose host opened it; a `hello` for it from another client's host is answered with `"ok": false` (more hosts of the same client may join), and that host no longer hosts the project it had before, since its editor has left it.
- `GET /health` — `{"ok": true}`

WebSocket handshakes that carry a browser `Origin` not listed in `SENSEI_ALLOWED_ORIGINS` (comma-separated, default `http://127.0.0.1:5180,http://localhost:5180,sensei://app`: the desktop UI's dev server and its packaged origin) are rejected with HTTP 403. Native clients such as host adapters send no `Origin` and are accepted.

### Optional tokens
When auth is on, every WebSocket client must offer the subprotocol `sensei.token.<its token>`; a missing or wrong token is rejected with HTTP 403. The service echoes the offered subprotocol back. The token travels as a subprotocol, not a header or query string, because browsers cannot set WebSocket headers and query strings end up in logs.

The service runs in one of two modes:
- `SENSEI_TOKEN` (letters, digits, `.`, `_`, `~`, `-`): one shared token, valid on both endpoints.
- `SENSEI_CLIENTS_FILE`: per-client tokens (managed with `python -m sensei_service.clients`). Each client has roles: `host` may use `/ws/host`, `ui` may use `/ws/ui`; a token used on an endpoint its client has no role for is rejected with 403.

Setting both is refused at startup; setting neither leaves auth off.

## Host -> Service

### hello
{
  "type": "hello",
  "host": { "name": "mock-host", "version": "0.1" },
  "project": { "id": "p1", "fps": 24 }
}

### context_frame
{
  "type": "context_frame",
  "ts": 1730000000,
  "playhead": 12.4,
  "scene": {
    "shot_id": "A012_C003",
    "emotion": "tense",
    "lighting": "low-key",
    "notes": "night interior, neon spill"
  },
  "timeline": {
    "clips": [
      { "id": "c1", "in": 10.0, "out": 14.2, "kind": "video", "label": "Wide" }
    ],
    "cuts": [14.2],
    "audio": { "bpm": 92, "beats": [12.0, 12.65, 13.3] }
  }
}

`scene.shot_id` names the clip under the playhead in a way the host can find it again (1–64 characters, no control characters): grades and effects suggested for this frame carry it as `shot`.

## Service -> Host/UI

### suggestions
{
  "type": "suggestions",
  "ts": 1730000001,
  "project_id": "p1",
  "host": "mock-host",
  "items": [
    {
      "id": "sug_7bec91a2",
      "kind": "color_grade",
      "title": "Moody teal shadows + warm highlights",
      "confidence": 0.78,
      "rationale": "Tense emotion and low-key lighting benefit from cooler shadows and controlled warmth.",
      "action": {
        "op": "apply_color_grade",
        "params": { "shot": "A012_C003", "lut": "teal_orange_07", "contrast": 1.1, "sat": 0.92, "shadow_tint": "teal", "highlight_tint": "warm" }
      }
    }
  ]
}

(For the `context_frame` above the service also returns a `vfx` and a `keyframe` item; they are omitted here.)

Items are ordered by confidence, highest first; `confidence` is the rule's own, adjusted by the feedback stored for that kind of suggestion. When feedback moved it, the item carries `"adjusted": { "prior": 0.74, "applied": 0, "dismissed": 1 }` (the rule's own confidence, and the decisions that moved it); otherwise `"adjusted": null`. The `rationale` is always the rule's own. An item's `id` is derived from its content, so an unchanged suggestion keeps its id across frames. Times in action params (`time`, `at`) are seconds, rounded to milliseconds.

### Actions
An action describes an edit. These are the only ops a host is ever asked to perform; the service checks every action against these bounds (and rejects unknown keys) before sending it.

| `op` | `params` (bounds) |
|------|----------|
| `apply_color_grade` | `shot` (the frame's `scene.shot_id`), `lut` (`[a-z0-9_]{1,64}`), `contrast` (0.5–2), `sat` (0–2), optional `shadow_tint`, `highlight_tint` (`teal` or `warm`) |
| `apply_transition_at_cut` | `type` (`dip_to_black` or `cross_dissolve`), `frames` (1–48), `at` (cut time, ≥ 0) |
| `snap_keyframe_to_time` | `time` (the next beat, ≥ 0), `target` (`next_keyframe`) |
| `apply_vfx_preset` | `shot` (the frame's `scene.shot_id`), `preset` (`[a-z0-9_]{1,64}`), `threshold`, `intensity`, `radius` (0–1) |

A grade or effect is for the shot it was suggested for, not whichever clip is under the playhead when it is applied, so the host must apply it to the clip `shot` names, and reject it if that clip is gone.

## Applying a suggestion

1. The UI names a suggestion; it never sends an action itself:
   `{ "type": "apply", "project_id": "p1", "suggestion_id": "sug_7bec91a2" }`
2. The service looks the suggestion up in that project's latest suggestions, re-checks its action against the bounds above, and sends it to every host of the project:
   `{ "type": "apply_action", "action_id": "act_3f1c09a2b4d7", "project_id": "p1", "suggestion_id": "sug_7bec91a2", "op": "apply_color_grade", "params": { ... } }`
   The `action_id` is derived from the project and suggestion, so applying the same suggestion again yields the same id; a host must apply an id at most once. A host must reject an action whose `project_id` isn't the project it has open: the editor may have switched since the suggestion was made, and the action would land on the wrong timeline.
3. The host answers on its socket:
   `{ "type": "action_result", "action_id": "act_3f1c09a2b4d7", "status": "applied", "detail": null }`
   `status` is `applied`, `marked` (the host can't do it directly and left a marker for the editor), `duplicate` (already applied) or `rejected` (with a `detail`). A `detail` is shown on every UI: up to 200 characters, no control characters. A host connection answers each `apply_action` it was sent once; any other `action_result` gets an `error`.
4. The service forwards each host's result to every UI, with `project_id` and `suggestion_id` added.
5. The UI that applied gets one `ack_apply` with the outcome, which only the service can know (it knows how many hosts still owe an answer):
   - `"ok": true` as soon as one host carries the action out (`applied`, `marked` or `duplicate`);
   - `"ok": false` and a `detail` if the service couldn't send it (no host connected, the suggestion is no longer current, or it fails the bounds), or once every host it was sent to has answered without carrying it out, or left the project before answering.

   With several hosts on a project, one host's `rejected` is therefore not the outcome; a UI should go by `ack_apply`.

## UI -> Service (feedback)
{
  "type": "feedback",
  "suggestion_id": "sug_001",
  "verdict": "accepted",
  "project_id": "p1"
}

`verdict` is one of `accepted`, `rejected`, `modified`; `notes` is optional. Feedback trains the ranking, so it is only stored for a suggestion in its project's latest suggestions. Each client has one vote per project and suggestion: a later verdict replaces the earlier one, and applying a suggestion votes `accepted` when it is carried out (`ack_apply` with `"ok": true`). An apply that isn't carried out counts for nothing. Decisions count in the order they were made: if the client sends feedback on a suggestion while its Apply is still waiting for the hosts, and the feedback is recorded, that feedback stands, even if a host carries the Apply out afterwards. Feedback that isn't recorded (`"ok": false`) changes nothing.

## Service replies
### ack (to hello)
{ "type": "ack", "ok": true }

A host refused its project still gets an `ack`, so it can't mistake a late `error` about an earlier message for the answer:
{ "type": "ack", "ok": false, "detail": "project 'p1' is held by another client" }

### ack_apply (to apply, once the outcome is known)
{ "type": "ack_apply", "ok": true, "project_id": "p1", "suggestion_id": "sug_7bec91a2", "status": "applied", "detail": null }

When `ok`, `status` says how it was carried out: the result of the host that did it, `applied`, `marked` (left for the editor to finish) or `duplicate` (it already had been). Otherwise `status` is null:
{ "type": "ack_apply", "ok": false, "project_id": "p1", "suggestion_id": "sug_7bec91a2", "status": null, "detail": "the clip it was suggested for is no longer on the timeline" }

### ack_feedback (to feedback)
{ "type": "ack_feedback", "ok": true, "project_id": "p1", "suggestion_id": "sug_001", "detail": null }

Feedback on a suggestion that isn't current is not stored. The reply names the suggestion, so a UI can undo a
verdict it already showed:
{ "type": "ack_feedback", "ok": false, "project_id": "p1", "suggestion_id": "sug_001", "detail": "this suggestion is no longer current" }

### project_closed (to UIs)
Sent when the last host of a project disconnects; the project's latest suggestions are dropped.
{ "type": "project_closed", "project_id": "p1" }

### error
Sent for invalid JSON, a failed validation, or an unknown `type`. The connection stays open.
{ "type": "error", "message": "invalid context_frame: scene: Field required" }
