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

        calls = len(self.log_lines())
        self.assertLessEqual(calls, 2,
                             "expected O(1) gh calls for 50 free tasks (single page), got %d: %s"
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

        calls = len(self.log_lines())
        self.assertLessEqual(calls, 3,
                             "expected O(1) gh calls for a 30-candidate depends-on fan-out, got %d: %s"
                             % (calls, self.log_text()))


if __name__ == "__main__":
    unittest.main()
