import WebSocket from "ws";
import { makeFrame } from "./timeline.js";

const url = process.env.SENSEI_WS_URL ?? "ws://127.0.0.1:8000/ws/host";
// optional shared secret, presented as a WebSocket subprotocol (see docs/PROTOCOL.md)
const protocols = process.env.SENSEI_TOKEN ? [`sensei.token.${process.env.SENSEI_TOKEN}`] : [];

const PROJECT = { id: "p1", fps: 24 };

type ApplyAction = {
  type: "apply_action";
  action_id: string;
  project_id: string;
  suggestion_id: string;
  op: string;
  params: Record<string, unknown>;
};
type ActionResult = { type: "action_result"; action_id: string; status: "applied" | "duplicate" | "rejected"; detail?: string };

const SUPPORTED_OPS = new Set(["apply_color_grade", "apply_transition_at_cut", "snap_keyframe_to_time", "apply_vfx_preset"]);
// Host-side guard: an action id is applied at most once, even if the service sends it again
// (a repeated Apply, or a resend after a reconnect). Kept across reconnects.
const applied = new Set<string>();

function applyAction(action: ApplyAction): ActionResult {
  const result = (status: ActionResult["status"], detail?: string): ActionResult =>
    ({ type: "action_result", action_id: action.action_id, status, detail });
  // an action for another project would land on the wrong timeline
  if (action.project_id !== PROJECT.id) return result("rejected", `this action is for ${action.project_id}, but ${PROJECT.id} is open`);
  if (applied.has(action.action_id)) return result("duplicate");
  if (!SUPPORTED_OPS.has(action.op)) return result("rejected", `unsupported op ${action.op}`);
  applied.add(action.action_id);
  // a real adapter edits the timeline here; the mock host only records it
  console.log(`[mock-host] applied ${action.op} ${JSON.stringify(action.params)} (${action.action_id})`);
  return result("applied");
}

function connect() {
  const ws = new WebSocket(url, protocols);
  let timer: ReturnType<typeof setInterval> | undefined;

  ws.on("open", () => {
    ws.send(JSON.stringify({
      type: "hello",
      host: { name: "mock-host", version: "0.1" },
      project: PROJECT
    }));

    let playhead = 11.5;
    timer = setInterval(() => {
      // loop over the synthetic timeline (last beat is at 19.75)
      playhead = playhead >= 20 ? 10 : playhead + 0.25;
      ws.send(JSON.stringify(makeFrame(playhead)));
    }, 500);
  });

  ws.on("message", (buf) => {
    const text = buf.toString();
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      // never sent by the service; not worth stopping the host over
      console.error("[mock-host] ignored a message that isn't JSON:", text.slice(0, 80));
      return;
    }
    if (msg?.type === "apply_action") {
      ws.send(JSON.stringify(applyAction(msg as ApplyAction)));
      return;
    }
    console.log("[service]", text);
  });

  ws.on("error", (e: Error & { code?: string }) => console.error("[mock-host]", e.code ?? e.message));

  // The service may not be up yet, or may restart: reconnect.
  ws.on("close", () => {
    clearInterval(timer);
    setTimeout(connect, 1000);
  });
}

connect();
