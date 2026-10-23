# SENSEI — AI Copilot for Editing + Motion Graphics

SENSEI is a creative copilot for editing and motion-graphics software. A host adapter streams the timeline
context (playhead, cuts, beats, scene metadata) to a local service, which sends back suggestions that a desktop
UI shows in real time:
- Color grade suggestions from the scene's emotion and lighting
- Transitions at cuts near the playhead
- Keyframe snapping to the next soundtrack beat
- VFX presets from the scene notes

**Status (v0.1).** The suggestion engine is a set of heuristics with fixed confidences; there is no ML model yet,
and emotion, lighting, cuts and beats are supplied by the host rather than detected. The only working host is a
mock that streams a synthetic timeline: the Resolve and Adobe adapters are not implemented, and suggested actions
are not applied to a host yet. See [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Monorepo Layout
- `service/` — FastAPI WebSocket suggestion service (Python)
- `apps/copilot-desktop/` — Electron + React desktop UI
- `adapters/mock-host/` — a fake editing host that streams a synthetic timeline
- `adapters/resolve/`, `adapters/adobe-bridge/` — placeholders for real host integrations
- `docs/` — [architecture](docs/ARCHITECTURE.md), [protocol](docs/PROTOCOL.md), [security](docs/SECURITY.md), [roadmap](docs/ROADMAP.md)

## Quickstart (Demo)
Requirements: Docker, Node 22.12+ (Python 3.11+ only to run the service without Docker)

### 1) Start AI service + mock host
```bash
docker compose up --build
```
The service listens on `127.0.0.1:8000` (loopback only); the mock host streams a synthetic timeline into it.

### 2) Start the desktop UI
```bash
cd apps/copilot-desktop
npm install
npm run dev
```
The first run downloads the Electron binary. Suggestions stream into the window, one section per project, as the
mock host's playhead moves; Apply / Dismiss send feedback to the service (visible in its logs). A decision is
remembered for that suggestion: ids are derived from content, so a suggestion you applied or dismissed shows as
decided whenever it reappears, for the rest of the session. The buttons are disabled while the UI is disconnected.

`npm start` runs the built UI instead of the dev server, and `npm run dist` packages a standalone app into `release/`.
If the service runs elsewhere, point the UI at it with `SENSEI_SERVICE_URL` (default `ws://127.0.0.1:8000`); the
UI's Content-Security-Policy follows it.

### Without Docker
```bash
# service
cd service
pip install --require-hashes -r requirements.lock
uvicorn sensei_service.main:app --port 8000

# mock host (second terminal)
cd adapters/mock-host
npm install
npm start
```

### Optional: require a token
Give the service, the hosts and the desktop UI the same `SENSEI_TOKEN` (letters, digits, `.`, `_`, `~`, `-`); clients without it are rejected.
```bash
export SENSEI_TOKEN=$(openssl rand -hex 16)          # PowerShell: $env:SENSEI_TOKEN = [guid]::NewGuid().ToString("N")
docker compose up --build                            # passes it to the service and the mock host
cd apps/copilot-desktop && npm run dev               # Electron hands it to the UI
```
Browser pages may only connect from the origins in `SENSEI_ALLOWED_ORIGINS` (default: the desktop UI) — see
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Development
```bash
# service tests
cd service
pip install --require-hashes -r requirements-test.lock
pytest

# type-check the TypeScript packages
cd apps/copilot-desktop && npm run typecheck
cd adapters/mock-host && npm run typecheck
```
Dependencies are locked: `service/requirements.lock` and `service/requirements-test.lock` are hash-pinned (the
`uv pip compile` command that regenerates each is in its header), and each npm package has a `package-lock.json`.
CI runs all of the above plus a Docker Compose smoke test.
