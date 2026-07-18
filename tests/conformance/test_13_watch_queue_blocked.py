#!/usr/bin/env python3
"""kraken.py watch's entire emit gate is `count > 0` on
`kraken.py list-startable --snapshot`'s ":startable$" lines. Proving a
blocked-only queue's snapshot carries zero ":startable" lines IS the no-wake
proof. Also textually pins the watch gate in kraken.py."""
import unittest

from harness import KrakenConformanceTest, KRAKEN


class WatchQueueBlockedTests(KrakenConformanceTest):
    def test_blocked_only_queue_has_no_startable_lines(self):
        # The blocker lives in a different project — the project:app snapshot is
        # genuinely blocked-only.
        self.mk_issue(1, "blocker (other project)", "kraken-task", "project:other")
        self.mk_issue(2, "blocked candidate", "kraken-task", "project:app")
        self.mk_blocked_by(2, 1)

        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot exit (blocked-only queue)")
        self.assertEqual(r.out, "2:held", "blocked-only queue snapshot has no startable line")

        count = sum(1 for l in r.out.split("\n") if l.endswith(":startable"))
        self.assertEqual(count, 0, "blocked-only queue: zero startable lines (watch's exact emit gate)")

        # Closing the blocker flips the candidate to startable in the snapshot.
        self.set_issue_state(1, "closed")
        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot exit (blocker closed)")
        self.assertEqual(r.out, "2:startable", "closing the blocker flips the candidate to startable")

        count = sum(1 for l in r.out.split("\n") if l.endswith(":startable"))
        self.assertEqual(count, 1, "exactly the newly-clear task startable once the blocker closes")

    def test_watch_gate_is_textually_pinned(self):
        with open(KRAKEN, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("REMIND_SECONDS", src,
                         "kraken.py watch must not retain the 30-min re-emission safety net")
        self.assertIn("count > 0 and (snapshot != prev or due)", src,
                      "kraken.py watch must gate emission on count>0 AND (snapshot changed OR retry due)")


if __name__ == "__main__":
    unittest.main()
