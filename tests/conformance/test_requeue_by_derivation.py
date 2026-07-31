#!/usr/bin/env python3
"""Requeue by derivation (PROTOCOL.md §6). Through protocol/4 an operator reply
MUTATED the queue: a workflow watched the comment stream and removed the holding
label. protocol/5 reads the same fact off the thread instead — "an operator
comment newer than the last worker comment" — so there is no workflow, no label
write, and no window in which the queue disagrees with the thread.

Every case here therefore asserts on `list-startable` (what the queue says) and
on `claim-next` (that a worker can actually take the task back), never on a label
having been rewritten by something.
"""
import json
import unittest

from harness import KrakenConformanceTest


class RequeueByDerivationTests(KrakenConformanceTest):
    def startable(self):
        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "list-startable exit: %s" % r.err)
        return {int(l.split("\t")[0]) for l in r.out.split("\n") if l.strip()}

    def worker_comment(self, n, mtype="needs-decision"):
        """A worker comment the way kraken.py composes one — disclaimer + marker."""
        self.mk_comment(n, "%s\n\nwhich option?\n\n%s" % (
            self.disclaimer_line("w1"),
            '<!-- kraken {"type":"%s","worker":"w1"} -->' % mtype))

    # --- needs-decision: any bare operator reply requeues ---------------------

    def test_bare_reply_requeues_needs_decision(self):
        self.mk_issue(1, "answered", "kraken-task", "project:app", "needs-decision")
        self.worker_comment(1)
        self.assertNotIn(1, self.startable(), "held before the operator answered")

        self.mk_comment(1, "option B, go")
        self.assertIn(1, self.startable(), "an answered task did not rejoin the queue")

    def test_an_unanswered_escalation_stays_held(self):
        self.mk_issue(2, "unanswered", "kraken-task", "project:app", "needs-decision")
        self.worker_comment(2)
        self.assertNotIn(2, self.startable())

    def test_a_worker_comment_after_the_reply_re_holds(self):
        """The operator answered, a worker took it and escalated AGAIN: the newest
        worker comment outranks the older reply. Self-correcting, no state."""
        self.mk_issue(3, "re-escalated", "kraken-task", "project:app", "needs-decision")
        self.worker_comment(3)
        self.mk_comment(3, "option B, go")
        self.assertIn(3, self.startable())
        self.worker_comment(3)
        self.assertNotIn(3, self.startable(), "a re-escalated task stayed startable")

    def test_kraken_authored_comments_never_requeue(self):
        """The reconciler's own stale-claim note must not instantly undo the
        escalation it just posted. protocol/4 needed a `user.type == Bot` gate for
        that; the marker makes it structural, so the derivation needs no author
        metadata in the query at all."""
        self.mk_issue(4, "reclaimed by the reconcile", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_comment(4, "%s\n\nThe worker has gone silent.\n\n%s" % (
            self.disclaimer_line("w-drain"),
            '<!-- kraken {"type":"stale-claim","reason":"no worker heartbeat for 8h"} -->'))
        self.assertNotIn(4, self.startable(),
                         "the reconciler's own comment requeued the task it just held")

    def test_operator_quoting_the_disclaimer_mid_body_still_requeues(self):
        self.mk_issue(5, "quoted disclaimer", "kraken-task", "project:app",
                      "needs-decision")
        self.worker_comment(5)
        self.mk_comment(5, "answering below:\n\n%s\n\noption B, go"
                        % self.disclaimer_line("w1"))
        self.assertIn(5, self.startable(),
                      "an operator reply quoting the disclaimer was misread as a worker's")

    # --- awaiting-merge: the SAME rule, the same gesture (protocol/8) ---------

    def test_awaiting_merge_requeues_on_a_bare_comment(self):
        """protocol/8's load-bearing case, and the one that motivated it: a review
        comment asking for follow-up work put the task back in the queue. Through
        protocol/7 this thread stayed held — the ask was read, matched against a
        directive that was not there, and discarded, leaving the operator watching
        a queue that never moved."""
        self.mk_issue(6, "delivered", "kraken-task", "project:app", "awaiting-merge")
        self.worker_comment(6, mtype="delivered")
        self.mk_comment(6, "please also commit it to config/app.yml")
        self.assertIn(6, self.startable(),
                      "a review comment did not bring delivered work back")

    def test_a_standalone_requeue_line_still_requeues(self):
        """The retired directive keeps working — not as a special shape any more,
        just as an ordinary comment. An operator who learned the old gesture is
        never punished for it."""
        self.mk_issue(7, "delivered, bounced", "kraken-task", "project:app",
                      "awaiting-merge")
        self.worker_comment(7, mtype="delivered")
        self.mk_comment(7, "requeue:\nplease fix the typo in the README first")
        self.assertIn(7, self.startable(), "the legacy requeue gesture stopped working")

    def test_a_prose_requeue_bounces_it_too(self):
        """The inverse of the protocol/7 pin, and deliberately so: nothing matches
        a directive any more, so this requeues because it is a COMMENT. The
        accepted trade — a spurious requeue costs one drain, a swallowed work
        request costs a debugging session (HISTORY.md, protocol/8)."""
        self.mk_issue(8, "delivered", "kraken-task", "project:app", "awaiting-merge")
        self.worker_comment(8, mtype="delivered")
        self.mk_comment(8, "requeue: is something I considered, but hold off")
        self.assertIn(8, self.startable())

    def test_a_pasted_marker_still_reads_as_a_worker_comment(self):
        """Accepted edge (§4), unchanged by protocol/8: any hidden marker reads as
        worker-authored, so an operator who pastes one is not heard. Removing the
        label by hand is the escape hatch."""
        self.mk_issue(9, "delivered", "kraken-task", "project:app", "awaiting-merge")
        self.worker_comment(9, mtype="delivered")
        self.mk_comment(9, 'bounce it\n\n<!-- kraken {"type":"requeue"} -->')
        self.assertNotIn(9, self.startable())

    def test_an_unreviewed_delivery_stays_held(self):
        """The floor the symmetry must not break: no operator comment, no
        requeue. A delivered task sits in the review queue until somebody says
        something."""
        self.mk_issue(15, "delivered, untouched", "kraken-task", "project:app",
                      "awaiting-merge")
        self.worker_comment(15, mtype="delivered")
        self.assertNotIn(15, self.startable(),
                         "delivered work rejoined the queue with nothing said about it")

    def test_a_requeued_delivery_is_actually_claimable(self):
        """End to end on the other held label: the filter offers it, the guard
        accepts the label the derivation lifted, and the claim swaps it for
        in-progress — the same path `needs-decision` takes."""
        self.mk_issue(16, "delivered, reviewed", "kraken-task", "project:app",
                      "awaiting-merge")
        self.worker_comment(16, mtype="delivered")
        self.mk_comment(16, "almost — rename the flag and re-push")

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w3")
        self.assertEqual(r.rc, 0, "claim-next on a reviewed delivery: %s%s"
                         % (r.out, r.err))
        self.assertIn("claim-next: claimed issue=16 worker=w3", r.out.split("\n"))
        self.assertTrue(self.has_label(16, "in-progress"), "in-progress not projected")
        self.assertFalse(self.has_label(16, "awaiting-merge"),
                         "the lifted label was not swapped off at claim time")

    # --- the derivation and the claim agree ----------------------------------

    def test_a_requeued_task_is_actually_claimable(self):
        """The guard used to refuse ANY held label, which would have refused the
        very task the filter just offered. It now accepts the labels the
        derivation lifted, and the claim's projection swaps them off — so the
        label catches up with the thread at claim time instead of before it."""
        self.mk_issue(10, "answered and claimable", "kraken-task", "project:app",
                      "needs-decision")
        self.worker_comment(10)
        self.mk_comment(10, "option B, go")

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w2")
        self.assertEqual(r.rc, 0, "claim-next on a requeued task: %s%s" % (r.out, r.err))
        self.assertIn("claim-next: claimed issue=10 worker=w2", r.out.split("\n"))
        self.assertTrue(self.has_label(10, "in-progress"), "in-progress not projected")
        self.assertFalse(self.has_label(10, "needs-decision"),
                         "the lifted label was not swapped off at claim time")
        self.assertTrue(self.claim_ref_exists(10), "the CAS did not run")

    def test_a_live_claim_ref_outranks_the_thread(self):
        """A comment on a CLAIMED task is context for its worker, never a requeue:
        the lock is the truth (§5)."""
        self.mk_issue(11, "claimed and commented on", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_claim_ref(11, "w1", age_hours=0)
        self.worker_comment(11)
        self.mk_comment(11, "any news?")
        self.assertNotIn(11, self.startable(),
                         "a comment on a ref-held task offered it to another worker")

    def test_a_requeued_task_still_has_to_clear_its_dependencies(self):
        """Lifting a held label rejoins the CANDIDATES, not the startable set —
        the blocked-by check still applies."""
        self.mk_issue(12, "blocker", "kraken-task", "project:app")
        self.mk_issue(13, "answered but blocked", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_blocked_by(13, 12)
        self.worker_comment(13)
        self.mk_comment(13, "option B, go")
        self.assertNotIn(13, self.startable(),
                         "a requeued task jumped its open blocker")

    def test_status_drops_an_answered_task_from_the_decision_queue(self):
        """The console reads the queue the way a worker does, so a decision
        already made stops sitting in the operator's decision list."""
        self.mk_issue(14, "answered", "kraken-task", "project:app", "needs-decision")
        self.worker_comment(14)
        self.assertIn(14, self.decision_queue(),
                      "an unanswered escalation left the decision queue")

        self.mk_comment(14, "option B, go")
        self.assertNotIn(14, self.decision_queue(),
                         "an answered task still sits in the decision queue")

    def decision_queue(self):
        r = self.kraken("status", "OWNER/tasks", "--project", "app", "--json")
        self.assertEqual(r.rc, 0, "status exit: %s" % r.err)
        return {i["number"] for i in json.loads(r.out)["decision_queue"]}


if __name__ == "__main__":
    unittest.main()
