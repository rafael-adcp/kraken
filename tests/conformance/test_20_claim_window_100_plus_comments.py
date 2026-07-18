#!/usr/bin/env python3
"""Claim-window arbitration must see the WHOLE comment thread, not just the
first page. Drives the real kraken.py claim through the gh-stub with a
>100-comment thread whose reset and true winner live past comment 100."""
import unittest

from harness import KrakenConformanceTest


class ClaimWindow100PlusCommentsTests(KrakenConformanceTest):
    def test_arbitration_reads_past_comment_100(self):
        self.mk_issue(50, "long-lived task", "kraken-task", "project:app")

        # comment #1: a worker claims it, then goes dark.
        self.mk_comment(50, '<!-- kraken {"type":"claim","worker":"dead-worker"} -->')
        # comments #2-#100: 99 comments of unrelated chatter.
        for i in range(1, 100):
            self.mk_comment(50, "noise %d" % i)
        # comment #101 (past the boundary): the reaper clears the dead claim.
        self.mk_comment(50, '<!-- kraken {"type":"stale-claim","reason":"no activity for 7h"} -->')
        # comment #102 (past the boundary): a second worker wins the fresh window.
        self.mk_comment(50, '<!-- kraken {"type":"claim","worker":"heir"} -->')

        r = self.kraken("claim", "OWNER/tasks", 50, "challenger")
        self.assertEqual(r.rc, 10, "challenger loses to the winner past the 100-comment boundary")
        self.assertEqual(r.out, "claim: lost-tiebreaker issue=50 winner=heir",
                         "arbitration reads past comment 100 — heir wins, not the falsely-still-claimed dead-worker")


if __name__ == "__main__":
    unittest.main()
