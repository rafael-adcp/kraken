#!/usr/bin/env python3
"""Requeue by derivation (PROTOCOL.md §3.1, §6). Through protocol/4 an operator
reply MUTATED the queue: a workflow watched the comment stream and removed the
holding label. protocol/5 read the same fact off the thread instead; protocol/9
stopped reading the thread at all.

The rule now is one integer against another: the issue's total comment count
against the count the state record froze when the transition put the task down.
Anything added since — by anyone — is news the task has not been read against.

Every case here therefore asserts on `list-startable` (what the queue says) and
on `claim-next` (that a worker can actually take the task back), never on a label
having been rewritten by something.
"""
import json
import unittest

from harness import KrakenConformanceTest, make_marker


class RequeueByDerivationTests(KrakenConformanceTest):
    def startable(self):
        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "list-startable exit: %s" % r.err)
        return {int(l.split("\t")[0]) for l in r.out.split("\n") if l.strip()}

    def decision_queue(self):
        r = self.kraken("status", "OWNER/tasks", "--project", "app", "--json")
        self.assertEqual(r.rc, 0, "status exit: %s" % r.err)
        return {i["number"] for i in json.loads(r.out)["decision_queue"]}

    def escalate(self, n, worker="w1"):
        """The state an escalation leaves behind (§7): the question on the
        thread, the label, and the record anchored on the thread INCLUDING that
        question — which is what stops a worker's own comment from requeueing the
        task it just held."""
        self.mk_comment(n, "%s\n\nwhich option?\n\n%s" % (
            self.disclaimer_line(worker),
            make_marker({"type": "needs-decision", "worker": worker})))
        self.mk_state_record(n, "needs-decision", worker=worker)

    # --- needs-decision: any new comment requeues -----------------------------

    def test_bare_reply_requeues_needs_decision(self):
        self.mk_issue(1, "answered", "kraken-task", "project:app", "needs-decision")
        self.escalate(1)
        self.assertNotIn(1, self.startable(), "held before the operator answered")

        self.mk_comment(1, "option B, go")
        self.assertIn(1, self.startable(), "an answered task did not rejoin the queue")

    def test_an_unanswered_escalation_stays_held(self):
        self.mk_issue(2, "unanswered", "kraken-task", "project:app", "needs-decision")
        self.escalate(2)
        self.assertNotIn(2, self.startable())

    def test_a_re_escalation_re_holds_the_task(self):
        """The operator answered, a worker took it and escalated AGAIN. The new
        escalation records the count including its own question, so the older
        reply is behind the anchor and the task is held once more. Self-
        correcting, and it needs no memory of who said what."""
        self.mk_issue(3, "re-escalated", "kraken-task", "project:app", "needs-decision")
        self.escalate(3)
        self.mk_comment(3, "option B, go")
        self.assertIn(3, self.startable())
        self.escalate(3)
        self.assertNotIn(3, self.startable(), "a re-escalated task stayed startable")

    def test_a_machine_comment_does_not_requeue_what_it_just_held(self):
        """No comment is classified any more — protocol/9 retired the
        worker-vs-operator discriminator entirely — so the reconciler's own
        stale-claim note would requeue the escalation it just posted if it did
        not record the count including that note. It does, so it does not."""
        self.mk_issue(4, "reclaimed by the reconcile", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_comment(4, "%s\n\nThe worker has gone silent.\n\n%s" % (
            self.disclaimer_line("w-drain"),
            make_marker({"type": "stale-claim", "reason": "no heartbeat for 8h"})))
        self.mk_state_record(4, "needs-decision", worker="w-drain")
        self.assertNotIn(4, self.startable(),
                         "the reconciler's own comment requeued the task it just held")

    def test_the_operators_words_are_never_classified(self):
        """An operator reply that quotes the disclaimer, or pastes a raw kraken
        marker, requeues like any other comment. Through protocol/8 both were
        read as worker-authored and swallowed — an accepted edge whose only
        escape hatch was removing the label by hand. Counting instead of
        classifying removes the whole class."""
        for n, body in ((5, "answering below:\n\n%s\n\noption B, go"
                            % self.disclaimer_line("w1")),
                        (6, "bounce it\n\n%s"
                            % make_marker({"type": "delivered", "worker": "w1"}))):
            self.mk_issue(n, "quoted", "kraken-task", "project:app", "needs-decision")
            self.escalate(n)
            self.mk_comment(n, body)
            self.assertIn(n, self.startable(),
                          "an operator reply was misread as a worker's: #%d" % n)

    # --- awaiting-merge: the SAME rule, the same gesture ----------------------

    def test_awaiting_merge_requeues_on_a_bare_comment(self):
        """protocol/8's load-bearing case, and the one that motivated it: a review
        comment asking for follow-up work puts the task back in the queue. Through
        protocol/7 this thread stayed held — the ask was read, matched against a
        directive that was not there, and discarded, leaving the operator watching
        a queue that never moved."""
        self.mk_issue(7, "delivered", "kraken-task", "project:app")
        self.deliver_state(7)
        self.assertNotIn(7, self.startable(), "held before the review said anything")

        self.mk_comment(7, "please also commit it to config/app.yml")
        self.assertIn(7, self.startable(),
                      "a review comment did not bring delivered work back")

    def test_any_review_comment_requeues_whatever_it_says(self):
        """Three shapes that used to be three different verdicts: the retired
        `requeue:` directive, a sentence that merely contains it, and a
        congratulation. All three are just comments now. The accepted trade
        (HISTORY.md, protocol/8): a spurious requeue costs one drain, a swallowed
        work request costs a debugging session."""
        for n, body in ((8, "requeue:\nplease fix the typo in the README first"),
                        (9, "requeue: is something I considered, but hold off"),
                        (10, "nice, thanks")):
            self.mk_issue(n, "delivered", "kraken-task", "project:app")
            self.deliver_state(n)
            self.mk_comment(n, body)
            self.assertIn(n, self.startable(),
                          "a review comment did not requeue: #%d" % n)

    def test_an_unreviewed_delivery_stays_held(self):
        """The floor the symmetry must not break: nothing said, no requeue. A
        delivered task sits in the review queue until somebody comments."""
        self.mk_issue(15, "delivered, untouched", "kraken-task", "project:app")
        self.deliver_state(15)
        self.assertNotIn(15, self.startable(),
                         "delivered work rejoined the queue with nothing said about it")

    def test_a_requeued_delivery_is_actually_claimable(self):
        """End to end on the other held state: the filter offers it, the guard
        re-derives the same verdict from the same record, and the claim swaps the
        stale badge for in-progress."""
        self.mk_issue(16, "delivered, reviewed", "kraken-task", "project:app")
        self.deliver_state(16)
        self.mk_comment(16, "almost — rename the flag and re-push")

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w3")
        self.assertEqual(r.rc, 0, "claim-next on a reviewed delivery: %s%s"
                         % (r.out, r.err))
        self.assertIn("claim-next: claimed issue=16 worker=w3", r.out.split("\n"))
        self.assertTrue(self.has_label(16, "in-progress"), "in-progress not projected")
        self.assertFalse(self.has_label(16, "awaiting-merge"),
                         "the stale badge was not swapped off at claim time")

    # --- the derivation and the claim agree ----------------------------------

    def test_a_requeued_task_is_actually_claimable(self):
        """The guard used to refuse ANY held label, which would have refused the
        very task the filter just offered. Both now re-derive the verdict from
        the same record and the same count, so they cannot disagree."""
        self.mk_issue(11, "answered and claimable", "kraken-task", "project:app",
                      "needs-decision")
        self.escalate(11)
        self.mk_comment(11, "option B, go")

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w2")
        self.assertEqual(r.rc, 0, "claim-next on a requeued task: %s%s" % (r.out, r.err))
        self.assertIn("claim-next: claimed issue=11 worker=w2", r.out.split("\n"))
        self.assertTrue(self.has_label(11, "in-progress"), "in-progress not projected")
        self.assertFalse(self.has_label(11, "needs-decision"),
                         "the stale badge was not swapped off at claim time")
        self.assertTrue(self.claim_ref_exists(11), "the CAS did not run")

    def test_a_live_claim_ref_outranks_the_count(self):
        """A comment on a CLAIMED task is context for its worker, never a
        requeue: the lock is the truth (§5)."""
        self.mk_issue(12, "claimed and commented on", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_claim_ref(12, "w1", age_hours=0)
        self.escalate(12)
        self.mk_comment(12, "any news?")
        self.assertNotIn(12, self.startable(),
                         "a comment on a ref-held task offered it to another worker")

    def test_a_requeued_task_still_has_to_clear_its_dependencies(self):
        """Lifting a hold rejoins the CANDIDATES, not the startable set — the
        blocked-by check still applies."""
        self.mk_issue(13, "blocker", "kraken-task", "project:app")
        self.mk_issue(14, "answered but blocked", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_blocked_by(14, 13)
        self.escalate(14)
        self.mk_comment(14, "option B, go")
        self.assertNotIn(14, self.startable(),
                         "a requeued task jumped its open blocker")

    def test_status_drops_an_answered_task_from_the_decision_queue(self):
        """The console reads the queue the way a worker does, so a decision
        already made stops sitting in the operator's decision list."""
        self.mk_issue(18, "answered", "kraken-task", "project:app", "needs-decision")
        self.escalate(18)
        self.assertIn(18, self.decision_queue(),
                      "an unanswered escalation left the decision queue")

        self.mk_comment(18, "option B, go")
        self.assertNotIn(18, self.decision_queue(),
                         "an answered task still sits in the decision queue")

    # --- the record is what decides, not the badge ---------------------------

    def test_a_record_holds_a_task_whose_badge_never_landed(self):
        """A delivery whose label swap did not land is still delivered: the
        record is the state, and the badge is projection nothing repairs (§3.1).
        Under protocol/8 this task was offered to the next worker, which is a
        second delivery of work already waiting for review."""
        self.mk_issue(19, "delivered, badge lost", "kraken-task", "project:app")
        self.deliver_state(19)
        self.set_labels(19, ["kraken-task", "project:app"])
        self.assertNotIn(19, self.startable(),
                         "a delivery whose badge never landed was offered again")

    def test_a_stale_badge_cannot_hold_a_task_the_record_released(self):
        """The inverse: a release whose label removal did not land leaves the
        badge behind. The record says queued, so the task is startable."""
        self.mk_issue(20, "released, badge stuck", "kraken-task", "project:app",
                      "awaiting-merge")
        self.mk_state_record(20, "queued", worker="w1")
        self.assertIn(20, self.startable(),
                      "a stale badge held a task the record had released")


if __name__ == "__main__":
    unittest.main()
