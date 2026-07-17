#!/usr/bin/env python3
"""Unit tests for kraken.py — the parts the gh-stub conformance suite cannot
exercise in isolation: the claim-window arbitration over hidden markers, marker
decoding edge cases, the marker-only reading invariant (free text can never
forge a machine line), and comment pagination beyond 100 comments.

Stdlib only (unittest), no network, no gh. Run: python3 tests/unit/test_kraken.py
"""

import os
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace
from io import StringIO
from contextlib import redirect_stdout

# Import kraken.py from the plugin folder without installing anything.
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(HERE, "..", "..", "skills", "unleash")
sys.path.insert(0, os.path.abspath(SKILL_DIR))

import kraken  # noqa: E402


# --- protocol/3 marker builders (what a real comment carries) ----------------

def clm(worker):
    return kraken.make_marker({"type": "claim", "worker": worker})


def hb(worker):
    return kraken.make_marker({"type": "heartbeat", "worker": worker})


def dlv(worker, pr=None):
    payload = {"type": "delivered", "worker": worker}
    if pr:
        payload["pr"] = pr
    return kraken.make_marker(payload)


def rls(worker, reason=None):
    payload = {"type": "released", "worker": worker}
    if reason:
        payload["reason"] = reason
    return kraken.make_marker(payload)


def stale(reason=None):
    payload = {"type": "stale-claim"}
    if reason:
        payload["reason"] = reason
    return kraken.make_marker(payload)


def needs(worker):
    return kraken.make_marker({"type": "needs-decision", "worker": worker})


class ArbitrationTests(unittest.TestCase):
    """arbitrate_winner: the claim tiebreaker, isolated from any transport. Every
    fixture is a protocol/3 hidden marker — the only wire format consumers read."""

    def test_first_claim_in_window_wins(self):
        self.assertEqual(kraken.arbitrate_winner([clm("alice"), clm("bob")]), "alice")

    def test_no_claim_yields_empty(self):
        self.assertEqual(kraken.arbitrate_winner(["some prose", rls("x")]), "")

    def test_released_resets_window(self):
        # tired-worker released; the newcomer is now the first live claim.
        self.assertEqual(
            kraken.arbitrate_winner([clm("tired-worker"), rls("tired-worker"), clm("fresh")]),
            "fresh")

    def test_stale_claim_resets_window(self):
        self.assertEqual(
            kraken.arbitrate_winner([clm("dead"), stale("no activity for 7h"), clm("reaper-heir")]),
            "reaper-heir")

    def test_needs_decision_resets_window(self):
        self.assertEqual(
            kraken.arbitrate_winner([clm("w1"), needs("w1"), clm("w2")]), "w2")

    def test_delivered_is_a_review_bounce_reset(self):
        # THE review-bounce gap: without delivered as a reset, the original
        # claimant would win every future arbitration and a bounced-back task
        # could never be re-claimed by anyone else.
        self.assertEqual(
            kraken.arbitrate_winner([clm("w1"), dlv("w1"), clm("w2")]), "w2")

    def test_no_reset_keeps_original_owner(self):
        # A control: with no reset, the first (rightful) owner keeps it even if a
        # newcomer's claim comment lands later.
        self.assertEqual(
            kraken.arbitrate_winner([clm("rightful-owner"), clm("interloper")]),
            "rightful-owner")

    def test_heartbeat_does_not_reset(self):
        # heartbeat is deliberately NOT a window reset — a worker heartbeating
        # must never make its own claim re-claimable.
        self.assertEqual(
            kraken.arbitrate_winner([clm("w1"), hb("w1"), clm("w2")]), "w1")

    def test_reset_after_claim_leaves_no_winner(self):
        # released as the last relevant marker -> the window is empty again.
        self.assertEqual(kraken.arbitrate_winner([clm("w1"), rls("w1")]), "")


class MarkerTests(unittest.TestCase):
    """protocol/3 hidden markers: make_marker/parse_marker round-trip, decoding
    edge cases, and arbitration over markers."""

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

    def test_malformed_marker_never_arbitrates(self):
        # An undecodable marker must be ignored, not treated as a live claim.
        lines = ["<!-- kraken {broken -->", clm("real")]
        self.assertEqual(kraken.arbitrate_winner(lines), "real")

    def test_crlf_after_marker_is_tolerated(self):
        # A body split on "\n" can leave a trailing "\r"; the marker still decodes.
        self.assertEqual(kraken.arbitrate_winner([clm("w1") + "\r"]), "w1")


