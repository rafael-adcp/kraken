#!/usr/bin/env python3
"""kraken.py list-startable owns the blocked-by check too: a candidate with an
open native blocker (or, as a fallback, an open `depends-on: #N` target) must
never list as startable in either mode — closing the blocker un-blocks it."""
import unittest

from harness import KrakenConformanceTest


class ListStartableBlockedTests(KrakenConformanceTest):
    def test_blocked_by_and_depends_on(self):
        # --- native blocked-by: open blocker excludes; closing it un-excludes ---
        self.mk_issue(1, "blocker open", "kraken-task", "project:app")
        self.mk_issue(2, "blocked candidate", "kraken-task", "project:app")
        self.mk_blocked_by(2, 1)

        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "default mode exit (blocker open)")
        self.assertEqual(r.out, "1\tblocker open", "blocked candidate excluded while blocker open")

        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot mode exit (blocker open)")
        self.assertEqual(r.out, "1:startable\n2:held", "blocked candidate reports held in snapshot")

        # Close the blocker: the candidate now lists, oldest-first intact.
        self.set_issue_state(1, "closed")
        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "default mode exit (blocker closed)")
        self.assertEqual(r.out, "2\tblocked candidate",
                         "candidate lists once blocker closes (blocker itself closed, so absent)")

        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot mode exit (blocker closed)")
        self.assertEqual(r.out, "2:startable", "candidate startable once blocker closes")

        # --- depends-on: #N body fallback, honored only with no native blockers ---
        self.mk_issue(3, "dep target open", "kraken-task", "project:app")
        self.mk_issue(4, "fallback candidate", "kraken-task", "project:app")
        self.mk_body(4, "goal text\n\ndepends-on: #3\n")

        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "default mode exit (depends-on open)")
        self.assertEqual(r.out, "2\tblocked candidate\n3\tdep target open",
                         "depends-on candidate excluded while target open")

        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot mode exit (depends-on open)")
        self.assertEqual(r.out, "2:startable\n3:startable\n4:held",
                         "depends-on candidate reports held while target open")

        # Close the depends-on target: the fallback candidate now lists.
        self.set_issue_state(3, "closed")
        r = self.kraken("list-startable", "OWNER/tasks", "app")
        self.assertEqual(r.rc, 0, "default mode exit (depends-on closed)")
        self.assertEqual(r.out, "2\tblocked candidate\n4\tfallback candidate",
                         "depends-on candidate lists once target closes")

        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot mode exit (depends-on closed)")
        self.assertEqual(r.out, "2:startable\n4:startable",
                         "depends-on candidate startable once target closes")

        # --- native blockers take priority over an irrelevant depends-on line ---
        self.mk_issue(5, "native blocker still open", "kraken-task", "project:app")
        self.mk_issue(6, "has both", "kraken-task", "project:app")
        self.mk_blocked_by(6, 5)
        self.mk_body(6, "depends-on: #3")  # #3 is closed — must not clear #6

        r = self.kraken("list-startable", "OWNER/tasks", "app", "--snapshot")
        self.assertEqual(r.rc, 0, "snapshot mode exit (native + depends-on)")
        self.assertIn("6:held", r.out.split("\n"),
                      "native blocker must win over an irrelevant depends-on fallback")


if __name__ == "__main__":
    unittest.main()
