# Adobe Bridge (not started)

Goal: connect SENSEI to Premiere Pro / After Effects. Nothing is implemented here yet.

Two common pathways:
- CEP (legacy panels) — easiest for quick prototypes in older environments
- UXP (newer extensibility) — preferred long-term

A bridge needs:
1) A UI panel shell inside the host
2) A bridge process that talks to the SENSEI service over WebSocket (`/ws/host`, see `docs/PROTOCOL.md`)
3) A thin "host API" wrapper to read timeline state & apply actions

Next steps:
- Define the minimal timeline schema you can extract reliably from Adobe APIs
- Implement `getTimelineContext()` and `applyAction(action)`
