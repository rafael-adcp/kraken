#!/usr/bin/env python3
"""The claim state file lifecycle: kraken.py claim writes
${KRAKEN_STATE_DIR}/claim-<worker>.json on a won claim (exit 0), and every
terminal worker transition — deliver, escalate, release — removes it."""
import json
import os
import unittest

from harness import KrakenConformanceTest


class ClaimStateFileTests(KrakenConformanceTest):
    def test_claim_state_file_lifecycle(self):
        state_file = self.claim_state_file("w1")

        # --- claim writes the state file on exit 0 --------------------------
        self.mk_issue(7, "a task", "kraken-task", "project:app")
        r = self.kraken("claim", "acme/tasks", 7, "w1")
        self.assertEqual(r.rc, 0, "clean claim exit")
        self.assertTrue(os.path.isfile(state_file), "claim did not write the state file")
        with open(state_file, encoding="utf-8") as f:
            content = f.read()
        for field in ('"repo"', '"issue"', '"worker"', "acme/tasks", "w1"):
            self.assertIn(field, content, "state file missing %s" % field)
        self.assertNotIn('"session"', content,
                         "a claim made outside Claude Code must record no session")

        # --- a lost/held claim writes NO new state file (leaves w1's intact) --
        self.mk_issue(8, "held task", "kraken-task", "project:app", "in-progress")
        r = self.kraken("claim", "acme/tasks", 8, "w1")
        self.assertEqual(r.rc, 11, "held claim exit")
        self.assertTrue(os.path.isfile(state_file),
                        "guard/skip wrongly removed an unrelated claim state file")

        # --- release removes it ---------------------------------------------
        r = self.kraken("release", "acme/tasks", 7, "w1", "backing out")
        self.assertEqual(r.rc, 0, "release exit")
        self.assertFalse(os.path.isfile(state_file), "release did not remove the state file")

        # --- escalate removes it --------------------------------------------
        self.mk_issue(9, "blocked task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 9, "w1")
        esf = self.claim_state_file("w1")
        self.assertTrue(os.path.isfile(esf), "re-claim for escalate test did not write state file")
        q = os.path.join(self.state, "q.md")
        self._write(q, "which way?\n")
        r = self.kraken("escalate", "acme/tasks", 9, "w1", q)
        self.assertEqual(r.rc, 0, "escalate exit")
        self.assertFalse(os.path.isfile(esf), "escalate did not remove the state file")

        # --- deliver removes it ---------------------------------------------
        self.mk_issue(10, "shipped task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 10, "w1")
        dsf = self.claim_state_file("w1")
        self.assertTrue(os.path.isfile(dsf), "re-claim for deliver test did not write state file")
        rf = os.path.join(self.state, "r.md")
        self._write(rf, "done, validated\n")
        r = self.kraken("deliver", "acme/tasks", 10, "w1", rf, "https://x/pr/1")
        self.assertEqual(r.rc, 0, "deliver exit")
        self.assertFalse(os.path.isfile(dsf), "deliver did not remove the state file")

    def test_claim_records_the_claude_code_session(self):
        # The SessionEnd hook frees only the ending session's own claims (#173),
        # so a claim made inside Claude Code records the session that made it.
        self.mk_issue(7, "a task", "kraken-task", "project:app")
        r = self.kraken("claim", "acme/tasks", 7, "w1",
                        env={"CLAUDE_CODE_SESSION_ID": "sess-123"})
        self.assertEqual(r.rc, 0, "clean claim exit")
        with open(self.claim_state_file("w1"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record.get("session"), "sess-123",
                         "claim did not record the Claude Code session")

    def claimed_session(self, env):
        self.mk_issue(7, "a task", "kraken-task", "project:app")
        r = self.kraken("claim", "acme/tasks", 7, "w1", env=env)
        self.assertEqual(r.rc, 0, "clean claim exit")
        with open(self.claim_state_file("w1"), encoding="utf-8") as f:
            return json.load(f).get("session")

    def test_claim_records_the_copilot_session(self):
        # Copilot CLI fires the same SessionEnd hook with its own session id,
        # which its shell tool exposes as COPILOT_AGENT_SESSION_ID.
        self.assertEqual(
            self.claimed_session({"COPILOT_CLI": "1",
                                  "COPILOT_AGENT_SESSION_ID": "cop-9"}),
            "cop-9", "claim did not record the Copilot CLI session")

    def test_copilot_session_wins_over_an_inherited_claude_one(self):
        # A `copilot` launched from a Claude Code session inherits its
        # CLAUDE_CODE_SESSION_ID; recording that would let the Claude session's
        # end release the Copilot session's claim.
        self.assertEqual(
            self.claimed_session({"COPILOT_CLI": "1",
                                  "COPILOT_AGENT_SESSION_ID": "cop-9",
                                  "CLAUDE_CODE_SESSION_ID": "claude-parent"}),
            "cop-9", "claim recorded the inherited Claude Code session")

    def test_copilot_without_a_session_id_records_none(self):
        # Inside Copilot an inherited Claude id is never a fallback.
        self.assertIsNone(
            self.claimed_session({"COPILOT_CLI": "1",
                                  "CLAUDE_CODE_SESSION_ID": "claude-parent"}),
            "claim recorded a session Copilot did not name")


if __name__ == "__main__":
    unittest.main()
