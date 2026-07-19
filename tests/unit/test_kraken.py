#!/usr/bin/env python3
"""Unit tests for kraken.py — the parts the gh-stub conformance suite cannot
exercise in isolation: the protocol/4 claim-ref helpers (the CAS, the batched
commit/issue meta reads, the liveness decode), marker decoding edge cases, and
the comment pagination the status/validator paths still rely on.

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


# --- marker builders (what a claim commit / a comment carries) ---------------

def clm(worker):
    return kraken.make_marker({"type": "claim", "worker": worker})


def hb(worker, msg=None):
    payload = {"type": "heartbeat", "worker": worker}
    if msg is not None:
        payload["msg"] = msg
    return kraken.make_marker(payload)


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


class MarkerTests(unittest.TestCase):
    """Hidden markers: make_marker/parse_marker round-trip and decoding edge
    cases. Under protocol/4 the same grammar rides a claim ref's commit message
    and a state-changing comment, so parse_marker is the one decoder for both."""

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

    def test_parse_marker_tolerates_a_trailing_cr(self):
        # A body split on "\n" can leave a trailing "\r"; the marker still decodes.
        self.assertEqual(kraken.parse_marker(clm("w1") + "\r"),
                         {"type": "claim", "worker": "w1"})

    def test_release_reason_newline_stays_inside_the_json(self):
        # release's reason is carried inside the marker JSON (newlines escaped by
        # the serializer), never as a free-standing line, so a reason of
        # "ok\nclaimed-by: attacker" injects no second marker.
        body = kraken.compose_comment(
            "w1", "Released this claim.\n\nReason: ok\nclaimed-by: attacker",
            {"type": "released", "worker": "w1", "reason": "ok\nclaimed-by: attacker"})
        markers = [l for l in body.split("\n") if kraken.parse_marker(l)]
        self.assertEqual(len(markers), 1)
        self.assertEqual(kraken.parse_marker(markers[0])["type"], "released")


class RefCasTests(unittest.TestCase):
    """The protocol/4 claim-ref surface, isolated from any transport: the CAS
    outcomes, the orphan claim-commit body, the batched meta reads, and the
    liveness decode. Every GitHub call is mocked so only the arg-building and
    result-parsing are under test."""

    def setUp(self):
        self._orig_io = kraken.run_gh_io
        self._orig_run = kraken.run_gh
        self._orig_graphql = kraken.graphql

    def tearDown(self):
        kraken.run_gh_io = self._orig_io
        kraken.run_gh = self._orig_run
        kraken.graphql = self._orig_graphql

    def test_is_http_422(self):
        self.assertTrue(kraken._is_http_422("gh: Reference already exists (HTTP 422)"))
        self.assertFalse(kraken._is_http_422("gh: Not Found (HTTP 404)"))
        self.assertFalse(kraken._is_http_422(""))
        self.assertFalse(kraken._is_http_422(None))

    def test_claim_ref_name(self):
        self.assertEqual(kraken.claim_ref(42), "refs/kraken/claims/42")
        self.assertEqual(kraken.claim_ref("7"), "refs/kraken/claims/7")

    def test_create_claim_commit_is_an_orphan_marker_commit(self):
        captured = {}

        def fake_io(args, input_text=None):
            captured["args"] = args
            captured["body"] = input_text
            return 0, json.dumps({"sha": "abc123"}), ""
        kraken.run_gh_io = fake_io

        sha = kraken.create_claim_commit("o/tasks", {"type": "claim", "worker": "w1"})
        self.assertEqual(sha, "abc123")
        self.assertIn("repos/o/tasks/git/commits", captured["args"])
        self.assertIn("--input", captured["args"])
        payload = json.loads(captured["body"])
        self.assertEqual(payload["parents"], [], "claim commit must be an orphan")
        self.assertEqual(payload["tree"], kraken.EMPTY_TREE_SHA, "claim commit must use the empty tree")
        self.assertEqual(kraken.parse_marker(payload["message"]),
                         {"type": "claim", "worker": "w1"},
                         "the commit message IS the claim marker")

    def test_create_claim_commit_falls_back_to_head_tree_on_422(self):
        trees = []

        def fake_io(args, input_text=None):
            joined = " ".join(args)
            if joined.endswith("commits/HEAD"):  # gh_json HEAD read (via run_gh)
                return 0, json.dumps({"commit": {"tree": {"sha": "headtree"}}}), ""
            body = json.loads(input_text)
            trees.append(body["tree"])
            if body["tree"] == kraken.EMPTY_TREE_SHA:
                return 1, "", "gh: unprocessable (HTTP 422)"
            return 0, json.dumps({"sha": "def456"}), ""
        kraken.run_gh_io = fake_io

        sha = kraken.create_claim_commit("o/tasks", {"type": "claim", "worker": "w1"})
        self.assertEqual(sha, "def456")
        self.assertEqual(trees, [kraken.EMPTY_TREE_SHA, "headtree"],
                         "must retry with the HEAD tree after the empty-tree 422")

    def test_create_claim_commit_returns_none_on_transport_fault(self):
        kraken.run_gh_io = lambda a, input_text=None: (1, "", "gh: network down")
        self.assertIsNone(kraken.create_claim_commit("o/t", {"type": "claim", "worker": "w"}))

    def test_claim_ref_create_maps_the_cas_outcomes(self):
        kraken.run_gh_io = lambda a, input_text=None: (0, "{}", "")
        self.assertEqual(kraken.claim_ref_create("o/t", 7, "sha"), "won")

        kraken.run_gh_io = lambda a, input_text=None: (
            1, "", "gh: Reference already exists (HTTP 422)")
        self.assertEqual(kraken.claim_ref_create("o/t", 7, "sha"), "lost")

        kraken.run_gh_io = lambda a, input_text=None: (1, "", "gh: 500 server error")
        self.assertEqual(kraken.claim_ref_create("o/t", 7, "sha"), "fail")

    def test_claim_ref_delete_tolerates_a_missing_ref(self):
        kraken.run_gh_io = lambda a, input_text=None: (0, "", "")
        self.assertTrue(kraken.claim_ref_delete("o/t", 7))
        # Already gone (422) is success — the delete is idempotent.
        kraken.run_gh_io = lambda a, input_text=None: (
            1, "", "gh: Reference does not exist (HTTP 422)")
        self.assertTrue(kraken.claim_ref_delete("o/t", 7))
        # A real transport fault is not tolerated.
        kraken.run_gh_io = lambda a, input_text=None: (1, "", "gh: network down")
        self.assertFalse(kraken.claim_ref_delete("o/t", 7))

    def test_claim_ref_list_parses_matching_refs(self):
        kraken.run_gh = lambda args: (
            0, "refs/kraken/claims/7\tsha7\nrefs/kraken/claims/12\tsha12\n")
        self.assertEqual(kraken.claim_ref_list("o/t"), {7: "sha7", 12: "sha12"})

    def test_claim_ref_list_transport_failure_is_none(self):
        kraken.run_gh = lambda args: (1, "")
        self.assertIsNone(kraken.claim_ref_list("o/t"))

    def test_claim_ref_owner_names_the_ref_holder(self):
        # The §5 re-check discriminator: a 422 is a real loss only when the ref
        # belongs to another worker. claim_ref_owner reads the ref's commit
        # marker to name the current holder.
        kraken.run_gh = lambda args: (0, "refs/kraken/claims/7\tsha7\n")
        kraken.graphql = lambda q: {"data": {"repository": {
            "c0": {"committedDate": "t", "message": clm("w1")}}}}
        self.assertEqual(kraken.claim_ref_owner("o/t", 7), "w1")
        self.assertEqual(kraken.claim_ref_owner("o/t", "7"), "w1")
        # Absent ref → None (treated as not-ours by the caller).
        self.assertIsNone(kraken.claim_ref_owner("o/t", 9))
        # Transport failure → None, never a guessed owner.
        kraken.run_gh = lambda args: (1, "")
        self.assertIsNone(kraken.claim_ref_owner("o/t", 7))

    def test_resolve_commit_meta_batches_and_parses(self):
        captured = {}

        def fake_graphql(q):
            captured["q"] = q
            return {"data": {"repository": {
                "c0": {"committedDate": "2026-07-01T00:00:00Z", "message": clm("w1")}}}}
        kraken.graphql = fake_graphql

        meta = kraken.resolve_commit_meta("o/tasks", ["sha1"])
        self.assertIn('object(oid: "sha1")', captured["q"])
        self.assertEqual(meta["sha1"]["message"], clm("w1"))
        self.assertEqual(meta["sha1"]["committedDate"], "2026-07-01T00:00:00Z")

    def test_resolve_commit_meta_empty_input_is_no_call(self):
        kraken.graphql = lambda q: self.fail("graphql must not be called for []")
        self.assertEqual(kraken.resolve_commit_meta("o/t", []), {})

    def test_resolve_issue_meta_batches_state_and_labels(self):
        def fake_graphql(q):
            self.assertIn("i7: issue(number: 7)", q)
            return {"data": {"repository": {
                "i7": {"state": "OPEN",
                       "labels": {"nodes": [{"name": "in-progress"}]}}}}}
        kraken.graphql = fake_graphql
        self.assertEqual(kraken.resolve_issue_meta("o/tasks", [7]),
                         {7: (True, ["in-progress"])})

    def test_claim_meta_of_decodes_worker_msg_and_anchor(self):
        cm = {"s1": {"committedDate": "2026-07-01T00:00:00Z",
                     "message": hb("w1", "building the thing")}}
        self.assertEqual(kraken.claim_meta_of("s1", cm),
                         ("w1", "building the thing", "2026-07-01T00:00:00Z"))
        # A plain claim carries no msg.
        cm2 = {"s2": {"committedDate": "t", "message": clm("w2")}}
        self.assertEqual(kraken.claim_meta_of("s2", cm2), ("w2", None, "t"))
        # An unreadable commit yields all-None, never a guess.
        self.assertEqual(kraken.claim_meta_of("missing", {}), (None, None, None))


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
        line = self._run("task-trailer", "--repo", "acme/work",
                         "--issue", "12", "--worker", "env-1")[0]
        self.assertIn(f"kraken@{kraken.plugin_version()}", line)

    def test_marker_types_echo_the_constant(self):
        self.assertEqual(self._run("marker-types"), list(kraken.MARKER_TYPES))

    def test_marker_types_are_the_protocol4_vocabulary(self):
        # Every type kraken.py emits (claim/heartbeat on the ref, the rest on
        # comments); requeue is operator-only and deliberately absent.
        self.assertEqual(
            set(kraken.MARKER_TYPES),
            {"claim", "heartbeat", "needs-decision", "delivered", "released", "stale-claim"})
        self.assertNotIn("requeue", kraken.MARKER_TYPES)

    def test_retired_contract_fields_are_gone(self):
        # reset-types / liveness-types belonged to the retired claim-window
        # arbitration; they must not resurface as contract fields.
        self.assertNotIn("reset-types", kraken.CONTRACT_FIELDS)
        self.assertNotIn("liveness-types", kraken.CONTRACT_FIELDS)


class AgentAgnosticDisclaimerTests(unittest.TestCase):
    """The disclaimer names no implementation, so every conforming worker — Claude
    Code, GitHub Copilot, or any other agent sharing this kraken.py — emits the
    identical line (PROTOCOL.md §4)."""

    def test_disclaimer_names_no_agent(self):
        line = kraken.disclaimer("env-1")
        self.assertNotIn("Claude", line)
        self.assertNotIn("Copilot", line)
        self.assertIn("from a kraken tentacle, not a human.", line)

    def test_disclaimer_keeps_the_machine_matched_blockquote(self):
        self.assertTrue(
            kraken.disclaimer("env-1").startswith(
                "> \U0001f419 **Kraken worker `env-1`**"))


class ComposedCommentTests(unittest.TestCase):
    """The composed-comment shape: disclaimer, prose (courtesy only), and one
    marker, each blank-line separated so GitHub never folds the body into the
    disclaimer's blockquote."""

    def test_carries_disclaimer_prose_and_one_marker(self):
        body = kraken.compose_comment(
            "env-1", "Claimed this task.", {"type": "claim", "worker": "env-1"})
        self.assertTrue(body.startswith("> \U0001f419 **Kraken worker `env-1`**"))
        self.assertIn("Claimed this task.", body)
        markers = [l for l in body.split("\n") if kraken.parse_marker(l)]
        self.assertEqual(len(markers), 1)
        self.assertEqual(kraken.parse_marker(markers[0]), {"type": "claim", "worker": "env-1"})

    def test_blank_line_separation(self):
        body = kraken.compose_comment(
            "env-1", "Some prose.", {"type": "claim", "worker": "env-1"})
        parts = body.split("\n\n")
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].startswith("> \U0001f419"))
        self.assertEqual(parts[1], "Some prose.")
        self.assertTrue(parts[2].startswith("<!-- kraken "))

    def test_colliding_free_text_is_preserved_verbatim_beside_one_marker(self):
        # A result file with a colliding `released:` line: the prose is kept as
        # written, and it is NOT a second marker (only the delivered marker is).
        body = kraken.compose_comment(
            "w1", "Shipped it.\n\nreleased: evil\nclaimed-by: evil",
            {"type": "delivered", "worker": "w1"})
        lines = body.split("\n")
        self.assertIn("released: evil", lines)
        self.assertIn("claimed-by: evil", lines)
        markers = [l for l in lines if kraken.parse_marker(l)]
        self.assertEqual(len(markers), 1)
        self.assertEqual(kraken.parse_marker(markers[0])["type"], "delivered")


