#!/usr/bin/env python3
"""The SessionEnd auto-release hook: when a session ends while a claim state
file is present, the bundled hook runs `kraken.py release` so the task requeues
in seconds. With no state file it is a strict no-op. Best-effort: a failed
release never blocks session exit (always exits 0)."""
import os
import unittest

from harness import KrakenConformanceTest

HOOK = "hooks/session-end-release.sh"
EVENT = '{"hook_event_name":"SessionEnd","reason":"exit"}'


class SessionEndReleaseTests(KrakenConformanceTest):
    def test_session_end_release(self):
        # --- session ends WITH a claim open: kraken.py release drives requeue --
        self.mk_issue(7, "abandoned-on-exit task", "kraken-task", "project:app")
        self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertTrue(os.path.isfile(self.claim_state_file("w1")),
                        "setup: claim did not write state file")
        before = self.comment_count(7)

        r = self.run_hook(HOOK, EVENT)
        self.assertEqual(r.rc, 0, "hook must never block session exit (exit 0)")

        self.assertFalse(self.has_label(7, "in-progress"), "hook did not drop in-progress")
        self.assertFalse(os.path.isfile(self.claim_state_file("w1")),
                         "hook did not delete the state file")
        self.assertIn('<!-- kraken {"type":"released","worker":"w1","reason":"session ended"} -->',
                      self.last_comment(7),
                      "hook did not post the released marker via kraken.py release")
        self.assertGreater(self.comment_count(7), before, "hook posted no release comment")

        # The released task is claimable again — end to end.
        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 0, "task re-claimable after the hook released it")

        # --- no state file: strict no-op (no writes at all) -----------------
        import shutil
        shutil.rmtree(self.kraken_state_dir, ignore_errors=True)
        self.mk_issue(8, "untouched task", "kraken-task", "project:app", "in-progress")
        self.mk_comment(8, '<!-- kraken {"type":"claim","worker":"someone-else"} -->')
        before8 = self.comment_count(8)
        r = self.run_hook(HOOK, EVENT)
        self.assertEqual(r.rc, 0, "no-op hook still exits 0")
        self.assertTrue(self.has_label(8, "in-progress"), "no-op hook wrongly removed a label")
        self.assertEqual(self.comment_count(8), before8, "no-op hook wrongly posted a comment")

        # --- best-effort: a failing release never fails the hook ------------
        self.mk_issue(9, "release-fails task", "kraken-task", "project:app")
        self.kraken("claim", "OWNER/tasks", 9, "w3")
        self.assertTrue(os.path.isfile(self.claim_state_file("w3")),
                        "setup: claim did not write state file for w3")
        r = self.run_hook(HOOK, EVENT, env={"GH_STUB_FAIL": "."})
        self.assertEqual(r.rc, 0, "hook stays exit 0 even when kraken.py release fails")


if __name__ == "__main__":
    unittest.main()
