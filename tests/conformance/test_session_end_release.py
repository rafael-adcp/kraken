#!/usr/bin/env python3
"""The SessionEnd auto-release hook: when a session ends while it still holds a
claim, the bundled hook runs `kraken.py release` so the task requeues in
seconds. It frees ONLY the ending session's own claims (issue #173): SessionEnd
fires for every Claude Code session on the host, so a claim recorded by another
session — or recording no session at all — is left to its lease TTL. With no
state file it is a strict no-op. Best-effort: a failed release never blocks
session exit (always exits 0)."""
import json
import os
import shutil
import unittest

from harness import KrakenConformanceTest

HOOK = "hooks/session-end-release.sh"


def event(session_id=None):
    """A SessionEnd event as Claude Code sends it on stdin."""
    payload = {"hook_event_name": "SessionEnd", "reason": "exit"}
    if session_id is not None:
        payload["session_id"] = session_id
    return json.dumps(payload)


def in_session(session_id):
    """The environment Claude Code gives a Bash tool call inside that session."""
    return {"CLAUDE_CODE_SESSION_ID": session_id}


class SessionEndReleaseTests(KrakenConformanceTest):
    def test_session_end_release(self):
        # --- session ends WITH its own claim open: kraken.py release drives requeue
        self.mk_issue(7, "abandoned-on-exit task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "w1", env=in_session("sess-A"))
        self.assertTrue(os.path.isfile(self.claim_state_file("w1")),
                        "setup: claim did not write state file")
        before = self.comment_count(7)

        r = self.run_hook(HOOK, event("sess-A"))
        self.assertEqual(r.rc, 0, "hook must never block session exit (exit 0)")

        self.assertFalse(self.has_label(7, "in-progress"), "hook did not drop in-progress")
        self.assertFalse(os.path.isfile(self.claim_state_file("w1")),
                         "hook did not delete the state file")
        self.assertIn('<!-- kraken {"type":"released","worker":"w1","reason":"session ended"} -->',
                      self.last_comment(7),
                      "hook did not post the released marker via kraken.py release")
        self.assertGreater(self.comment_count(7), before, "hook posted no release comment")

        # The released task is claimable again — end to end.
        r = self.kraken("claim", "acme/tasks", 7, "w2")
        self.assertEqual(r.rc, 0, "task re-claimable after the hook released it")

        # --- no state file: strict no-op (no writes at all) -----------------
        shutil.rmtree(self.kraken_state_dir, ignore_errors=True)
        self.mk_issue(8, "untouched task", "kraken-task", "project:app", "in-progress")
        self.mk_comment(8, '<!-- kraken {"type":"claim","worker":"someone-else"} -->')
        before8 = self.comment_count(8)
        r = self.run_hook(HOOK, event("sess-A"))
        self.assertEqual(r.rc, 0, "no-op hook still exits 0")
        self.assertTrue(self.has_label(8, "in-progress"), "no-op hook wrongly removed a label")
        self.assertEqual(self.comment_count(8), before8, "no-op hook wrongly posted a comment")

        # --- best-effort: a failing release never fails the hook ------------
        self.mk_issue(9, "release-fails task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 9, "w3", env=in_session("sess-C"))
        self.assertTrue(os.path.isfile(self.claim_state_file("w3")),
                        "setup: claim did not write state file for w3")
        r = self.run_hook(HOOK, event("sess-C"), env={"GH_STUB_FAIL": "."})
        self.assertEqual(r.rc, 0, "hook stays exit 0 even when kraken.py release fails")

    def assert_claim_survives(self, issue, worker, hook_event, why):
        before = self.comment_count(issue)
        r = self.run_hook(HOOK, hook_event)
        self.assertEqual(r.rc, 0, "hook must never block session exit (exit 0)")
        self.assertTrue(self.has_label(issue, "in-progress"), why)
        self.assertTrue(os.path.isfile(self.claim_state_file(worker)),
                        "hook deleted a state file it does not own: " + why)
        self.assertEqual(self.comment_count(issue), before, "hook posted a comment: " + why)

    def test_other_session_ending_leaves_a_live_claim_alone(self):
        # Issue #173: worker mac-2 is mid-task; an unrelated 30-second
        # `claude -p` on the same machine ends and fires SessionEnd.
        self.mk_issue(7, "live task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "mac-2", env=in_session("worker-session"))
        self.assert_claim_survives(7, "mac-2", event("unrelated-session"),
                                   "another session's end released a live claim")

        # The worker's own session ending still releases it.
        self.run_hook(HOOK, event("worker-session"))
        self.assertFalse(self.has_label(7, "in-progress"),
                         "the owning session's end did not release its claim")

    def test_only_the_ending_sessions_claim_among_several(self):
        self.mk_issue(7, "task of session A", "kraken-task", "project:app")
        self.mk_issue(8, "task of session B", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "wa", env=in_session("sess-A"))
        self.kraken("claim", "acme/tasks", 8, "wb", env=in_session("sess-B"))

        self.run_hook(HOOK, event("sess-A"))

        self.assertFalse(self.has_label(7, "in-progress"), "sess-A's claim was not released")
        self.assertTrue(self.has_label(8, "in-progress"), "sess-B's live claim was released")
        self.assertTrue(os.path.isfile(self.claim_state_file("wb")),
                        "sess-B's state file was removed")

    def test_copilot_session_end_releases_its_own_claim(self):
        # Copilot CLI loads the plugin's hooks.json and fires SessionEnd with the
        # Claude-shaped payload; the claim names the session by its
        # COPILOT_AGENT_SESSION_ID, so the ending Copilot session frees it.
        self.mk_issue(7, "copilot-held task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "copilot-1",
                    env={"COPILOT_CLI": "1", "COPILOT_AGENT_SESSION_ID": "cop-A"})
        self.assert_claim_survives(7, "copilot-1", event("some-claude-session"),
                                   "another session's end released a Copilot claim")

        self.run_hook(HOOK, event("cop-A"))
        self.assertFalse(self.has_label(7, "in-progress"),
                         "the Copilot session's end did not release its claim")

    def test_claim_recording_no_session_is_left_to_the_lease(self):
        # A claim made outside any agent session (a bare shell) names no
        # session, so no SessionEnd can prove it owns it — the lease TTL (and
        # the Copilot loop's own release-on-exit) cover it instead.
        self.mk_issue(7, "copilot-held task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "copilot-1")
        self.assert_claim_survives(7, "copilot-1", event("some-claude-session"),
                                   "a Claude session's end released a claim it never made")

    def test_event_naming_no_session_releases_nothing(self):
        self.mk_issue(7, "live task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "w1", env=in_session("sess-A"))
        self.assert_claim_survives(7, "w1", event(None),
                                   "an event with no session_id released a claim")
        self.assert_claim_survives(7, "w1", "not json at all",
                                   "an unparseable event released a claim")


if __name__ == "__main__":
    unittest.main()
