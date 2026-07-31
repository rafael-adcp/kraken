#!/usr/bin/env python3
"""The state record end to end (PROTOCOL.md §3.1): what each transition writes,
in what order, and what happens when the write does not land.

The record is what a task IS between workers, so these are the cases where
getting it wrong loses work rather than merely looking untidy: a task freed
before its state was recorded reads as queued while it is in fact delivered, and
a record whose comment anchor was computed instead of read swallows whatever the
operator said in between.
"""
import os
import unittest

from harness import KrakenConformanceTest


class StateRecordTests(KrakenConformanceTest):
    def _file(self, name, text):
        path = os.path.join(self.state, name)
        self._write(path, text)
        return path

    # --- what each terminal transition records -------------------------------

    def test_delivery_records_the_state_the_pr_and_the_anchor(self):
        self.mk_issue(1, "shipped", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(1, "w1")
        self.mk_comment(1, "some earlier chatter")

        r = self.kraken("deliver", "OWNER/tasks", 1, "w1",
                        self._file("r.md", "done, validated\n"),
                        "https://github.com/o/r/pull/12")
        self.assertEqual(r.rc, 0, "deliver: %s%s" % (r.out, r.err))

        record = self.state_record(1)
        self.assertEqual(record["state"], "awaiting-merge")
        self.assertEqual(record["worker"], "w1")
        self.assertEqual(record["pr"], "https://github.com/o/r/pull/12")
        self.assertEqual(record["expiries"], 0)
        # The anchor INCLUDES the delivery's own comment (§3.1): 1 earlier + 1.
        self.assertEqual(record["comments"], 2,
                         "the anchor must count the comment the transition posted")

    def test_escalation_records_needs_decision(self):
        self.mk_issue(2, "blocked", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "w1")

        r = self.kraken("escalate", "OWNER/tasks", 2, "w1",
                        self._file("q.md", "which way?\n"))
        self.assertEqual(r.rc, 0, "escalate: %s%s" % (r.out, r.err))
        record = self.state_record(2)
        self.assertEqual((record["state"], record["comments"]),
                         ("needs-decision", 1))

    def test_release_records_queued_and_keeps_the_expiry_count(self):
        """A release puts the task back in the queue, and the record is what
        carries its history there: `expiries` is cumulative over the task's whole
        life, so handing it back does not forgive the workers it already killed."""
        self.mk_issue(3, "abandoned", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(3, "w1")
        self.mk_state_record(3, "queued", worker="w0", expiries=2)

        r = self.kraken("release", "OWNER/tasks", 3, "w1", "no docker")
        self.assertEqual(r.rc, 0, "release: %s%s" % (r.out, r.err))
        record = self.state_record(3)
        self.assertEqual(record["state"], "queued")
        self.assertEqual(record["expiries"], 2, "the expiry count was reset")

    def test_the_delivery_pr_survives_a_later_escalation(self):
        """`pr` is sticky: work delivered on a branch does not stop existing
        because the next worker had a question about it."""
        self.mk_issue(4, "delivered then questioned", "kraken-task", "project:app")
        self.deliver_state(4, pr="https://github.com/o/r/pull/7")
        self.mk_comment(4, "one more thing")
        self.assertEqual(self.kraken("claim", "OWNER/tasks", 4, "w2").rc, 0)

        r = self.kraken("escalate", "OWNER/tasks", 4, "w2",
                        self._file("q2.md", "which flag name?\n"))
        self.assertEqual(r.rc, 0, "escalate: %s%s" % (r.out, r.err))
        record = self.state_record(4)
        self.assertEqual(record["state"], "needs-decision")
        self.assertEqual(record["pr"], "https://github.com/o/r/pull/7",
                         "the delivery URL was erased by an escalation")

    def test_the_anchor_is_read_back_not_computed(self):
        """§3.1's MUST: the count comes from the issue AFTER the transition's own
        comment landed. A `+1` on an earlier read would swallow anything that
        arrived in between — the one way this mechanism can lose an operator's
        words. Here two comments land during the transition and the record has to
        reflect the thread as it actually stands."""
        self.mk_issue(5, "busy thread", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(5, "w1")
        for i in range(4):
            self.mk_comment(5, "chatter %d" % i)

        r = self.kraken("deliver", "OWNER/tasks", 5, "w1",
                        self._file("r5.md", "done\n"))
        self.assertEqual(r.rc, 0, "deliver: %s%s" % (r.out, r.err))
        self.assertEqual(self.state_record(5)["comments"], self.comment_count(5),
                         "the anchor does not match the thread it was read from")

    # --- ordering, and what a failure leaves behind ---------------------------

    def test_the_record_lands_before_the_claim_ref_is_deleted(self):
        """The ordering §3.1 exists for: the ref is what holds the task, so a
        record written after the delete leaves a window in which the task is
        observably queued while it is in fact delivered."""
        self.mk_issue(6, "ordering", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(6, "w1")

        self.truncate_log()
        self.assertEqual(self.kraken("deliver", "OWNER/tasks", 6, "w1",
                                     self._file("r6.md", "done\n")).rc, 0)
        log = self.log_lines()
        record_at = next(i for i, l in enumerate(log)
                         if l.startswith("POST ") and l.endswith("/git/refs"))
        delete_at = next(i for i, l in enumerate(log)
                         if l.startswith("DELETE ") and "/git/refs/kraken/claims/" in l)
        self.assertLess(record_at, delete_at,
                        "the claim ref was deleted before the record was written")

    def test_a_failed_record_write_fails_the_transition(self):
        """§3.1: a record that did not land fails the transition, which leaves the
        task held by the lease this worker still owns — the correct place to be
        stuck. The alternative is a delivered task the queue calls queued."""
        self.mk_issue(7, "record fails", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(7, "w1")

        r = self.kraken("deliver", "OWNER/tasks", 7, "w1",
                        self._file("r7.md", "done\n"),
                        fail=r"POST \S*/git/commits")
        self.assertEqual(r.rc, 20, "a failed record write must be exit 20")
        self.assertEqual(r.out, "deliver: gh-failure issue=7 stage=record")
        self.assertTrue(self.claim_ref_exists(7),
                        "the lease was released despite the record failing")
        self.assertTrue(self.has_label(7, "in-progress"),
                        "the label moved before the record landed")
        self.assertFalse(self.has_label(7, "awaiting-merge"))
        self.assertFalse(self.state_record_exists(7))

    def test_a_first_claim_writes_no_record(self):
        """A claim is not a state change: the lease says the task is being
        executed, and the record says what it is BETWEEN workers (§3.1)."""
        self.mk_issue(8, "fresh", "kraken-task", "project:app")
        self.assertEqual(self.kraken("claim", "OWNER/tasks", 8, "w1").rc, 0)
        self.assertFalse(self.state_record_exists(8),
                         "a first claim wrote a state record")

    def test_a_steal_records_one_more_expiry(self):
        self.mk_issue(9, "abandoned", "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(9, "w-dead")
        self.mk_state_record(9, "queued", worker="w-dead", expiries=1)

        self.assertEqual(self.kraken("claim", "OWNER/tasks", 9, "w-thief").rc, 0)
        record = self.state_record(9)
        self.assertEqual(record["expiries"], 2)
        self.assertEqual(record["worker"], "w-thief", "the record names its writer")

    # --- the migration (§6 rule 3) -------------------------------------------

    def test_a_held_label_with_no_record_is_migrated_by_the_first_drain(self):
        """The upgrade path, and the reason protocol/9 needs no migrate command:
        a queue written by an older revision wears held labels and has no records
        at all. The first drain's reconcile writes what each label already means,
        and from then on the requeue derivation works normally."""
        self.mk_issue(10, "delivered pre-upgrade", "kraken-task", "project:app",
                      "awaiting-merge")
        self.mk_comment(10, "the delivery comment an older worker posted")
        self.mk_issue(11, "escalated pre-upgrade", "kraken-task", "project:app",
                      "needs-decision")
        self.mk_issue(12, "free", "kraken-task", "project:app")

        # The drain claims the free task and repairs the other two on the way.
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "claim-next: %s%s" % (r.out, r.err))
        self.assertIn("claim-next: claimed issue=12 worker=w1", r.out.split("\n"))

        self.assertEqual(self.state_record(10)["state"], "awaiting-merge")
        self.assertEqual(self.state_record(10)["comments"], 1,
                         "the anchor must be the thread as it stood at migration")
        self.assertEqual(self.state_record(11)["state"], "needs-decision")
        self.assertFalse(self.state_record_exists(12),
                         "a task no label holds must not be given a record")
        # The migration touches nothing else: no comment, no label, no ref.
        self.assertEqual(self.comment_count(10), 1)
        self.assertTrue(self.has_label(10, "awaiting-merge"))

    def test_a_migrated_task_requeues_on_the_next_comment(self):
        """The other half: before the migration a held label derives no requeue
        at all (there is no anchor to compare against), and after it the normal
        rule applies. That is what makes the upgrade safe — a pre-upgrade
        delivery is never mistaken for a queued task."""
        self.mk_issue(13, "delivered pre-upgrade", "kraken-task", "project:app",
                      "awaiting-merge")
        self.mk_comment(13, "please also rename the flag")   # said before the upgrade

        startable = self.kraken("list-startable", "OWNER/tasks", "app").out
        self.assertNotIn("13\t", startable,
                         "an unrecorded held task was handed out before its migration")

        self.assertEqual(self.kraken("reap", "OWNER/tasks", "w1").rc, 0)
        self.assertEqual(self.state_record(13)["state"], "awaiting-merge")

        # The accepted edge (HISTORY.md, protocol/9): the pre-upgrade comment is
        # consumed by the migration, so it takes one more to move the task.
        startable = self.kraken("list-startable", "OWNER/tasks", "app").out
        self.assertNotIn("13\t", startable)
        self.mk_comment(13, "saying it again")
        startable = self.kraken("list-startable", "OWNER/tasks", "app").out
        self.assertIn("13\t", startable,
                      "a migrated task did not requeue on the next comment")

    def test_the_migration_is_a_one_shot(self):
        """Rule 3 fires once per task. A drain that rewrote the record every pass
        would re-anchor it on the current thread each time, and no requeue would
        ever be derivable."""
        self.mk_issue(14, "held", "kraken-task", "project:app", "needs-decision")
        self.assertEqual(self.kraken("reap", "OWNER/tasks", "w1").rc, 0)
        first = self.state_record(14)

        self.mk_comment(14, "option B")
        r = self.kraken("reap", "OWNER/tasks", "w1")
        self.assertEqual(r.rc, 0)
        self.assertIn("migrated=0", r.out, "the migration ran twice")
        self.assertEqual(self.state_record(14)["comments"], first["comments"],
                         "the second pass re-anchored the record")

    def test_an_orphan_record_is_swept(self):
        """A record on an issue that is no longer an open task is state over
        nothing — the same orphan rule a leftover lock gets (§10), so the
        namespace does not grow one ref per task the queue has ever closed."""
        self.mk_issue(15, "closed since", "kraken-task", "project:app")
        self.mk_state_record(15, "awaiting-merge", worker="w1")
        self.set_issue_state(15, "closed")

        r = self.kraken("reap", "OWNER/tasks", "w1")
        self.assertEqual(r.rc, 0, "reap: %s%s" % (r.out, r.err))
        self.assertIn("orphan_states=1", r.out)
        self.assertFalse(self.state_record_exists(15))


if __name__ == "__main__":
    unittest.main()
