#!/usr/bin/env python3
"""Hybrid drift handshake conformance: before its first claim, a drain reads the
coordination repo's vendored `.github/kraken.py` and gates on the WIRE PROTOCOL,
not the exact bytes.

  - unreadable/absent, or byte-different with an unparseable or MISMATCHED
    PROTOCOL_VERSION -> refuse the drain (exit 12, fail closed), naming
    `init --upgrade`;
  - byte-different but the SAME PROTOCOL_VERSION -> a loud stderr warning, drain
    proceeds (patch releases no longer brick the fleet);
  - byte-identical -> silent, drains normally.

Proven against the gh stub with no LLM.
"""
import os
import re
import unittest

from harness import KrakenConformanceTest, KRAKEN


class ProtocolHandshakeTests(KrakenConformanceTest):
    def _seed_startable(self):
        # A real, claimable task, so a failure to claim is the handshake's doing
        # and not an empty queue.
        self.mk_issue(7, "ready task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it")

    def _bundled_text(self):
        with open(KRAKEN, "r", encoding="utf-8") as f:
            return f.read()

    def _vendor(self, text, name):
        # Write `text` to a temp file and vendor it as .github/kraken.py.
        path = os.path.join(self.state, name)
        self._write(path, text)
        self.mk_content(".github/kraken.py", path)

    def _assert_no_claim(self):
        self.assertFalse(self.has_label(7, "in-progress"),
                         "a refused drain still claimed the task")
        self.assertFalse(self.claim_ref_exists(7),
                         "a refused drain still created a claim ref")
        self.assertEqual(self.comment_count(7), 0,
                         "a refused drain still commented on the task")

    def test_warns_on_same_version_byte_drift_and_proceeds(self):
        # Vendored = bundled bytes + a trailing marker: byte-different but the
        # SAME PROTOCOL_VERSION -> warn on stderr, drain proceeds.
        self._seed_startable()
        self._vendor(self._bundled_text() + "\n# stale vendored copy (drifted)\n",
                     "same-version-drift.py")
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "same-version byte drift must proceed, not refuse")
        self.assertIn("claim-next: claimed issue=7 worker=w1", r.out.split("\n"),
                      "warned drain did not claim the startable task")
        self.assertTrue(self.has_label(7, "in-progress"), "task not claimed")
        self.assertIn("drift handshake (warning)", r.err,
                      "same-version drift did not warn on stderr")
        self.assertIn("proceeding", r.err, "warning did not say it was proceeding")

    def test_refuses_on_protocol_version_mismatch(self):
        # Vendored declares a DIFFERENT PROTOCOL_VERSION -> the wire contracts
        # disagree -> refuse (exit 12).
        self._seed_startable()
        text = re.sub(r"(?m)^PROTOCOL_VERSION\s*=\s*\d+",
                      "PROTOCOL_VERSION = 999", self._bundled_text())
        self._vendor(text, "version-mismatch.py")
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 12, "a protocol-version mismatch must refuse (exit 12)")
        self.assertIn("kraken-protocol/999", r.out, "refusal did not name the vendored version")
        self.assertIn("init --upgrade", r.out, "refusal did not point at init --upgrade")
        self._assert_no_claim()

    def test_refuses_when_version_unparseable_fail_closed(self):
        # Byte-different and no readable PROTOCOL_VERSION -> the protocol cannot
        # be verified -> refuse (fail closed).
        self._seed_startable()
        self._vendor("# a stale, drifted kraken.py with no version line\n",
                     "unparseable.py")
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 12, "an unparseable vendored version must refuse (fail closed)")
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
        # the drain proceeds silently and claims the task.
        self._seed_startable()
        self.mk_content(".github/kraken.py", KRAKEN)
        r = self.kraken("claim-next", "OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "matching content must drain normally")
        self.assertIn("claim-next: claimed issue=7 worker=w1", r.out.split("\n"),
                      "in-sync drain did not claim the startable task")
        self.assertTrue(self.has_label(7, "in-progress"), "task not claimed")
        self.assertNotIn("drift handshake", r.err,
                         "a byte-identical vendored copy must be silent")


if __name__ == "__main__":
    unittest.main()
