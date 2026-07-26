#!/usr/bin/env python3
"""PROTOCOL.md §5: a worker MUST work one task at a time and MUST NOT claim a
second task while it holds a claim. kraken.py claim reads the claim-<worker>.json
state file and refuses (exit 11, writing nothing) when it already marks an open
claim — for `claim` on a different issue and for `claim-next` on any open claim.
A recorded claim on the same issue is a permitted re-claim."""
import os
import unittest

from harness import KrakenConformanceTest


class ClaimOneAtATimeTests(KrakenConformanceTest):
    def test_one_task_at_a_time_guard(self):
        state_file = self.claim_state_file("w1")

        # --- w1 takes its one task ------------------------------------------
        self.mk_issue(7, "first task", "kraken-task", "project:app")
        self.mk_issue(8, "second task", "kraken-task", "project:app")
        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "clean first claim exit")
        self.assertTrue(os.path.isfile(state_file), "first claim did not write the state file")
        self.assertTrue(self.has_label(7, "in-progress"), "first claim did not label issue 7 in-progress")

        # --- claim of a DIFFERENT task is refused while the claim is open ----
        before = self.comment_count(8)
        r = self.kraken("claim", "OWNER/tasks", 8, "w1")
        self.assertEqual(r.rc, 11, "second claim (different task) is refused")
        self.assertIn("refused", r.out, "refusal message should name the refusal (got: %s)" % r.out)
        self.assertIn("holds=7", r.out, "refusal should report the open claim it holds (got: %s)" % r.out)
        self.assertFalse(self.has_label(8, "in-progress"), "refused claim wrongly labeled issue 8 in-progress")
        self.assertEqual(self.comment_count(8), before, "refused claim wrongly commented on issue 8")
        self.assertTrue(os.path.isfile(state_file), "refused claim wrongly removed the open claim state file")
        with open(state_file, encoding="utf-8") as f:
            self.assertIn('"issue": "7"', f.read(), "open claim state file no longer records issue 7")

        # --- claim-next is refused too while any claim is open --------------
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 11, "claim-next is refused while a claim is held")
        self.assertIn("refused", r.out, "claim-next refusal should name the refusal (got: %s)" % r.out)
        self.assertFalse(self.has_label(8, "in-progress"), "refused claim-next wrongly labeled issue 8 in-progress")
        self.assertEqual(self.comment_count(8), before, "refused claim-next wrongly commented on issue 8")

        # --- re-claiming the SAME issue is allowed (the network-failure caveat)
        self.remove_label(7, "in-progress")
        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "re-claiming the same held issue is permitted")
        self.assertNotIn("refused", r.out, "re-claiming the same issue must not be refused as a second claim")

        # --- resolving the claim clears the guard ---------------------------
        r = self.kraken("release", "OWNER/tasks", 7, "w1", "backing out")
        self.assertEqual(r.rc, 0, "release exit")
        self.assertFalse(os.path.isfile(state_file), "release did not remove the state file")
        r = self.kraken("claim", "OWNER/tasks", 8, "w1")
        self.assertEqual(r.rc, 0, "claim after release is no longer refused")
        self.assertTrue(self.has_label(8, "in-progress"), "post-release claim did not label issue 8 in-progress")


if __name__ == "__main__":
    unittest.main()
