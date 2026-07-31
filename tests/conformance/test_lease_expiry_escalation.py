#!/usr/bin/env python3
"""The repeat-expiry guard (PROTOCOL.md §6, rule 2): a short TTL means an
abandoned task returns to the queue on its own — but a task that kills whatever
worker touches it (a poisoned environment, a step that always hangs) would then
circulate forever, burning a drain per round and telling nobody.

So the steal is counted. Each one increments `expiries` in the task's state
record (PROTOCOL.md §3.1), and once a task has expired LEASE_EXPIRY_ESCALATE
times it stops being stolen and becomes the operator's call instead. The count
rides the queue read the drain already performs, so the guard costs no request —
and it is EXACT, unlike the `lease-expired` marker count it replaces, which
undercounted as soon as the comment window scrolled past the oldest steals.
"""
import unittest

from harness import KrakenConformanceTest, expiry_marker
from kraken import LEASE_EXPIRY_ESCALATE


class LeaseExpiryEscalationTests(KrakenConformanceTest):
    def _poisoned(self, n, expiries):
        self.mk_issue(n, "kills every worker", "kraken-task", "project:app",
                      "in-progress")
        self.mk_expired_lease(n, "w-dead")
        # The audit trail each past steal left, and the count that now decides.
        for i in range(expiries):
            self.mk_comment(n, expiry_marker("w%d" % i))
        self.mk_state_record(n, "queued", worker="w-dead", expiries=expiries)

    def test_below_the_threshold_the_task_is_still_stolen(self):
        self._poisoned(1, LEASE_EXPIRY_ESCALATE - 1)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "the task was escalated too early: %s%s"
                         % (r.out, r.err))
        self.assertTrue(self.has_label(1, "in-progress"))
        self.assertFalse(self.has_label(1, "needs-decision"),
                         "escalated before the threshold")

    def test_at_the_threshold_the_task_goes_to_the_operator(self):
        self._poisoned(2, LEASE_EXPIRY_ESCALATE)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 3, "the drain kept stealing a task nobody finishes: %s%s"
                         % (r.out, r.err))
        self.assertTrue(self.has_label(2, "needs-decision"),
                        "a repeatedly expired task never reached the operator")
        self.assertFalse(self.has_label(2, "in-progress"))
        self.assertFalse(self.claim_ref_exists(2), "the lease was not released")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(2))
        self.assertIn(str(LEASE_EXPIRY_ESCALATE), self.last_comment(2),
                      "the reason does not say how many times it expired")
        self.assert_disclaimer(2, "w-drain")

    def test_a_live_lease_is_never_evicted_by_the_count(self):
        # The count is about a task nobody can finish, not about a busy worker:
        # whoever holds a LIVE lease is working, and the history of past steals
        # must not throw them off it.
        self.mk_issue(3, "expired a lot, but running now", "kraken-task",
                      "project:app", "in-progress")
        held = self.mk_claim_ref(3, "w-live", age_seconds=5)
        for i in range(LEASE_EXPIRY_ESCALATE + 2):
            self.mk_comment(3, expiry_marker("w%d" % i))
        self.mk_state_record(3, "queued", worker="w-live",
                             expiries=LEASE_EXPIRY_ESCALATE + 2)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 3, "a live lease was disturbed: %s%s" % (r.out, r.err))
        self.assertEqual(self.claim_ref(3), held, "the live lease was replaced")
        self.assertTrue(self.has_label(3, "in-progress"))
        self.assertFalse(self.has_label(3, "needs-decision"))

    def test_each_steal_increments_the_count_by_exactly_one(self):
        # The guard is only as honest as its counter: a steal that counted twice
        # (or not at all) would escalate early (or never). The record is written
        # by the steal itself, so this is the count end to end.
        self.mk_issue(4, "abandoned", "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(4, "w-dead")

        self.assertEqual(self.kraken("claim-next", "OWNER/tasks", "app", "w-1").rc, 0)
        self.assertEqual(self.state_record(4)["expiries"], 1,
                         "a steal did not increment the expiry count by one")
        markers = [b for b in self.comment_bodies(4)
                   if '"type":"lease-expired"' in b]
        self.assertEqual(len(markers), 1,
                         "a steal did not leave exactly one audit marker")

    def test_the_count_survives_a_window_a_marker_scan_would_lose(self):
        # The exactness protocol/9 bought: a long thread used to push the oldest
        # `lease-expired` markers out of the 25-comment window, so a task that
        # had burned five workers read as having burned one and kept circulating.
        self.mk_issue(5, "long thread, poisoned", "kraken-task", "project:app",
                      "in-progress")
        self.mk_expired_lease(5, "w-dead")
        for i in range(LEASE_EXPIRY_ESCALATE):
            self.mk_comment(5, expiry_marker("w%d" % i))
        for i in range(40):
            self.mk_comment(5, "chatter %d" % i)
        self.mk_state_record(5, "queued", worker="w-dead",
                             expiries=LEASE_EXPIRY_ESCALATE)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 3, "a buried expiry history was stolen again: %s%s"
                         % (r.out, r.err))
        self.assertTrue(self.has_label(5, "needs-decision"))


if __name__ == "__main__":
    unittest.main()
