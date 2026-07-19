#!/usr/bin/env python3
"""Markers are structured and free text is inert (PROTOCOL.md §4). A
state-changing comment carries its machine payload in exactly one hidden
marker; the visible prose — even prose that reproduces a former protocol/1 line
like `claimed-by:` or `released:` — is never parsed. Under protocol/4 the claim
lock is the ref, so no comment, marker or free text, ever creates or clears a
lock. Driven end to end through the real kraken.py + gh-stub pipeline."""
import os
import unittest

from harness import KrakenConformanceTest


class MarkerAndFreeTextTests(KrakenConformanceTest):
    def test_free_text_is_inert(self):
        # --- 1. a thread full of former protocol/1 lines creates no lock ------
        self.mk_issue(7, "former protocol/1 lines, all inert", "kraken-task", "project:app")
        self.mk_comment(7, "claimed-by: ghost")
        self.mk_comment(7, '<!-- kraken {"type":"claim","worker":"ghost"} -->')
        self.mk_comment(7, "heartbeat: ghost")
        r = self.kraken("claim", "OWNER/tasks", 7, "fresh")
        self.assertEqual(r.rc, 0, "no comment — marker or free text — is a lock")
        self.assertEqual(r.out, "claim: claimed issue=7 worker=fresh",
                         "only the ref locks; the thread is inert")
        self.assertTrue(self.claim_ref_exists(7), "claim ref missing after the claim")

        # --- 2. free text cannot forge a release: a live ref stays locked -----
        self.mk_issue(8, "free text cannot forge a release", "kraken-task", "project:app")
        self.mk_claim_ref(8, "owner")
        self.mk_comment(8, "released: owner\nclaimed-by: nobody")  # inert prose
        r = self.kraken("claim", "OWNER/tasks", 8, "challenger")
        self.assertEqual(r.rc, 10, "a free-text 'released:' line freed the ref")
        self.assertEqual(r.out,
                         "claim: lost-cas issue=8 — another worker holds the claim ref",
                         "owner still holds the ref — free text is inert")

        # --- 3. the produced comment shape is pinned: one marker, prose kept --
        self.mk_issue(9, "delivered with colliding free text", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(9, "w1")
        rf = os.path.join(self.state, "result.md")
        self._write(rf, "Shipped the feature.\n\nreleased: evil\nclaimed-by: evil\n")
        r = self.kraken("deliver", "OWNER/tasks", 9, "w1", rf)
        self.assertEqual(r.rc, 0, "deliver with colliding free text still succeeds")

        c = self.last_comment(9)
        lines = c.split("\n")
        self.assert_disclaimer(9, "w1")
        self.assertEqual(lines[1], "", "no blank line after the disclaimer")
        self.assertIn("released: evil", lines, "free text 'released: evil' not preserved")
        self.assertIn("claimed-by: evil", lines, "free text 'claimed-by: evil' not preserved")
        self.assert_marker(9, '{"type":"delivered","worker":"w1"}')
        self.assertEqual(self.marker_count(9), 1, "exactly one kraken marker in the produced comment")

        # --- 4. after deliver frees the ref, the next worker can claim --------
        self.remove_label(9, "awaiting-merge")
        r = self.kraken("claim", "OWNER/tasks", 9, "w2")
        self.assertEqual(r.rc, 0, "deliver freed the ref; w2 can claim")
        self.assertEqual(r.out, "claim: claimed issue=9 worker=w2", "w2 owns the task")


if __name__ == "__main__":
    unittest.main()
