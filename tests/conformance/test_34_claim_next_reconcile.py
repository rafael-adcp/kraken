#!/usr/bin/env python3
"""protocol/5's §6 move: the READER reconciles. A drain repairs the queue on the
read it was going to perform anyway — no cron, no server-side job — and the cost
of doing so is O(1) in the number of live claims, not O(N).

Two things are under test, both against the real gh stub:

  1. **The four rules fire from the claim path.** `claim-next` alone (never
     `reap`) reclaims a dead worker's claim, drops an orphan lock, heals a
     crashed projection and requeues an orphan label — and then claims out of the
     state it just repaired, in the SAME invocation.
  2. **The reconcile costs one batched read.** Growing the number of live claim
     refs from 1 to 20 must not grow the call count: the ref dates resolve
     through a single aliased GraphQL query, the way `resolve_depends_on` does.
"""
import unittest

from harness import KrakenConformanceTest, KRAKEN


class ClaimNextReconcileTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        # The drain still performs the drift handshake before its first claim;
        # seed a matching vendored copy so the reconcile is what is under test.
        self.mk_content(".github/kraken.py", KRAKEN)

    def test_the_claim_path_runs_the_four_rules(self):
        # #1 DEAD — an 8h-old claim ref. Rule 2: reclaim to needs-decision.
        self.mk_issue(1, "dead worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(1, "dead-worker", age_hours=8)

        # #2 ALIVE — heartbeated just now. Untouched by every rule.
        self.mk_issue(2, "live worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "live-worker", age_hours=0, mtype="heartbeat",
                          msg="still going")

        # #3 ORPHAN PROJECTION — in-progress label, no ref. Rule 4: requeue.
        self.mk_issue(3, "crashed release", "kraken-task", "project:app", "in-progress")

        # #4 ORPHAN LOCK — a ref left on an escalated issue. Rule 1: drop it.
        self.mk_issue(4, "escalated, ref lingered", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_claim_ref(4, "gone-worker", age_hours=1)

        # #5 HEAL — a fresh ref whose in-progress projection never landed. Rule 3.
        self.mk_issue(5, "projection crashed", "kraken-task", "project:app")
        self.mk_claim_ref(5, "healthy-worker", age_hours=0)

        # #6 ORPHAN LOCK ON CLOSED — the issue is gone from the open walk, so the
        #    rule needs no per-ref issue read to spot it.
        self.mk_issue(6, "closed, ref lingered", "kraken-task", "project:app")
        self.set_issue_state(6, "closed")
        self.mk_claim_ref(6, "old-worker", age_hours=2)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "the drain should have claimed #3: %s" % r.err)

        # Rule 2 — #1 reclaimed for the operator, its lock released.
        self.assertTrue(self.has_label(1, "needs-decision"), "#1 not reclaimed")
        self.assertFalse(self.has_label(1, "in-progress"), "#1 still in-progress")
        self.assertFalse(self.claim_ref_exists(1), "#1 lock not released")
        self.assertIn('<!-- kraken {"type":"stale-claim"', self.last_comment(1),
                      "#1 has no stale-claim marker on record")
        # A worker posts the reclaim now, so §4 attribution applies to it.
        self.assert_disclaimer(1, "w-drain")

        # #2 — a live claim is never disturbed.
        self.assertTrue(self.has_label(2, "in-progress"), "#2 was reclaimed while alive")
        self.assertTrue(self.claim_ref_exists(2), "#2 lost its lock while alive")
        self.assertEqual(self.comment_count(2), 0, "#2 got a spurious comment")

        # Rule 4 — #3's orphan label removed... and then claimed by this very
        # drain, out of the state it just repaired, with no second queue fetch.
        self.assertTrue(self.claim_ref_exists(3), "#3 was not claimed after requeue")
        self.assertTrue(self.has_label(3, "in-progress"), "#3 not re-projected")
        self.assertIn("claim-next: claimed issue=3 worker=w-drain", r.out.split("\n"),
                      "the drain did not claim the task it had just requeued")

        # Rule 1 — both orphan locks dropped, nothing else touched.
        self.assertFalse(self.claim_ref_exists(4), "#4 orphan lock survived")
        self.assertTrue(self.has_label(4, "needs-decision"), "#4 lost its label")
        self.assertEqual(self.comment_count(4), 0, "#4 got a spurious comment")
        self.assertFalse(self.claim_ref_exists(6), "#6 orphan lock survived")

        # Rule 3 — #5's projection healed, its lock intact.
        self.assertTrue(self.has_label(5, "in-progress"), "#5 not healed")
        self.assertTrue(self.claim_ref_exists(5), "#5 lost its live lock")
        self.assertEqual(self.comment_count(5), 0, "#5 got a spurious comment")

    def test_an_agreeing_queue_costs_zero_writes(self):
        """The overwhelmingly common case: nothing to repair. The reconcile must
        then be pure read — a drain that rewrites labels it agrees with would
        cost a write per claim on every single poll."""
        self.mk_issue(1, "running", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(1, "live-worker", age_hours=0)
        self.mk_issue(2, "queued", "kraken-task", "project:app")

        self.truncate_log()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "claim-next exit")

        # Not one write touched the task the reconcile agreed with. (#2, the task
        # this drain went on to claim, is written to — that is the claim, not the
        # reconcile.)
        for line in self.log_lines():
            self.assertNotIn("/issues/1/", line,
                             "a reconcile with nothing to repair wrote: %s" % line)
            self.assertNotIn("claims/1", line,
                             "a reconcile with nothing to repair touched the lock: %s" % line)
        self.assertTrue(self.claim_ref_exists(1), "the live claim was disturbed")
        self.assertEqual(self.comment_count(1), 0, "the live claim got a comment")

    def test_reconcile_cost_is_flat_in_the_number_of_claims(self):
        """The load-bearing claim of protocol/5: reconciling on the read costs a
        FIXED number of round trips. One live claim and twenty must cost the
        same — the ref dates resolve through one aliased GraphQL query."""
        def cost(first, last):
            for n in range(first, last + 1):
                self.mk_issue(n, "running %d" % n, "kraken-task", "project:app",
                              "in-progress")
                self.mk_claim_ref(n, "live-worker", age_hours=0)
            self.truncate_log()
            r = self.kraken("claim-next", "OWNER/tasks", "app", "w-probe")
            # Nothing is startable (every task is held by a live claim), which is
            # the honest empty result — and the cheapest shape to measure.
            self.assertEqual(r.rc, 3, "expected an empty queue: %s%s" % (r.out, r.err))
            return len(self.log_lines())

        one = cost(1, 1)
        twenty = cost(2, 20)
        self.assertEqual(one, twenty,
                         "the reconcile is O(N) in live claims: %d call(s) for 1 "
                         "claim, %d for 20 — %s" % (one, twenty, self.log_text()))

    def test_no_claim_refs_means_no_commit_read_at_all(self):
        """With no lock live there is nothing to reconcile, so the batched
        commit-date read must not be issued — the reconcile is free on an idle
        queue, not merely cheap."""
        self.mk_issue(1, "queued", "kraken-task", "project:app")
        self.truncate_log()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "claim-next exit")
        self.assertNotIn("object(oid:", self.log_text(),
                         "a commit-date read was issued with no claim ref live")


if __name__ == "__main__":
    unittest.main()
