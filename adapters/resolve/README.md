# DaVinci Resolve adapter

`resolve_adapter.py` connects DaVinci Resolve to the SENSEI service as a host: it streams the open timeline's
context to `/ws/host` while you work, and carries out the suggestions you apply in the desktop UI.

## What it reads
| Context | Where it comes from in Resolve |
|---------|-------------------------------|
| Project | The current project and timeline. Switching either announces a new project to the service. |
| Playhead | `Timeline.GetCurrentTimecode()`, read 4 times a second (on the Cut, Edit, Color and Deliver pages). |
| Clips and cuts | Video track 1: every clip after the first starts with a cut. Re-read every 2 seconds; only the 30 seconds either side of the playhead are sent. |
| Beats | Timeline markers of one colour: **Blue** by default (the colour of markers you tap with `M` during playback). |
| Scene | The clip under the playhead: its unique id (the `shot_id`); keywords `emotion:<word>` and `lighting:<word>` in its metadata (e.g. `emotion:tense`, `lighting:low-key`); its Description and Comments as notes. |

## What applying does
| Suggestion | In Resolve | Reported as |
|------------|-----------|-------------|
| Color grade | Sets an ASC CDL on node 1 of the clip it was suggested for (wherever the playhead is now), approximating the suggested look (contrast, saturation, shadow and highlight tints) | `applied` |
| Transition at a cut | Leaves a marker on the cut saying which transition to add | `marked` |
| Snap keyframe to beat | Leaves a marker on the beat | `marked` |
| VFX preset | Leaves a marker on the clip it was suggested for, with the preset and its settings | `marked` |

Resolve's scripting API has no calls to add transitions, keyframes or effects, so those suggestions become notes
for the editor. Every action leaves a purple marker whose custom data is `sensei:<action id>`; an action already on
the timeline is reported as `duplicate`, even after the adapter restarts. SENSEI's markers never count as beats.
An action suggested for another timeline (you switched timelines after it was suggested) is `rejected`, so it never
lands on the one now open.

## Setup
1. Resolve runs scripts with the 64-bit Python installed on the machine. The adapter needs Python 3.9 or later and
   the `websockets` package for that Python:
   ```bash
   python -m pip install "websockets>=13"
   ```
2. Copy `resolve_adapter.py` into Resolve's `Utility` scripts folder:
   - Windows: `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`
   - macOS: `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility`
   - Linux: `~/.local/share/DaVinciResolve/Fusion/Scripts/Utility`
3. If the service isn't at `ws://127.0.0.1:8000/ws/host`, or requires a token, create `~/.sensei/resolve.json`
   (a script started from Resolve's menu doesn't see your shell's environment):
   ```json
   { "url": "ws://127.0.0.1:8000/ws/host", "token": "<token>", "beat_color": "Blue" }
   ```
   With per-client tokens, give the adapter its own host-only client, so the token in this file can't read the
   suggestion stream: `python -m sensei_service.clients --file clients.json add resolve --role host`.
4. Start the service and the desktop UI, then in Resolve choose **Workspace > Scripts > resolve_adapter**. Its log
   is in **Workspace > Console**. It keeps running, and reconnecting to the service, until Resolve quits.

It also runs as a standalone process (`python resolve_adapter.py`, stopped with Ctrl+C) where your Resolve edition
allows external scripting (Preferences > System > General > External scripting using). It then reads
`SENSEI_WS_URL`, `SENSEI_TOKEN` and `SENSEI_BEAT_COLOR` from the environment before the file, and finds Resolve's
scripting module in its default location (set `RESOLVE_SCRIPT_API` if yours differs).

## Tests
The tests drive the adapter with `tests/fake_resolve.py`, a stand-in for the parts of Resolve's scripting API it
uses, following the call signatures and return types of Resolve's scripting README (v20). The end-to-end tests run
it against the real service with token auth on, and a UI that applies suggestions.
```bash
pip install --require-hashes -r ../../service/requirements-test.lock
pytest
```

## Limitations
- Verified against the fake API above, not yet against a running copy of Resolve.
- The grade is a CDL approximation: the named look (e.g. `teal_orange_07`) isn't a LUT file Resolve has.
- Only video track 1 is read for clips and cuts, and timecode-based playheads assume the timeline's frame rate.
