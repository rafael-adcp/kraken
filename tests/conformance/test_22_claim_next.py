#!/usr/bin/env python3
"""kraken.py claim-next: the deterministic list -> guard -> CAS loop collapsed
into one invocation. Clean win, held-skip, honest empty (exit 3), --json, and
THE race (two concurrent claim-next workers claim DIFFERENT tasks — the ref CAS
guarantees no double-claim)."""
import json
import re
import unittest

from harness import KrakenConformanceTest, KRAKEN


class ClaimNextTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        # Keep the best-effort claim-state file out of the real home dir (already
        # isolated by the harness, but the original test sets a distinct path).
        self.kraken_state_dir = self.state + "/kraken-state"
        # The drain performs a protocol handshake against the coordination repo's
        # vendored .github/kraken.py before its first claim: seed a matching copy
        # so the version check passes and the claim loop is what is under test.
        self.mk_content(".github/kraken.py", KRAKEN)

    def test_claim_next(self):
        # --- 1. clean win: the oldest startable is claimed, briefing printed --
        self.mk_issue(7, "oldest task", "kraken-task", "project:app")
        self.mk_issue(9, "younger task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it")

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "clean claim-next exit")
        self.assertIn("claim-next: claimed issue=7 worker=w1", r.out.split("\n"),
                      "claim-next result line missing")
        self.assertIn("7\toldest task", r.out.split("\n"), "number+title line missing")
        self.assertIn("ship it", r.out, "issue body not emitted")
        self.assertTrue(self.has_label(7, "in-progress"), "in-progress not added to the claimed task")
        self.assertFalse(self.has_label(9, "in-progress"), "the younger task was wrongly claimed")
        self.assertEqual(self.comment_count(7), 1, "exactly one claim comment on the won task")
        self.assert_disclaimer(7, "w1")

        # --- 2. held-skip: #7 is now in-progress, claim-next moves to #9 -----
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w2")
        self.assertEqual(r.rc, 0, "claim-next skips held, claims the next candidate")
        self.assertIn("claim-next: claimed issue=9 worker=w2", r.out.split("\n"),
                      "expected #9 claimed after skipping held #7")
        self.assertTrue(self.has_label(9, "in-progress"), "#9 not claimed")

        # --- 3. honest empty: both held -> exit 3, nothing written -----------
        before = self.comment_count(7)
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w3")
        self.assertEqual(r.rc, 3, "claim-next exit on an empty queue")
        self.assertEqual(r.out, "claim-next: none project:app", "none machine line")
        self.assertEqual(self.comment_count(7), before, "no write on an empty queue")

        # --- 4. JSON mode: the win is a structured object (last line) --------
        self.mk_issue(12, "json task", "kraken-task", "project:jsonp")
        self.mk_body(12, "### Goal\njson body")
        r = self.kraken("claim-next", "OWNER/tasks", "jsonp", "w-json", "--json")
        self.assertEqual(r.rc, 0, "claim-next --json exit")
        payload = json.loads(r.out.split("\n")[-1])
        self.assertEqual(payload["issue"], 12, "claim-next --json issue wrong")
        self.assertEqual(payload["title"], "json task", "claim-next --json title wrong")

        # --- 5. THE race: two workers, two startable tasks, two claims -------
        self.mk_issue(20, "race oldest", "kraken-task", "project:race")
        self.mk_issue(22, "race younger", "kraken-task", "project:race")
        results = self.run_concurrent(
            [("claim-next", "OWNER/tasks", "race", "w-a"),
             ("claim-next", "OWNER/tasks", "race", "w-b")])
        self.assertEqual(results[0].rc, 0, "worker A won a task in the race")
        self.assertEqual(results[1].rc, 0, "worker B won a task in the race")

        def won_issue(out):
            m = re.search(r"^claim-next: claimed issue=(\d+)", out, re.M)
            return m.group(1) if m else None

        a_issue = won_issue(results[0].out_raw)
        b_issue = won_issue(results[1].out_raw)
        self.assertIsNotNone(a_issue, "worker A printed no claim-next win")
        self.assertIsNotNone(b_issue, "worker B printed no claim-next win")
        self.assertNotEqual(a_issue, b_issue, "both workers claimed the SAME task (#%s)" % a_issue)

        self.assertTrue(self.has_label(20, "in-progress"), "#20 was not claimed by either worker")
        self.assertTrue(self.has_label(22, "in-progress"), "#22 was not claimed by either worker")


if __name__ == "__main__":
    unittest.main()
