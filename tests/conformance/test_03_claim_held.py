#!/usr/bin/env python3
"""kraken.py claim guard: a held task is skipped with exit 11 and ZERO writes —
stacking needs-decision or awaiting-merge under a new claim is the corruption
class the guard exists for.

The guard reads exactly two labels, because exactly two labels hold: they are
operator-facing states with no lock behind them, so the label IS the state and it
can be refused on without touching the git API at all.

`in-progress` is not one of them (protocol/7, PROTOCOL.md §3). It is write-only,
so it decides nothing here: a task wearing one is refused only if a LIVE lease
sits behind it — and then the verdict is a **loss** (exit 10), not an unclear
task, because somebody demonstrably owns it. Wearing one with no lease at all is
a stale badge, and the task is claimed like any other."""
import re
import unittest

from harness import KrakenConformanceTest


class ClaimHeldTests(KrakenConformanceTest):
    def test_operator_held_labels_are_refused_without_touching_git(self):
        n = 10
        for held in ("needs-decision", "awaiting-merge"):
            n += 1
            self.mk_issue(n, "held by %s" % held, "kraken-task", "project:app", held)

            self.truncate_log()
            r = self.kraken("claim", "OWNER/tasks", n, "w1")
            self.assertEqual(r.rc, 11, "claim on %s exit" % held)
            self.assertEqual(r.out, "claim: held issue=%d label=%s" % (n, held),
                             "machine line for %s" % held)
            self.assertEqual(self.comment_count(n), 0, "no comment written on %s" % held)
            self.assertFalse(self.claim_ref_exists(n),
                             "guard created a claim ref despite %s" % held)
            wrote = any(re.search(r"/issues/%d/(comments|labels)" % n, l) for l in self.log_lines())
            self.assertFalse(wrote, "guard wrote to issue %d despite %s" % (n, held))
            cas = any("git/" in l for l in self.log_lines())
            self.assertFalse(cas, "guard reached the git-data API despite %s" % held)

    def test_a_live_lease_is_a_lost_claim_not_an_unclear_one(self):
        # The label is incidental here — the LEASE is what refuses. Somebody owns
        # the task, so the verdict is 10 (lost), and no CAS may be attempted: the
        # generation it would create is the next one, so it would succeed.
        self.mk_issue(21, "running", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(21, "w-other", age_hours=0)

        self.truncate_log()
        r = self.kraken("claim", "OWNER/tasks", 21, "w1")
        self.assertEqual(r.rc, 10, "claim on a live lease exit")
        self.assertIn("claim: lost-cas issue=21", r.out)
        self.assertEqual(self.comment_count(21), 0, "no comment written")
        wrote = any(re.search(r"/issues/21/(comments|labels)", l) for l in self.log_lines())
        self.assertFalse(wrote, "the guard wrote to the issue")
        # The probe is a READ; nothing may be created or deleted on the git API.
        for line in self.log_lines():
            self.assertNotRegex(line, r"^(POST|PATCH|DELETE) \S*git/",
                                "the guard wrote to the git API: %s" % line)

    def test_a_live_lease_refuses_even_with_no_label_at_all(self):
        # The mirror of the case above, and the reason the guard cannot be a pure
        # label read: the crash window between a won CAS and its projection.
        self.mk_issue(23, "claimed, badge never landed", "kraken-task", "project:app")
        self.mk_claim_ref(23, "w-other", age_hours=0)

        r = self.kraken("claim", "OWNER/tasks", 23, "w1")
        self.assertEqual(r.rc, 10, "a lease with no badge failed to hold the task")
        self.assertIn("claim: lost-cas issue=23", r.out)

    def test_in_progress_with_no_lease_at_all_is_claimable(self):
        # A stale badge: a crashed release, or a claim from before protocol/4.
        # protocol/6 refused it and left the reconciler to requeue it; protocol/7
        # reads the lock instead of the badge, finds nothing holding the task,
        # and claims it.
        self.mk_issue(22, "stale badge", "kraken-task", "project:app",
                      "in-progress")
        r = self.kraken("claim", "OWNER/tasks", 22, "w1")
        self.assertEqual(r.rc, 0, "a task held by nothing was refused: %s%s"
                         % (r.out, r.err))
        self.assertTrue(self.claim_ref_exists(22), "no claim ref was created")
        self.assertTrue(self.has_label(22, "in-progress"), "#22 lost its badge")


if __name__ == "__main__":
    unittest.main()
