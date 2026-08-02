#!/usr/bin/env python3
"""Queue priority: a `priority:high` startable task is claimed before older
normal ones. classify_queue sorts the high tier first while keeping createdAt
FIFO within each tier (a stable sort), so claim-next and list-startable both see
high-priority work at the front of the queue without any protocol change — an old
worker that ignores the label simply falls back to pure FIFO."""
import unittest

from harness import KrakenConformanceTest


class PriorityOrderingTests(KrakenConformanceTest):
    def test_priority_high_claimed_before_older_normal(self):
        # #7 is older (lower number => earlier createdAt), #9 is younger but
        # carries priority:high. Pure FIFO would claim #7; priority ordering must
        # claim #9 first.
        self.mk_issue(7, "older normal task", "kraken-task", "project:app")
        self.mk_issue(9, "younger high-priority task",
                      "kraken-task", "project:app", "priority:high")

        r = self.kraken("claim-next", "acme/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "clean claim-next exit")
        self.assertIn("claim-next: claimed issue=9 worker=w1", r.out.split("\n"),
                      "high-priority #9 not claimed before older normal #7")
        self.assertTrue(self.has_label(9, "in-progress"), "#9 not claimed")
        self.assertFalse(self.has_label(7, "in-progress"),
                         "older normal #7 wrongly claimed before high-priority #9")

        # With #9 held, the next claim falls through to the older normal #7.
        r = self.kraken("claim-next", "acme/tasks", "app", "w2")
        self.assertEqual(r.rc, 0, "second claim-next exit")
        self.assertIn("claim-next: claimed issue=7 worker=w2", r.out.split("\n"),
                      "normal #7 not claimed once the high-priority task is held")

    def test_fifo_preserved_within_the_high_tier(self):
        # Two high-priority tasks: the older one (#5) still wins over the younger
        # one (#8) — createdAt FIFO holds inside the priority tier (stable sort).
        self.mk_issue(5, "older high-priority", "kraken-task", "project:app", "priority:high")
        self.mk_issue(8, "younger high-priority", "kraken-task", "project:app", "priority:high")
        # A normal task OLDER than both high-priority ones, to prove the whole
        # high tier still leads despite losing the FIFO race overall.
        self.mk_issue(3, "oldest normal", "kraken-task", "project:app")

        r = self.kraken("list-startable", "acme/tasks", "app")
        self.assertEqual(r.rc, 0, "list-startable exit")
        numbers = [line.split("\t")[0] for line in r.out.split("\n") if line.strip()]
        self.assertEqual(numbers, ["5", "8", "3"],
                         "expected high tier (FIFO) then normal tier (FIFO): %r" % numbers)


if __name__ == "__main__":
    unittest.main()
