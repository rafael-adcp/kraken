#!/usr/bin/env python3
"""gh/network failures surface as exit 20 at every stage — never a silent
success, never a zero."""
import unittest

from harness import KrakenConformanceTest


class GhFailureTests(KrakenConformanceTest):
    def test_gh_failures_surface_as_20(self):
        self.mk_issue(7, "a task", "kraken-task", "project:app")

        # list-startable: the batched listing/native-blocked-by gh graphql call fails.
        r = self.kraken("list-startable", "OWNER/tasks", "app", fail="graphql")
        self.assertEqual(r.rc, 20, "list-startable exit on gh graphql failure")

        # list-startable: the depends-on fallback's own batched gh graphql call
        # fails — a candidate needing the fallback must still surface 20. The
        # 'issue(number:' pattern only appears in that second call.
        self.mk_issue(70, "dep target", "kraken-task", "project:app")
        self.mk_issue(71, "fallback candidate", "kraken-task", "project:app")
        self.mk_body(71, "depends-on: #70")
        r = self.kraken("list-startable", "OWNER/tasks", "app", fail=r"issue\(number:")
        self.assertEqual(r.rc, 20, "list-startable exit on depends-on fallback gh graphql failure")

        # claim: failure at the guard read — nothing written.
        r = self.kraken("claim", "OWNER/tasks", 7, "w1", fail="issue view")
        self.assertEqual(r.rc, 20, "claim exit on guard failure")
        self.assertEqual(r.out, "claim: gh-failure issue=7 stage=guard", "guard failure machine line")
        self.assertEqual(self.comment_count(7), 0, "no comment after guard failure")

        # claim: failure at the comment — label landed, state honestly ambiguous (20).
        r = self.kraken("claim", "OWNER/tasks", 7, "w1", fail="issue comment")
        self.assertEqual(r.rc, 20, "claim exit on comment failure")
        self.assertEqual(r.out, "claim: gh-failure issue=7 stage=comment", "comment failure machine line")

        # release: failure posting released: — the label must NOT have been removed.
        self.mk_issue(8, "held task", "kraken-task", "project:app", "in-progress")
        r = self.kraken("release", "OWNER/tasks", 8, "w1", fail="issue comment")
        self.assertEqual(r.rc, 20, "release exit on comment failure")
        self.assertTrue(self.has_label(8, "in-progress"),
                        "release removed the label before the released: comment landed")


if __name__ == "__main__":
    unittest.main()
