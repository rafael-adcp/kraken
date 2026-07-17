#!/usr/bin/env python3
"""Unit tests for kraken.py — the parts the gh-stub conformance suite cannot
exercise in isolation: the claim-window arbitration grammar (including the
review-bounce reset), machine-line parsing edge cases, and comment pagination
beyond 100 comments.

Stdlib only (unittest), no network, no gh. Run: python3 tests/unit/test_kraken.py
"""

import os
import sys
import unittest
from types import SimpleNamespace
from io import StringIO
from contextlib import redirect_stdout

# Import kraken.py from the plugin folder without installing anything.
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(HERE, "..", "..", "skills", "unleash")
sys.path.insert(0, os.path.abspath(SKILL_DIR))

import kraken  # noqa: E402


class ArbitrationTests(unittest.TestCase):
    """arbitrate_winner: the claim tiebreaker, isolated from any transport."""

    def test_first_claim_in_window_wins(self):
        lines = ["claimed-by: alice", "claimed-by: bob"]
        self.assertEqual(kraken.arbitrate_winner(lines), "alice")

    def test_no_claim_yields_empty(self):
        self.assertEqual(kraken.arbitrate_winner(["some prose", "released: x"]), "")

    def test_released_resets_window(self):
        # tired-worker released; the newcomer is now the first live claim.
        lines = ["claimed-by: tired-worker", "released: tired-worker", "claimed-by: fresh"]
        self.assertEqual(kraken.arbitrate_winner(lines), "fresh")

    def test_stale_claim_resets_window(self):
        lines = ["claimed-by: dead", "stale-claim: no activity for 7h", "claimed-by: reaper-heir"]
        self.assertEqual(kraken.arbitrate_winner(lines), "reaper-heir")

    def test_needs_decision_resets_window(self):
        lines = ["claimed-by: w1", "needs-decision: w1", "claimed-by: w2"]
        self.assertEqual(kraken.arbitrate_winner(lines), "w2")

    def test_delivered_is_a_review_bounce_reset(self):
        # THE review-bounce gap: without delivered: as a reset, the original
        # claimant would win every future arbitration and a bounced-back task
        # could never be re-claimed by anyone else.
        lines = ["claimed-by: w1", "delivered: w1", "claimed-by: w2"]
        self.assertEqual(kraken.arbitrate_winner(lines), "w2")

    def test_no_reset_keeps_original_owner(self):
        # A control: with no reset line, the first (rightful) owner keeps it even
        # if a newcomer's claim comment lands later.
        lines = ["claimed-by: rightful-owner", "claimed-by: interloper"]
        self.assertEqual(kraken.arbitrate_winner(lines), "rightful-owner")

    def test_heartbeat_does_not_reset(self):
        # heartbeat: is deliberately NOT a window reset — a worker heartbeating
        # must never make its own claim re-claimable.
        lines = ["claimed-by: w1", "heartbeat: w1", "claimed-by: w2"]
        self.assertEqual(kraken.arbitrate_winner(lines), "w1")

    def test_reset_after_claim_leaves_no_winner(self):
        # released as the last relevant line -> the window is empty again.
        lines = ["claimed-by: w1", "released: w1"]
        self.assertEqual(kraken.arbitrate_winner(lines), "")


