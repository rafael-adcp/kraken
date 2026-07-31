#!/usr/bin/env python3
"""PROTOCOL.md §5: a worker MUST work one task at a time and MUST NOT claim a
second task while it holds a lease. The guard is derived from the claim refs
themselves — the claim-<worker>.json state file is a lifecycle-hook hint, never
the arbiter — and refuses (exit 11, writing nothing) for `claim` on a different
issue and for `claim-next` on any open claim, even when the local state file is
gone. A held claim on the same issue is a permitted re-claim."""
import os
import unittest

from harness import KrakenConformanceTest


class ClaimOneAtATimeTests(KrakenConformanceTest):
    def test_one_task_at_a_time_guard(self):
        state_file = self.claim_state_file("w1")

        # --- w1 takes its one task ------------------------------------------
        self.mk_issue(7, "first task", "kraken-task", "project:app")
        self.mk_issue(8, "second task", "kraken-task", "project:app")
        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "clean first claim exit")
        self.assertTrue(os.path.isfile(state_file), "first claim did not write the state file")
        self.assertTrue(self.has_label(7, "in-progress"), "first claim did not label issue 7 in-progress")

        # --- claim of a DIFFERENT task is refused while the claim is open ----
        before = self.comment_count(8)
        r = self.kraken("claim", "OWNER/tasks", 8, "w1")
        self.assertEqual(r.rc, 11, "second claim (different task) is refused")
        self.assertIn("refused", r.out, "refusal message should name the refusal (got: %s)" % r.out)
        self.assertIn("holds=7", r.out, "refusal should report the open claim it holds (got: %s)" % r.out)
        self.assertFalse(self.has_label(8, "in-progress"), "refused claim wrongly labeled issue 8 in-progress")
        self.assertEqual(self.comment_count(8), before, "refused claim wrongly commented on issue 8")
        self.assertTrue(os.path.isfile(state_file), "refused claim wrongly removed the open claim state file")
        with open(state_file, encoding="utf-8") as f:
            self.assertIn('"issue": "7"', f.read(), "open claim state file no longer records issue 7")

        # --- claim-next is refused too while any claim is open --------------
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 11, "claim-next is refused while a claim is held")
        self.assertIn("refused", r.out, "claim-next refusal should name the refusal (got: %s)" % r.out)
        self.assertFalse(self.has_label(8, "in-progress"), "refused claim-next wrongly labeled issue 8 in-progress")
        self.assertEqual(self.comment_count(8), before, "refused claim-next wrongly commented on issue 8")

        # --- re-claiming the SAME issue is allowed (the network-failure caveat)
        self.remove_label(7, "in-progress")
        r = self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "re-claiming the same held issue is permitted")
        self.assertNotIn("refused", r.out, "re-claiming the same issue must not be refused as a second claim")

        # --- the guard is the ladder, not the scratch file ------------------
        # A state file lost with its machine must not hand the worker a second
        # task: the claim ref still stands, and the ref is what refuses.
        os.remove(state_file)
        r = self.kraken("claim", "OWNER/tasks", 8, "w1")
        self.assertEqual(r.rc, 11, "claim must refuse on the claim ref alone, without the state file")
        self.assertIn("holds=7", r.out, "ref-derived refusal should still name the held claim (got: %s)" % r.out)
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 11, "claim-next must refuse on the claim ref alone, without the state file")

        # --- resolving the claim clears the guard ---------------------------
        r = self.kraken("release", "OWNER/tasks", 7, "w1", "backing out")
        self.assertEqual(r.rc, 0, "release exit")
        self.assertFalse(os.path.isfile(state_file), "release did not remove the state file")
        r = self.kraken("claim", "OWNER/tasks", 8, "w1")
        self.assertEqual(r.rc, 0, "claim after release is no longer refused")
        self.assertTrue(self.has_label(8, "in-progress"), "post-release claim did not label issue 8 in-progress")

    def test_a_named_claim_never_reads_the_queue(self):
        """The guard asks who holds what, and the claim-ref LADDER answers it.
        Deciding it from the queue instead made a named `claim` pay a paginated
        issue walk plus a comment hydration — O(queue size) — to learn something
        no issue body was ever going to say.

        Pinned on the stub's call log rather than on a count: every queue read in
        this program is GraphQL and every ref read is REST, so "the guard does
        not read the queue" is exactly "this run made no GraphQL call". An upper
        bound would pass again the day someone puts the walk back."""
        for n in range(1, 41):
            self.mk_issue(n, "free %d" % n, "kraken-task", "project:app")
        # Held tasks with threads: the shape that made the old guard hydrate
        # comment windows it never looked at.
        for n in range(41, 61):
            self.mk_issue(n, "held %d" % n, "kraken-task", "project:app",
                          "needs-decision")
            self.mk_comment(n, "operator chatter on a task we are not claiming")

        self.truncate_log()
        r = self.kraken("claim", "OWNER/tasks", 1, "w1")
        self.assertEqual(r.rc, 0, "claim exit: %s%s" % (r.out, r.err))
        self.assertTrue(self.has_label(1, "in-progress"), "the claim did not land")

        graphql = [l for l in self.log_lines() if "/graphql" in l]
        self.assertEqual(
            graphql, [],
            "a named claim read the queue to run the §5 guard (%d GraphQL call(s)) "
            "on a 60-task queue: %s" % (len(graphql), self.log_text()))

    def test_a_moot_claim_does_not_block_a_new_one(self):
        """A ref on a task that already left the queue is awaiting collection,
        not an open claim. The rule is unchanged — but the named guard now
        decides it from ONE issue fetch instead of from the queue walk, and the
        two observations must reach the same verdict or a worker gets bricked by
        a ref nobody is holding."""
        # A terminal transition whose ref delete was lost: the task is delivered
        # and the lease is still standing.
        self.mk_issue(3, "delivered, ref left behind", "kraken-task",
                      "project:app", "awaiting-merge")
        self.mk_claim_ref(3, "w1", age_hours=0)
        self.mk_issue(4, "the next task", "kraken-task", "project:app")

        r = self.kraken("claim", "OWNER/tasks", 4, "w1")
        self.assertEqual(r.rc, 0,
                         "a moot ref on a delivered task blocked a new claim: %s%s"
                         % (r.out, r.err))
        self.assertTrue(self.has_label(4, "in-progress"), "the new claim did not land")


if __name__ == "__main__":
    unittest.main()
