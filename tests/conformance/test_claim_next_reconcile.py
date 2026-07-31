#!/usr/bin/env python3
"""protocol/5's §6 move: the READER reconciles. A drain repairs the queue on the
read it was going to perform anyway — no cron, no server-side job — and the cost
of doing so is O(1) in the number of live claims, not O(N).

Three things are under test, all against the real gh stub:

  1. **Both rules fire from the claim path.** `claim-next` alone (never `reap`)
     reclaims a dead worker's claim and drops an orphan lock — and then claims
     out of the state it just repaired, in the SAME invocation. Nothing it does
     involves the `in-progress` label: under protocol/7 that label is write-only,
     so a task wearing a stale one is simply startable and a live lease wearing
     none is simply held.
  2. **The reconcile costs one batched read.** Growing the number of live claim
     refs from 1 to 20 must not grow the call count: the ref dates resolve
     through a single aliased GraphQL query, the way `resolve_depends_on` does.
  3. **The drain got no more expensive.** The whole point of moving the reconcile
     onto the reader is that the queue read was already being paid for. The added
     commit-date read is covered by the drift handshake's contents read, which
     protocol/5 retired along with the vendoring — so the call count per drain is
     no higher than protocol/4's, and lower on an idle queue.
"""
import re
import unittest

from harness import KrakenConformanceTest, expiry_marker
from kraken import LEASE_EXPIRY_ESCALATE


