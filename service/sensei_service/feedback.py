"""Stores feedback and learns from it which kinds of suggestion this user takes.

Each suggestion *type* (an "arm": kind, op and its main param, e.g. the neon bloom preset) has a
Beta-Bernoulli preference whose prior mean is the rule's own confidence, worth PRIOR_STRENGTH
decisions. Applying a suggestion counts as a success once a host confirms it, dismissing it as a
failure; the posterior mean replaces the rule's confidence before suggestions are ranked.

A client has one vote per project and suggestion (its latest verdict), so repeating a decision
doesn't count twice; only the most recent MAX_VOTES votes are kept.
"""
import sqlite3
import threading
import time
from pathlib import Path

from .protocol import Adjustment

PRIOR_STRENGTH = 5  # how many decisions the rule's confidence is worth
MAX_VOTES = 10_000  # votes kept; the oldest are forgotten first
SIGNATURE_PARAM = {  # the param that distinguishes suggestions of the same kind
    "apply_color_grade": "lut",
    "apply_transition_at_cut": "type",
    "apply_vfx_preset": "preset",
}


def arm_of(kind: str, op: str, params: dict) -> str:
    key = SIGNATURE_PARAM.get(op)
    return f"{kind}/{op}" + (f"/{params.get(key)}" if key else "")


class FeedbackStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The store is created at import but used from whichever thread runs the event loop (several,
        # under the test client), so the connection isn't bound to one thread; a lock serialises access.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS votes (
            client TEXT NOT NULL, project_id TEXT NOT NULL, suggestion_id TEXT NOT NULL,
            arm TEXT NOT NULL, verdict TEXT NOT NULL, ts REAL NOT NULL,
            PRIMARY KEY (client, project_id, suggestion_id))""")
        self.db.commit()
        # per-arm (accepted, rejected) counts, kept in memory and updated as votes change
        self.counts: dict[str, tuple[int, int]] = {}
        self.total = 0
        for arm, verdict, n in self.db.execute("SELECT arm, verdict, COUNT(*) FROM votes GROUP BY arm, verdict"):
            self._count(arm, verdict, n)

    def _count(self, arm: str, verdict: str, n: int = 1):
        # n < 0 takes votes back
        accepted, rejected = self.counts.get(arm, (0, 0))
        if verdict == "rejected":
            rejected += n
        else:  # accepted, modified (applied with tweaks)
            accepted += n
        self.counts[arm] = (accepted, rejected)
        self.total += n

    def vote(self, client: str, project_id: str, suggestion_id: str, arm: str, verdict: str):
        # client is "" when auth is off
        key = (client, project_id, suggestion_id)
        with self.lock:
            old = self.db.execute("SELECT arm, verdict FROM votes WHERE client = ? AND project_id = ? "
                                  "AND suggestion_id = ?", key).fetchone()
            if old is not None:
                self._count(*old, n=-1)
            self.db.execute("INSERT OR REPLACE INTO votes VALUES (?, ?, ?, ?, ?, ?)", (*key, arm, verdict, time.time()))
            self._count(arm, verdict)
            if self.total > MAX_VOTES:
                oldest = self.db.execute("SELECT rowid, arm, verdict FROM votes ORDER BY ts, rowid LIMIT ?",
                                         (self.total - MAX_VOTES,)).fetchall()
                self.db.executemany("DELETE FROM votes WHERE rowid = ?", [(rowid,) for rowid, _, _ in oldest])
                for _, arm_, verdict_ in oldest:
                    self._count(arm_, verdict_, n=-1)
            self.db.commit()

    def adjust(self, item):
        # Replaces a suggestion's rule confidence with the posterior mean for its arm.
        accepted, rejected = self.counts.get(arm_of(item.kind, item.action.op, item.action.params), (0, 0))
        if accepted + rejected == 0:
            return item
        prior = item.confidence
        item.confidence = round((prior * PRIOR_STRENGTH + accepted) / (PRIOR_STRENGTH + accepted + rejected), 2)
        item.adjusted = Adjustment(prior=prior, applied=accepted, dismissed=rejected)
        return item
