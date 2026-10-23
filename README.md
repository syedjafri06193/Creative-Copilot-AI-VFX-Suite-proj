# SENSEI — AI Copilot for Editing + Motion Graphics

SENSEI is a creative copilot for editing and motion-graphics software. A host adapter streams the timeline
context (playhead, cuts, beats, scene metadata) to a local service, which sends back suggestions that a desktop
UI shows in real time:
- Color grade suggestions from the scene's emotion and lighting
- Transitions at cuts near the playhead
- Keyframe snapping to the next soundtrack beat
- VFX presets from the scene notes

**Status (v0.1).** The suggestion engine is a set of heuristics whose confidences are adjusted by a small model
learned from your Apply/Dismiss decisions; nothing analyses the media itself, and emotion, lighting, cuts and beats
are supplied by the host rather than detected. Applied suggestions are sent to the host, which applies them. Hosts:
a mock that streams a synthetic timeline, and a DaVinci Resolve adapter (tested against a stand-in for Resolve's
scripting API, not yet inside Resolve itself); the Adobe adapter is not implemented. See
[`docs/ROADMAP.md`](docs/ROADMAP.md).

## Monorepo Layout
- `service/` — FastAPI WebSocket suggestion service (Python)
- `apps/copilot-desktop/` — Electron + React desktop UI
- `adapters/mock-host/` — a fake editing host that streams a synthetic timeline
- `adapters/resolve/` — the DaVinci Resolve adapter ([setup](adapters/resolve/README.md))
- `adapters/adobe-bridge/` — placeholder for an Adobe integration
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
mock host's playhead moves. Each card shows the kind of suggestion in its own colour, how confident SENSEI is, and what
applying it will change; the window follows the system's dark or light theme. Apply sends the suggestion to the host (the mock host logs it, and the card shows the
host's answer); Dismiss sends feedback to the service (visible in its logs). A decision is
remembered for that suggestion: ids are derived from content, so a suggestion you applied or dismissed shows as
decided whenever it reappears, for the rest of the session. If it didn't take effect (the suggestion changed just as
you clicked, or no host carried it out), the decision is undone and the card says why; so is a decision whose answer
was lost because the connection dropped, since its outcome is unknown (deciding again is safe). The buttons are disabled while
the UI is disconnected.

SENSEI learns from these decisions: kinds of suggestion you apply gain confidence and kinds you dismiss lose it
(each suggestion counts once, with your latest decision), which changes their ranking (a card says when your decisions moved its score). Feedback is stored in
`~/.sensei/feedback.sqlite3` (the `sensei-data` volume under Docker). Delete it to start over, or set
`SENSEI_FEEDBACK_DB=:memory:` to keep nothing between runs.

`npm start` runs the built UI instead of the dev server, and `npm run dist` packages a standalone app for your OS
into `release/` (on Windows also an installer, `release/installer/SENSEI-Setup-<version>.exe`). Builds are unsigned
unless signing is configured: `SENSEI_WIN_CERT_FILE` (a `.pfx`) and `SENSEI_WIN_CERT_PASSWORD` sign the Windows app
and installer; `SENSEI_MAC_SIGN_IDENTITY` signs the macOS app, which is also notarized when `APPLE_ID`,
`APPLE_APP_SPECIFIC_PASSWORD` and `APPLE_TEAM_ID` are set. CI packages all three OSes and keeps the builds for a week.
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

### Optional: require tokens
Every client presents its token through `SENSEI_TOKEN` (the mock host and the Resolve adapter read it from the
environment; Electron hands it to the UI). The service checks them in one of two modes.

**One shared token.** Give the service and every client the same `SENSEI_TOKEN` (letters, digits, `.`, `_`, `~`, `-`).
```bash
export SENSEI_TOKEN=$(openssl rand -hex 16)          # PowerShell: $env:SENSEI_TOKEN = [guid]::NewGuid().ToString("N")
docker compose up --build                            # passes it to the service and the mock host
cd apps/copilot-desktop && npm run dev               # Electron hands it to the UI
```

**Per-client tokens.** Each client gets its own token and roles (`host` may stream frames, `ui` may follow
suggestions), so a leaked host token can't read the suggestion stream and a client can be revoked on its own. The
service stores only token hashes in `service/clients.json`.
```bash
cd service
python -m sensei_service.clients --file clients.json add mock-host --role host    # prints its token once
python -m sensei_service.clients --file clients.json add desktop --role ui
cd ..
SENSEI_MOCK_HOST_TOKEN=<mock-host token> docker compose -f docker-compose.yml -f docker-compose.clients.yml up --build
cd apps/copilot-desktop && SENSEI_TOKEN=<desktop token> npm run dev
```
`remove <name>` revokes a client (restart the service to apply). Stop the stack with a plain `docker compose down`. Setting both `SENSEI_CLIENTS_FILE` and
`SENSEI_TOKEN` for the service is refused, so auth can't be weakened by accident.
Browser pages may only connect from the origins in `SENSEI_ALLOWED_ORIGINS` (default: the desktop UI) — see
[`docs/PROTOCOL.md`](docs/PROTOCOL.md).

## Development
```bash
# service tests
cd service
pip install --require-hashes -r requirements-test.lock
pytest

# Resolve adapter tests (fake Resolve, real service; same environment as the service tests)
cd adapters/resolve && pytest

# type-check the TypeScript packages
cd apps/copilot-desktop && npm run typecheck
cd adapters/mock-host && npm run typecheck
```
Dependencies are locked: `service/requirements.lock` and `service/requirements-test.lock` are hash-pinned (the
`uv pip compile` command that regenerates each is in its header), and each npm package has a `package-lock.json`.
CI runs all of the above plus a Docker Compose smoke test.
