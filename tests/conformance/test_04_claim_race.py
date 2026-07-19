#!/usr/bin/env python3
"""THE claim-race test: two workers claim the same issue concurrently, both
passing the label guard (the stub's barrier holds each one at the guard read
until both have arrived — the worst-case interleaving, deterministically).
The invariant under test: the ref CAS admits EXACTLY one winner — one exits 0,
the other exits 10 having written NOTHING (no comment, no label change) — and
the surviving ref names the winner."""
import json
import os
import unittest

from harness import KrakenConformanceTest


class ClaimRaceTests(KrakenConformanceTest):
    def test_claim_race_exactly_one_winner(self):
        self.mk_issue(7, "contested task", "kraken-task", "project:app")

        results = self.run_concurrent(
            [("claim", "OWNER/tasks", 7, "w-a"), ("claim", "OWNER/tasks", 7, "w-b")],
            env={"GH_STUB_BARRIER": "2"},
        )
        rc_a, rc_b = results[0].rc, results[1].rc

        # Exactly one winner, one CAS loss — never zero, never two.
        self.assertEqual(sorted([rc_a, rc_b]), [0, 10],
                         "race exit codes (exactly one 0 and one 10)")
        winner = "w-a" if rc_a == 0 else "w-b"

        # The surviving ref names the winner.
        sha = self.claim_ref(7)
        self.assertIsNotNone(sha, "claim ref missing after the race")
        with open(os.path.join(self.state, "objects", sha + ".json"),
                  encoding="utf-8") as f:
            commit = json.load(f)
        self.assertIn('"worker":"%s"' % winner, commit["message"],
                      "the surviving ref must carry the winner's claim commit")

        # The loser wrote NOTHING: only the winner's claim comment exists, and
        # nobody removed a label while backing off.
        self.assertTrue(self.has_label(7, "in-progress"), "in-progress label missing after race")
        self.assertEqual(self.comment_count(7), 1,
                         "exactly one claim comment (the CAS loser writes nothing)")
        self.assertIn('"worker":"%s"' % winner, self.last_comment(7),
                      "the one claim comment must be the winner's")
        self.assertFalse(any("remove-label" in l for l in self.log_lines()),
                         "a racer removed a label while backing off")


if __name__ == "__main__":
    unittest.main()
