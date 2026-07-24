#!/usr/bin/env python3
"""Project preflight conformance: before it reads the queue, a drain checks that
the coordination repo actually carries the `project:<name>` label it was pointed
at. A worker scoped to a project that does not exist is permanently deaf — every
task is filtered out client-side — so an unconfigured project must refuse loudly
(exit 13, naming the projects that DO exist) instead of reading as an empty queue
forever. A configured project, idle or not, drains normally.
"""
import unittest

from harness import KrakenConformanceTest, KRAKEN


class ProjectPreflightTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        # The drift handshake runs in the same drain: seed a matching vendored
        # copy so the project check is what is under test.
        self.mk_content(".github/kraken.py", KRAKEN)

    def _seed_startable(self):
        self.mk_issue(7, "ready task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it")

    def test_unconfigured_project_refuses_before_claiming(self):
        # A genuinely startable task exists — under project:app, not the
        # misspelled project this worker was pointed at.
        self._seed_startable()

        r = self.kraken("claim-next", "OWNER/tasks", "ap", "w1")
        self.assertEqual(r.rc, 13, "an unconfigured project must refuse the drain")
        self.assertIn("project:ap", r.out, "refusal did not name the missing label")
        self.assertIn("app", r.out, "refusal did not list the configured projects")
        self.assertFalse(self.has_label(7, "in-progress"),
                         "a refused drain still claimed the task")
        self.assertFalse(self.claim_ref_exists(7),
                         "a refused drain still created a claim ref")
        self.assertEqual(self.comment_count(7), 0,
                         "a refused drain still commented on the task")

    def test_configured_project_drains_normally(self):
        self._seed_startable()

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "a configured project must drain normally")
        self.assertTrue(self.has_label(7, "in-progress"), "task not claimed")

    def test_configured_but_idle_project_is_an_honest_empty_queue(self):
        # The label exists in the repo registry, no open task carries it: the
        # worker is correctly configured, just idle. That is exit 3, not 13 —
        # the refusal must not swallow the honest-empty signal.
        self.mk_label("project:idle")

        r = self.kraken("claim-next", "OWNER/tasks", "idle", "w1")
        self.assertEqual(r.rc, 3,
                         "an idle but configured project is an empty queue, not a refusal")
        self.assertEqual(r.out, "claim-next: none project:idle")


if __name__ == "__main__":
    unittest.main()
