#!/usr/bin/env python3
"""kraken.py list-startable must never silently truncate the queue. The shared
listing call once capped at 100 (newest-first), silently dropping the OLDEST
tasks. We fill the queue past 100 with cheap held tasks and plant the startable
tasks among the OLDEST numbers — the ones newest-first truncation drops first."""
import unittest

from harness import KrakenConformanceTest


class ListStartablePaginationTests(KrakenConformanceTest):
    def test_over_100_queue_not_truncated(self):
        # Three genuinely-startable tasks at the oldest end of the queue...
        self.mk_issue(1, "oldest startable", "kraken-task", "project:app")
        self.mk_issue(2, "second startable", "kraken-task", "project:app")
        self.mk_issue(3, "third startable", "kraken-task", "project:app")
        # ...then 102 held tasks (younger numbers) that push the queue past 100.
        for n in range(100, 202):
            self.mk_issue(n, "held %d" % n, "kraken-task", "project:app", "in-progress")

        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "default mode exit (queue > 100)")
        self.assertEqual(r.out, "1\toldest startable\n2\tsecond startable\n3\tthird startable",
                         "oldest startable tasks survive a >100 queue")

        snap = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(snap.rc, 0, "snapshot mode exit (queue > 100)")
        snap_lines = [l for l in snap.out.split("\n") if l]
        self.assertEqual(len(snap_lines), 105,
                         "snapshot lists the whole queue (3 startable + 102 held), not a truncated 100")
        startable = sum(1 for l in snap_lines if l.endswith(":startable"))
        self.assertEqual(startable, 3, "all three oldest startable tasks reported in the full snapshot")


if __name__ == "__main__":
    unittest.main()
