import WebSocket from "ws";
import { makeFrame } from "./timeline.js";

const url = process.env.SENSEI_WS_URL ?? "ws://127.0.0.1:8000/ws/host";
// optional shared secret, presented as a WebSocket subprotocol (see docs/PROTOCOL.md)
const protocols = process.env.SENSEI_TOKEN ? [`sensei.token.${process.env.SENSEI_TOKEN}`] : [];

function connect() {
  const ws = new WebSocket(url, protocols);
  let timer: ReturnType<typeof setInterval> | undefined;

  ws.on("open", () => {
    ws.send(JSON.stringify({
      type: "hello",
      host: { name: "mock-host", version: "0.1" },
      project: { id: "p1", fps: 24 }
    }));

    let playhead = 11.5;
    timer = setInterval(() => {
      // loop over the synthetic timeline (last beat is at 19.75)
      playhead = playhead >= 20 ? 10 : playhead + 0.25;
      ws.send(JSON.stringify(makeFrame(playhead)));
    }, 500);
  });

  ws.on("message", (buf) => {
    const msg = buf.toString();
    console.log("[service]", msg);
  });

  ws.on("error", (e: Error & { code?: string }) => console.error("[mock-host]", e.code ?? e.message));

  // The service may not be up yet, or may restart: reconnect.
  ws.on("close", () => {
    clearInterval(timer);
    setTimeout(connect, 1000);
  });
}

connect();
