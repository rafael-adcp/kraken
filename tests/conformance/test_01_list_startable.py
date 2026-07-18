#!/usr/bin/env python3
"""kraken.py list-startable: startable filter, oldest-first ordering, snapshot mode."""
import unittest

from harness import KrakenConformanceTest


class ListStartableTests(KrakenConformanceTest):
    def test_list_startable(self):
        self.mk_issue(1, "old startable", "kraken-task", "project:app")
        self.mk_issue(2, "claimed", "kraken-task", "project:app", "in-progress")
        self.mk_issue(3, "other project", "kraken-task", "project:other")
        self.mk_issue(4, "young startable", "kraken-task", "project:app")
        self.mk_issue(5, "delivered", "kraken-task", "project:app", "awaiting-merge")
        self.mk_issue(6, "escalated", "kraken-task", "project:app", "needs-decision")
        self.mk_issue(7, "closed", "kraken-task", "project:app")
        self.set_issue_state(7, "closed")
        # §2: a kraken-task carrying NO project:<name> label is invisible to every
        # conforming worker — neither startable list nor snapshot.
        self.mk_issue(8, "no project label", "kraken-task")

        # Default mode: startable only, oldest first, number<TAB>title.
        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "default mode exit")
        self.assertEqual(r.out, "1\told startable\n4\tyoung startable", "default mode output")

        # Snapshot mode: every open task in the project, sorted by number, with state.
        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot mode exit")
        self.assertEqual(r.out, "1:startable\n2:held\n4:startable\n5:held\n6:held",
                         "snapshot mode output")

        # Empty queue: exit 0, no output.
        r = self.kraken("list-startable", "OWNER/tasks", "nothing-here")
        self.assertEqual(r.rc, 0, "empty queue exit")
        self.assertEqual(r.out, "", "empty queue output")


if __name__ == "__main__":
    unittest.main()
