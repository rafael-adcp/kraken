#!/usr/bin/env python3
"""The StopFailure auto-release hook (matcher rate_limit): a usage limit kills
the turn but not the session, so SessionEnd never fires — this hook releases the
held claim and stamps the wake-retry flag `kraken.py watch` reads. Best-effort:
always exits 0, and the flag must be stamped regardless."""
import os
import shutil
import unittest

from harness import KrakenConformanceTest

HOOK = "hooks/stop-failure-release.sh"
EVENT = '{"hook_event_name":"StopFailure","error_type":"rate_limit"}'


class StopFailureReleaseTests(KrakenConformanceTest):
    def _wake_retry(self):
        return os.path.join(self.kraken_state_dir, "wake-retry")

    def test_stop_failure_release(self):
        # --- limit hits WITH a claim open: release + flag --------------------
        self.mk_issue(7, "limit-struck task", "kraken-task", "project:app")
        self.kraken("claim", "OWNER/tasks", 7, "w1")
        self.assertTrue(os.path.isfile(self.claim_state_file("w1")), "setup: claim did not write state file")
        before = self.comment_count(7)

        r = self.run_hook(HOOK, EVENT)
        self.assertEqual(r.rc, 0, "hook must always exit 0")
        self.assertFalse(self.has_label(7, "in-progress"), "hook did not drop in-progress")
        self.assertFalse(os.path.isfile(self.claim_state_file("w1")), "hook did not delete the state file")
        self.assertIn('<!-- kraken {"type":"released","worker":"w1","reason":"usage limit"} -->',
                      self.last_comment(7),
                      "hook did not post the released marker (reason: usage limit)")
        self.assertGreater(self.comment_count(7), before, "hook posted no release comment")
        self.assertTrue(os.path.isfile(self._wake_retry()), "hook did not stamp the wake-retry flag")

        # The released task is claimable again — end to end.
        r = self.kraken("claim", "OWNER/tasks", 7, "w2")
        self.assertEqual(r.rc, 0, "task re-claimable after the hook released it")

        # --- no claim open: no queue writes, but the flag still lands --------
        shutil.rmtree(self.kraken_state_dir, ignore_errors=True)
        self.mk_issue(8, "untouched task", "kraken-task", "project:app", "in-progress")
        self.mk_comment(8, '<!-- kraken {"type":"claim","worker":"someone-else"} -->')
        before8 = self.comment_count(8)
        r = self.run_hook(HOOK, EVENT)
        self.assertEqual(r.rc, 0, "claimless hook still exits 0")
        self.assertTrue(self.has_label(8, "in-progress"), "claimless hook wrongly removed a label")
        self.assertEqual(self.comment_count(8), before8, "claimless hook wrongly posted a comment")
        self.assertTrue(os.path.isfile(self._wake_retry()), "claimless hook did not stamp the wake-retry flag")

        # --- best-effort: a failing release never fails the hook, flag lands --
        self.mk_issue(9, "release-fails task", "kraken-task", "project:app")
        self.kraken("claim", "OWNER/tasks", 9, "w3")
        self.assertTrue(os.path.isfile(self.claim_state_file("w3")),
                        "setup: claim did not write state file for w3")
        os.remove(self._wake_retry())
        r = self.run_hook(HOOK, EVENT, env={"GH_STUB_FAIL": "."})
        self.assertEqual(r.rc, 0, "hook stays exit 0 even when kraken.py release fails")
        self.assertTrue(os.path.isfile(self._wake_retry()), "flag must land even when the release fails")


if __name__ == "__main__":
    unittest.main()
