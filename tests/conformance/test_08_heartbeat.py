#!/usr/bin/env python3
"""kraken.py heartbeat — the LEASE RENEWAL (PROTOCOL.md §5/§6): force-move the
claim ref to a fresh commit carrying the progress text, which restarts the TTL.
NO timeline comment, no label change, and the task stays locked afterwards (a
worker renewing must never free its own lease). Renewing a lease that is no
longer this worker's is exit 10, and it is the cheapest place a live worker
finds out it was stolen from."""
import json
import os
import re
import unittest

from harness import KrakenConformanceTest, lease_ttl


class HeartbeatTests(KrakenConformanceTest):
    def test_heartbeat_advances_the_ref_and_posts_nothing(self):
        self.mk_issue(7, "long task", "kraken-task", "project:app", "in-progress")
        old_sha = self.mk_claim_ref(7, "w1", age_seconds=120)

        r = self.kraken("heartbeat", "OWNER/tasks", 7, "w1", "tests green, writing docs")
        self.assertEqual(r.rc, 0, "heartbeat exit")
        self.assertEqual(r.out, "heartbeat: renewed issue=7 worker=w1", "machine line")

        # The ref moved to a fresh commit carrying the marker + progress text.
        new_sha = self.claim_ref(7)
        self.assertIsNotNone(new_sha, "heartbeat deleted the claim ref")
        self.assertNotEqual(new_sha, old_sha, "heartbeat did not advance the ref")
        with open(os.path.join(self.state, "objects", new_sha + ".json"),
                  encoding="utf-8") as f:
            commit = json.load(f)
        self.assertIn('"type":"heartbeat"', commit["message"], "heartbeat marker type")
        self.assertIn('"worker":"w1"', commit["message"], "heartbeat marker worker")
        self.assertIn("tests green, writing docs", commit["message"],
                      "progress message missing from the heartbeat commit")

        # No timeline noise, no label change.
        self.assertEqual(self.comment_count(7), 0, "heartbeat posted a comment")
        self.assertTrue(self.has_label(7, "in-progress"), "heartbeat touched the labels")
        self.assertFalse(any(re.search(r"/issues/\d+/labels", l) for l in self.log_lines()),
                         "heartbeat ran a label edit")

        # Lock invariant: the task is still held after its own renewal, even
        # if the label projection is lost out of band.
        self.set_labels(7, ["kraken-task", "project:app"])
        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 10, "claim against a renewed lease")
        self.assertEqual(r.out,
                         "claim: lost-cas issue=7 — another worker holds the claim ref",
                         "the renewal freed its own lease")

    def test_renewal_restarts_the_ttl(self):
        # The renewal's whole job: a lease one second from expiry holds for a
        # full TTL again afterwards, so a live worker is never stolen from.
        self.mk_issue(8, "long task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(8, "w1", age_seconds=lease_ttl() - 1)

        self.assertEqual(self.kraken("heartbeat", "OWNER/tasks", 8, "w1", "still going").rc, 0)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w2")
        self.assertEqual(r.rc, 3, "a renewed lease must not be stealable: %s%s"
                         % (r.out, r.err))

    def test_renewing_a_stolen_lease_is_a_loss(self):
        # The resurrected worker's earliest signal. It must not renew a lease
        # that now belongs to somebody else — that would evict the live holder.
        self.mk_issue(9, "taken over", "kraken-task", "project:app", "in-progress")
        theirs = self.mk_claim_ref(9, "w2", age_seconds=10)

        r = self.kraken("heartbeat", "OWNER/tasks", 9, "w1", "back from the dead")
        self.assertEqual(r.rc, 10, "renewing another worker's lease must be a loss")
        self.assertIn("lost-lease issue=9", r.out)
        self.assertEqual(self.claim_ref(9), theirs,
                         "the renewal moved the new holder's lease")

    def test_renewing_a_deleted_lease_is_a_loss(self):
        self.mk_issue(11, "released", "kraken-task", "project:app")
        r = self.kraken("heartbeat", "OWNER/tasks", 11, "w1", "still going")
        self.assertEqual(r.rc, 10, "renewing an absent lease must be a loss")
        self.assertFalse(self.claim_ref_exists(11),
                         "the renewal re-created a lease nobody holds")


if __name__ == "__main__":
    unittest.main()
