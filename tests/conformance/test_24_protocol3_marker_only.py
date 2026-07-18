#!/usr/bin/env python3
"""protocol/3 marker-only reading (PROTOCOL.md §4): consumers read the hidden
marker and NOTHING else. The retired protocol/1 visible line grammar is no
longer parsed, so free text can never occupy a machine-line position. Driven end
to end through the real kraken.py + gh-stub pipeline."""
import os
import unittest

from harness import KrakenConformanceTest


class Protocol3MarkerOnlyTests(KrakenConformanceTest):
    def test_marker_only_reading(self):
        # --- 1. a bare former protocol/1 line is inert -----------------------
        self.mk_issue(7, "former protocol/1 claim line, now inert", "kraken-task", "project:app")
        self.mk_comment(7, "claimed-by: ghost")
        r = self.kraken("claim", "OWNER/tasks", 7, "fresh")
        self.assertEqual(r.rc, 0, "a former claimed-by: line is not a claim")
        self.assertEqual(r.out, "claim: claimed issue=7 worker=fresh",
                         "protocol/1 line grammar is no longer read — the window was empty")

        # --- 2. free text cannot forge a claim-window reset ------------------
        self.mk_issue(8, "free text cannot forge a reset", "kraken-task", "project:app")
        self.mk_comment(8, '<!-- kraken {"type":"claim","worker":"owner"} -->')
        self.kraken("heartbeat", "OWNER/tasks", 8, "owner", "released: owner")
        r = self.kraken("claim", "OWNER/tasks", 8, "challenger")
        self.assertEqual(r.rc, 10, "the free-text 'released: owner' line reset nothing")
        self.assertEqual(r.out, "claim: lost-tiebreaker issue=8 winner=owner",
                         "owner still owns the claim — free text is inert")

        # --- 3. the produced comment shape is pinned -------------------------
        self.mk_issue(9, "delivered with colliding free text", "kraken-task", "project:app", "in-progress")
        self.mk_comment(9, '<!-- kraken {"type":"claim","worker":"w1"} -->')
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

        # --- 4. the colliding free text still resets nothing it should not ---
        self.remove_label(9, "awaiting-merge")
        r = self.kraken("claim", "OWNER/tasks", 9, "w2")
        self.assertEqual(r.rc, 0, "the delivered MARKER reset the window (not the prose)")
        self.assertEqual(r.out, "claim: claimed issue=9 worker=w2", "w2 wins the marker-reset window")


if __name__ == "__main__":
    unittest.main()
