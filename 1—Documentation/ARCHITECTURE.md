# Architecture

## Components
1. Host Adapter
   - Runs next to an NLE / motion-graphics host and streams its timeline context to the service: playhead, cuts,
     beat times and scene metadata (emotion, lighting, notes).
   - Implemented: `adapters/mock-host`, which replays a synthetic timeline, and `adapters/resolve`, which reads
     DaVinci Resolve's open timeline through its scripting API (clips and cuts from video track 1, beats from
     markers, mood from clip keywords). Planned: Adobe (`adapters/adobe-bridge` is a placeholder).
   - Applies actions the service sends (`apply_action`), at most once per action id, and reports the result. The
     mock host records them; the Resolve adapter sets a CDL for a grade and leaves markers for what its API can't
     do (transitions, keyframes, effects).

2. SENSEI Service (FastAPI, `service/`)
   - `/ws/host`: a host registers its project with `hello`, then streams context frames; each frame is answered with
     suggestions and action payloads.
   - Suggestion engine (`service/sensei_service/engine/`): four rules (color grade, transition, rhythm keyframe, VFX
     preset), each a heuristic over the frame with a fixed confidence. Results are ranked by confidence, and ids are
     derived from content, so an unchanged suggestion keeps its id across frames.
   - `/ws/ui`: UIs receive the latest suggestions of every open project, then every update, scoped per project.
   - Applying: routes a suggestion a UI applied to the project's hosts (bounded, idempotent `apply_action`) and the
     hosts' results back to every UI.
   - Feedback (Apply counts as accepted once a host confirms it, Dismiss as rejected; one vote per client, project and suggestion) is stored
     locally in SQLite and learned from (`feedback.py`): each suggestion type has a Beta-Bernoulli preference whose prior mean is the rule's confidence,
     worth 5 decisions; the posterior mean replaces the confidence before ranking.
   - (Future) Pluggable model backends: local LLM/VLM, remote APIs, hybrid

3. Copilot Desktop (Electron + React, `apps/copilot-desktop/`)
   - Shows one live suggestion stream per project: each card gives the kind of suggestion, its confidence (and how
     your decisions moved it), and what applying it changes, and an Apply's status until the service says how it ended
   - Apply sends the suggestion to the host through the service and shows the host's answer; Dismiss sends feedback.
     A decision that didn't take effect (the suggestion changed meanwhile, or no host carried it out) is undone, and the card says why;
     the service tells the UI, since only it knows when every host has answered
   - Sandboxed renderer; the packaged UI is served over a privileged `sensei://` scheme

## Data flow
Host -> `hello` + context frames -> Service -> suggestions (per project) -> UI -> feedback -> Service

UI apply -> Service (checks the action) -> Host applies it -> result -> Service -> UI

## Key design choices
- WebSocket JSON protocol ([`PROTOCOL.md`](PROTOCOL.md)), validated by the pydantic models in
  `service/sensei_service/protocol.py`
- Host adapters are swappable: anything that speaks the protocol on `/ws/host` is a host
- Hosts and UIs use separate endpoints; the service fans each project's suggestions out to every UI
- Local-first: loopback binding, browser-origin allowlist, optional token ([`SECURITY.md`](SECURITY.md))
