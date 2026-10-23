import React, { useEffect, useRef, useState } from "react";
import SuggestionCard from "./components/SuggestionCard";
import type { Suggestion, Verdict } from "./types";

type ProjectStream = { host: string; items: Suggestion[] };

// used when the page runs outside Electron (no preload); Electron passes SENSEI_SERVICE_URL instead
const DEFAULT_SERVICE_URL = "ws://127.0.0.1:8000";

export default function App() {
  const [projects, setProjects] = useState<Record<string, ProjectStream>>({});
  // keyed by "<project>/<suggestion id>" so a verdict survives the card being remounted
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let stopped = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    // optional shared secret, presented as a WebSocket subprotocol (see docs/PROTOCOL.md)
    let protocols: string[] = [];
    let url = `${DEFAULT_SERVICE_URL}/ws/ui`;

    const connect = () => {
      const ws = new WebSocket(url, protocols);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        // the service replays the latest suggestions of every open project right after connect
        setProjects({});
      };

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === "suggestions") {
            setProjects((prev) => ({ ...prev, [msg.project_id]: { host: msg.host, items: msg.items } }));
          } else if (msg.type === "project_closed") {
            setProjects((prev) => {
              const next = { ...prev };
              delete next[msg.project_id];
              return next;
            });
          }
        } catch {}
      };

      // The service may start after the UI, or restart: keep retrying.
      ws.onclose = () => {
        setConnected(false);
        if (!stopped) retry = setTimeout(connect, 1000);
      };
    };

    (window.sensei?.config() ?? Promise.resolve({ token: null, serviceUrl: DEFAULT_SERVICE_URL })).then(({ token, serviceUrl }) => {
      url = `${serviceUrl}/ws/ui`;
      if (token) protocols = [`sensei.token.${token}`];
      if (!stopped) connect();
    });
    return () => {
      stopped = true;
      clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  const sendFeedback = (projectId: string, suggestionId: string, verdict: Verdict) => {
    const ws = wsRef.current;
    // Only show a verdict the service actually received; the buttons are disabled while disconnected.
    if (ws?.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "feedback", suggestion_id: suggestionId, verdict, project_id: projectId }));
    setVerdicts((prev) => ({ ...prev, [`${projectId}/${suggestionId}`]: verdict }));
  };

  const projectIds = Object.keys(projects).sort();

  return (
    <div style={{ padding: 18, fontFamily: "system-ui" }}>
      <h1 style={{ margin: 0 }}>SENSEI</h1>
      <p style={{ marginTop: 6, opacity: 0.75 }}>
        Real-time creative copilot — suggestions stream in as your timeline context changes.
      </p>
      <p style={{ fontSize: 12, opacity: 0.6 }}>
        {connected ? "Connected to SENSEI service" : "Connecting to SENSEI service…"}
      </p>
      {connected && projectIds.length === 0 && (
        <p style={{ opacity: 0.6 }}>Waiting for an editing host to send its timeline…</p>
      )}

      {projectIds.map((projectId) => (
        <section key={projectId} style={{ marginTop: 16 }}>
          <h2 style={{ fontSize: 14, margin: "0 0 8px" }}>
            Project {projectId} <span style={{ fontWeight: 400, opacity: 0.6 }}>· {projects[projectId].host}</span>
          </h2>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {projects[projectId].items.map((s) => (
              <SuggestionCard
                key={s.id}
                s={s}
                status={verdicts[`${projectId}/${s.id}`] ?? "idle"}
                disabled={!connected}
                onFeedback={(verdict) => sendFeedback(projectId, s.id, verdict)}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
