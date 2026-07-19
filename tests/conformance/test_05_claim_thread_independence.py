#!/usr/bin/env python3
"""Thread-history independence: under the ref CAS, the comment thread neither
blocks nor grants a claim — only the ref does. This replaces the retired
claim-window arbitration (whose reset markers existed to stop a dead worker's
old claim comment from winning forever): with the CAS there is no history to
interpret, so a thread full of stale claim/release/stale-claim markers is
inert, and a live ref alone holds the task."""
import unittest

from harness import KrakenConformanceTest


class ClaimThreadIndependenceTests(KrakenConformanceTest):
    def test_stale_thread_markers_never_block_a_claim(self):
        # A reaped thread: dead worker's claim + the reaper's stale-claim note.
        self.mk_issue(7, "reaped task", "kraken-task", "project:app")
        self.mk_comment(7, '<!-- kraken {"type":"claim","worker":"dead-worker"} -->')
        self.mk_comment(7, '<!-- kraken {"type":"stale-claim","reason":"no activity for 7h"} -->')

        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 0, "claim over a reaped thread")
        self.assertEqual(r.out, "claim: claimed issue=7 worker=w2", "w2 owns the task")

        # A bare claim marker with NO release after it — under the old window
        # this thread was owned forever; under the CAS it is just history.
        self.mk_issue(8, "abandoned thread", "kraken-task", "project:app")
        self.mk_comment(8, '<!-- kraken {"type":"claim","worker":"ghost"} -->')

        r = self.kraken("claim", "OWNER/tasks", 8, "w3")
        self.assertEqual(r.rc, 0, "claim over a threadful of stale markers")
        self.assertEqual(r.out, "claim: claimed issue=8 worker=w3", "w3 owns the task")

    def test_a_live_ref_alone_holds_the_task(self):
        # No labels, no comments — only the ref. The CAS answers 422 and the
        # loser writes nothing.
        self.mk_issue(9, "locked by ref only", "kraken-task", "project:app")
        self.mk_claim_ref(9, "rightful-owner")

        r = self.kraken("claim", "OWNER/tasks", 9, "w4")
        self.assertEqual(r.rc, 10, "claim against a live ref loses")
        self.assertEqual(r.out,
                         "claim: lost-cas issue=9 — another worker holds the claim ref",
                         "CAS loss line")
        self.assertEqual(self.comment_count(9), 0, "the CAS loser wrote a comment")
        self.assertFalse(self.has_label(9, "in-progress"),
                         "the CAS loser touched the labels")


if __name__ == "__main__":
    unittest.main()
