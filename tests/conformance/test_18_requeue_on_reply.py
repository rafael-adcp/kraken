#!/usr/bin/env python3
"""Auto-requeue conformance. Drives kraken.py requeue-check against the gh-stub:
the triggering comment's body and author type arrive through the environment
(COMMENT_BODY / COMMENT_AUTHOR_TYPE), the held-issue number as an argument."""
import unittest

from harness import KrakenConformanceTest


class RequeueOnReplyTests(KrakenConformanceTest):
    def run_case(self, issue, body, author):
        return self.kraken("requeue-check", "OWNER/tasks", issue,
                           env={"REPO": "OWNER/tasks",
                                "COMMENT_BODY": body,
                                "COMMENT_AUTHOR_TYPE": author})

    def test_requeue_on_reply(self):
        # Derive the worker disclaimer from kraken.py (the single source of truth).
        disclaimer = self.disclaimer_line("w1")

        self.mk_issue(1, "decision answered by a bare reply", "kraken-task", "project:app", "needs-decision")
        r = self.run_case(1, "option B, go", "User")
        self.assertEqual(r.rc, 0, "#1 run")
        self.assertFalse(self.has_label(1, "needs-decision"), "#1 needs-decision not removed on a human reply")
        self.assertTrue(self.last_comment(1).startswith("requeue: "), "#1 missing requeue confirmation comment")

        self.mk_issue(2, "worker comment must not requeue", "kraken-task", "project:app", "needs-decision")
        r = self.run_case(2, "%s\n\nneeds-decision: w1\n\nwhich option?" % disclaimer, "User")
        self.assertEqual(r.rc, 0, "#2 run")
        self.assertTrue(self.has_label(2, "needs-decision"), "#2 worker comment wrongly requeued")
        self.assertEqual(self.comment_count(2), 0, "#2 got a comment it should not have")

        # #2b — first-line anchoring: disclaimer quoted MID-body still requeues.
        self.mk_issue(20, "operator reply quoting the disclaimer mid-body still requeues",
                      "kraken-task", "project:app", "needs-decision")
        r = self.run_case(20, "answering your question below:\n\n%s\n\noption B, go" % disclaimer, "User")
        self.assertEqual(r.rc, 0, "#2b run")
        self.assertFalse(self.has_label(20, "needs-decision"),
                         "#2b operator reply quoting the disclaimer mid-body was misread as a worker comment")
        self.assertTrue(self.last_comment(20).startswith("requeue: "), "#2b missing requeue confirmation comment")

        # #2c — structural discrimination: a marker-bearing worker comment with NO
        # leading disclaimer is still recognized as a worker and does not requeue.
        self.mk_issue(21, "marker-only worker comment must not requeue needs-decision",
                      "kraken-task", "project:app", "needs-decision")
        r = self.run_case(21, 'progress\n\n<!-- kraken {"type":"note","worker":"w1"} -->', "User")
        self.assertEqual(r.rc, 0, "#2c run")
        self.assertTrue(self.has_label(21, "needs-decision"),
                        "#2c a marker-bearing worker comment wrongly requeued needs-decision")
        self.assertEqual(self.comment_count(21), 0, "#2c got a comment it should not have")

        self.mk_issue(3, "no held label", "kraken-task", "project:app")
        r = self.run_case(3, "nice work everyone", "User")
        self.assertEqual(r.rc, 0, "#3 run")
        self.assertEqual(self.comment_count(3), 0, "#3 got a comment on an unheld issue")

        self.mk_issue(4, "bot comment must not requeue", "kraken-task", "project:app", "needs-decision")
        r = self.run_case(4, "stale-claim: no worker heartbeat for 8h — the worker likely died.", "Bot")
        self.assertEqual(r.rc, 0, "#4 run")
        self.assertTrue(self.has_label(4, "needs-decision"), "#4 bot comment wrongly requeued needs-decision")
        self.assertEqual(self.comment_count(4), 0, "#4 bot comment produced output")

        self.mk_issue(5, "awaiting-merge, bare comment stays held", "kraken-task", "project:app", "awaiting-merge")
        r = self.run_case(5, "I'll merge this tomorrow, looks good", "User")
        self.assertEqual(r.rc, 0, "#5 run")
        self.assertTrue(self.has_label(5, "awaiting-merge"), "#5 awaiting-merge wrongly requeued on a bare comment")
        self.assertEqual(self.comment_count(5), 0, "#5 got a comment it should not have")

        self.mk_issue(6, "awaiting-merge, standalone requeue: directive", "kraken-task", "project:app", "awaiting-merge")
        r = self.run_case(6, "requeue:\nplease fix the typo in the README before I merge", "User")
        self.assertEqual(r.rc, 0, "#6 run")
        self.assertFalse(self.has_label(6, "awaiting-merge"),
                         "#6 awaiting-merge not removed on a standalone requeue: directive")
        self.assertTrue(self.last_comment(6).startswith("requeue: "), "#6 missing requeue confirmation comment")

        # #6b — accepted edge (PROTOCOL.md §4): a comment carrying ANY hidden marker
        # now reads as worker-authored, so a pasted requeue marker no longer bounces
        # a delivered task. The standalone `requeue:` line (#6) — which carries no
        # marker — or hand-removal is the operator's path.
        self.mk_issue(60, "awaiting-merge, pasted requeue marker reads as worker",
                      "kraken-task", "project:app", "awaiting-merge")
        r = self.run_case(60, 'bounce it back\n\n<!-- kraken {"type":"requeue"} -->', "User")
        self.assertEqual(r.rc, 0, "#6b run")
        self.assertTrue(self.has_label(60, "awaiting-merge"),
                        "#6b a pasted marker was misread and wrongly bounced delivered work")
        self.assertEqual(self.comment_count(60), 0, "#6b got a comment it should not have")

        # #6c — accidental-collision fix: a prose "requeue:" must NOT bounce work.
        self.mk_issue(61, "awaiting-merge, requeue: buried in prose", "kraken-task", "project:app", "awaiting-merge")
        r = self.run_case(61, "requeue: is something I considered, but let's hold off until Monday", "User")
        self.assertEqual(r.rc, 0, "#6c run")
        self.assertTrue(self.has_label(61, "awaiting-merge"),
                        "#6c a prose 'requeue:' sentence wrongly bounced delivered work")
        self.assertEqual(self.comment_count(61), 0, "#6c got a comment it should not have")

        # #7 — debounce: a second bare comment on the now-requeued #1 no-ops.
        r = self.run_case(1, "and one more thing", "User")
        self.assertEqual(r.rc, 0, "#7 run")
        self.assertEqual(self.comment_count(1), 1, "#7 a second comment requeued/commented again (no debounce)")


if __name__ == "__main__":
    unittest.main()
