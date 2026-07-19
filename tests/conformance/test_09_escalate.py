#!/usr/bin/env python3
"""kraken.py escalate: question posted with the needs-decision marker, labels
swapped, the claim ref (lock) deleted — and after the human requeues, ANY
worker can claim the now-unlocked task."""
import os
import unittest

from harness import KrakenConformanceTest


class EscalateTests(KrakenConformanceTest):
    def test_escalate(self):
        self.mk_issue(7, "blocked task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(7, "w1")

        q = os.path.join(self.state, "question.md")
        self._write(q, "Should pagination be cursor- or offset-based?\n\n"
                       "- A: cursor (recommended)\n- B: offset\n")

        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", q)
        self.assertEqual(r.rc, 0, "escalate exit")
        self.assertEqual(r.out, "escalate: escalated issue=7 worker=w1", "machine line")

        self.assertFalse(self.has_label(7, "in-progress"), "in-progress still present after escalate")
        self.assertTrue(self.has_label(7, "needs-decision"), "needs-decision missing after escalate")
        self.assertFalse(self.claim_ref_exists(7), "escalate left the lock behind")
        self.assert_marker(7, '{"type":"needs-decision","worker":"w1"}')
        body = self.last_comment(7).split("\n")
        self.assertIn("Should pagination be cursor- or offset-based?", body, "question body missing")
        self.assertIn("- A: cursor (recommended)", body, "options missing")

        # Human answers and requeues (removes the label) — a fresh worker must win.
        self.mk_comment(7, "option A, go")
        self.remove_label(7, "needs-decision")

        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 0, "re-claim after decision")
        self.assertEqual(r.out, "claim: claimed issue=7 worker=w2", "escalation freed the lock")

        # Bad invocation: missing question file is a 2, not a half-executed transition.
        r = self.kraken("escalate", "OWNER/tasks", 7, "w1", "/nonexistent/q.md")
        self.assertEqual(r.rc, 2, "missing question file exit")


if __name__ == "__main__":
    unittest.main()
