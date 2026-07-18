#!/usr/bin/env python3
"""kraken.py deliver: result posted with a delivered marker, labels swapped —
and after a review bounce (human removes awaiting-merge), ANY worker wins the
fresh window (the review-bounce window reset)."""
import os
import unittest

from harness import KrakenConformanceTest


class DeliverTests(KrakenConformanceTest):
    def test_deliver(self):
        self.mk_issue(7, "shipped task", "kraken-task", "project:app", "in-progress")
        self.mk_comment(7, '<!-- kraken {"type":"claim","worker":"w1"} -->')

        r = os.path.join(self.state, "result.md")
        self._write(r, "Added cursor pagination. Acceptance run: 12/12 green.\n")

        res = self.kraken("deliver", "OWNER/tasks", 7, "w1", r, "https://github.com/owner/app/pull/9")
        self.assertEqual(res.rc, 0, "deliver exit")
        self.assertEqual(
            res.out,
            "deliver: delivered issue=7 worker=w1 pr=https://github.com/owner/app/pull/9",
            "machine line")

        self.assertFalse(self.has_label(7, "in-progress"), "in-progress still present after deliver")
        self.assertTrue(self.has_label(7, "awaiting-merge"), "awaiting-merge missing after deliver")
        self.assert_marker(7, '{"type":"delivered","worker":"w1","pr":"https://github.com/owner/app/pull/9"}')
        self.assertTrue(any(l.startswith("Added cursor pagination")
                            for l in self.last_comment(7).split("\n")),
                        "result body missing")

        # Review bounce: feedback comment, awaiting-merge removed — w2 must win.
        self.mk_comment(7, "please rename the flag before merge")
        self.remove_label(7, "awaiting-merge")

        res = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(res.rc, 0, "re-claim after review bounce")
        self.assertEqual(res.out, "claim: claimed issue=7 worker=w2", "delivery reset the claim window")

        # No PR URL (diff-in-comment path): no pr field, everything else identical.
        self.mk_issue(8, "patch task", "kraken-task", "project:app", "in-progress")
        res = self.kraken("deliver", "OWNER/tasks", 8, "w1", r)
        self.assertEqual(res.rc, 0, "deliver without pr exit")
        self.assertEqual(res.out, "deliver: delivered issue=8 worker=w1", "machine line without pr")
        self.assert_marker(8, '{"type":"delivered","worker":"w1"}')
        self.assertNotIn('"pr":', self.last_comment(8), "pr field present without a PR URL")
        self.assertTrue(self.has_label(8, "awaiting-merge"), "awaiting-merge missing on no-pr deliver")


if __name__ == "__main__":
    unittest.main()