class MachineLineParsingTests(unittest.TestCase):
    """Machine-line grammar edge cases: CR stripping, spacing, prefix bodies
    that only look like machine lines."""

    def test_crlf_is_stripped_from_claim(self):
        self.assertEqual(kraken.arbitrate_winner(["claimed-by: w1\r"]), "w1")

    def test_crlf_is_stripped_from_reset(self):
        lines = ["claimed-by: w1", "released: w1\r", "claimed-by: w2"]
        self.assertEqual(kraken.arbitrate_winner(lines), "w2")

    def test_leading_space_after_colon_trimmed(self):
        self.assertEqual(kraken.arbitrate_winner(["claimed-by:    spaced"]), "spaced")

    def test_no_space_after_colon(self):
        self.assertEqual(kraken.arbitrate_winner(["claimed-by:tight"]), "tight")

    def test_prose_mentioning_claimed_by_midline_is_ignored(self):
        # Only a line that STARTS with the prefix is a machine line.
        lines = ["I think claimed-by: nobody is wrong", "claimed-by: real"]
        self.assertEqual(kraken.arbitrate_winner(lines), "real")

    def test_multiline_comment_bodies_scan_per_line(self):
        # comment_bodies returns bodies split to lines; a disclaimer blockquote
        # above the machine line must not shadow it.
        lines = [
            "> \U0001f419 **Kraken worker `w1`** — automated comment...",
            "",
            "claimed-by: w1",
        ]
        self.assertEqual(kraken.arbitrate_winner(lines), "w1")


class CommentPaginationTests(unittest.TestCase):
    """comment_bodies must page past 100 comments — a long-lived task's claim
    window can scroll out of a single 100-comment page, and re-arbitration on a
    truncated history would let a stale claim win forever."""

    def setUp(self):
        self._orig_run_gh = kraken.run_gh
        self.calls = []

    def tearDown(self):
        kraken.run_gh = self._orig_run_gh

    def test_uses_paginated_rest_endpoint(self):
        def fake_run_gh(args):
            self.calls.append(args)
            return 0, ""
        kraken.run_gh = fake_run_gh

        kraken.comment_bodies("OWNER/tasks", "42")
        self.assertEqual(len(self.calls), 1)
        args = self.calls[0]
        self.assertEqual(args[0], "api")
        self.assertIn("repos/OWNER/tasks/issues/42/comments", args)
        self.assertIn("--paginate", args)

    def test_returns_all_bodies_beyond_one_hundred(self):
        # Simulate the transport returning 150 comment bodies (what --paginate
        # yields once every page is walked). The 130th is the live claim.
        bodies = [f"comment number {i}" for i in range(150)]
        bodies[129] = "claimed-by: winner-past-page-one"

        def fake_run_gh(args):
            return 0, "\n".join(bodies)
        kraken.run_gh = fake_run_gh

        result = kraken.comment_bodies("OWNER/tasks", "42")
        self.assertEqual(len(result), 150)
        # And the arbitration reads the claim that lives past the 100 boundary —
        # it would be invisible under a 100-comment truncation.
        self.assertEqual(kraken.arbitrate_winner(result), "winner-past-page-one")

    def test_transport_failure_returns_none(self):
        def fake_run_gh(args):
            return 1, ""
        kraken.run_gh = fake_run_gh
        self.assertIsNone(kraken.comment_bodies("OWNER/tasks", "42"))

    def test_reset_past_page_boundary_still_resets(self):
        # A reset at position 120 must still clear a claim from position 5, even
        # when both are far apart across pages.
        bodies = [f"noise {i}" for i in range(150)]
        bodies[5] = "claimed-by: dead"
        bodies[120] = "released: dead"
        bodies[140] = "claimed-by: heir"

        def fake_run_gh(args):
            return 0, "\n".join(bodies)
        kraken.run_gh = fake_run_gh

        result = kraken.comment_bodies("OWNER/tasks", "42")
        self.assertEqual(kraken.arbitrate_winner(result), "heir")


