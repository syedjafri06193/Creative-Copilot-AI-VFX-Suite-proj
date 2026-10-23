import React, { useEffect, useRef, useState } from "react";
import SuggestionCard, { type Stage } from "./components/SuggestionCard";
import { CARRIED_OUT_TEXT, hostName } from "./describe";
import type { ActionOutcome, CarriedOut, Suggestion, Verdict } from "./types";

type ProjectStream = { host: string; items: Suggestion[] };
type Connection = "connecting" | "connected" | "unreachable";

// used when the page runs outside Electron (no preload); Electron passes SENSEI_SERVICE_URL instead
const DEFAULT_SERVICE_URL = "ws://127.0.0.1:8000";

const MAX_OUTCOMES = 4; // host answers kept per card, so other UIs' repeated Applies can't grow it forever

const LOST_ANSWER = "The connection dropped before the service answered, so this may or may not have gone through";

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

export default function App() {
  const [projects, setProjects] = useState<Record<string, ProjectStream>>({});
  // keyed by "<project>/<suggestion id>" so a verdict survives the card being remounted
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({});
  // the hosts' answers since this UI last applied it (one per host when a project has several), by the same key
  const [outcomes, setOutcomes] = useState<Record<string, ActionOutcome[]>>({});
  // why the latest decision didn't go through, by the same key
  const [notes, setNotes] = useState<Record<string, string>>({});
  // how each Apply of this UI went through, once the service said so (ack_apply's status), by the same key
  const [carriedOut, setCarriedOut] = useState<Record<string, CarriedOut>>({});
  // The one place this UI announces to screen readers: how its own decisions turned out. The cards show their
  // status without announcing it, so a reconnect that redraws every card stays quiet. `n` counts
  // announcements, so the same words twice in a row (a retry refused for the same reason) are spoken twice.
  const [announcement, setAnnouncement] = useState({ text: "", n: 0 });
  const announce = (text: string) => setAnnouncement((prev) => ({ text, n: prev.n + 1 }));
  // titles of the suggestions decided on, by key, for those announcements (a suggestion may have gone by then)
  const titlesRef = useRef(new Map<string, string>());
  const [connection, setConnection] = useState<Connection>("connecting");
  const [serviceUrl, setServiceUrl] = useState(DEFAULT_SERVICE_URL);
  const wsRef = useRef<WebSocket | null>(null);
  // Decisions sent on the current connection and not answered yet (ack_apply / ack_feedback), by key. The
  // ref is what the close handler reads; the state copy shows "Applying…" on the card.
  const unansweredRef = useRef(new Set<string>());
  const [unanswered, setUnanswered] = useState<ReadonlySet<string>>(new Set());
  const syncUnanswered = () => setUnanswered(new Set(unansweredRef.current));

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
        setConnection("connected");
        // the service replays the latest suggestions of every open project right after connect
        setProjects({});
      };

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === "suggestions") {
            setProjects((prev) => ({ ...prev, [msg.project_id]: { host: msg.host, items: msg.items } }));
          } else if (msg.type === "action_result") {
            // one host's answer, shown as it comes; whether the Apply went through is ack_apply's to say
            const key = `${msg.project_id}/${msg.suggestion_id}`;
            const outcome: ActionOutcome = { status: msg.status, detail: msg.detail };
            setOutcomes((prev) => ({ ...prev, [key]: [...(prev[key] ?? []), outcome].slice(-MAX_OUTCOMES) }));
          } else if (msg.type === "ack_apply" || msg.type === "ack_feedback") {
            // The service's answer to this UI's decision. If it didn't go through (no host carried the Apply
            // out, or the Dismiss wasn't recorded), undo the verdict and say why.
            const key = `${msg.project_id}/${msg.suggestion_id}`;
            // an answer is only ever to a decision this connection is waiting on; anything else changes nothing
            if (!unansweredRef.current.has(key)) return;
            const title = titlesRef.current.get(key) ?? "A suggestion";
            unansweredRef.current.delete(key);
            syncUnanswered();
            if (!msg.ok) {
              const note = `${msg.type === "ack_apply" ? "Couldn't apply" : "Dismiss wasn't recorded"}: ${msg.detail}`;
              setVerdicts((prev) => without(prev, key));
              setNotes((prev) => ({ ...prev, [key]: note }));
              announce(`${title}: ${note}`);
            } else if (msg.type === "ack_apply") {
              const how: CarriedOut = msg.status ?? "applied"; // a service without status only said it went through
              setCarriedOut((prev) => ({ ...prev, [key]: how }));
              announce(`${title}: ${CARRIED_OUT_TEXT[how]}`);
            }
          } else if (msg.type === "error") {
            console.warn("[sensei] the service refused a message:", msg.message);
          } else if (msg.type === "project_closed") {
            setProjects((prev) => without(prev, msg.project_id));
          }
        } catch {}
      };

      // The service may start after the UI, or restart: keep retrying.
      ws.onclose = () => {
        setConnection("unreachable");
        // Answers are sent to the connection that made the decision, so those still owed are lost with it:
        // the outcome is unknown. Undo those verdicts. Deciding again is safe: an action a host already
        // carried out comes back as a duplicate, and a later vote replaces the earlier one.
        const lost = [...unansweredRef.current];
        unansweredRef.current.clear();
        syncUnanswered();
        if (lost.length > 0) {
          setVerdicts((prev) => lost.reduce((rest, key) => without(rest, key), prev));
          setNotes((prev) => ({ ...prev, ...Object.fromEntries(lost.map((key) => [key, LOST_ANSWER])) }));
          announce(lost.length === 1
            ? `${titlesRef.current.get(lost[0]) ?? "A suggestion"}: ${LOST_ANSWER}`
            : `The connection dropped before the service answered ${lost.length} decisions, so they may or may not have gone through`);
        }
        if (!stopped) retry = setTimeout(connect, 1000);
      };
    };

    (window.sensei?.config() ?? Promise.resolve({ token: null, serviceUrl: DEFAULT_SERVICE_URL })).then(({ token, serviceUrl }) => {
      url = `${serviceUrl}/ws/ui`;
      setServiceUrl(serviceUrl);
      if (token) protocols = [`sensei.token.${token}`];
      if (!stopped) connect();
    });
    return () => {
      stopped = true;
      clearTimeout(retry);
      wsRef.current?.close();
    };
  }, []);

  // Apply asks the service to send the suggestion's action to the host (the outcome arrives as
  // action_result); Dismiss is plain feedback. Each is answered once (ack_apply, ack_feedback): the
  // decision shows at once, and it is undone if it didn't go through.
  const decide = (projectId: string, s: Suggestion, verdict: Verdict) => {
    const ws = wsRef.current;
    // Only show a verdict the service actually received; the buttons are disabled while disconnected.
    if (ws?.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify(verdict === "accepted"
      ? { type: "apply", project_id: projectId, suggestion_id: s.id }
      : { type: "feedback", suggestion_id: s.id, verdict, project_id: projectId }));
    const key = `${projectId}/${s.id}`;
    titlesRef.current.set(key, s.title);
    unansweredRef.current.add(key);
    syncUnanswered();
    setVerdicts((prev) => ({ ...prev, [key]: verdict }));
    setCarriedOut((prev) => without(prev, key));
    // what the card said about an earlier attempt no longer applies
    setNotes((prev) => without(prev, key));
    if (verdict === "accepted") setOutcomes((prev) => without(prev, key));
  };

  const stageOf = (key: string): Stage => {
    const verdict = verdicts[key];
    if (verdict === "rejected") return "dismissed";
    if (verdict === "accepted") return unanswered.has(key) ? "applying" : carriedOut[key] ?? "applied";
    return "idle";
  };

  const connected = connection === "connected";
  const projectIds = Object.keys(projects).sort();

  return (
    <div className="app">
      <header className="topbar">
        <h1 className="wordmark">SENSEI</h1>
        <p className={`connection${connected ? " connection--ok" : ""}`} role="status">
          {connected ? "Connected" : connection === "connecting" ? "Connecting…" : `Can't reach ${serviceUrl}, retrying`}
        </p>
      </header>
      {/* The region stays put and its child is replaced on every announcement: screen readers announce what is
          added to a live region (repeated words too), and often ignore a region that is itself new. */}
      <p className="visually-hidden" role="status"><span key={announcement.n}>{announcement.text}</span></p>

      <main>
        {projectIds.length === 0 && (connected ? (
          <section className="empty">
            <h2>No timeline open yet</h2>
            <p>Suggestions appear here once an editing host connected to SENSEI opens a timeline, and they change as its playhead moves.</p>
            <p>In DaVinci Resolve, choose Workspace &gt; Scripts &gt; resolve_adapter. To try SENSEI without an editor, run the mock host: <code>npm start</code> in adapters/mock-host.</p>
          </section>
        ) : connection === "unreachable" && (
          <section className="empty">
            <h2>Can't reach the SENSEI service</h2>
            <p>Check that it's running (<code>docker compose up</code>, or uvicorn as the README describes), and, if it requires tokens, that this app has one with the ui role.</p>
            <p>This window connects by itself as soon as it can.</p>
          </section>
        ))}

        {projectIds.map((projectId) => {
          const { host, items } = projects[projectId];
          return (
            <section key={projectId} className="project" aria-labelledby={`project-${projectId}`}>
              <div className="project__header">
                <h2 className="project__name" id={`project-${projectId}`}>{projectId}</h2>
                <span className="project__host">{hostName(host)}</span>
                <span className="project__count">{items.length === 1 ? "1 suggestion" : `${items.length} suggestions`}</span>
              </div>
              <ul className="cards">
                {items.map((s) => {
                  const key = `${projectId}/${s.id}`;
                  return (
                    <SuggestionCard
                      key={s.id}
                      s={s}
                      stage={stageOf(key)}
                      outcomes={outcomes[key] ?? []}
                      note={notes[key]}
                      disabled={!connected}
                      onDecide={(verdict) => decide(projectId, s, verdict)}
                    />
                  );
                })}
              </ul>
            </section>
          );
        })}
      </main>
    </div>
  );
}
