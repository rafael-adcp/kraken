#!/usr/bin/env python3
"""kraken.py claim guard: a held task is skipped with exit 11 and ZERO writes —
stacking in-progress on awaiting-merge is the corruption class the guard exists for.

`in-progress` is the one held label that is a LEASE's projection (protocol/6,
PROTOCOL.md §5), so it holds only while the lease behind it does: refusing costs
one extra single-ref read, and a task wearing it with no lease at all is still
refused. The other two are operator-facing states with no lease behind them —
they must still be refused on the label alone, without touching the git API."""
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

    def test_in_progress_behind_a_live_lease_is_refused(self):
        self.mk_issue(21, "running", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(21, "w-other", age_hours=0)

        self.truncate_log()
        r = self.kraken("claim", "OWNER/tasks", 21, "w1")
        self.assertEqual(r.rc, 11, "claim on a live lease exit")
        self.assertEqual(r.out, "claim: held issue=21 label=in-progress")
        self.assertEqual(self.comment_count(21), 0, "no comment written")
        wrote = any(re.search(r"/issues/21/(comments|labels)", l) for l in self.log_lines())
        self.assertFalse(wrote, "the guard wrote to the issue")
        # The probe is a READ; nothing may be created or deleted on the git API.
        for line in self.log_lines():
            self.assertNotRegex(line, r"^(POST|PATCH|DELETE) \S*git/",
                                "the guard wrote to the git API: %s" % line)

    def test_in_progress_with_no_lease_at_all_is_still_refused(self):
        # An orphan projection: the reconciler's rule 4 requeues it (§6), the
        # claim path must not quietly take it as if the label meant nothing.
        self.mk_issue(22, "orphan projection", "kraken-task", "project:app",
                      "in-progress")
        r = self.kraken("claim", "OWNER/tasks", 22, "w1")
        self.assertEqual(r.rc, 11, "claim on an orphan projection exit")
        self.assertEqual(r.out, "claim: held issue=22 label=in-progress")
        self.assertFalse(self.claim_ref_exists(22), "a claim ref was created")


if __name__ == "__main__":
    unittest.main()
