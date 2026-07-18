#!/usr/bin/env python3
"""THE claim-race test: two workers claim the same issue concurrently, both
passing the label guard (the stub's barrier holds each one at the guard read
until both have arrived — the worst-case interleaving, deterministically).
The invariant under test: EXACTLY one exits 0, the other exits 10 and removes
nothing, and the winner is whoever's claim marker landed first."""
import glob
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

        # Exactly one winner, one back-off — never zero, never two.
        self.assertEqual(sorted([rc_a, rc_b]), [0, 10],
                         "race exit codes (exactly one 0 and one 10)")

        # The winner is whoever's claim marker landed first in server order.
        first = ""
        for path in sorted(glob.glob(os.path.join(self.issue_dir(7), "comments", "*.md"))):
            with open(path, encoding="utf-8") as f:
                body = f.read()
            if '<!-- kraken {"type":"claim"' in body:
                first = body
                break
        winner = "w-a" if rc_a == 0 else "w-b"
        self.assertIn('"worker":"%s"' % winner, first,
                      "winner (%s) must match the first claim marker in server order" % winner)

        # The loser backed off without removing anything.
        self.assertTrue(self.has_label(7, "in-progress"), "in-progress label missing after race")
        self.assertEqual(self.comment_count(7), 2, "both claim comments preserved")
        self.assertFalse(any("remove-label" in l for l in self.log_lines()),
                         "a racer removed a label while backing off")


if __name__ == "__main__":
    unittest.main()
