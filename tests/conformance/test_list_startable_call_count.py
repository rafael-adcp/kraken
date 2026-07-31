#!/usr/bin/env python3
"""The queue watcher's idle poll must cost O(1) gh calls, not O(N) in queue size.
Pins the invocation count against the gh-stub's log."""
import unittest

from harness import KrakenConformanceTest


class ListStartableCallCountTests(KrakenConformanceTest):
    def test_o1_call_count(self):
        # 50 free (non-held, unblocked) tasks — the shape that used to cost one
        # gh api call each on top of the listing call.
        for n in range(1, 51):
            self.mk_issue(n, "task %d" % n, "kraken-task", "project:app")

        self.truncate_log()
        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot exit (50 free tasks)")
        startable = sum(1 for l in r.out.split("\n") if l.endswith(":startable"))
        self.assertEqual(startable, 50, "all 50 free tasks report startable")

        # One GraphQL listing page + one matching-refs read for the claim refs
        # — both O(1) in queue size (the ref read is a single paginated call).
        calls = len(self.log_lines())
        self.assertLessEqual(calls, 2,
                             "expected O(1) gh calls for 50 free tasks (listing + claim refs), got %d: %s"
                             % (calls, self.log_text()))

        # The depends-on fallback must also stay O(1): 30 candidates all falling
        # back to the same body-line check resolve in a single batched query.
        self.mk_issue(900, "dep target", "kraken-task", "project:app")
        for n in range(901, 931):
            self.mk_issue(n, "fallback %d" % n, "kraken-task", "project:app")
            self.mk_body(n, "depends-on: #900")

        self.truncate_log()
        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot exit (depends-on fan-out)")
        lines = [l for l in r.out.split("\n") if l]
        self.assertEqual(len(lines), 81, "50 free + 1 open dep target + 30 fallback candidates, all reported")
        held = sum(1 for l in lines if l.endswith(":held"))
        self.assertEqual(held, 30, "all 30 fallback candidates held while the dep target is open")

        # Listing page + claim-refs read + one batched depends-on resolve.
        calls = len(self.log_lines())
        self.assertLessEqual(calls, 3,
                             "expected O(1) gh calls for a 30-candidate depends-on fan-out, got %d: %s"
                             % (calls, self.log_text()))

    def test_twenty_held_tasks_cost_no_more_than_an_empty_queue(self):
        """The protocol/9 cost claim. A 60-task queue with a long thread on every
        task decides the 20 that are held from the state records — which arrive on
        the SAME matching-refs read as the claim refs, and resolve through the SAME
        batched commit read. Through protocol/8 this cost a comment-hydration call
        on top; through protocol/7 it would have shipped 60 comment threads on the
        walk."""
        for n in range(1, 41):
            self.mk_issue(n, "free %d" % n, "kraken-task", "project:app")
        for n in range(41, 61):
            self.mk_issue(n, "held %d" % n, "kraken-task", "project:app",
                          "needs-decision")
        for n in range(1, 61):
            for c in range(5):
                self.mk_comment(n, "operator chatter %d" % c)
        for n in range(41, 61):
            # Recorded AFTER the chatter, so nothing has been said since.
            self.mk_state_record(n, "needs-decision", worker="w1")

        self.truncate_log()
        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot exit (40 free + 20 held)")
        lines = [l for l in r.out.split("\n") if l]
        self.assertEqual(sum(1 for l in lines if l.endswith(":startable")), 40,
                         "the 40 free tasks must all be startable")
        self.assertEqual(sum(1 for l in lines if l.endswith(":held")), 20,
                         "the 20 unanswered escalations must stay held")

        # Listing page + the one refs read + ONE batched commit read for all 20
        # records. Pinned EXACTLY: an upper bound would also be met by fetching
        # comment threads again, which is what this test exists to forbid.
        calls = len(self.log_lines())
        self.assertEqual(calls, 3,
                         "expected walk + refs + one batched record read for 20 "
                         "held tasks, got %d: %s" % (calls, self.log_text()))
        self.assertEqual([l for l in self.log_lines() if "/comments" in l], [],
                         "the classification read a comment thread")

    def test_a_queue_with_nothing_held_reads_nothing_extra(self):
        """The common case, and the hot one: every task has a thread, none of
        them is held, and no record exists — so the poll costs exactly what an
        empty queue costs."""
        for n in range(1, 31):
            self.mk_issue(n, "free %d" % n, "kraken-task", "project:app")
            self.mk_comment(n, "some chatter on the thread")

        self.truncate_log()
        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot exit (30 free tasks with threads)")
        self.assertEqual(sum(1 for l in r.out.split("\n") if l.endswith(":startable")),
                         30)

        # Listing page + the refs read, and nothing else: no commit read, because
        # there is no ref to resolve, and no comment read at all.
        calls = len(self.log_lines())
        self.assertEqual(calls, 2,
                         "an idle queue must cost two calls, got %d: %s"
                         % (calls, self.log_text()))


if __name__ == "__main__":
    unittest.main()