class ClaimNextReconcileTests(KrakenConformanceTest):
    def test_the_claim_path_runs_the_reconcile_rules(self):
        # #1 UNFINISHABLE — an expired lease on a task already stolen
        # LEASE_EXPIRY_ESCALATE times. Rule 2: reclaim to needs-decision. (A
        # FIRST expiry is not a rule at all under protocol/6 — it is stolen, see
        # test_an_expired_lease_is_stolen_not_escalated.)
        self.mk_issue(1, "nobody can finish this", "kraken-task", "project:app",
                      "in-progress")
        self.mk_expired_lease(1, "dead-worker")
        for i in range(LEASE_EXPIRY_ESCALATE):
            self.mk_comment(1, expiry_marker("w%d" % i))
        # The count the guard acts on is the record's, not the thread's (§3.1).
        self.mk_state_record(1, "queued", worker="dead-worker",
                             expiries=LEASE_EXPIRY_ESCALATE)

        # #2 ALIVE — renewed just now. Untouched by every rule.
        self.mk_issue(2, "live worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "live-worker", age_seconds=5, mtype="heartbeat",
                          msg="still going")

        # #3 STALE BADGE — in-progress label, no ref (a crashed release). No rule
        #    fires: the label holds nothing, so the task is already startable and
        #    this drain claims it outright.
        self.mk_issue(3, "crashed release", "kraken-task", "project:app", "in-progress")

        # #4 ORPHAN LOCK — a ref left on an escalated issue. Rule 1: drop it.
        # "Escalated" is the RECORD's word now, not the label's (§3.1).
        self.mk_issue(4, "escalated, ref lingered", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_state_record(4, "needs-decision", worker="gone-worker")
        self.mk_claim_ref(4, "gone-worker", age_seconds=60)

        # #5 MISSING BADGE — a fresh ref whose in-progress projection never
        #    landed. No rule fires either: the LEASE holds the task, badge or no
        #    badge, so it is not offered and not repaired.
        self.mk_issue(5, "projection crashed", "kraken-task", "project:app")
        self.mk_claim_ref(5, "healthy-worker", age_seconds=5)

        # #6 ORPHAN LOCK ON CLOSED — the issue is gone from the open walk, so the
        #    rule needs no per-ref issue read to spot it.
        self.mk_issue(6, "closed, ref lingered", "kraken-task", "project:app")
        self.set_issue_state(6, "closed")
        self.mk_claim_ref(6, "old-worker", age_seconds=120)

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

        # #3 — claimed outright. protocol/6 spent a label write and a comment to
        # "requeue" it first; protocol/7 skips straight to the claim, and the
        # badge it already wore is simply re-asserted by the projection.
        self.assertTrue(self.claim_ref_exists(3), "#3 was not claimed")
        self.assertTrue(self.has_label(3, "in-progress"), "#3 not projected")
        self.assertIn("claim-next: claimed issue=3 worker=w-drain", r.out.split("\n"),
                      "the drain did not claim the task wearing a stale badge")
        self.assertNotIn('<!-- kraken {"type":"stale-claim"',
                         "\n".join(self.comment_bodies(3)),
                         "#3 got a requeue note for a label that held nothing")

        # Rule 1 — both orphan locks dropped, nothing else touched.
        self.assertFalse(self.claim_ref_exists(4), "#4 orphan lock survived")
        self.assertTrue(self.has_label(4, "needs-decision"), "#4 lost its label")
        self.assertEqual(self.comment_count(4), 0, "#4 got a spurious comment")
        self.assertFalse(self.claim_ref_exists(6), "#6 orphan lock survived")

        # #5 — left exactly as found: no badge written, lock intact, no comment.
        self.assertFalse(self.has_label(5, "in-progress"), "#5 was healed")
        self.assertTrue(self.claim_ref_exists(5), "#5 lost its live lock")
        self.assertEqual(self.comment_count(5), 0, "#5 got a spurious comment")

    def test_an_expired_lease_is_stolen_not_escalated(self):
        """The protocol/6 recovery path, end to end and with nothing installed:
        a worker dies holding a lease, and the very next drain takes the task
        over — no cron, no hook, no operator, and no needs-decision detour."""
        self.mk_issue(1, "abandoned mid-flight", "kraken-task", "project:app",
                      "in-progress")
        dead = self.mk_expired_lease(1, "dead-worker")

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "the drain should have stolen #1: %s%s"
                         % (r.out, r.err))

        # The lease changed hands: a NEW ref, this worker's, and the label the
        # dead worker left behind now projects the new lease.
        self.assertTrue(self.claim_ref_exists(1), "#1 left with no lease at all")
        self.assertNotEqual(self.claim_ref(1), dead, "#1 kept the dead lease")
        self.assertTrue(self.has_label(1, "in-progress"), "#1 lost its projection")
        self.assertFalse(self.has_label(1, "needs-decision"),
                         "a first expiry must not reach the operator")

        # The steal is on the record, naming both workers — the audit trail, and
        # what §6's repeat guard counts.
        bodies = "\n".join(self.comment_bodies(1))
        self.assertIn('"type":"lease-expired"', bodies, "no lease-expired marker")
        self.assertIn("dead-worker", bodies, "the steal does not name the old holder")
        self.assert_disclaimer(1, "w-drain")

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

    def _preamble(self):
        """The requests a drain makes BEFORE it starts guarding a candidate — its
        preflight and queue read. That is the phase the reconcile changed; the
        guard/CAS/projection that follows is identical to protocol/4's and is
        pinned elsewhere. The guard's single-issue GET is the boundary."""
        out = []
        for line in self.log_lines():
            if re.match(r"GET /repos/[^/]+/[^/]+/issues/[0-9]+$", line):
                break
            out.append(line)
        return out

    def test_a_drain_costs_no_more_than_it_did_before(self):
        """The issue's acceptance: count the API calls a drain makes, before and
        after. It is a net-zero SWAP, not a literal zero — one read out, one read
        in — so this pins the budget per case rather than asserting a zero the
        code does not achieve:

            case                        protocol/4   protocol/5
            idle queue, no live claim       4            3
            queue with live claims          4            4

        protocol/4's preamble: the handshake contents GET, the project-label
        read, the GraphQL queue walk, the matching-refs read. protocol/5's: the
        project-label read, the GraphQL queue walk, the matching-refs read, and —
        only when a lock is live — one batched commit-date read.
        """
        self.mk_issue(1, "queued", "kraken-task", "project:app")

        self.truncate_log()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "claim-next exit: %s%s" % (r.out, r.err))
        idle = self._preamble()
        self.assertEqual(len(idle), 3,
                         "an idle drain should cost 3 reads (protocol/4 spent 4), "
                         "got %d: %s" % (len(idle), "\n".join(idle)))
        self.assertFalse([l for l in idle if "object(oid:" in l or "graphql" in l][1:],
                         "more than one GraphQL call in an idle preamble: %s"
                         % "\n".join(idle))

        # With a live claim the commit-date read appears — landing exactly on
        # protocol/4's budget, because the handshake read is gone.
        self.mk_issue(2, "running", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "live-worker", age_hours=0)
        self.mk_issue(3, "queued", "kraken-task", "project:app")

        self.truncate_log()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w2")
        self.assertEqual(r.rc, 0, "claim-next exit: %s%s" % (r.out, r.err))
        live = self._preamble()
        self.assertEqual(len(live), 4,
                         "a drain over live claims should cost 4 reads — exactly "
                         "protocol/4's budget — got %d: %s"
                         % (len(live), "\n".join(live)))
        self.assertEqual(len(live) - len(idle), 1,
                         "the reconcile's marginal cost is not exactly one read")

    def test_the_drain_no_longer_reads_the_vendored_program(self):
        """The handshake is gone with the vendoring: a drain must not read
        .github/kraken.py at all, and must not refuse when it is absent."""
        self.mk_issue(1, "queued", "kraken-task", "project:app")
        self.truncate_log()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0,
                         "a drain refused against a repo with no vendored copy: %s%s"
                         % (r.out, r.err))
        self.assertNotIn("contents/.github/kraken.py", self.log_text(),
                         "the drain still reads the retired vendored program")

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