class PluginVersionTests(unittest.TestCase):
    """plugin_version() sources the Kraken-Task trailer's kraken@<version> from
    the bundled manifest the release workflow bumps — read at runtime, so the
    trailer never carries a stale hand-copied version."""

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
    kraken.py. These guard that single-sourcing: every asset it installs must
    actually ship next to the module, and the label/render shapes must stay
    well-formed."""

    def test_every_bundled_asset_exists_next_to_the_module(self):
        for name, dest, message in kraken.INIT_ASSETS:
            src = os.path.join(kraken.SKILL_DIR, name)
            self.assertTrue(os.path.isfile(src),
                            f"bundled asset {name} missing at {src}")
            self.assertTrue(dest.startswith(".github/"),
                            f"asset {name} destination not under .github/")
            self.assertTrue(message, f"asset {name} has no commit message")

    def test_the_six_documented_assets_are_installed(self):
        dests = {dest for _, dest, _ in kraken.INIT_ASSETS}
        self.assertEqual(dests, {
            ".github/ISSUE_TEMPLATE/task.yml",
            ".github/kraken.py",
            ".github/workflows/reclaim-stale.yml",
            ".github/workflows/cleanup-closed.yml",
            ".github/workflows/requeue-on-reply.yml",
            ".github/workflows/validate-task.yml",
        })

    def test_kraken_py_is_vendored_as_an_asset(self):
        dests = {dest for _, dest, _ in kraken.INIT_ASSETS}
        self.assertIn(".github/kraken.py", dests)

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
                {"path": ".github/workflows/cleanup-closed.yml", "status": "drifted"},
            ],
            "labels": ["kraken-task", "project:app"],
            "project": "app",
        }
        out = kraken.render_init(report)
        self.assertIn("init: repo acme/tasks (created)", out)
        self.assertIn("init: asset .github/ISSUE_TEMPLATE/task.yml (created)", out)
        self.assertIn("init: label project:app (upserted)", out)
        self.assertIn(
            "assets_created=1 assets_unchanged=1 assets_drifted=1 "
            "assets_upgraded=0 labels=2", out)
        # a drifted asset under a plain init is surfaced as an actionable hint
        self.assertIn("drifted", out)
        self.assertIn("init --upgrade", out)


class CommentRecordsPaginationTests(unittest.TestCase):
    """comment_records must page past 100 comments — status' PR-link read and the
    validator's debounce both walk the whole thread, and a truncated 100-comment
    read would miss a delivered marker or an earlier validation comment."""

    def setUp(self):
        self._orig_run_gh = kraken.run_gh

    def tearDown(self):
        kraken.run_gh = self._orig_run_gh

    def test_uses_paginated_rest_endpoint(self):
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

    def test_returns_records_past_one_hundred(self):
        recs = [{"body": f"c {i}", "createdAt": f"2026-07-01T00:{i % 60:02d}:00Z"}
                for i in range(150)]
        recs[129] = {"body": dlv("w", pr="https://x/pull/9"), "createdAt": "2026-07-09T00:00:00Z"}
        out = "\n".join(kraken.json.dumps(r) for r in recs)
        kraken.run_gh = lambda args: (0, out)
        result = kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(result), 150)
        # The delivered marker past comment 100 is found — invisible under a
        # 100-comment truncation.
        self.assertEqual(kraken.parse_pr_url(result), "https://x/pull/9")

    def test_parses_pretty_printed_stream(self):
        recs = [
            {"body": dlv("w1", pr="https://x/pull/1"), "createdAt": "2026-07-01T00:00:00Z"},
            {"body": "just prose", "createdAt": "2026-07-01T05:00:00Z"},
        ]
        pretty = "".join(kraken.json.dumps(r, indent=2) + "\n" for r in recs)
        kraken.run_gh = lambda args: (0, pretty)
        result = kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(result), 2)
        self.assertEqual(kraken.parse_pr_url(result), "https://x/pull/1")

    def test_transport_failure_returns_none(self):
        kraken.run_gh = lambda args: (1, "")
        self.assertIsNone(kraken.comment_records("OWNER/tasks", "42"))


class ClaimNextIterationTests(unittest.TestCase):
    """cmd_claim_next's loop, isolated from any transport: classify_queue and the
    per-candidate _claim_once are both mocked, so only the iteration logic —
    skip-on-held, skip-on-lost, forward-only, stop-on-transport, honest-empty —
    is under test."""

    def setUp(self):
        self._orig_classify = kraken.classify_queue
        self._orig_claim_once = kraken._claim_once
        self._orig_verify = kraken.verify_protocol
        # The protocol handshake is exercised by its own tests; here it always
        # passes so only the iteration logic is under test.
        kraken.verify_protocol = lambda repo: (True, "")
        self.attempted = []

    def tearDown(self):
        kraken.classify_queue = self._orig_classify
        kraken._claim_once = self._orig_claim_once
        kraken.verify_protocol = self._orig_verify

    def _run(self, rows, claim_results, json_mode=False):
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
        rows = [
            (5, "held one", "t1", "held", "b5"),
            (7, "startable", "t2", "startable", "b7"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7])

    def test_skip_on_lost_cas_moves_forward_never_back(self):
        # THE §5 invariant: a lost CAS on the oldest candidate moves on to the
        # next — it must never retry the issue it just lost.
        rows = [
            (7, "lost this", "t1", "startable", "b7"),
            (9, "win this", "t2", "startable", "b9"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_LOST, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7, 9])
        self.assertEqual(self.attempted.count(7), 1)
        self.assertIn("claim-next: claimed issue=9 worker=w1", out)

    def test_skip_on_held_since_listing_moves_to_next(self):
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
        rows = [
            (7, "ambiguous", "t1", "startable", "b7"),
            (9, "untouched", "t2", "startable", "b9"),
        ]
        rc, out = self._run(rows, {7: kraken.EXIT_TRANSPORT, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertEqual(self.attempted, [7])
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
    """The status console's pure helpers, isolated from any transport: PR-URL
    parsing, ISO parsing, and age formatting."""

    def _rec(self, body, created):
        return {"body": body, "createdAt": created}

    def test_parse_pr_url_from_delivered_marker(self):
        recs = [self._rec(dlv("w1", pr="https://github.com/o/r/pull/42") + "\n\nbody", "t")]
        self.assertEqual(kraken.parse_pr_url(recs), "https://github.com/o/r/pull/42")

    def test_parse_pr_url_newest_wins(self):
        recs = [
            self._rec(dlv("w1", pr="https://github.com/o/r/pull/1"), "t1"),
            self._rec(dlv("w2", pr="https://github.com/o/r/pull/9"), "t2"),
        ]
        self.assertEqual(kraken.parse_pr_url(recs), "https://github.com/o/r/pull/9")

    def test_parse_pr_url_fallback_to_url_in_prose(self):
        recs = [self._rec("landed in https://github.com/o/r/pull/7 fyi", "t")]
        self.assertEqual(kraken.parse_pr_url(recs), "https://github.com/o/r/pull/7")

    def test_parse_pr_url_none_when_absent(self):
        recs = [self._rec(dlv("w1") + "\n\njust prose", "t")]
        self.assertIsNone(kraken.parse_pr_url(recs))

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
    """compute_status: the whole report assembled from queue nodes, the claim
    refs (in-flight worker/age/msg) and injected readers (awaiting-merge PR
    link) — grouping, project filter, orphan flagging, and transport-failure
    propagation, all with no gh."""

    NOW = kraken.parse_iso("2026-07-01T10:00:00Z")

    def _node(self, number, title, labels, created="2026-07-01T00:00:00Z"):
        return {
            "number": number, "title": title, "createdAt": created,
            "labels": {"nodes": [{"name": n} for n in labels]},
        }

    def _call(self, nodes, project="", claim_refs=None, commit_meta=None,
              comments=None, merged=None, projects=None):
        comments = comments or {}
        merged = merged or {}
        return kraken.compute_status(
            "o/tasks", project, nodes, self.NOW,
            claim_refs=claim_refs or {},
            commit_meta=commit_meta or {},
            comment_reader=lambda r, i: comments.get(i, []),
            pr_merged=lambda u: merged.get(u, False),
            project_lister=lambda r: projects if projects is not None else [])

    def test_groups_by_held_label_with_ref_liveness(self):
        nodes = [
            self._node(88, "review", ["kraken-task", "project:app", "awaiting-merge"]),
            self._node(97, "decide", ["kraken-task", "project:app", "needs-decision"]),
            self._node(99, "running", ["kraken-task", "project:app", "in-progress"]),
            self._node(12, "queued", ["kraken-task", "project:app"]),
        ]
        comments = {88: [{"body": dlv("w", pr="https://x/pull/1"), "createdAt": "t"}]}
        claim_refs = {99: "s99"}
        commit_meta = {"s99": {"committedDate": "2026-07-01T09:00:00Z",
                               "message": hb("w1", "still going")}}
        report = self._call(nodes, claim_refs=claim_refs, commit_meta=commit_meta,
                            comments=comments, projects=["app"])
        self.assertEqual([r["number"] for r in report["review_queue"]], [88])
        self.assertEqual([r["number"] for r in report["decision_queue"]], [97])
        self.assertEqual([r["number"] for r in report["in_flight"]], [99])
        self.assertEqual(report["in_flight"][0]["worker"], "w1")
        self.assertEqual(report["in_flight"][0]["heartbeat_age_seconds"], 3600)
        self.assertEqual(report["in_flight"][0]["heartbeat_msg"], "still going")

    def test_in_flight_from_ref_even_without_the_label(self):
        # The crash window: a won CAS whose in-progress projection has not landed
        # is still in flight (the ref is the truth).
        nodes = [self._node(50, "just claimed", ["kraken-task", "project:app"])]
        report = self._call(
            nodes, claim_refs={50: "s"},
            commit_meta={"s": {"committedDate": "2026-07-01T09:30:00Z", "message": clm("w9")}},
            projects=["app"])
        self.assertEqual([r["number"] for r in report["in_flight"]], [50])
        self.assertEqual(report["in_flight"][0]["worker"], "w9")

    def test_in_progress_label_without_a_ref_is_unknown_age(self):
        # An orphan projection (label, no ref): surfaced in flight but with no
        # worker/age — the reaper will requeue it.
        nodes = [self._node(99, "silent", ["kraken-task", "project:app", "in-progress"])]
        report = self._call(nodes, projects=["app"])
        item = report["in_flight"][0]
        self.assertIsNone(item["heartbeat_age_seconds"])
        self.assertIsNone(item["worker"])

    def test_project_filter(self):
        nodes = [
            self._node(1, "a", ["kraken-task", "project:app", "in-progress"]),
            self._node(2, "b", ["kraken-task", "project:web", "in-progress"]),
        ]
        claim_refs = {1: "s1", 2: "s2"}
        commit_meta = {"s1": {"committedDate": "2026-07-01T09:00:00Z", "message": clm("w")},
                       "s2": {"committedDate": "2026-07-01T09:00:00Z", "message": clm("w")}}
        report = self._call(nodes, project="web", claim_refs=claim_refs, commit_meta=commit_meta)
        self.assertEqual([r["number"] for r in report["in_flight"]], [2])
        self.assertEqual(report["project"], "web")
        self.assertEqual(report["projects"], ["web"])

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
        report = self._call(nodes, comments=comments, merged=merged, projects=["app"])
        self.assertEqual(report["orphans"], [88])
        flags = {r["number"]: r["orphan"] for r in report["review_queue"]}
        self.assertTrue(flags[88])
        self.assertFalse(flags[91])

    def test_awaiting_merge_comment_failure_propagates_none(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            claim_refs={}, commit_meta={},
            comment_reader=lambda r, i: None,  # transport failure
            pr_merged=lambda u: False,
            project_lister=lambda r: [])
        self.assertIsNone(report)

    def test_pr_read_failure_propagates_none(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        comments = {88: [{"body": dlv("w", pr="https://x/pull/5"), "createdAt": "t"}]}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            claim_refs={}, commit_meta={},
            comment_reader=lambda r, i: comments.get(i, []),
            pr_merged=lambda u: None,  # transport failure
            project_lister=lambda r: [])
        self.assertIsNone(report)


class ReconcilerClassificationTests(unittest.TestCase):
    """cmd_reap's four reconciler rules, isolated from transport: the claim refs,
    their commit meta, the per-issue meta, and the in-progress list are all
    injected via mocked helpers, so only the rule dispatch — reclaim / orphan
    lock / heal / requeue — is under test. Every write is recorded, none real."""

    def setUp(self):
        self._saved = {name: getattr(kraken, name) for name in (
            "claim_ref_list", "resolve_commit_meta", "resolve_issue_meta",
            "open_issue_numbers", "claim_ref_delete", "swap_labels", "post_comment")}
        self.deleted = []
        self.swaps = []
        self.comments = []
        kraken.claim_ref_delete = lambda repo, n: (self.deleted.append(n) or True)
        kraken.swap_labels = lambda repo, n, remove=None, add=None: (
            self.swaps.append((n, remove, add)) or True)
        kraken.post_comment = lambda repo, n, body: (self.comments.append(n) or True)

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(kraken, name, fn)

    def _reap(self, refs, commit_meta, issue_meta, in_progress, max_hours=6):
        kraken.claim_ref_list = lambda repo: refs
        kraken.resolve_commit_meta = lambda repo, shas: commit_meta
        kraken.resolve_issue_meta = lambda repo, nums: issue_meta
        kraken.open_issue_numbers = lambda repo, label: in_progress
        args = SimpleNamespace(repo="o/tasks", max_hours=max_hours)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.cmd_reap(args)
        return rc, buf.getvalue()

    def _fresh(self):
        return {"committedDate": kraken.datetime.datetime.now(
            kraken.datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message": clm("w")}

    def _old(self, hours):
        dt = kraken.datetime.datetime.now(kraken.datetime.timezone.utc) - \
            kraken.datetime.timedelta(hours=hours)
        return {"committedDate": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "message": clm("w")}

    def test_rule1_orphan_lock_on_held_or_closed(self):
        # ref on a needs-decision issue, and a ref on a closed issue: both deleted.
        refs = {4: "s4", 6: "s6"}
        rc, out = self._reap(
            refs, {"s4": self._fresh(), "s6": self._fresh()},
            {4: (True, ["needs-decision"]), 6: (False, [])}, [])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(sorted(self.deleted), [4, 6])
        self.assertEqual(self.swaps, [], "orphan-lock must not touch labels")
        self.assertEqual(self.comments, [], "orphan-lock must not comment")

    def test_rule2_stale_claim_reclaims_and_deletes(self):
        refs = {1: "s1"}
        rc, out = self._reap(refs, {"s1": self._old(8)},
                             {1: (True, ["in-progress"])}, [1])
        self.assertIn((1, "in-progress", "needs-decision"), self.swaps)
        self.assertIn(1, self.comments)
        self.assertIn(1, self.deleted, "stale claim's ref must be deleted last")

    def test_rule3_heal_missing_label(self):
        refs = {5: "s5"}
        rc, out = self._reap(refs, {"s5": self._fresh()},
                             {5: (True, [])}, [])
        self.assertIn((5, None, "in-progress"), self.swaps)
        self.assertNotIn(5, self.deleted, "a live claim's ref must survive the heal")

    def test_rule4_requeue_orphan_projection(self):
        # in-progress label #3 with NO ref -> requeue (label removed + note).
        rc, out = self._reap({}, {}, {}, [3])
        self.assertIn((3, "in-progress", None), self.swaps)
        self.assertIn(3, self.comments)

    def test_fresh_claim_with_label_is_left_alone(self):
        refs = {2: "s2"}
        rc, out = self._reap(refs, {"s2": self._fresh()},
                             {2: (True, ["in-progress"])}, [2])
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.comments, [])


class WakeRetryDueTests(unittest.TestCase):
    """The watcher's lost-wake retry gate (wake_retry_due): re-emit ONLY when the
    StopFailure hook stamped the wake-retry flag after the watcher's last
    emission and the retry spacing has elapsed."""

    def test_no_flag_means_no_retry(self):
        self.assertFalse(kraken.wake_retry_due(None, 1000.0, 300, 999999.0))

    def test_flag_after_last_emit_and_spacing_elapsed_is_due(self):
        self.assertTrue(kraken.wake_retry_due(1001.0, 1000.0, 300, 1300.0))

    def test_flag_before_last_emit_is_stale(self):
        self.assertFalse(kraken.wake_retry_due(900.0, 1000.0, 300, 999999.0))

    def test_spacing_not_elapsed_yet(self):
        self.assertFalse(kraken.wake_retry_due(1001.0, 1000.0, 300, 1299.0))

    def test_refreshed_flag_re_arms_after_each_failed_retry(self):
        self.assertFalse(kraken.wake_retry_due(1301.0, 1300.0, 300, 1500.0))
        self.assertTrue(kraken.wake_retry_due(1301.0, 1300.0, 300, 1600.0))


class AssetClassifierTests(unittest.TestCase):
    """The pure asset classifier — the read side of `init --upgrade`, isolated
    from any network. The plugin's bundled bytes are the single source of truth,
    so there is no manifest to keep in sync: an asset is either in sync with the
    bundled copy or drifted from it."""

    def test_bundled_asset_covers_every_init_asset(self):
        # A stale workflow is as harmful as a stale parser (the reaper's
        # permissions flipped between protocol/3 and /4), so every one of the six
        # init assets must be readable as a bundled reference, not just kraken.py.
        for name, _dest, _msg in kraken.INIT_ASSETS:
            self.assertTrue(kraken.bundled_asset(name),
                            "%s has no bundled bytes to compare against" % name)

    def test_classify_asset_absent(self):
        self.assertEqual(kraken.classify_asset(None, b"bundled"), "absent")

    def test_classify_asset_unchanged(self):
        self.assertEqual(kraken.classify_asset(b"same", b"same"), "unchanged")

    def test_classify_asset_drifted(self):
        self.assertEqual(kraken.classify_asset(b"hand edit", b"bundled"), "drifted")


class ProtocolHandshakeTests(unittest.TestCase):
    """The drift handshake's pure logic, with the one contents read mocked: the
    vendored `.github/kraken.py` is compared byte-for-byte with this worker's
    bundled copy, and the match / drift / fail-closed decision gates a drain."""

    def setUp(self):
        self._orig_get = kraken.gh_get_content

    def tearDown(self):
        kraken.gh_get_content = self._orig_get

    def _vendored(self, raw):
        kraken.gh_get_content = lambda repo, path: raw

    def test_matching_content_is_ok(self):
        # The coordination repo vendors the exact bundled kraken.py -> in sync.
        self._vendored(kraken.bundled_asset("kraken.py"))
        ok, msg = kraken.verify_protocol("o/tasks")
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_drift_refuses_and_names_upgrade(self):
        self._vendored(b"# a stale, drifted kraken.py\n")
        ok, msg = kraken.verify_protocol("o/tasks")
        self.assertFalse(ok)
        self.assertIn("differs", msg)
        self.assertIn("init --upgrade", msg)

    def test_unreadable_vendored_file_fails_closed(self):
        self._vendored(None)  # 404 / transport fault both surface as None
        ok, msg = kraken.verify_protocol("o/tasks")
        self.assertFalse(ok)
        self.assertIn("cannot verify", msg)
        self.assertIn("init --upgrade", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
