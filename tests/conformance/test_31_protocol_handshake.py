#!/usr/bin/env python3
"""Drift handshake conformance: before its first claim, a drain reads the
coordination repo's vendored `.github/kraken.py` and refuses to drain when it
differs from this worker's bundled copy — or when that file cannot be read (fail
closed) — naming `init --upgrade` as the fix. A byte-identical vendored copy
drains normally.

This turns the silent asset drift `init --upgrade` repairs into a loud,
actionable refusal, proven against the gh stub with no LLM.
"""
import os
import unittest

from harness import KrakenConformanceTest, KRAKEN


class ProtocolHandshakeTests(KrakenConformanceTest):
    def _seed_startable(self):
        # A real, claimable task, so a failure to claim is the handshake's doing
        # and not an empty queue.
        self.mk_issue(7, "ready task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it")

    def _assert_no_claim(self):
        self.assertFalse(self.has_label(7, "in-progress"),
                         "a refused drain still claimed the task")
        self.assertFalse(self.claim_ref_exists(7),
                         "a refused drain still created a claim ref")
        self.assertEqual(self.comment_count(7), 0,
                         "a refused drain still commented on the task")

    def test_refuses_on_drift(self):
        # The coordination repo vendors a stale, drifted kraken.py (bundled bytes
        # plus a trailing marker) -> its bytes differ from this worker's -> refuse.
        self._seed_startable()
        with open(KRAKEN, "r", encoding="utf-8") as f:
            text = f.read()
        drifted = os.path.join(self.state, "drifted-kraken.py")
        self._write(drifted, text + "\n# stale vendored copy (drifted)\n")
        self.mk_content(".github/kraken.py", drifted)
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 12, "drift must refuse with EXIT_PROTOCOL_MISMATCH")
        self.assertIn("differs", r.out, "refusal did not name the drift")
        self.assertIn("init --upgrade", r.out, "refusal did not point at init --upgrade")
        self._assert_no_claim()

    def test_refuses_when_vendored_file_absent_fail_closed(self):
        # No vendored kraken.py at all -> the drift cannot be verified -> refuse
        # (fail closed) rather than draining blind.
        self._seed_startable()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 12, "an unverifiable vendored asset must refuse (fail closed)")
        self.assertIn("cannot verify", r.out, "fail-closed refusal not surfaced")
        self.assertIn("init --upgrade", r.out, "refusal did not point at init --upgrade")
        self._assert_no_claim()

    def test_matching_content_drains_normally(self):
        # The coordination repo vendors the exact bundled kraken.py -> in sync ->
        # the drain proceeds and claims the task.
        self._seed_startable()
        self.mk_content(".github/kraken.py", KRAKEN)
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "matching content must drain normally")
        self.assertIn("claim-next: claimed issue=7 worker=w1", r.out.split("\n"),
                      "in-sync drain did not claim the startable task")
        self.assertTrue(self.has_label(7, "in-progress"), "task not claimed")


if __name__ == "__main__":
    unittest.main()