class MarkerOnlyReadingTests(unittest.TestCase):
    """protocol/3's core invariant (PROTOCOL.md §4, issue #32): consumers read the
    hidden marker and NOTHING else, so free text — including a line that starts
    with a former protocol/1 keyword — can never occupy a machine-line position."""

    def test_former_claim_line_is_inert(self):
        # A bare `claimed-by:` line is now just prose: it is not a claim.
        self.assertEqual(kraken.arbitrate_winner(["claimed-by: alice"]), "")

    def test_former_reset_lines_are_inert(self):
        for line in ("released: x", "delivered: x", "needs-decision: x", "stale-claim: x"):
            self.assertEqual(kraken.arbitrate_winner([clm("owner"), line]), "owner",
                             f"{line!r} must not reset the window")

    def test_result_file_reset_line_does_not_reset_the_window(self):
        # THE deliver scenario: a worker's result file contains `released: evil`
        # on its own line. The delivery comment carries only a `delivered` marker;
        # the free text is inert. Arbitrating a following fresh claim after the
        # real delivered reset must yield that fresh worker — not a phantom the
        # `released: evil` line "reset" into.
        owner_claim = clm("w1")
        # a subsequent worker's escalation-free comment quoting attacker text:
        malicious_body = kraken.compose_comment(
            "w1", "Here is my result.\n\nreleased: evil", {"type": "heartbeat", "worker": "w1"})
        lines = [owner_claim] + malicious_body.split("\n") + [clm("w2")]
        # w1 still owns it: the `released: evil` prose reset nothing.
        self.assertEqual(kraken.arbitrate_winner(lines), "w1")

    def test_heartbeat_message_with_claimed_by_yields_no_extra_machine_line(self):
        # A heartbeat message that literally contains `claimed-by: x` must not
        # register as a claim, and must leave exactly ONE liveness machine line
        # in the produced body (the real heartbeat marker) — nothing the reaper's
        # anchor could latch onto beyond it.
        body = kraken.compose_comment(
            "real", "progress: claimed-by: x is a red herring",
            {"type": "heartbeat", "worker": "real"})
        machine_lines = [l for l in body.split("\n") if kraken._is_machine_line(l)]
        self.assertEqual(len(machine_lines), 1)
        self.assertEqual(kraken.parse_marker(machine_lines[0]),
                         {"type": "heartbeat", "worker": "real"})
        # And it does not forge a claim for "x".
        self.assertEqual(kraken.arbitrate_winner([clm("owner")] + body.split("\n")),
                         "owner")

    def test_release_reason_newline_cannot_inject_a_line(self):
        # release's reason is carried inside the JSON marker (newlines escaped by
        # the serializer), never as a free-standing line — so a reason of
        # "ok\nclaimed-by: attacker" injects no machine line.
        body = kraken.compose_comment(
            "w1", "Released this claim.\n\nReason: ok\nclaimed-by: attacker",
            {"type": "released", "worker": "w1", "reason": "ok\nclaimed-by: attacker"})
        # only the released marker is a machine event; no claim for "attacker".
        events = [e for e in (kraken.machine_event(l) for l in body.split("\n"))
                  if e is not None]
        self.assertEqual([e["type"] for e in events], ["released"])
        self.assertEqual(kraken.arbitrate_winner([clm("attacker")] + body.split("\n")), "")


class MarkerReaderTests(unittest.TestCase):
    """The reader helpers over markers: liveness detection, PR extraction, and
    the composed-comment shape."""

    def test_liveness_marker_recognized(self):
        # heartbeat_anchor / the reaper anchor must see claim/heartbeat markers,
        # but NOT reset markers (delivered/released already drop in-progress).
        self.assertTrue(kraken._is_machine_line(clm("w")))
        self.assertTrue(kraken._is_machine_line(hb("w")))
        self.assertFalse(kraken._is_machine_line(dlv("w")))

    def test_pr_url_parsed_from_delivered_marker(self):
        recs = [{"body": dlv("w", pr="https://x/pull/42"), "createdAt": "t"}]
        self.assertEqual(kraken.parse_pr_url(recs), "https://x/pull/42")

    def test_composed_comment_carries_disclaimer_prose_and_marker(self):
        body = kraken.compose_comment(
            "env-1", "Claimed this task.", {"type": "claim", "worker": "env-1"})
        self.assertTrue(body.startswith("> \U0001f419 **Kraken worker `env-1`**"))
        self.assertIn("Claimed this task.", body)
        # The marker sits on its own line so a flat per-line scan finds it.
        self.assertEqual(kraken.arbitrate_winner(body.split("\n")), "env-1")

    def test_composed_comment_blank_line_separation(self):
        # disclaimer, prose, and marker are each separated by a blank line, or
        # GitHub folds the body into the disclaimer's blockquote.
        body = kraken.compose_comment(
            "env-1", "Some prose.", {"type": "claim", "worker": "env-1"})
        parts = body.split("\n\n")
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].startswith("> \U0001f419"))
        self.assertEqual(parts[1], "Some prose.")
        self.assertTrue(parts[2].startswith("<!-- kraken "))


