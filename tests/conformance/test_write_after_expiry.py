#!/usr/bin/env python3
"""The resurrected worker (PROTOCOL.md §5): a worker goes silent long enough for
its lease to expire, another worker steals the task, and then the first one comes
back — a suspended laptop, a rate-limit wait, a build that took an hour.

It MUST discover it lost the lease BEFORE writing anything. A delivery, an
escalation or a release written onto a task somebody else is now executing is
worse than none: it lies to the review queue, or deletes the live holder's lease
out from under it.

Every check answers by EXIT CODE — 10, lost — never by stderr text (#53). The
work is not lost either: the branch and the PR exist, and re-claiming the task
delivers them honestly.
"""
import os
import unittest

from harness import KrakenConformanceTest


class WriteAfterExpiryTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.result = os.path.join(self.state, "result.md")
        with open(self.result, "w", encoding="utf-8") as f:
            f.write("Did the work, opened the PR.\n")

    def _stolen(self, n, title):
        """Seed the aftermath of a steal: w-dead's lease expired and w-live now
        holds the task."""
        self.mk_issue(n, title, "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(n, "w-dead")
        r = self.kraken("claim", "acme/tasks", n, "w-live")
        self.assertEqual(r.rc, 0, "the steal that sets the scene failed: %s%s"
                         % (r.out, r.err))
        return self.claim_ref(n)

    def test_deliver_after_the_lease_was_stolen_writes_nothing(self):
        theirs = self._stolen(1, "taken over")
        before = self.comment_count(1)

        r = self.kraken("deliver", "acme/tasks", 1, "w-dead", self.result,
                        "https://github.com/acme/work/pull/9")
        self.assertEqual(r.rc, 10, "a stolen-from worker delivered anyway")
        self.assertIn("lost-lease issue=1", r.out)

        self.assertEqual(self.comment_count(1), before,
                         "the zombie's result landed on the thread")
        self.assertFalse(self.has_label(1, "awaiting-merge"),
                         "the zombie moved the task into the review queue")
        self.assertTrue(self.has_label(1, "in-progress"),
                        "the live holder lost its projection")
        self.assertEqual(self.claim_ref(1), theirs,
                         "the zombie deleted the live holder's lease")

    def test_escalate_after_the_lease_was_stolen_writes_nothing(self):
        theirs = self._stolen(2, "taken over")
        before = self.comment_count(2)

        r = self.kraken("escalate", "acme/tasks", 2, "w-dead", self.result)
        self.assertEqual(r.rc, 10, "a stolen-from worker escalated anyway")
        self.assertEqual(self.comment_count(2), before,
                         "the zombie put a question on the thread")
        self.assertFalse(self.has_label(2, "needs-decision"),
                         "the zombie sent a live task to the operator")
        self.assertEqual(self.claim_ref(2), theirs, "the zombie freed the lease")

    def test_release_after_the_lease_was_stolen_frees_nothing(self):
        # The nastiest one: release DELETES the ref, so a zombie release would
        # hand a task another worker is actively executing back to the queue.
        theirs = self._stolen(3, "taken over")
        before = self.comment_count(3)

        r = self.kraken("release", "acme/tasks", 3, "w-dead", "giving up")
        self.assertEqual(r.rc, 10, "a stolen-from worker released anyway")
        self.assertEqual(self.comment_count(3), before,
                         "the zombie posted a released marker")
        self.assertTrue(self.has_label(3, "in-progress"),
                        "the zombie stripped the live holder's label")
        self.assertEqual(self.claim_ref(3), theirs,
                         "the zombie deleted the live holder's lease")

    def test_a_refused_release_still_clears_the_local_claim_state(self):
        # The lifecycle hooks and the ambush loop retry `release` off this file.
        # Leaving it behind would make them re-attempt forever a release that is
        # no longer their business.
        self._stolen(4, "taken over")
        state = self.claim_state_file("w-dead")
        os.makedirs(os.path.dirname(state), exist_ok=True)
        with open(state, "w", encoding="utf-8") as f:
            f.write('{"repo": "acme/tasks", "issue": "4", "worker": "w-dead"}\n')

        r = self.kraken("release", "acme/tasks", 4, "w-dead", "session ended")
        self.assertEqual(r.rc, 10)
        self.assertFalse(os.path.exists(state),
                         "a refused release left its claim state behind")

    def test_the_holder_may_still_deliver_under_an_expired_lease(self):
        # Expired but NOT stolen: the lease is still this worker's, and
        # finishing the transition is what frees it honestly. Refusing here
        # would strand finished work behind a clock nobody else has read yet.
        self.mk_issue(5, "slow but alive", "kraken-task", "project:app",
                      "in-progress")
        self.mk_expired_lease(5, "w-slow")

        r = self.kraken("deliver", "acme/tasks", 5, "w-slow", self.result,
                        "https://github.com/acme/work/pull/10")
        self.assertEqual(r.rc, 0, "the lease holder was refused its own delivery: %s%s"
                         % (r.out, r.err))
        self.assertTrue(self.has_label(5, "awaiting-merge"))
        self.assertFalse(self.claim_ref_exists(5), "delivery did not free the lease")


if __name__ == "__main__":
    unittest.main()
