#!/usr/bin/env python3
"""Cleanup conformance (§10): on a closed kraken-task issue every label MUST be
stripped except kraken-task itself and its project:<name> label. Drives
kraken.py cleanup against the gh-stub."""
import unittest

from harness import KrakenConformanceTest


class CleanupClosedTests(KrakenConformanceTest):
    def test_cleanup(self):
        env = {"REPO": "OWNER/tasks"}

        # A stale in-progress claim on a closed issue: every state label goes.
        self.mk_issue(1, "closed with a stale held label", "kraken-task", "project:app", "in-progress")
        self.set_issue_state(1, "closed")
        r = self.kraken("cleanup", "OWNER/tasks", 1, env=env)
        self.assertEqual(r.rc, 0, "#1 run")
        self.assertTrue(self.has_label(1, "kraken-task"), "#1 kraken-task wrongly stripped")
        self.assertTrue(self.has_label(1, "project:app"), "#1 project:app wrongly stripped")
        self.assertFalse(self.has_label(1, "in-progress"), "#1 in-progress not stripped from a closed issue")

        # Multiple dead-state labels plus an unrelated one: all non-identity go.
        self.mk_issue(2, "closed with several labels", "kraken-task", "project:web",
                      "awaiting-merge", "needs-decision", "priority:high")
        self.set_issue_state(2, "closed")
        r = self.kraken("cleanup", "OWNER/tasks", 2, env=env)
        self.assertEqual(r.rc, 0, "#2 run")
        self.assertTrue(self.has_label(2, "kraken-task"), "#2 kraken-task wrongly stripped")
        self.assertTrue(self.has_label(2, "project:web"), "#2 project:web wrongly stripped")
        self.assertFalse(self.has_label(2, "awaiting-merge"), "#2 awaiting-merge not stripped")
        self.assertFalse(self.has_label(2, "needs-decision"), "#2 needs-decision not stripped")
        self.assertFalse(self.has_label(2, "priority:high"), "#2 non-kraken label not stripped")

        # Already clean: nothing but identity labels — a no-op, no error.
        self.mk_issue(3, "closed and already clean", "kraken-task", "project:app")
        self.set_issue_state(3, "closed")
        r = self.kraken("cleanup", "OWNER/tasks", 3, env=env)
        self.assertEqual(r.rc, 0, "#3 run")
        self.assertTrue(self.has_label(3, "kraken-task"), "#3 kraken-task wrongly stripped")
        self.assertTrue(self.has_label(3, "project:app"), "#3 project:app wrongly stripped")

        # Not a kraken-task issue: a no-op guard, nothing stripped.
        self.mk_issue(4, "closed non-task issue", "needs-decision", "priority:high")
        self.set_issue_state(4, "closed")
        r = self.kraken("cleanup", "OWNER/tasks", 4, env=env)
        self.assertEqual(r.rc, 0, "#4 run")
        self.assertTrue(self.has_label(4, "needs-decision"), "#4 label wrongly stripped from a non-task issue")
        self.assertTrue(self.has_label(4, "priority:high"), "#4 label wrongly stripped from a non-task issue")


if __name__ == "__main__":
    unittest.main()