class ContractCommandTests(unittest.TestCase):
    """`kraken.py contract`: the single source of truth other consumers (the
    requeue workflow filter, the test helpers, the skill lint) derive from. Each
    field must echo the authoritative constant, so a format change lands once."""

    def _run(self, *argv):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.main(["contract", *argv])
        self.assertEqual(rc, kraken.EXIT_OK)
        return buf.getvalue().splitlines()

    def test_disclaimer_defaults_to_the_doc_placeholder(self):
        self.assertEqual(self._run("disclaimer"),
                         [kraken.DISCLAIMER.format(worker="<worker-name>")])

    def test_disclaimer_substitutes_the_worker(self):
        self.assertEqual(self._run("disclaimer", "--worker", "env-1"),
                         [kraken.disclaimer("env-1")])

    def test_task_trailer_defaults_to_the_doc_placeholders(self):
        # No flags → every field is the doc placeholder, but the version is the
        # real stamp (plugin_version), never a placeholder — that is the point.
        self.assertEqual(
            self._run("task-trailer"),
            [kraken.task_trailer("<coordination-repo>", "<issue>", "<worker-name>")],
        )

    def test_task_trailer_substitutes_repo_issue_worker(self):
        self.assertEqual(
            self._run("task-trailer", "--repo", "acme/work",
                      "--issue", "12", "--worker", "env-1"),
            [kraken.task_trailer("acme/work", "12", "env-1")],
        )

    def test_task_trailer_stamps_the_live_plugin_version(self):
        # The kraken@<version> field must be the manifest's version, never a
        # literal — this is the drift the single-sourcing exists to kill.
        line = self._run("task-trailer", "--repo", "acme/work",
                         "--issue", "12", "--worker", "env-1")[0]
        self.assertIn(f"kraken@{kraken.plugin_version()}", line)

    def test_reset_types_echo_the_constant(self):
        self.assertEqual(self._run("reset-types"), list(kraken.RESET_TYPES))

    def test_liveness_types_echo_the_constant(self):
        self.assertEqual(self._run("liveness-types"), list(kraken.LIVENESS_TYPES))

    def test_marker_types_echo_the_constant(self):
        self.assertEqual(self._run("marker-types"), list(kraken.MARKER_TYPES))

    def test_marker_types_are_the_liveness_and_reset_vocabulary(self):
        # protocol/3 vocabulary kraken.py builds/arbitrates on — requeue is
        # operator-only and deliberately absent.
        self.assertEqual(tuple(kraken.MARKER_TYPES),
                         kraken.LIVENESS_TYPES + kraken.RESET_TYPES)
        self.assertNotIn("requeue", kraken.MARKER_TYPES)


class PluginVersionTests(unittest.TestCase):
    """plugin_version() sources the Kraken-Task trailer's kraken@<version> from
    the bundled manifest the release workflow bumps — read at runtime, so the
    trailer never carries a stale hand-copied version and never has to be
    guessed by the worker model."""

    def _manifest(self, contents):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contents)
        self.addCleanup(os.remove, path)
        return path

    def test_reads_the_version_from_the_manifest(self):
        path = self._manifest(json.dumps({"version": "9.9.9"}))
        self.assertEqual(kraken.plugin_version(path), "9.9.9")

    def test_bundled_manifest_matches_the_shipped_plugin_json(self):
        # The default lookup must resolve the real bundled manifest, not "unknown".
        with open(kraken.PLUGIN_MANIFEST, encoding="utf-8") as f:
            shipped = json.load(f)["version"]
        self.assertEqual(kraken.plugin_version(), shipped)

    def test_missing_manifest_falls_back_to_unknown(self):
        self.assertEqual(
            kraken.plugin_version("/no/such/plugin.json"),
            kraken.PLUGIN_VERSION_UNKNOWN,
        )

    def test_malformed_manifest_falls_back_to_unknown(self):
        path = self._manifest("{not json")
        self.assertEqual(kraken.plugin_version(path), kraken.PLUGIN_VERSION_UNKNOWN)

    def test_manifest_without_version_falls_back_to_unknown(self):
        path = self._manifest(json.dumps({"name": "kraken"}))
        self.assertEqual(kraken.plugin_version(path), kraken.PLUGIN_VERSION_UNKNOWN)


