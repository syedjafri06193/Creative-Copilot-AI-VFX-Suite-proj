export type Verdict = "accepted" | "rejected";

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
};