class ClaimNextIterationTests(unittest.TestCase):
    """cmd_claim_next's loop, isolated from any transport: the classification
    (classify_queue) and the per-candidate claim (_claim_once) are both mocked,
    so only the iteration logic — skip-on-held, skip-on-lost, forward-only,
    stop-on-transport, honest-empty — is under test."""

    def setUp(self):
        self._orig_classify = kraken.classify_queue
        self._orig_claim_once = kraken._claim_once
        self.attempted = []  # issue numbers _claim_once was actually called on

    def tearDown(self):
        kraken.classify_queue = self._orig_classify
        kraken._claim_once = self._orig_claim_once

    def _run(self, rows, claim_results, json_mode=False):
        """rows: what classify_queue returns (or None). claim_results: dict
        {issue_number: exit_code} the mocked _claim_once replays. Returns
        (exit_code, stdout)."""
        kraken.classify_queue = lambda repo, project, include_body=False: rows

        def fake_claim_once(repo, issue, worker):
            self.attempted.append(issue)
            return claim_results[issue]
        kraken._claim_once = fake_claim_once

        args = SimpleNamespace(repo="OWNER/tasks", project="app",
                               worker="w1", json=json_mode)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.cmd_claim_next(args)
        return rc, buf.getvalue()

    def test_claims_first_startable(self):
        rows = [(7, "oldest", "t1", "startable", "body-7")]
        rc, out = self._run(rows, {7: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7])
        self.assertIn("claim-next: claimed issue=7 worker=w1", out)
        self.assertIn("7\toldest", out)
        self.assertIn("body-7", out)

    def test_skips_held_rows_without_attempting_them(self):
        # A held candidate is never even offered to _claim_once — the guard
        # cost is spent once in the listing, not re-paid per row.
        rows = [
            (5, "held one", "t1", "held", "b5"),
            (7, "startable", "t2", "startable", "b7"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7])  # 5 skipped, never attempted

    def test_skip_on_lost_tiebreaker_moves_forward_never_back(self):
        # THE §5 invariant: a lost tiebreaker on the oldest candidate moves on
        # to the next — it must never retry the issue it just lost.
        rows = [
            (7, "lost this", "t1", "startable", "b7"),
            (9, "win this", "t2", "startable", "b9"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_LOST, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7, 9])       # forward order
        self.assertEqual(self.attempted.count(7), 1)   # 7 never retried
        self.assertIn("claim-next: claimed issue=9 worker=w1", out)

    def test_skip_on_held_since_listing_moves_to_next(self):
        # A candidate that acquired a held label between listing and claim
        # (exit 11) is skipped, and the next candidate is tried.
        rows = [
            (7, "now held", "t1", "startable", "b7"),
            (9, "clear", "t2", "startable", "b9"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_NOT_CLEAR, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7, 9])

    def test_empty_queue_is_honest_none(self):
        rc, out = self._run([], {})
        self.assertEqual(rc, kraken.EXIT_NONE)
        self.assertEqual(self.attempted, [])
        self.assertIn("claim-next: none project:app", out)

    def test_all_candidates_lost_or_held_is_none(self):
        rows = [
            (7, "a", "t1", "startable", "b7"),
            (9, "b", "t2", "startable", "b9"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_LOST, 9: kraken.EXIT_NOT_CLEAR})
        self.assertEqual(rc, kraken.EXIT_NONE)
        self.assertEqual(self.attempted, [7, 9])

    def test_transport_during_claim_stops_immediately(self):
        # A gh/network fault leaves the claim ambiguous: claim-next must stop
        # (exit 20), never wander to another candidate with a write half-landed.
        rows = [
            (7, "ambiguous", "t1", "startable", "b7"),
            (9, "untouched", "t2", "startable", "b9"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_TRANSPORT, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertEqual(self.attempted, [7])  # 9 never reached
        self.assertIn("state unknown", out)

    def test_transport_during_listing_is_twenty(self):
        rc, out = self._run(None, {})
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertEqual(self.attempted, [])
        self.assertIn("claim-next: gh-failure stage=list", out)

    def test_json_mode_emits_structured_win(self):
        rows = [(7, "the title", "t1", "startable", "### Goal\ndo it")]
        rc, out = self._run(rows, {7: kraken.EXIT_OK}, json_mode=True)
        self.assertEqual(rc, kraken.EXIT_OK)
        import json as _json
        payload = _json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload, {"issue": 7, "title": "the title",
                                   "body": "### Goal\ndo it"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