class InitConstantsTests(unittest.TestCase):
    """The init subcommand single-sources the asset set and the label canon in
    kraken.py (issue #30). These guard that single-sourcing: every asset it
    installs must actually ship next to the module, and the label/render shapes
    must stay well-formed — a rename or a dropped field is caught here, not in a
    live bootstrap."""

    def test_every_bundled_asset_exists_next_to_the_module(self):
        for name, dest, message in kraken.INIT_ASSETS:
            src = os.path.join(kraken.SKILL_DIR, name)
            self.assertTrue(os.path.isfile(src),
                            f"bundled asset {name} missing at {src}")
            self.assertTrue(dest.startswith(".github/"),
                            f"asset {name} destination not under .github/")
            self.assertTrue(message, f"asset {name} has no commit message")

    def test_the_five_documented_assets_are_installed(self):
        dests = {dest for _, dest, _ in kraken.INIT_ASSETS}
        self.assertEqual(dests, {
            ".github/ISSUE_TEMPLATE/task.yml",
            ".github/workflows/reclaim-stale.yml",
            ".github/workflows/cleanup-closed.yml",
            ".github/workflows/requeue-on-reply.yml",
            ".github/workflows/validate-task.yml",
        })

    def test_canonical_labels_are_six_hex_colors(self):
        for name, color, desc in kraken.CANONICAL_LABELS:
            self.assertRegex(color, r"^[0-9A-F]{6}$",
                             f"label {name} color not a 6-digit hex")
            self.assertTrue(desc, f"label {name} has no description")
        self.assertRegex(kraken.PROJECT_LABEL_COLOR, r"^[0-9A-F]{6}$")

    def test_render_init_summarizes_every_decision(self):
        report = {
            "repo": "acme/tasks", "repo_status": "created",
            "assets": [
                {"path": ".github/ISSUE_TEMPLATE/task.yml", "status": "created"},
                {"path": ".github/workflows/reclaim-stale.yml", "status": "unchanged"},
                {"path": ".github/workflows/cleanup-closed.yml", "status": "customized"},
            ],
            "labels": ["kraken-task", "project:app"],
            "project": "app",
        }
        out = kraken.render_init(report)
        self.assertIn("init: repo acme/tasks (created)", out)
        self.assertIn("init: asset .github/ISSUE_TEMPLATE/task.yml (created)", out)
        self.assertIn("init: label project:app (upserted)", out)
        self.assertIn(
            "assets_created=1 assets_unchanged=1 assets_customized=1 labels=2", out)


