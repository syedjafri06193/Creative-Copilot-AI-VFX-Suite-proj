export type Verdict = "accepted" | "rejected";

// How an Apply went through: "marked" means the host left it for the editor to finish (ack_apply's status).
export type CarriedOut = "applied" | "marked" | "duplicate";

// A host's answer to an applied suggestion (each host of the project answers; ack_apply gives the overall outcome).
export type ActionOutcome = {
  status: CarriedOut | "rejected";
  detail?: string | null;
};

export type SenseiConfig = { token: string | null; serviceUrl: string };

declare global {
  interface Window {
    // exposed by the Electron preload; absent when the UI runs in a plain browser
    sensei?: { config(): Promise<SenseiConfig> };
  }
}

export type Suggestion = {
  id: string;
  kind: string;
  title: string;
  confidence: number;
  rationale: string;
  action: { op: string; params: Record<string, unknown> };
  // how the user's decisions on this kind of suggestion moved `confidence` from the rule's own `prior`
  adjusted?: { prior: number; applied: number; dismissed: number } | null;
};
