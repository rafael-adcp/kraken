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

        # claim: failure at the guard read — nothing written, no CAS attempted.
        r = self.kraken("claim", "OWNER/tasks", 7, "w1", fail="issue view")
        self.assertEqual(r.rc, 20, "claim exit on guard failure")
        self.assertEqual(r.out, "claim: gh-failure issue=7 stage=guard", "guard failure machine line")
        self.assertEqual(self.comment_count(7), 0, "no comment after guard failure")
        self.assertFalse(self.claim_ref_exists(7), "guard failure created a claim ref")

        # claim: failure creating the claim commit (the CAS never runs) — nothing written.
        r = self.kraken("claim", "OWNER/tasks", 7, "w1", fail="git/commits")
        self.assertEqual(r.rc, 20, "claim exit on commit failure")
        self.assertEqual(r.out, "claim: gh-failure issue=7 stage=commit", "commit failure machine line")
        self.assertFalse(self.claim_ref_exists(7), "commit failure created a claim ref")

        # claim: the CAS won but the projection comment fails — the claim is HELD
        # (the ref exists), state honestly ambiguous (20).
        r = self.kraken("claim", "OWNER/tasks", 7, "w1", fail="issue comment")
        self.assertEqual(r.rc, 20, "claim exit on projection-comment failure")
        self.assertEqual(r.out, "claim: gh-failure issue=7 stage=comment (claim held)",
                         "comment failure machine line")
        self.assertTrue(self.claim_ref_exists(7),
                        "the won claim ref must survive a failed projection")

        # release: failure posting the released marker — the label must NOT have
        # been removed and the ref must NOT have been deleted.
        self.mk_issue(8, "held task", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(8, "w1")
        r = self.kraken("release", "OWNER/tasks", 8, "w1", fail="issue comment")
        self.assertEqual(r.rc, 20, "release exit on comment failure")
        self.assertTrue(self.has_label(8, "in-progress"),
                        "release removed the label before the released comment landed")
        self.assertTrue(self.claim_ref_exists(8),
                        "release deleted the ref before the released comment landed")


if __name__ == "__main__":
    unittest.main()