class MarkerEdgeCaseTests(unittest.TestCase):
    """Marker decoding edge cases within a multi-line comment body: only a line
    carrying a well-formed marker is a machine line; prose that merely mentions a
    former keyword is inert."""

    def test_prose_mentioning_a_former_keyword_is_ignored(self):
        lines = ["I think claimed-by: nobody is wrong", clm("real")]
        self.assertEqual(kraken.arbitrate_winner(lines), "real")

    def test_multiline_comment_bodies_scan_per_line(self):
        # comment_bodies returns bodies split to lines; a disclaimer blockquote
        # above the marker must not shadow it.
        lines = [
            "> \U0001f419 **Kraken worker `w1`** — automated comment...",
            "",
            clm("w1"),
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
        bodies[129] = clm("winner-past-page-one")

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
        bodies[5] = clm("dead")
        bodies[120] = rls("dead")
        bodies[140] = clm("heir")

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
        # 130th carries the live liveness marker, invisible under a 100 truncation.
        recs = [{"body": f"c {i}", "createdAt": f"2026-07-01T00:{i % 60:02d}:00Z"}
                for i in range(150)]
        recs[129] = {"body": hb("late"), "createdAt": "2026-07-09T00:00:00Z"}
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
        recs = [
            {"body": clm("w1"), "createdAt": "2026-07-01T00:00:00Z"},
            {"body": hb("w1"), "createdAt": "2026-07-01T05:00:00Z"},
        ]
        pretty = "".join(kraken.json.dumps(r, indent=2) + "\n" for r in recs)

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
    heartbeat anchor (liveness-marker-only, newest-wins), PR-URL parsing, worker
    resolution, and age formatting."""

    def _rec(self, body, created):
        return {"body": body, "createdAt": created}

    def test_anchor_is_newest_liveness_marker(self):
        recs = [
            self._rec(clm("w1"), "2026-07-01T00:00:00Z"),
            self._rec(hb("w1"), "2026-07-01T05:00:00Z"),
        ]
        self.assertEqual(kraken.heartbeat_anchor(recs), "2026-07-01T05:00:00Z")

    def test_anchor_ignores_operator_comments(self):
        # THE anchoring invariant (mirrors the reaper): a fresh operator comment
        # must NOT reset the clock — only worker liveness markers anchor liveness.
        recs = [
            self._rec("> disclaimer\n\n" + clm("w1"), "2026-07-01T00:00:00Z"),
            self._rec("any update? — the operator", "2026-07-01T09:00:00Z"),
        ]
        self.assertEqual(kraken.heartbeat_anchor(recs), "2026-07-01T00:00:00Z")

    def test_anchor_none_when_worker_never_spoke(self):
        recs = [self._rec("someone mislabeled this", "2026-07-01T00:00:00Z")]
        self.assertIsNone(kraken.heartbeat_anchor(recs))

    def test_anchor_finds_marker_inside_multiline_body(self):
        recs = [self._rec("> \U0001f419 worker note\n\n" + hb("w1") + "\n\nstill going",
                          "2026-07-01T02:00:00Z")]
        self.assertEqual(kraken.heartbeat_anchor(recs), "2026-07-01T02:00:00Z")

    def test_parse_pr_url_from_delivered_marker(self):
        recs = [self._rec(dlv("w1", pr="https://github.com/o/r/pull/42") + "\n\nbody",
                          "t")]
        self.assertEqual(kraken.parse_pr_url(recs),
                         "https://github.com/o/r/pull/42")

    def test_parse_pr_url_newest_wins(self):
        recs = [
            self._rec(dlv("w1", pr="https://github.com/o/r/pull/1"), "t1"),
            self._rec(dlv("w2", pr="https://github.com/o/r/pull/9"), "t2"),
        ]
        self.assertEqual(kraken.parse_pr_url(recs),
                         "https://github.com/o/r/pull/9")

    def test_parse_pr_url_fallback_to_url_in_prose(self):
        recs = [self._rec("landed in https://github.com/o/r/pull/7 fyi", "t")]
        self.assertEqual(kraken.parse_pr_url(recs),
                         "https://github.com/o/r/pull/7")

    def test_parse_pr_url_none_when_absent(self):
        recs = [self._rec(dlv("w1") + "\n\njust prose", "t")]
        self.assertIsNone(kraken.parse_pr_url(recs))

    def test_worker_resolves_via_arbitration(self):
        recs = [
            self._rec(clm("dead"), "t1"),
            self._rec(stale("7h"), "t2"),
            self._rec(clm("heir"), "t3"),
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
            88: [{"body": dlv("w", pr="https://x/pull/1"), "createdAt": "t"}],
            99: [{"body": clm("w1"), "createdAt": "2026-07-01T09:00:00Z"}],
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
            1: [{"body": clm("w"), "createdAt": "2026-07-01T09:00:00Z"}],
            2: [{"body": clm("w"), "createdAt": "2026-07-01T09:00:00Z"}],
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
            88: [{"body": dlv("w", pr="https://x/pull/5"), "createdAt": "t"}],
            91: [{"body": dlv("w", pr="https://x/pull/6"), "createdAt": "t"}],
        }
        merged = {"https://x/pull/5": True, "https://x/pull/6": False}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            **self._readers(comments=comments, merged=merged, projects=["app"]))
        self.assertEqual(report["orphans"], [88])
        flags = {r["number"]: r["orphan"] for r in report["review_queue"]}
        self.assertTrue(flags[88])
        self.assertFalse(flags[91])

    def test_no_liveness_marker_yields_unknown_age(self):
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
        comments = {88: [{"body": dlv("w", pr="https://x/pull/5"), "createdAt": "t"}]}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            comment_reader=lambda r, i: comments.get(i, []),
            pr_merged=lambda u: None,  # transport failure
            project_lister=lambda r: [])
        self.assertIsNone(report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
