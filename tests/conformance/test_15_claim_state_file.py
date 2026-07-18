#!/usr/bin/env python3
"""The claim state file lifecycle: kraken.py claim writes
${KRAKEN_STATE_DIR}/claim-<worker>.json on a won claim (exit 0), and every
terminal worker transition — deliver, escalate, release — removes it."""
import os
import unittest

from harness import KrakenConformanceTest


class ClaimStateFileTests(KrakenConformanceTest):
    def test_claim_state_file_lifecycle(self):
        state_file = self.claim_state_file("w1")

        # --- claim writes the state file on exit 0 --------------------------
        self.mk_issue(7, "a task", "kraken-task", "project:app")
        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "clean claim exit")
        self.assertTrue(os.path.isfile(state_file), "claim did not write the state file")
        with open(state_file, encoding="utf-8") as f:
            content = f.read()
        for field in ('"repo"', '"issue"', '"worker"', "OWNER/tasks", "w1"):
            self.assertIn(field, content, "state file missing %s" % field)

        # --- a lost/held claim writes NO new state file (leaves w1's intact) --
        self.mk_issue(8, "held task", "kraken-task", "project:app", "in-progress")
        r = self.kraken("claim", "OWNER/tasks", 8, "w1")
        self.assertEqual(r.rc, 11, "held claim exit")
        self.assertTrue(os.path.isfile(state_file),
                        "guard/skip wrongly removed an unrelated claim state file")

        # --- release removes it ---------------------------------------------
        r = self.kraken("release", "OWNER/tasks", 7, "w1", "backing out")
        self.assertEqual(r.rc, 0, "release exit")
        self.assertFalse(os.path.isfile(state_file), "release did not remove the state file")

        # --- escalate removes it --------------------------------------------
        self.mk_issue(9, "blocked task", "kraken-task", "project:app")
        self.kraken("claim", "OWNER/tasks", 9, "w1")
        esf = self.claim_state_file("w1")
        self.assertTrue(os.path.isfile(esf), "re-claim for escalate test did not write state file")
        q = os.path.join(self.state, "q.md")
        self._write(q, "which way?\n")
        r = self.kraken("escalate", "OWNER/tasks", 9, "w1", q)
        self.assertEqual(r.rc, 0, "escalate exit")
        self.assertFalse(os.path.isfile(esf), "escalate did not remove the state file")

        # --- deliver removes it ---------------------------------------------
        self.mk_issue(10, "shipped task", "kraken-task", "project:app")
        self.kraken("claim", "OWNER/tasks", 10, "w1")
        dsf = self.claim_state_file("w1")
        self.assertTrue(os.path.isfile(dsf), "re-claim for deliver test did not write state file")
        rf = os.path.join(self.state, "r.md")
        self._write(rf, "done, validated\n")
        r = self.kraken("deliver", "OWNER/tasks", 10, "w1", rf, "https://x/pr/1")
        self.assertEqual(r.rc, 0, "deliver exit")
        self.assertFalse(os.path.isfile(dsf), "deliver did not remove the state file")


if __name__ == "__main__":
    unittest.main()
