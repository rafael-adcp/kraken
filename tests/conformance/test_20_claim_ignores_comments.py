#!/usr/bin/env python3
"""The claim path never reads the comment thread. Under protocol/3 arbitration
had to paginate the whole thread (this test's ancestor pinned reading past
comment 100); the ref CAS retires that entirely — ownership is the ref, so a
150-comment thread costs the claim exactly zero comment reads. Drives the real
kraken.py claim through the gh-stub and asserts on the stub's call log."""
import unittest

from harness import KrakenConformanceTest


class ClaimIgnoresCommentsTests(KrakenConformanceTest):
    def test_claim_reads_no_comments_regardless_of_thread_length(self):
        self.mk_issue(50, "long-lived task", "kraken-task", "project:app")
        # A 150-comment thread, including stale claim/reset markers that used to
        # drive arbitration — all inert now.
        self.mk_comment(50, '<!-- kraken {"type":"claim","worker":"dead-worker"} -->')
        for i in range(1, 149):
            self.mk_comment(50, "noise %d" % i)
        self.mk_comment(50, '<!-- kraken {"type":"stale-claim","reason":"no activity for 7h"} -->')

        self.truncate_log()
        r = self.kraken("claim", "OWNER/tasks", 50, "w1")
        self.assertEqual(r.rc, 0, "claim succeeds regardless of the thread")
        self.assertEqual(r.out, "claim: claimed issue=50 worker=w1", "machine line")
        self.assertTrue(self.claim_ref_exists(50), "claim ref missing after the claim")

        # The whole point: not one comment read on the claim path.
        read_comments = [l for l in self.log_lines() if "issues/50/comments" in l]
        self.assertEqual(read_comments, [],
                         "claim read the comment thread: %s" % read_comments)


if __name__ == "__main__":
    unittest.main()
