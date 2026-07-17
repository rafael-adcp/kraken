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


class MarkerTests(unittest.TestCase):
    """protocol/2 hidden markers: make_marker/parse_marker round-trip, decoding
    edge cases, and arbitration over markers — the successor to the protocol/1
    line grammar."""

    def test_make_marker_is_compact_ascii_json(self):
        m = kraken.make_marker({"type": "claim", "worker": "env-1"})
        self.assertEqual(m, '<!-- kraken {"type":"claim","worker":"env-1"} -->')

    def test_make_marker_round_trips_through_parse(self):
        payload = {"type": "delivered", "worker": "w1", "pr": "https://x/pull/9"}
        self.assertEqual(kraken.parse_marker(kraken.make_marker(payload)), payload)

    def test_parse_marker_ignores_a_line_without_a_marker(self):
        self.assertIsNone(kraken.parse_marker("just some prose"))

    def test_parse_marker_rejects_undecodable_json(self):
        self.assertIsNone(kraken.parse_marker("<!-- kraken {not json} -->"))

    def test_parse_marker_rejects_a_payload_without_a_string_type(self):
        self.assertIsNone(kraken.parse_marker('<!-- kraken {"worker":"w"} -->'))
        self.assertIsNone(kraken.parse_marker('<!-- kraken {"type":5} -->'))

    def test_parse_marker_tolerates_surrounding_prose(self):
        line = 'context here <!-- kraken {"type":"claim","worker":"w"} --> trailing'
        self.assertEqual(kraken.parse_marker(line),
                         {"type": "claim", "worker": "w"})

    def test_first_claim_marker_in_window_wins(self):
        lines = [kraken.make_marker({"type": "claim", "worker": "alice"}),
                 kraken.make_marker({"type": "claim", "worker": "bob"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "alice")

    def test_marker_reset_clears_marker_claim(self):
        lines = [kraken.make_marker({"type": "claim", "worker": "w1"}),
                 kraken.make_marker({"type": "delivered", "worker": "w1"}),
                 kraken.make_marker({"type": "claim", "worker": "w2"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "w2")

    def test_heartbeat_marker_does_not_reset(self):
        lines = [kraken.make_marker({"type": "claim", "worker": "w1"}),
                 kraken.make_marker({"type": "heartbeat", "worker": "w1"}),
                 kraken.make_marker({"type": "claim", "worker": "w2"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "w1")

    def test_malformed_marker_never_arbitrates(self):
        # An undecodable marker must be ignored, not treated as a live claim.
        lines = ["<!-- kraken {broken -->",
                 kraken.make_marker({"type": "claim", "worker": "real"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "real")


class MigrationTests(unittest.TestCase):
    """The protocol/1 -> /2 migration contract (PROTOCOL.md §4): a consumer reads
    BOTH formats, so pre-existing legacy threads and mixed threads arbitrate."""

    def test_legacy_only_thread_arbitrates(self):
        lines = ["claimed-by: dead", "delivered: dead", "claimed-by: heir"]
        self.assertEqual(kraken.arbitrate_winner(lines), "heir")

    def test_marker_reset_clears_a_legacy_claim(self):
        # cross-format: a protocol/2 reset marker ends a protocol/1 claim window.
        lines = ["claimed-by: old",
                 kraken.make_marker({"type": "stale-claim", "reason": "7h"}),
                 kraken.make_marker({"type": "claim", "worker": "new"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "new")

    def test_legacy_reset_clears_a_marker_claim(self):
        # cross-format the other way: a protocol/1 released: line ends a /2 claim.
        lines = [kraken.make_marker({"type": "claim", "worker": "old"}),
                 "released: old",
                 kraken.make_marker({"type": "claim", "worker": "new"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "new")

    def test_legacy_claim_still_wins_when_first(self):
        lines = ["claimed-by: rightful",
                 kraken.make_marker({"type": "claim", "worker": "interloper"})]
        self.assertEqual(kraken.arbitrate_winner(lines), "rightful")

    def test_liveness_marker_recognized(self):
        # heartbeat_anchor / the reaper anchor must see claim/heartbeat markers,
        # but NOT reset markers (delivered/released already drop in-progress).
        self.assertTrue(
            kraken._is_machine_line(kraken.make_marker({"type": "claim", "worker": "w"})))
        self.assertTrue(
            kraken._is_machine_line(kraken.make_marker({"type": "heartbeat", "worker": "w"})))
        self.assertFalse(
            kraken._is_machine_line(kraken.make_marker({"type": "delivered", "worker": "w"})))

    def test_pr_url_parsed_from_delivered_marker(self):
        recs = [{"body": kraken.make_marker(
            {"type": "delivered", "worker": "w", "pr": "https://x/pull/42"}),
            "createdAt": "t"}]
        self.assertEqual(kraken.parse_pr_url(recs), "https://x/pull/42")

    def test_composed_comment_carries_disclaimer_prose_and_marker(self):
        body = kraken.compose_comment(
            "env-1", "Claimed this task.", {"type": "claim", "worker": "env-1"})
        self.assertTrue(body.startswith("> \U0001f419 **Kraken worker `env-1`**"))
        self.assertIn("Claimed this task.", body)
        # The marker sits on its own line so a flat per-line scan finds it.
        self.assertEqual(kraken.arbitrate_winner(body.split("\n")), "env-1")


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

    def test_comment_records_uses_same_paginated_endpoint(self):
        # status' heartbeat/PR-link path reads timestamps off the SAME paginated
        # REST comments endpoint, so its anchor is never truncated at 100 either.
        calls = []

        def fake_run_gh(args):
            calls.append(args)
            return 0, ""
        kraken.run_gh = fake_run_gh
        kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "api")
        self.assertIn("repos/OWNER/tasks/issues/42/comments", calls[0])
        self.assertIn("--paginate", calls[0])

    def test_comment_records_returns_records_past_one_hundred(self):
        # 150 comment records (compact one-per-line, what gh --jq streams); the
        # 130th carries the live machine line, invisible under a 100 truncation.
        recs = [{"body": f"c {i}", "createdAt": f"2026-07-01T00:{i % 60:02d}:00Z"}
                for i in range(150)]
        recs[129] = {"body": "heartbeat: late", "createdAt": "2026-07-09T00:00:00Z"}
        out = "\n".join(kraken.json.dumps(r) for r in recs)

        def fake_run_gh(args):
            return 0, out
        kraken.run_gh = fake_run_gh
        result = kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(result), 150)
        self.assertEqual(kraken.heartbeat_anchor(result), "2026-07-09T00:00:00Z")

    def test_comment_records_parses_pretty_printed_stream(self):
        # The conformance stub pretty-prints each object across lines; the
        # object-by-object decoder must handle that as well as compact output.
        pretty = ('{\n  "body": "claimed-by: w1",\n  "createdAt": "2026-07-01T00:00:00Z"\n}\n'
                  '{\n  "body": "heartbeat: w1",\n  "createdAt": "2026-07-01T05:00:00Z"\n}\n')

        def fake_run_gh(args):
            return 0, pretty
        kraken.run_gh = fake_run_gh
        result = kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(result), 2)
        self.assertEqual(kraken.heartbeat_anchor(result), "2026-07-01T05:00:00Z")


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


class StatusHelperTests(unittest.TestCase):
    """The status console's pure helpers, isolated from any transport: the
    heartbeat anchor (machine-line-only, newest-wins), PR-URL parsing, worker
    resolution, and age formatting."""

    def _rec(self, body, created):
        return {"body": body, "createdAt": created}

    def test_anchor_is_newest_machine_line(self):
        recs = [
            self._rec("claimed-by: w1", "2026-07-01T00:00:00Z"),
            self._rec("heartbeat: w1", "2026-07-01T05:00:00Z"),
        ]
        self.assertEqual(kraken.heartbeat_anchor(recs), "2026-07-01T05:00:00Z")

    def test_anchor_ignores_operator_comments(self):
        # THE anchoring invariant (mirrors the reaper): a fresh operator comment
        # must NOT reset the clock — only worker machine lines anchor liveness.
        recs = [
            self._rec("> disclaimer\n\nclaimed-by: w1", "2026-07-01T00:00:00Z"),
            self._rec("any update? — the operator", "2026-07-01T09:00:00Z"),
        ]
        self.assertEqual(kraken.heartbeat_anchor(recs), "2026-07-01T00:00:00Z")

    def test_anchor_none_when_worker_never_spoke(self):
        recs = [self._rec("someone mislabeled this", "2026-07-01T00:00:00Z")]
        self.assertIsNone(kraken.heartbeat_anchor(recs))

    def test_anchor_finds_machine_line_inside_multiline_body(self):
        recs = [self._rec("> \U0001f419 worker note\n\nheartbeat: w1\n\nstill going",
                          "2026-07-01T02:00:00Z")]
        self.assertEqual(kraken.heartbeat_anchor(recs), "2026-07-01T02:00:00Z")

    def test_parse_pr_url_from_pr_machine_line(self):
        recs = [self._rec("delivered: w1\npr: https://github.com/o/r/pull/42\n\nbody",
                          "t")]
        self.assertEqual(kraken.parse_pr_url(recs),
                         "https://github.com/o/r/pull/42")

    def test_parse_pr_url_newest_wins(self):
        recs = [
            self._rec("pr: https://github.com/o/r/pull/1", "t1"),
            self._rec("delivered: w2\npr: https://github.com/o/r/pull/9", "t2"),
        ]
        self.assertEqual(kraken.parse_pr_url(recs),
                         "https://github.com/o/r/pull/9")

    def test_parse_pr_url_fallback_to_url_in_prose(self):
        recs = [self._rec("landed in https://github.com/o/r/pull/7 fyi", "t")]
        self.assertEqual(kraken.parse_pr_url(recs),
                         "https://github.com/o/r/pull/7")

    def test_parse_pr_url_none_when_absent(self):
        recs = [self._rec("delivered: w1\n\njust prose", "t")]
        self.assertIsNone(kraken.parse_pr_url(recs))

    def test_worker_resolves_via_arbitration(self):
        recs = [
            self._rec("claimed-by: dead", "t1"),
            self._rec("stale-claim: 7h", "t2"),
            self._rec("claimed-by: heir", "t3"),
        ]
        self.assertEqual(
            kraken.arbitrate_winner(kraken.flat_comment_lines(recs)), "heir")

    def test_parse_iso_roundtrip(self):
        self.assertEqual(kraken.parse_iso("2026-07-01T00:00:00Z"), 1782864000.0)
        self.assertIsNone(kraken.parse_iso("not-a-date"))
        self.assertIsNone(kraken.parse_iso(""))

    def test_format_age_buckets(self):
        self.assertEqual(kraken.format_age(0), "0s")
        self.assertEqual(kraken.format_age(42), "42s")
        self.assertEqual(kraken.format_age(12 * 60), "12m")
        self.assertEqual(kraken.format_age(3 * 3600), "3h")
        self.assertEqual(kraken.format_age(4 * 86400), "4d")
        self.assertEqual(kraken.format_age(None), "unknown")


class StatusComputeTests(unittest.TestCase):
    """compute_status: the whole report assembled from queue nodes and injected
    readers — grouping, project filter, orphan flagging, in-flight ages, and the
    transport-failure propagation, all with no gh."""

    NOW = kraken.parse_iso("2026-07-01T10:00:00Z")

    def _node(self, number, title, labels, created="2026-07-01T00:00:00Z"):
        return {
            "number": number, "title": title, "createdAt": created,
            "labels": {"nodes": [{"name": n} for n in labels]},
        }

    def _readers(self, comments=None, merged=None, projects=None):
        comments = comments or {}
        merged = merged or {}

        def comment_reader(repo, issue):
            return comments.get(issue, [])

        def pr_merged(url):
            return merged.get(url, False)

        def project_lister(repo):
            return projects if projects is not None else []

        return dict(comment_reader=comment_reader, pr_merged=pr_merged,
                    project_lister=project_lister)

    def test_groups_by_held_label(self):
        nodes = [
            self._node(88, "review", ["kraken-task", "project:app", "awaiting-merge"]),
            self._node(97, "decide", ["kraken-task", "project:app", "needs-decision"]),
            self._node(99, "running", ["kraken-task", "project:app", "in-progress"]),
            self._node(12, "queued", ["kraken-task", "project:app"]),
        ]
        comments = {
            88: [{"body": "delivered: w\npr: https://x/pull/1", "createdAt": "t"}],
            99: [{"body": "claimed-by: w1", "createdAt": "2026-07-01T09:00:00Z"}],
        }
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            **self._readers(comments=comments, projects=["app"]))
        self.assertEqual([r["number"] for r in report["review_queue"]], [88])
        self.assertEqual([r["number"] for r in report["decision_queue"]], [97])
        self.assertEqual([r["number"] for r in report["in_flight"]], [99])
        # A non-held (queued) task is surfaced by list-startable, not here.
        self.assertEqual(report["in_flight"][0]["worker"], "w1")
        self.assertEqual(report["in_flight"][0]["heartbeat_age_seconds"], 3600)

    def test_project_filter(self):
        nodes = [
            self._node(1, "a", ["kraken-task", "project:app", "in-progress"]),
            self._node(2, "b", ["kraken-task", "project:web", "in-progress"]),
        ]
        comments = {
            1: [{"body": "claimed-by: w", "createdAt": "2026-07-01T09:00:00Z"}],
            2: [{"body": "claimed-by: w", "createdAt": "2026-07-01T09:00:00Z"}],
        }
        report = kraken.compute_status(
            "o/tasks", "web", nodes, self.NOW, **self._readers(comments=comments))
        self.assertEqual([r["number"] for r in report["in_flight"]], [2])
        self.assertEqual(report["project"], "web")
        self.assertEqual(report["projects"], ["web"])  # scoped, no label list call

    def test_orphan_flag_only_when_pr_merged(self):
        nodes = [
            self._node(88, "merged pr", ["kraken-task", "project:app", "awaiting-merge"]),
            self._node(91, "open pr", ["kraken-task", "project:app", "awaiting-merge"],
                       created="2026-07-01T01:00:00Z"),
        ]
        comments = {
            88: [{"body": "pr: https://x/pull/5", "createdAt": "t"}],
            91: [{"body": "pr: https://x/pull/6", "createdAt": "t"}],
        }
        merged = {"https://x/pull/5": True, "https://x/pull/6": False}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            **self._readers(comments=comments, merged=merged, projects=["app"]))
        self.assertEqual(report["orphans"], [88])
        flags = {r["number"]: r["orphan"] for r in report["review_queue"]}
        self.assertTrue(flags[88])
        self.assertFalse(flags[91])

    def test_no_machine_line_yields_unknown_age(self):
        nodes = [self._node(99, "silent", ["kraken-task", "project:app", "in-progress"])]
        comments = {99: [{"body": "operator note", "createdAt": "2026-07-01T09:00:00Z"}]}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            **self._readers(comments=comments, projects=["app"]))
        item = report["in_flight"][0]
        self.assertIsNone(item["heartbeat_age_seconds"])
        self.assertIsNone(item["worker"])

    def test_comment_transport_failure_propagates_none(self):
        nodes = [self._node(99, "x", ["kraken-task", "project:app", "in-progress"])]

        def failing_reader(repo, issue):
            return None
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            comment_reader=failing_reader,
            pr_merged=lambda u: False,
            project_lister=lambda r: [])
        self.assertIsNone(report)

    def test_pr_read_failure_propagates_none(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        comments = {88: [{"body": "pr: https://x/pull/5", "createdAt": "t"}]}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            comment_reader=lambda r, i: comments.get(i, []),
            pr_merged=lambda u: None,  # transport failure
            project_lister=lambda r: [])
        self.assertIsNone(report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
