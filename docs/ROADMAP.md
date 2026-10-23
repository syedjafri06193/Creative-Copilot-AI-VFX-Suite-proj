# Roadmap

## v0.1 (this repo)
- Live suggestions via WebSocket, scoped per project
- Mock host adapter
- Electron UI (dev server, built, packaged)
- Suggestion engine with heuristics, ranked by confidence
- Feedback messages (validated and logged)
- Apply accepted actions in the host (action ids, idempotent apply, bounded params, host-side guards)
- Per-client tokens with roles
- Feedback stored and learned from (per-suggestion-type preferences that re-rank suggestions)
- Resolve adapter (Python), tested against a stand-in for Resolve's scripting API

## v0.2
- Resolve adapter verified inside Resolve
- Adobe bridge proof-of-concept (CEP/UXP)
- Audio beat detection utility module
- Persistent projects

## v0.3
- Local model option (e.g., on-device LLM/VLM)
- Scene analysis ingestion (EDL/XML + proxies)
- Suggestion personalization + style profiles

## v1.0
- Commercial-grade host integrations
- Robust permissions (per-user identity on top of per-client tokens)
- Template marketplace for looks / motion packs
