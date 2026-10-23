# Security

## Principles
- Treat host project data as sensitive.
- Default: run suggestions locally; optional remote inference behind explicit user opt-in (none exists today).
- Never transmit media without consent; use metadata/proxies by default (today only timeline metadata is sent).

## Controls in place
- **Network exposure:** the service binds to loopback; Docker Compose publishes it on `127.0.0.1` only.
- **Browser origins:** browsers don't apply the same-origin policy to WebSockets, so handshakes from browser
  origins outside `SENSEI_ALLOWED_ORIGINS` are rejected.
- **Optional token:** with `SENSEI_TOKEN` set, every client must present it as a WebSocket subprotocol; it is
  compared in constant time and never appears in URLs.
- **Desktop renderer:** context isolation, no Node integration, sandbox; a Content-Security-Policy (no `eval`,
  WebSockets only to the service); popups and navigation away from the UI are blocked. The token reaches the page
  over IPC through a sandboxed preload, never through the bundle or a URL.
- **Input handling:** every message is validated; malformed input gets an error reply rather than a crash.
- **Supply chain:** Python dependencies are hash-pinned and installed with `--require-hashes`; npm dependencies are
  locked; CI actions are pinned to commit SHAs; the images run as non-root users.

## Known gaps
- Suggested actions are not applied to hosts yet. Before they are: give actions ids, make "apply" idempotent with
  host-side guards, and limit params so an action cannot trigger destructive operations.
- Feedback is logged, not stored; retention rules are needed once it is persisted.
- The token is a shared secret from the environment; there is no per-user identity.
