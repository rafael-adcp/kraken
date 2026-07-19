#!/usr/bin/env python3
"""Protocol-version handshake conformance: before its first claim, a drain reads
the coordination repo's vendored `.github/kraken.py` PROTOCOL_VERSION and refuses
to drain on any mismatch — or when that file cannot be read (fail closed) —
naming `init --upgrade` as the fix. A matching version drains normally.

This turns the silent asset drift `init --upgrade` repairs into a loud,
actionable refusal, proven against the gh stub with no LLM.
"""
import os
import unittest

from harness import KrakenConformanceTest, KRAKEN

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "protocol3")


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

    def test_refuses_on_version_mismatch(self):
        # The coordination repo vendors the protocol/3 (v0.4.0) kraken.py; this
        # worker speaks protocol/4 -> refuse.
        self._seed_startable()
        self.mk_content(".github/kraken.py", os.path.join(FIXTURES, "kraken.py.txt"))
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 12, "version mismatch must refuse with EXIT_PROTOCOL_MISMATCH")
        self.assertIn("mismatch", r.out, "refusal did not name a version mismatch")
        self.assertIn("init --upgrade", r.out, "refusal did not point at init --upgrade")
        self._assert_no_claim()

    def test_refuses_when_vendored_file_absent_fail_closed(self):
        # No vendored kraken.py at all -> the version cannot be verified -> refuse
        # (fail closed) rather than draining blind.
        self._seed_startable()
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 12, "an unverifiable version must refuse (fail closed)")
        self.assertIn("cannot verify", r.out, "fail-closed refusal not surfaced")
        self.assertIn("init --upgrade", r.out, "refusal did not point at init --upgrade")
        self._assert_no_claim()

    def test_matching_version_drains_normally(self):
        # The coordination repo vendors the current bundled kraken.py -> versions
        # match -> the drain proceeds and claims the task.
        self._seed_startable()
        self.mk_content(".github/kraken.py", KRAKEN)
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "matching versions must drain normally")
        self.assertIn("claim-next: claimed issue=7 worker=w1", r.out.split("\n"),
                      "matching-version drain did not claim the startable task")
        self.assertTrue(self.has_label(7, "in-progress"), "task not claimed")


if __name__ == "__main__":
    unittest.main()
