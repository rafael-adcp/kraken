#!/usr/bin/env python3
"""kraken.py claim guard: a held task is skipped with exit 11 and ZERO writes —
stacking in-progress on awaiting-merge is the corruption class the guard exists for."""
import re
import unittest

from harness import KrakenConformanceTest


class ClaimHeldTests(KrakenConformanceTest):
    def test_claim_held_is_skipped(self):
        n = 10
        for held in ("in-progress", "needs-decision", "awaiting-merge"):
            n += 1
            self.mk_issue(n, "held by %s" % held, "kraken-task", "project:app", held)

            r = self.kraken("claim", "OWNER/tasks", n, "w1")
            self.assertEqual(r.rc, 11, "claim on %s exit" % held)
            self.assertEqual(r.out, "claim: held issue=%d label=%s" % (n, held),
                             "machine line for %s" % held)
            self.assertEqual(self.comment_count(n), 0, "no comment written on %s" % held)
            wrote = any(re.search(r"issue (edit|comment) %d " % n, l) for l in self.log_lines())
            self.assertFalse(wrote, "guard wrote to issue %d despite %s" % (n, held))


if __name__ == "__main__":
    unittest.main()
