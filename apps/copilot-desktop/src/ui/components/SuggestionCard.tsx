import React, { useId, useRef } from "react";
import { CARRIED_OUT_TEXT, KIND_LABEL, OUTCOME_TEXT, summarize } from "../describe";
import type { ActionOutcome, CarriedOut, Suggestion, Verdict } from "../types";

// Where this UI's decision on the suggestion stands. "applying" lasts until the service says whether and how
// a host carried it out (ack_apply): "applied", "marked" (left for the editor to finish) or "duplicate". A
// Dismiss shows as dismissed at once: its answer is a local write that comes back within milliseconds, so a
// "dismissing" state would only flash. A decision that didn't go through is undone, back to "idle".
export type Stage = "idle" | "applying" | CarriedOut | "dismissed";

type Props = {
  s: Suggestion;
  stage: Stage;
  outcomes: ActionOutcome[]; // the hosts' answers, one per host when the project has several
  note?: string; // why the latest decision didn't go through
  disabled: boolean; // not connected to the service
  onDecide: (verdict: Verdict) => void;
};

const STAGE_TEXT: Record<Exclude<Stage, "idle">, string> = {
  applying: "Applying…",
  ...CARRIED_OUT_TEXT,
  dismissed: "Dismissed"
};

const STAGE_CLASS: Record<Exclude<Stage, "idle">, string> = {
  applying: "state--waiting",
  applied: "state--done",
  duplicate: "state--done",
  marked: "state--marked", // there is still work for the editor: not a finished green
  dismissed: ""
};

const ANSWER_CLASS: Record<ActionOutcome["status"], string> = {
  applied: "answer--ok",
  duplicate: "answer--ok",
  marked: "",
  rejected: "answer--refused"
};

export default function SuggestionCard({ s, stage, outcomes, note, disabled, onDecide }: Props) {
  const titleId = useId();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const confidence = Math.round(s.confidence * 100);
  const what = summarize(s);
  // A lone host answer that only restates the card (its status, or the refusal the note already gives) is
  // left out; several hosts, other answers, or an Apply from another UI are shown.
  const lone = outcomes.length === 1 ? outcomes[0] : null;
  const restated = lone !== null &&
    (lone.status === stage || (stage === "idle" && !!note && lone.status === "rejected"));
  const showAnswers = outcomes.length > 0 && !restated;

  // The buttons give way to the decision's status, so keyboard focus moves to the card's title (always
  // there) instead of falling back to the page.
  const decide = (verdict: Verdict) => {
    onDecide(verdict);
    titleRef.current?.focus();
  };

  return (
    <li className={`card${stage === "dismissed" ? " card--dismissed" : ""}`} data-kind={s.kind} aria-labelledby={titleId}>
      <div className="card__top">
        <span className="card__kind">{KIND_LABEL[s.kind] ?? s.kind}</span>
        <span className="meter" title="How sure SENSEI is, after what it learned from your decisions">
          <span className="meter__track" aria-hidden="true">
            <span className="meter__fill" style={{ width: `${confidence}%` }} />
          </span>
          {confidence}%
        </span>
      </div>
      <h3 className="card__title" id={titleId} ref={titleRef} tabIndex={-1}>{s.title}</h3>
      <p className="card__why">{s.rationale}</p>
      {s.adjusted && (
        <p className="card__learned">
          {`Your decisions moved this from ${Math.round(s.adjusted.prior * 100)}%: ` +
            `${s.adjusted.applied} applied, ${s.adjusted.dismissed} dismissed`}
        </p>
      )}
      {what && <p className="card__what">{what}</p>}

      <div className="card__actions">
        {stage === "idle" ? (
          <>
            <button className="button button--primary" disabled={disabled} aria-describedby={titleId}
                    title={disabled ? "Reconnecting to the SENSEI service" : undefined} onClick={() => decide("accepted")}>
              Apply
            </button>
            <button className="button" disabled={disabled} aria-describedby={titleId}
                    title={disabled ? "Reconnecting to the SENSEI service" : undefined} onClick={() => decide("rejected")}>
              Dismiss
            </button>
          </>
        ) : (
          // shown, not announced: the app announces the outcomes of this window's decisions in one place
          <span className={`state ${STAGE_CLASS[stage]}`.trim()}>{STAGE_TEXT[stage]}</span>
        )}
      </div>

      {showAnswers && (
        <ul className="answers" aria-label="Answers from the editing hosts">
          {outcomes.map((outcome, i) => (
            <li key={i} className={ANSWER_CLASS[outcome.status]}>
              {OUTCOME_TEXT[outcome.status]}{outcome.detail ? `: ${outcome.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
      {note && <p className="note">{note}</p>}
    </li>
  );
}
