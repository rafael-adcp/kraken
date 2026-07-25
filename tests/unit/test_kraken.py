#!/usr/bin/env python3
"""Unit tests for kraken.py — the parts the gh-stub conformance suite cannot
exercise in isolation: the protocol/4 claim-ref helpers (the CAS, the batched
commit/issue meta reads, the liveness decode), marker decoding edge cases, and
the comment pagination the status/validator paths still rely on.

Stdlib only (unittest), no network, no gh. Run: python3 tests/unit/test_kraken.py
"""

import os
import re
import sys
import json
import tempfile
import unittest
from types import SimpleNamespace
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

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


def expired(worker="w2", previous="w1"):
    return kraken.make_marker({"type": "lease-expired", "worker": worker,
                               "previous_worker": previous})


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
        self._orig_request = kraken.http_request
        self._orig_graphql = kraken.graphql

    def tearDown(self):
        kraken.http_request = self._orig_request
        kraken.graphql = self._orig_graphql

    def test_claim_ref_name_carries_the_generation(self):
        # Generation 0 IS the protocol/5 name — never written any more, only
        # read, so an old queue needs no migration.
        self.assertEqual(kraken.claim_ref(42, 0), "refs/kraken/claims/42")
        self.assertEqual(kraken.claim_ref(42, 1), "refs/kraken/claims/42/1")
        self.assertEqual(kraken.claim_ref("7", 12), "refs/kraken/claims/7/12")

    def test_parse_claim_ref_reads_both_shapes_and_rejects_the_rest(self):
        self.assertEqual(kraken.parse_claim_ref("refs/kraken/claims/42"), (42, 0))
        self.assertEqual(kraken.parse_claim_ref("refs/kraken/claims/42/3"), (42, 3))
        for bad in ("refs/heads/main", "refs/kraken/claims/", "refs/kraken/claims/x",
                    "refs/kraken/claims/42/x", "refs/kraken/claims/42/1/2"):
            self.assertIsNone(kraken.parse_claim_ref(bad), bad)

    def test_create_claim_commit_is_an_orphan_marker_commit(self):
        captured = {}

        def fake_request(method, path, body=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return 201, json.dumps({"sha": "abc123"})
        kraken.http_request = fake_request

        sha = kraken.create_claim_commit("o/tasks", {"type": "claim", "worker": "w1"})
        self.assertEqual(sha, "abc123")
        self.assertEqual(captured["method"], "POST")
        self.assertIn("repos/o/tasks/git/commits", captured["path"])
        payload = captured["body"]  # the JSON body is a dict now, not a string
        self.assertEqual(payload["parents"], [], "claim commit must be an orphan")
        self.assertEqual(payload["tree"], kraken.EMPTY_TREE_SHA, "claim commit must use the empty tree")
        self.assertEqual(kraken.parse_marker(payload["message"]),
                         {"type": "claim", "worker": "w1"},
                         "the commit message IS the claim marker")

    def test_create_claim_commit_falls_back_to_head_tree_on_422(self):
        trees = []

        def fake_request(method, path, body=None):
            if method == "GET" and path.endswith("commits/HEAD"):
                return 200, json.dumps({"commit": {"tree": {"sha": "headtree"}}})
            trees.append(body["tree"])
            if body["tree"] == kraken.EMPTY_TREE_SHA:
                return 422, ""
            return 201, json.dumps({"sha": "def456"})
        kraken.http_request = fake_request

        sha = kraken.create_claim_commit("o/tasks", {"type": "claim", "worker": "w1"})
        self.assertEqual(sha, "def456")
        self.assertEqual(trees, [kraken.EMPTY_TREE_SHA, "headtree"],
                         "must retry with the HEAD tree after the empty-tree 422")

    def test_create_claim_commit_returns_none_on_transport_fault(self):
        kraken.http_request = lambda m, p, body=None: (kraken.STATUS_NETWORK_FAILURE, "")
        self.assertIsNone(kraken.create_claim_commit("o/t", {"type": "claim", "worker": "w"}))

    def test_claim_ref_create_maps_the_cas_outcomes(self):
        kraken.http_request = lambda m, p, body=None: (201, "{}")
        self.assertEqual(kraken.claim_ref_create("o/t", 7, 1, "sha"), "won")

        # HTTP 422 IS the CAS-lost signal — an integer status, not a stderr scrape.
        kraken.http_request = lambda m, p, body=None: (422, "")
        self.assertEqual(kraken.claim_ref_create("o/t", 7, 1, "sha"), "lost")

        kraken.http_request = lambda m, p, body=None: (500, "")
        self.assertEqual(kraken.claim_ref_create("o/t", 7, 1, "sha"), "fail")

    def test_advance_lease_creates_the_next_generation(self):
        # The single contended write behind a claim, a steal AND a renewal: it
        # always creates gen+1, so all three race on one ref name.
        created = {}

        def fake_request(method, path, body=None):
            if path.endswith("/git/commits"):
                return (201, json.dumps({"sha": "fresh"}))
            created["ref"] = (body or {}).get("ref")
            created["sha"] = (body or {}).get("sha")
            return (201, "{}")
        kraken.http_request = fake_request

        verdict, gen, sha = kraken.advance_lease(
            "o/t", 7, 4, {"type": "claim", "worker": "w1"})
        self.assertEqual((verdict, gen, sha), ("won", 5, "fresh"))
        self.assertEqual(created["ref"], "refs/kraken/claims/7/5")
        self.assertEqual(created["sha"], "fresh")

    def test_advance_lease_reports_the_cas_loss(self):
        def fake_request(method, path, body=None):
            if path.endswith("/git/commits"):
                return (201, json.dumps({"sha": "fresh"}))
            return (422, "")
        kraken.http_request = fake_request
        self.assertEqual(kraken.advance_lease("o/t", 7, 1, {"type": "claim"}),
                         ("lost", None, None))

    def test_claim_ref_delete_tolerates_a_missing_ref(self):
        kraken.http_request = lambda m, p, body=None: (204, "")
        self.assertTrue(kraken.claim_ref_delete("o/t", 7, 1))
        # Already gone (422) is success — the delete is idempotent.
        kraken.http_request = lambda m, p, body=None: (422, "")
        self.assertTrue(kraken.claim_ref_delete("o/t", 7, 1))
        # A real transport fault is not tolerated.
        kraken.http_request = lambda m, p, body=None: (kraken.STATUS_NETWORK_FAILURE, "")
        self.assertFalse(kraken.claim_ref_delete("o/t", 7, 1))

    def test_dropping_generations_reports_a_partial_failure(self):
        seen = []

        def fake_request(method, path, body=None):
            seen.append(path)
            return (204, "") if path.endswith("/1") else (500, "")
        kraken.http_request = fake_request
        self.assertFalse(kraken.drop_generations("o/t", 7, [1, 2]))
        self.assertEqual(len(seen), 2, "a failed delete must not stop the rest")

    def test_claim_ref_list_groups_generations_per_issue(self):
        kraken.http_request = lambda m, p, body=None: (200, json.dumps([
            {"ref": "refs/kraken/claims/7", "object": {"sha": "sha7g0"}},
            {"ref": "refs/kraken/claims/7/2", "object": {"sha": "sha7g2"}},
            {"ref": "refs/kraken/claims/12/1", "object": {"sha": "sha12"}},
            {"ref": "refs/heads/main", "object": {"sha": "nope"}},
        ]))
        self.assertEqual(kraken.claim_ref_list("o/t"),
                         {7: [(0, "sha7g0"), (2, "sha7g2")], 12: [(1, "sha12")]})

    def test_holder_shas_reads_only_the_highest_generation(self):
        # The lease clock lives on the holder; a superseded generation's date
        # decides nothing, so the batched commit read stays one per task.
        refs = {7: [(0, "old"), (2, "holder")], 12: [(1, "solo")]}
        self.assertEqual(sorted(kraken.holder_shas(refs)), ["holder", "solo"])

    def test_claim_refs_of_filters_the_prefix_match_client_side(self):
        # matching-refs is a PREFIX match: asking for claims/12 also returns
        # claims/120. Filtering server-side is not an option, so the client does
        # it — a neighbouring issue's lease must never read as this issue's.
        kraken.http_request = lambda m, p, body=None: (200, json.dumps([
            {"ref": "refs/kraken/claims/12/1", "object": {"sha": "mine"}},
            {"ref": "refs/kraken/claims/120/9", "object": {"sha": "theirs"}},
        ]))
        self.assertEqual(kraken.claim_refs_of("o/t", 12), (True, [(1, "mine")]))

    def test_claim_refs_of_separates_absent_from_unreadable(self):
        kraken.http_request = lambda m, p, body=None: (200, "[]")
        self.assertEqual(kraken.claim_refs_of("o/t", 7), (True, []))
        kraken.http_request = lambda m, p, body=None: (500, "")
        self.assertEqual(kraken.claim_refs_of("o/t", 7), (False, []))

    def test_claim_ref_list_transport_failure_is_none(self):
        kraken.http_request = lambda m, p, body=None: (500, "")
        self.assertIsNone(kraken.claim_ref_list("o/t"))

    def test_claim_ref_owner_names_the_ref_holder(self):
        # The §5 re-check discriminator: a 422 is a real loss only when the
        # lease belongs to another worker. claim_ref_owner reads the issue's
        # generation ladder and takes the HIGHEST one — the holder — then reads
        # its commit marker to name them.
        def present(m, p, body=None):
            # Only issue 7 carries a lease; every other issue reads as empty.
            if "matching-refs/kraken/claims/7" in p:
                return (200, json.dumps(
                    [{"ref": "refs/kraken/claims/7/1", "object": {"sha": "sha7"}}]))
            return (200, "[]")
        kraken.http_request = present
        kraken.graphql = lambda q: {"data": {"repository": {
            "c0": {"committedDate": "t", "message": clm("w1")}}}}
        self.assertEqual(kraken.claim_ref_owner("o/t", 7), "w1")
        self.assertEqual(kraken.claim_ref_owner("o/t", "7"), "w1")
        # No lease at all → None (treated as not-ours by the caller).
        self.assertIsNone(kraken.claim_ref_owner("o/t", 9))
        # Transport failure → None, never a guessed owner — an ambiguous read
        # must never turn a real CAS loss into a false win.
        kraken.http_request = lambda m, p, body=None: (500, "")
        self.assertIsNone(kraken.claim_ref_owner("o/t", 7))
        kraken.http_request = lambda m, p, body=None: (kraken.STATUS_NETWORK_FAILURE, "")
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

    def test_protocol_version_echoes_the_constant(self):
        # The authoritative accessor lint-skills.sh [1d] reads to keep
        # plugin.json's declared kraken-protocol/<n> from drifting off the code.
        self.assertEqual(self._run("protocol-version"), [str(kraken.PROTOCOL_VERSION)])

    def test_marker_types_are_the_protocol4_vocabulary(self):
        # Every type kraken.py emits (claim/heartbeat on the ref, the rest on
        # comments — including the non-state-changing `note`); requeue is
        # operator-only and deliberately absent.
        self.assertEqual(
            set(kraken.MARKER_TYPES),
            {"claim", "heartbeat", "needs-decision", "delivered", "released",
             "stale-claim", "lease-expired", "note"})
        self.assertNotIn("requeue", kraken.MARKER_TYPES)

    def test_lease_clock_is_readable_off_the_contract(self):
        # SKILL.md and the docs quote the cadence rather than copying it, so the
        # two fields must answer the live constants — including the derivation.
        ttl = int(self._run("lease-ttl")[0])
        renew = int(self._run("lease-renew")[0])
        self.assertEqual(ttl, kraken.lease_ttl_seconds())
        self.assertEqual(renew, ttl // kraken.LEASE_RENEW_DIVISOR)

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

    def test_the_documented_assets_are_installed(self):
        dests = {dest for _, dest, _ in kraken.INIT_ASSETS}
        self.assertEqual(dests, {
            ".github/ISSUE_TEMPLATE/task.yml",
        })

    def test_retired_assets_are_pruned_not_installed(self):
        """protocol/5 retired the scheduled reconciler: init must no longer
        install it AND must delete the copy an earlier release left behind."""
        dests = {dest for _, dest, _ in kraken.INIT_ASSETS}
        pruned = {dest for dest, _ in kraken.OBSOLETE_ASSETS}
        self.assertIn(".github/workflows/reclaim-stale.yml", pruned)
        self.assertIn(".github/workflows/requeue-on-reply.yml", pruned)
        self.assertIn(".github/workflows/cleanup-closed.yml", pruned)
        self.assertIn(".github/workflows/validate-task.yml", pruned)
        self.assertEqual(dests & pruned, set(),
                         "an asset cannot be both installed and pruned")
        for dest, sentinel in kraken.OBSOLETE_ASSETS:
            self.assertTrue(dest.startswith(".github/"))
            self.assertTrue(sentinel, f"{dest} has no authorship sentinel")

    def test_the_transition_program_is_no_longer_vendored(self):
        """protocol/5 runs nothing in the coordination repo, so there is nothing
        for a vendored copy of this program to execute — and no drift to police."""
        dests = {dest for _, dest, _ in kraken.INIT_ASSETS}
        pruned = {dest for dest, _ in kraken.OBSOLETE_ASSETS}
        self.assertNotIn(".github/kraken.py", dests)
        self.assertIn(".github/kraken.py", pruned,
                      "a repo set up by an older release keeps a stale copy forever")

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
                {"path": ".github/workflows/reclaim-stale.yml", "status": "removed"},
            ],
            "labels": ["kraken-task", "project:app"],
            "project": "app",
        }
        out = kraken.render_init(report)
        self.assertIn("init: repo acme/tasks (created)", out)
        self.assertIn("init: asset .github/ISSUE_TEMPLATE/task.yml (created)", out)
        self.assertIn("init: label project:app (upserted)", out)
        self.assertIn("init: asset .github/workflows/reclaim-stale.yml (removed)", out)
        self.assertIn("assets_created=1 assets_present=0 assets_removed=1 labels=2", out)


class CommentRecordsPaginationTests(unittest.TestCase):
    """comment_records must page past 100 comments — status' PR-link read and the
    validator's debounce both walk the whole thread, and a truncated 100-comment
    read would miss a delivered marker or an earlier validation comment."""

    def setUp(self):
        self._orig_request = kraken.http_request

    def tearDown(self):
        kraken.http_request = self._orig_request

    @staticmethod
    def _paged(recs):
        """A fake http_request that serves `recs` as REST comment pages, keyed on
        the per_page/page query http_paginated walks."""
        def fake(method, path, body=None):
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            per = kraken.PER_PAGE
            chunk = recs[(page - 1) * per: page * per]
            return 200, json.dumps(chunk)
        return fake

    def test_uses_paginated_rest_endpoint(self):
        calls = []

        def fake(method, path, body=None):
            calls.append((method, path))
            return 200, json.dumps([])
        kraken.http_request = fake
        kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertIn("repos/OWNER/tasks/issues/42/comments", calls[0][1])
        self.assertIn("per_page=", calls[0][1])
        self.assertIn("page=", calls[0][1])

    def test_returns_records_past_one_hundred(self):
        # REST spells the timestamp created_at; comment_records maps it.
        recs = [{"body": f"c {i}", "created_at": f"2026-07-01T00:{i % 60:02d}:00Z"}
                for i in range(150)]
        recs[129] = {"body": dlv("w", pr="https://x/pull/9"), "created_at": "2026-07-09T00:00:00Z"}
        kraken.http_request = self._paged(recs)
        result = kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(result), 150)
        # The delivered marker past comment 100 is found — invisible under a
        # 100-comment truncation.
        self.assertEqual(kraken.parse_pr_url(result), ("https://x/pull/9", "marker"))

    def test_maps_created_at_to_createdat(self):
        recs = [
            {"body": dlv("w1", pr="https://x/pull/1"), "created_at": "2026-07-01T00:00:00Z"},
            {"body": "just prose", "created_at": "2026-07-01T05:00:00Z"},
        ]
        kraken.http_request = self._paged(recs)
        result = kraken.comment_records("OWNER/tasks", "42")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["createdAt"], "2026-07-01T00:00:00Z")
        self.assertEqual(kraken.parse_pr_url(result), ("https://x/pull/1", "marker"))

    def test_transport_failure_returns_none(self):
        kraken.http_request = lambda m, p, body=None: (500, "")
        self.assertIsNone(kraken.comment_records("OWNER/tasks", "42"))


class ClaimNextIterationTests(unittest.TestCase):
    """cmd_claim_next's loop, isolated from any transport: classify_queue and the
    per-candidate _claim_once are both mocked, so only the iteration logic —
    skip-on-held, skip-on-lost, forward-only, stop-on-transport, honest-empty —
    is under test."""

    def setUp(self):
        self._orig_classify = kraken.classify_queue
        self._orig_claim_once = kraken._claim_once
        self._orig_verify_project = kraken.verify_project
        self._orig_read_queue = kraken.read_queue
        self._orig_commit_meta = kraken.resolve_commit_meta
        # The project preflight and the §6 reconcile are exercised by their own
        # tests; here they pass over an empty queue read, so only the iteration
        # logic is under test.
        kraken.verify_project = lambda repo, project: (True, "")
        kraken.read_queue = lambda repo, now=None, ttl=None: ([], {})
        kraken.resolve_commit_meta = lambda repo, shas: {}
        self.attempted = []

    def tearDown(self):
        kraken.classify_queue = self._orig_classify
        kraken._claim_once = self._orig_claim_once
        kraken.verify_project = self._orig_verify_project
        kraken.read_queue = self._orig_read_queue
        kraken.resolve_commit_meta = self._orig_commit_meta

    def _run(self, rows, claim_results, json_mode=False):
        kraken.classify_queue = (
            lambda repo, project, include_body=False, read=None: rows
        )
        # claim-next re-derives each candidate's requeue verdict off the node the
        # filter used, so the read has to carry a node per row.
        # rows=None models a failed queue read, which now surfaces at read_queue.
        nodes = [{"number": r[0], "title": r[1], "createdAt": r[2], "body": "",
                  "labels": {"nodes": [{"name": "kraken-task"}]},
                  "comments": {"nodes": []},
                  "blockedBy": {"nodes": []}} for r in (rows or [])]
        kraken.read_queue = (
            lambda repo, now=None, ttl=None: None if rows is None else (nodes, {})
        )

        def fake_claim_once(repo, issue, worker, allow_held=(), lease=None,
                            ttl=None, probe_lease=False):
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


class VerifyProjectTests(unittest.TestCase):
    """The project preflight, isolated from transport: a worker pointed at a
    `project:<name>` label the coordination repo does not carry is permanently
    deaf — every task is filtered out client-side, so the queue reads as empty
    forever. The check refuses on a genuinely absent label, and never declares a
    project missing from a label read that merely failed."""

    def setUp(self):
        self._orig_list = kraken.list_projects

    def tearDown(self):
        kraken.list_projects = self._orig_list

    def _verify(self, projects, project="app"):
        kraken.list_projects = lambda repo: projects
        return kraken.verify_project("OWNER/tasks", project)

    def test_configured_project_passes(self):
        ok, message = self._verify(["app", "docs"])
        self.assertIs(ok, True)
        self.assertEqual(message, "")

    def test_unknown_project_refuses_and_names_the_configured_ones(self):
        ok, message = self._verify(["app", "docs"], project="ap")
        self.assertIs(ok, False)
        self.assertIn("project:ap", message, "refusal did not name the missing label")
        self.assertIn("app", message, "refusal did not list the configured projects")
        self.assertIn("docs", message, "refusal did not list the configured projects")
        self.assertIn("--project", message, "refusal did not name the fix")

    def test_repo_without_any_project_label_refuses(self):
        ok, message = self._verify([], project="app")
        self.assertIs(ok, False)
        self.assertIn("none configured", message)

    def test_transport_failure_is_not_a_missing_project(self):
        ok, message = self._verify(None)
        self.assertIsNone(ok, "a failed label read must not read as a missing project")
        self.assertIn("gh-failure", message)


class ClaimNextProjectGateTests(unittest.TestCase):
    """cmd_claim_next's project preflight: an unconfigured project refuses with
    EXIT_UNKNOWN_PROJECT before the queue is read and before any write."""

    def setUp(self):
        self._orig_verify_project = kraken.verify_project
        self._orig_classify = kraken.classify_queue
        self._orig_read_queue = kraken.read_queue
        self._orig_commit_meta = kraken.resolve_commit_meta
        kraken.read_queue = lambda repo, now=None, ttl=None: ([], {})
        kraken.resolve_commit_meta = lambda repo, shas: {}
        self.classified = []

        def spy_classify(repo, project, include_body=False, read=None):
            self.classified.append(project)
            return []
        kraken.classify_queue = spy_classify

    def tearDown(self):
        kraken.verify_project = self._orig_verify_project
        kraken.classify_queue = self._orig_classify
        kraken.read_queue = self._orig_read_queue
        kraken.resolve_commit_meta = self._orig_commit_meta

    def _run(self, verdict):
        kraken.verify_project = lambda repo, project: verdict
        args = SimpleNamespace(repo="OWNER/tasks", project="app",
                               worker="w-gate", json=False)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.cmd_claim_next(args)
        return rc, buf.getvalue()

    def test_unknown_project_refuses_before_reading_the_queue(self):
        rc, out = self._run((False, "unknown project: no project:app label"))
        self.assertEqual(rc, kraken.EXIT_UNKNOWN_PROJECT)
        self.assertEqual(self.classified, [], "a refused drain still read the queue")
        self.assertIn("unknown project", out)

    def test_transport_failure_during_the_check_is_twenty(self):
        rc, out = self._run((None, "claim-next: gh-failure stage=project"))
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertEqual(self.classified, [], "an unverified project still read the queue")
        self.assertIn("gh-failure", out)

    def test_configured_project_proceeds_to_the_queue(self):
        rc, out = self._run((True, ""))
        self.assertEqual(rc, kraken.EXIT_NONE)
        self.assertEqual(self.classified, ["app"])


class WatchProjectGateTests(unittest.TestCase):
    """cmd_watch's startup preflight: a watcher armed on a project label that
    does not exist would poll forever without ever waking anyone, so it refuses
    before the first poll. A failed label read is NOT a refusal — the poll loop
    already tolerates transport faults, so the watcher warns and arms anyway."""

    def setUp(self):
        self._orig_verify_project = kraken.verify_project
        self._orig_snapshot = kraken.snapshot_state
        self.polled = []

        def spy_snapshot(repo, project):
            # The poll loop is infinite; the first poll is all this needs to see.
            self.polled.append(project)
            raise KeyboardInterrupt
        kraken.snapshot_state = spy_snapshot

    def tearDown(self):
        kraken.verify_project = self._orig_verify_project
        kraken.snapshot_state = self._orig_snapshot

    def _run(self, verdict):
        kraken.verify_project = lambda repo, project: verdict
        args = SimpleNamespace(repo="OWNER/tasks", project="app")
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = kraken.cmd_watch(args)
            except KeyboardInterrupt:
                rc = "polled"
        return rc, err.getvalue()

    def test_unknown_project_refuses_to_arm(self):
        rc, err = self._run((False, "unknown project: no project:app label"))
        self.assertEqual(rc, kraken.EXIT_UNKNOWN_PROJECT)
        self.assertEqual(self.polled, [], "a refused watcher still polled the queue")
        self.assertIn("unknown project", err)

    def test_configured_project_arms(self):
        rc, err = self._run((True, ""))
        self.assertEqual(rc, "polled")
        self.assertEqual(self.polled, ["app"])

    def test_transport_failure_warns_but_still_arms(self):
        rc, err = self._run((None, "watch: gh-failure stage=project"))
        self.assertEqual(rc, "polled", "a failed label read must not kill the watcher")
        self.assertIn("gh-failure", err)


class StatusHelperTests(unittest.TestCase):
    """The status console's pure helpers, isolated from any transport: PR-URL
    parsing, ISO parsing, and age formatting."""

    def _rec(self, body, created):
        return {"body": body, "createdAt": created}

    def test_parse_pr_url_from_delivered_marker(self):
        recs = [self._rec(dlv("w1", pr="https://github.com/o/r/pull/42") + "\n\nbody", "t")]
        self.assertEqual(kraken.parse_pr_url(recs),
                         ("https://github.com/o/r/pull/42", "marker"))

    def test_parse_pr_url_newest_wins(self):
        recs = [
            self._rec(dlv("w1", pr="https://github.com/o/r/pull/1"), "t1"),
            self._rec(dlv("w2", pr="https://github.com/o/r/pull/9"), "t2"),
        ]
        self.assertEqual(kraken.parse_pr_url(recs),
                         ("https://github.com/o/r/pull/9", "marker"))

    def test_parse_pr_url_marker_beats_a_foreign_link_in_prose(self):
        # #71: the delivery URL is a structured field, not something to grep out
        # of the thread. A human quoting SOMEONE ELSE'S PR first must never
        # outrank the worker's own delivered marker.
        recs = [
            self._rec("related work: https://github.com/other/repo/pull/3", "t1"),
            self._rec(dlv("w1", pr="https://github.com/o/r/pull/42"), "t2"),
        ]
        self.assertEqual(kraken.parse_pr_url(recs),
                         ("https://github.com/o/r/pull/42", "marker"))

    def test_parse_pr_url_fallback_to_url_in_prose(self):
        # A legacy thread, delivered before the marker carried `pr`: the prose is
        # all there is — read it, but say it was read from free text.
        recs = [self._rec("landed in https://github.com/o/r/pull/7 fyi", "t")]
        self.assertEqual(kraken.parse_pr_url(recs),
                         ("https://github.com/o/r/pull/7", "legacy-free-text"))

    def test_parse_pr_url_marker_without_pr_falls_back(self):
        # A delivered marker whose `pr` is missing or null is not a delivery URL;
        # it must not break the read, and it must not shadow the legacy prose.
        for marker in (dlv("w1"), kraken.make_marker(
                {"type": "delivered", "worker": "w1", "pr": None})):
            recs = [self._rec("see https://github.com/o/r/pull/7", "t1"),
                    self._rec(marker + "\n\ndone", "t2")]
            self.assertEqual(kraken.parse_pr_url(recs),
                             ("https://github.com/o/r/pull/7", "legacy-free-text"),
                             marker)

    def test_parse_pr_url_ignores_pr_on_a_non_delivered_marker(self):
        # `delivered` is the delivery marker; a `pr` key riding any other type is
        # not one, and a marker line is never re-read as prose either.
        recs = [self._rec(kraken.make_marker(
            {"type": "note", "worker": "w1", "pr": "https://github.com/o/r/pull/3"}), "t")]
        self.assertEqual(kraken.parse_pr_url(recs), (None, None))

    def test_parse_pr_url_none_when_absent(self):
        recs = [self._rec(dlv("w1") + "\n\njust prose", "t")]
        self.assertEqual(kraken.parse_pr_url(recs), (None, None))

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

    def test_parse_github_pr_url_parts(self):
        self.assertEqual(kraken.parse_github_pr_url("https://github.com/o/r/pull/42"),
                         ("o", "r", "42"))

    def test_parse_github_pr_url_none_for_non_github_host(self):
        # #121: a GitLab MR (or any non-GitHub delivery) is a legitimate,
        # protocol-allowed delivery URL — just one kraken can't ask gh about.
        gitlab = "https://gitlab.com/group/project/-/merge_requests/2313"
        self.assertIsNone(kraken.parse_github_pr_url(gitlab))

    def test_parse_github_pr_url_none_when_absent(self):
        self.assertIsNone(kraken.parse_github_pr_url(None))
        self.assertIsNone(kraken.parse_github_pr_url(""))


class PrIsMergedTests(unittest.TestCase):
    """pr_is_merged: the orphan heuristic's only transport call. #121 — a
    non-GitHub delivery URL must read as "not confirmed merged" (False), never
    as the transport failure (None) that used to abort the whole status read."""

    def setUp(self):
        self._orig_request = kraken.http_request

    def tearDown(self):
        kraken.http_request = self._orig_request

    def test_non_github_url_is_false_without_any_gh_call(self):
        calls = []
        kraken.http_request = lambda m, p, body=None: (calls.append(1), (500, ""))[1]
        gitlab = "https://gitlab.com/group/project/-/merge_requests/2313"
        self.assertFalse(kraken.pr_is_merged(gitlab))
        self.assertEqual(calls, [], "a non-GitHub URL must never hit gh")

    def test_unparseable_url_is_false(self):
        self.assertFalse(kraken.pr_is_merged("not a url"))
        self.assertFalse(kraken.pr_is_merged(None))

    def test_github_transport_failure_is_none(self):
        kraken.http_request = lambda m, p, body=None: (500, "")
        self.assertIsNone(kraken.pr_is_merged("https://github.com/o/r/pull/1"))

    def test_github_merged_pr_is_true(self):
        kraken.http_request = lambda m, p, body=None: (
            200, json.dumps({"merged_at": "2026-07-01T00:00:00Z"}))
        self.assertTrue(kraken.pr_is_merged("https://github.com/o/r/pull/1"))

    def test_github_open_pr_is_false(self):
        kraken.http_request = lambda m, p, body=None: (
            200, json.dumps({"merged_at": None, "merged": False, "state": "open"}))
        self.assertFalse(kraken.pr_is_merged("https://github.com/o/r/pull/1"))


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
              comments=None, merged=None, projects=None, ttl=None):
        comments = comments or {}
        merged = merged or {}
        commit_meta = commit_meta or {}
        # Tests seed {issue: sha}; the lease view wants the generation ladder.
        refs = {n: [(1, sha)] for n, sha in (claim_refs or {}).items()}
        return kraken.compute_status(
            "o/tasks", project, nodes, self.NOW,
            leases=kraken.lease_state(refs, commit_meta, self.NOW,
                                      kraken.lease_ttl_seconds(ttl)),
            commit_meta=commit_meta,
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

    def test_non_github_delivery_url_is_never_a_transport_failure(self):
        # #121: a GitLab MR delivery must not turn `status` into gh-failure
        # stage=read — it's neither an orphan nor a transport failure, just
        # unverifiable. End-to-end through the real kraken.pr_is_merged (not
        # the injected lambda) so the fix's actual call path is exercised.
        orig_request = kraken.http_request
        calls = []
        kraken.http_request = lambda m, p, body=None: (calls.append(1), (500, ""))[1]
        try:
            nodes = [self._node(88, "gitlab delivery",
                                ["kraken-task", "project:app", "awaiting-merge"])]
            gitlab_mr = "https://gitlab.com/group/project/-/merge_requests/2313"
            comments = {88: [{"body": dlv("w", pr=gitlab_mr), "createdAt": "t"}]}
            report = kraken.compute_status(
                "o/tasks", "", nodes, self.NOW,
                leases={}, commit_meta={},
                comment_reader=lambda r, i: comments.get(i, []),
                pr_merged=kraken.pr_is_merged,
                project_lister=lambda r: ["app"])
        finally:
            kraken.http_request = orig_request
        self.assertIsNotNone(report, "a non-GitHub delivery must not be gh-failure")
        self.assertEqual(report["orphans"], [])
        self.assertEqual(calls, [], "gh must never be asked about a non-GitHub URL")
        item = report["review_queue"][0]
        self.assertFalse(item["orphan"])
        self.assertTrue(item["merge_state_unknown"])

    def test_merge_state_unknown_false_for_a_verified_github_delivery(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        comments = {88: [{"body": dlv("w", pr="https://github.com/o/r/pull/5"), "createdAt": "t"}]}
        report = self._call(nodes, comments=comments,
                            merged={"https://github.com/o/r/pull/5": False}, projects=["app"])
        self.assertFalse(report["review_queue"][0]["merge_state_unknown"])

    def test_review_item_reports_where_the_pr_link_came_from(self):
        # #71: the report says whether the link is the delivery's own structured
        # field or a legacy guess off the prose — a downstream reader must be
        # able to tell them apart without re-reading the thread.
        nodes = [
            self._node(88, "delivered with a marker",
                       ["kraken-task", "project:app", "awaiting-merge"]),
            self._node(91, "legacy delivery",
                       ["kraken-task", "project:app", "awaiting-merge"],
                       created="2026-07-01T01:00:00Z"),
            self._node(92, "no PR at all",
                       ["kraken-task", "project:app", "awaiting-merge"],
                       created="2026-07-01T02:00:00Z"),
        ]
        comments = {
            88: [{"body": "cf https://github.com/other/repo/pull/3", "createdAt": "t1"},
                 {"body": dlv("w", pr="https://github.com/o/r/pull/5"), "createdAt": "t2"}],
            91: [{"body": "delivered in https://github.com/o/r/pull/6", "createdAt": "t"}],
            92: [{"body": dlv("w"), "createdAt": "t"}],
        }
        report = self._call(nodes, comments=comments, projects=["app"])
        items = {r["number"]: r for r in report["review_queue"]}
        self.assertEqual(items[88]["pr_url"], "https://github.com/o/r/pull/5",
                         "a foreign PR quoted by a human outranked the marker")
        self.assertEqual(items[88]["pr_source"], "marker")
        self.assertEqual(items[91]["pr_url"], "https://github.com/o/r/pull/6")
        self.assertEqual(items[91]["pr_source"], "legacy-free-text")
        self.assertIsNone(items[92]["pr_url"])
        self.assertIsNone(items[92]["pr_source"])

    def test_awaiting_merge_comment_failure_propagates_none(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            leases={}, commit_meta={},
            comment_reader=lambda r, i: None,  # transport failure
            pr_merged=lambda u: False,
            project_lister=lambda r: [])
        self.assertIsNone(report)

    def test_pr_read_failure_propagates_none(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        comments = {88: [{"body": dlv("w", pr="https://x/pull/5"), "createdAt": "t"}]}
        report = kraken.compute_status(
            "o/tasks", "", nodes, self.NOW,
            leases={}, commit_meta={},
            comment_reader=lambda r, i: comments.get(i, []),
            pr_merged=lambda u: None,  # transport failure
            project_lister=lambda r: [])
        self.assertIsNone(report)


class QueueHygieneTests(unittest.TestCase):
    """The read-side twin of the arrival-time validator (PROTOCOL.md §2.1): the
    same three checks — project label, Goal, Acceptance — decided from the bodies
    the queue walk already carries, so the console reports them for the whole
    queue at once and nothing has to run in the coordination repo."""

    GOOD = "### Goal\n\nShip it.\n\n### Acceptance\n\n`npm test` passes.\n"

    def _node(self, number, labels, body, created="2026-07-01T00:00:00Z"):
        return {"number": number, "title": "t%d" % number, "createdAt": created,
                "body": body, "labels": {"nodes": [{"name": l} for l in labels]}}

    def test_a_compliant_task_is_not_reported(self):
        node = self._node(1, ["kraken-task", "project:app"], self.GOOD)
        self.assertEqual(kraken.queue_hygiene([node]), [])

    def test_missing_project_label(self):
        node = self._node(1, ["kraken-task"], self.GOOD)
        self.assertEqual(kraken.queue_hygiene([node]),
                         [{"number": 1, "title": "t1", "missing": ["project label"]}])

    def test_missing_goal_and_acceptance(self):
        node = self._node(1, ["kraken-task", "project:app"], "no headings at all")
        self.assertEqual(kraken.queue_hygiene([node])[0]["missing"],
                         ["Goal", "Acceptance"])

    def test_blank_issue_form_field_counts_as_missing(self):
        body = "### Goal\n\nShip it.\n\n### Acceptance\n\n_No response_\n"
        node = self._node(1, ["kraken-task", "project:app"], body)
        self.assertEqual(kraken.queue_hygiene([node])[0]["missing"], ["Acceptance"])

    def test_a_project_scope_never_hides_a_task_with_no_project(self):
        """The load-bearing case: a task carrying no project label is invisible to
        every worker, and a project scope would bury it too. It is reported under
        any scope."""
        orphan = self._node(1, ["kraken-task"], self.GOOD)
        other = self._node(2, ["kraken-task", "project:web"], "empty")
        mine = self._node(3, ["kraken-task", "project:app"], "empty")
        reported = [i["number"] for i in kraken.queue_hygiene([orphan, other, mine],
                                                              project="app")]
        self.assertEqual(reported, [1, 3],
                         "the other project's task leaked in, or the orphan was hidden")

    def test_oldest_first(self):
        a = self._node(9, ["kraken-task"], self.GOOD, created="2026-07-02T00:00:00Z")
        b = self._node(4, ["kraken-task"], self.GOOD, created="2026-07-01T00:00:00Z")
        self.assertEqual([i["number"] for i in kraken.queue_hygiene([a, b])], [4, 9])


def _task_node(number, labels, comments=()):
    """A minimal open-task node the way fetch_open_tasks returns it — the only
    issue input reconcile_plan reads. `comments` is the trailing window the
    repeat-expiry guard counts `lease-expired` markers in."""
    return {"number": number, "title": "t%d" % number, "createdAt": "2026-01-01",
            "body": "", "labels": {"nodes": [{"name": l} for l in labels]},
            "comments": {"nodes": [{"body": b, "createdAt": "2026-01-01"}
                                   for b in comments]}}


def _at(hours_ago):
    dt = (kraken.datetime.datetime.now(kraken.datetime.timezone.utc)
          - kraken.datetime.timedelta(hours=hours_ago))
    return {"committedDate": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "message": clm("w")}


def _seconds_ago(seconds, worker="w"):
    dt = (kraken.datetime.datetime.now(kraken.datetime.timezone.utc)
          - kraken.datetime.timedelta(seconds=seconds))
    return {"committedDate": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message": clm(worker)}


def _leases(**by_issue):
    """{issue: age-in-seconds-or-None} -> the lease view a reader hands the
    planner, each task holding a single generation. None models a ref whose
    commit date could not be read."""
    refs, meta = {}, {}
    for key, age in by_issue.items():
        num = int(key.lstrip("i"))
        sha = "s%d" % num
        refs[num] = [(1, sha)]
        if age is not None:
            meta[sha] = _seconds_ago(age)
    return kraken.lease_state(refs, meta, kraken.time.time(),
                              kraken.LEASE_DEFAULT_TTL_SECONDS)


class LeaseStateTests(unittest.TestCase):
    """The lease clock as a pure decision (PROTOCOL.md §5): the ref's commit date
    is the lease timestamp, the TTL is how long it holds, and the READER applies
    the expiry — so this is the one place expiry is decided for every consumer."""

    def test_a_fresh_lease_is_live(self):
        leases = _leases(i1=10)
        self.assertTrue(leases[1]["live"])
        self.assertEqual(leases[1]["worker"], "w")
        self.assertLess(leases[1]["age"], 60)

    def test_the_holder_is_the_highest_generation(self):
        # Ownership is the top of the ladder: a superseded generation left
        # behind by a failed cleanup decides nothing, and its stale date must
        # not make a live lease read as expired.
        old, new = "s-old", "s-new"
        meta = {old: _seconds_ago(kraken.LEASE_DEFAULT_TTL_SECONDS + 600, "w-dead"),
                new: _seconds_ago(5, "w-live")}
        leases = kraken.lease_state({1: [(1, old), (2, new)]}, meta,
                                    kraken.time.time(),
                                    kraken.LEASE_DEFAULT_TTL_SECONDS)
        self.assertEqual(leases[1]["gen"], 2)
        self.assertEqual(leases[1]["worker"], "w-live")
        self.assertTrue(leases[1]["live"])
        self.assertEqual(leases[1]["gens"], [1, 2],
                         "the ladder must stay visible so a release drops it all")

    def test_a_lease_past_the_ttl_is_not_live(self):
        self.assertFalse(_leases(i1=kraken.LEASE_DEFAULT_TTL_SECONDS + 5)[1]["live"])

    def test_the_ttl_boundary_expires(self):
        # Exactly at the TTL the lease is over — `age < ttl` is the live test, so
        # the boundary belongs to the thief, never to a holder that stopped
        # renewing precisely one TTL ago.
        self.assertFalse(_leases(i1=kraken.LEASE_DEFAULT_TTL_SECONDS)[1]["live"])
        self.assertTrue(_leases(i1=kraken.LEASE_DEFAULT_TTL_SECONDS - 5)[1]["live"])

    def test_an_unreadable_commit_fails_open_to_the_steal(self):
        # Nothing proves the holder alive, so the lease must not hold the task
        # forever behind a clock nobody can read.
        lease = _leases(i1=None)[1]
        self.assertIsNone(lease["age"])
        self.assertFalse(lease["live"])

    def test_live_leases_keeps_only_the_held_ones(self):
        leases = _leases(i1=10, i2=kraken.LEASE_DEFAULT_TTL_SECONDS + 60, i3=None)
        self.assertEqual(kraken.live_leases(leases), {1: "s1"})


class LeaseTtlTests(unittest.TestCase):
    """The TTL's resolution order and the renewal cadence derived from it."""

    def setUp(self):
        self._saved = os.environ.get("KRAKEN_LEASE_TTL_SECONDS")
        os.environ.pop("KRAKEN_LEASE_TTL_SECONDS", None)

    def tearDown(self):
        os.environ.pop("KRAKEN_LEASE_TTL_SECONDS", None)
        if self._saved is not None:
            os.environ["KRAKEN_LEASE_TTL_SECONDS"] = self._saved

    def test_default_is_minutes_not_hours(self):
        # The whole point of the lease: recovery bounded in minutes. A default
        # that crept back into hours would silently restore protocol/5's latency.
        self.assertEqual(kraken.lease_ttl_seconds(),
                         kraken.LEASE_DEFAULT_TTL_SECONDS)
        self.assertLessEqual(kraken.LEASE_DEFAULT_TTL_SECONDS, 3600)

    def test_explicit_wins(self):
        os.environ["KRAKEN_LEASE_TTL_SECONDS"] = "60"
        self.assertEqual(kraken.lease_ttl_seconds(120), 120)

    def test_env_override(self):
        os.environ["KRAKEN_LEASE_TTL_SECONDS"] = "300"
        self.assertEqual(kraken.lease_ttl_seconds(), 300)

    def test_garbage_env_falls_back(self):
        os.environ["KRAKEN_LEASE_TTL_SECONDS"] = "soon"
        self.assertEqual(kraken.lease_ttl_seconds(),
                         kraken.LEASE_DEFAULT_TTL_SECONDS)

    def test_a_non_positive_ttl_falls_back(self):
        # A zero or negative TTL would expire every live lease on the next read
        # — the one misconfiguration that turns the queue into a stampede.
        for bad in ("0", "-300"):
            os.environ["KRAKEN_LEASE_TTL_SECONDS"] = bad
            self.assertEqual(kraken.lease_ttl_seconds(),
                             kraken.LEASE_DEFAULT_TTL_SECONDS)

    def test_renewal_is_derived_from_the_ttl(self):
        self.assertEqual(kraken.lease_renew_seconds(900), 300)
        os.environ["KRAKEN_LEASE_TTL_SECONDS"] = "600"
        self.assertEqual(kraken.lease_renew_seconds(), 200)

    def test_renewal_leaves_room_for_two_lost_renewals(self):
        ttl = kraken.LEASE_DEFAULT_TTL_SECONDS
        self.assertLessEqual(kraken.lease_renew_seconds(ttl) * 3, ttl)


class ExpiryCountTests(unittest.TestCase):
    """The repeat-expiry guard's counter: how often this task has already been
    stolen, read off the comment window the queue walk already carries."""

    def test_counts_only_lease_expired_markers(self):
        node = _task_node(1, ["kraken-task"], comments=[
            "> prose\n" + expired(), clm("w1"), stale("something else"),
            "operator reply", expired(previous="w2"),
        ])
        self.assertEqual(kraken.expiry_count(node), 2)

    def test_a_clean_thread_counts_zero(self):
        self.assertEqual(kraken.expiry_count(_task_node(1, ["kraken-task"])), 0)


class ReconcilerPlanTests(unittest.TestCase):
    """reconcile_plan's four rules as a PURE decision (PROTOCOL.md §6) — no
    transport at all, because the whole point of protocol/5 is that the rules run
    off the queue read a reader already has. `nodes` carries only OPEN kraken-task
    issues, which is what makes rule 1 free: a ref whose issue is absent from that
    walk is a lock over a closed (or no-longer-task) issue.

    Under protocol/6 an EXPIRED lease is deliberately NOT in here: it is unheld,
    the claim path steals it, and the reconciler writes nothing."""

    def _rules(self, plan):
        return sorted((a["rule"], a["issue"]) for a in plan)

    def test_rule1_orphan_lock_on_held_or_absent_issue(self):
        # #4 escalated but its ref lingered; #6 is closed, so it is not in nodes.
        nodes = [_task_node(4, ["kraken-task", "needs-decision"])]
        plan = kraken.reconcile_plan(nodes, _leases(i4=10, i6=10))
        self.assertEqual(self._rules(plan),
                         [("orphan-lock", 4), ("orphan-lock", 6)])

    def test_an_expired_lease_alone_plans_nothing(self):
        # THE protocol/6 change: expiry is not a repair. The reader has already
        # decided the task is free; planning a write here would escalate every
        # slow worker to the operator.
        nodes = [_task_node(1, ["kraken-task", "in-progress"])]
        expired_age = kraken.LEASE_DEFAULT_TTL_SECONDS + 60
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i1=expired_age)), [])

    def test_an_unreadable_lease_alone_plans_nothing(self):
        nodes = [_task_node(1, ["kraken-task", "in-progress"])]
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i1=None)), [])

    def test_rule2_reclaim_after_repeated_expiries(self):
        # A task nobody can finish: past the threshold it stops circulating and
        # becomes the operator's call.
        comments = [expired()] * kraken.LEASE_EXPIRY_ESCALATE
        nodes = [_task_node(1, ["kraken-task", "in-progress"], comments=comments)]
        expired_age = kraken.LEASE_DEFAULT_TTL_SECONDS + 60
        plan = kraken.reconcile_plan(nodes, _leases(i1=expired_age))
        self.assertEqual(self._rules(plan), [("reclaim", 1)])
        self.assertTrue(plan[0]["held"], "the in-progress label must be swapped off")
        self.assertIn(str(kraken.LEASE_EXPIRY_ESCALATE), plan[0]["reason"])

    def test_one_expiry_below_the_threshold_still_gets_stolen(self):
        comments = [expired()] * (kraken.LEASE_EXPIRY_ESCALATE - 1)
        nodes = [_task_node(1, ["kraken-task", "in-progress"], comments=comments)]
        expired_age = kraken.LEASE_DEFAULT_TTL_SECONDS + 60
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i1=expired_age)), [])

    def test_a_live_lease_is_never_reclaimed_however_many_expiries(self):
        # The count is about a dead task, not a busy one: whoever holds a LIVE
        # lease is working, and the history of past steals must not evict them.
        comments = [expired()] * (kraken.LEASE_EXPIRY_ESCALATE + 2)
        nodes = [_task_node(1, ["kraken-task", "in-progress"], comments=comments)]
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i1=10)), [])

    def test_rule3_heal_missing_label(self):
        nodes = [_task_node(5, ["kraken-task"])]
        plan = kraken.reconcile_plan(nodes, _leases(i5=10))
        self.assertEqual(self._rules(plan), [("heal", 5)])

    def test_heal_never_re_holds_an_expired_lease(self):
        # Restoring in-progress for a lease the reader just declared free would
        # put the task back behind a label with nothing behind IT.
        nodes = [_task_node(5, ["kraken-task"])]
        expired_age = kraken.LEASE_DEFAULT_TTL_SECONDS + 60
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i5=expired_age)), [])

    def test_rule4_requeue_orphan_projection(self):
        plan = kraken.reconcile_plan(
            [_task_node(3, ["kraken-task", "in-progress"])], {})
        self.assertEqual(self._rules(plan), [("requeue", 3)])

    def test_agreeing_state_plans_nothing(self):
        nodes = [_task_node(2, ["kraken-task", "in-progress"]),
                 _task_node(9, ["kraken-task"]),
                 _task_node(10, ["kraken-task", "awaiting-merge"])]
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i2=10)), [],
                         "a queue that already agrees must cost zero writes")


class ReconcilerApplyTests(unittest.TestCase):
    """apply_reconcile's write ordering and project_reconcile's in-memory fold.
    Every write is recorded, none real."""

    def setUp(self):
        self._saved = {n: getattr(kraken, n) for n in
                       ("drop_generations", "swap_labels", "post_comment")}
        self.writes = []
        kraken.drop_generations = lambda repo, n, gens: (
            self.writes.append(("del-ref", n)) or True)
        kraken.swap_labels = lambda repo, n, remove=None, add=None: (
            self.writes.append(("labels", n, remove, add)) or True)
        kraken.post_comment = lambda repo, n, body: (
            self.writes.append(("comment", n, body)) or True)

    def tearDown(self):
        for name, fn in self._saved.items():
            setattr(kraken, name, fn)

    def _apply(self, plan):
        buf = StringIO()
        with redirect_stdout(buf):
            counts = kraken.apply_reconcile("o/tasks", plan, "w-reaper")
        return counts, buf.getvalue()

    def test_reclaim_deletes_the_ref_last(self):
        counts, _ = self._apply([{"rule": "reclaim", "issue": 1,
                                  "reason": "the lease expired 3 times",
                                  "held": True, "gens": [1]}])
        self.assertEqual(counts["reclaim"], 1)
        kinds = [w[0] for w in self.writes]
        self.assertEqual(kinds, ["labels", "comment", "del-ref"],
                         "§5 ordering: writes first, ref delete last")
        self.assertIn(("labels", 1, "in-progress", "needs-decision"), self.writes)

    def test_reclaim_comment_carries_the_worker_disclaimer(self):
        self._apply([{"rule": "reclaim", "issue": 1, "reason": "silent",
                      "held": True, "gens": [1]}])
        body = [w for w in self.writes if w[0] == "comment"][0][2]
        self.assertTrue(body.startswith(kraken.disclaimer("w-reaper")),
                        "a worker posts the reclaim now, so §4 attribution applies")
        self.assertIn('"type":"stale-claim"', body)

    def test_orphan_lock_touches_nothing_but_the_ref(self):
        self._apply([{"rule": "orphan-lock", "issue": 4, "reason": "x",
                      "gens": [1]}])
        self.assertEqual(self.writes, [("del-ref", 4)])

    def test_transport_failure_answers_none(self):
        kraken.swap_labels = lambda repo, n, remove=None, add=None: False
        buf, err = StringIO(), StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            counts = kraken.apply_reconcile(
                "o/tasks", [{"rule": "heal", "issue": 5, "reason": "x"}], "w")
        self.assertIsNone(counts)
        self.assertIn("gh-failure stage=heal issue=5", err.getvalue())

    def test_project_reconcile_folds_the_plan_into_the_read(self):
        # The drain must see the state it just repaired without re-fetching it.
        nodes = [_task_node(1, ["kraken-task", "in-progress"]),
                 _task_node(3, ["kraken-task", "in-progress"]),
                 _task_node(5, ["kraken-task"])]
        leases = _leases(i1=10, i5=10, i6=10)
        plan = [{"rule": "reclaim", "issue": 1, "reason": "x", "held": True,
                 "gens": [1]},
                {"rule": "requeue", "issue": 3, "reason": "x"},
                {"rule": "heal", "issue": 5, "reason": "x"},
                {"rule": "orphan-lock", "issue": 6, "reason": "x", "gens": [1]}]
        kraken.project_reconcile(plan, nodes, leases)
        self.assertEqual(sorted(leases), [5],
                         "reclaimed/orphan leases must be dropped")
        labels = {n["number"]: set(kraken.label_names_of(n)) for n in nodes}
        self.assertEqual(labels[1], {"kraken-task", "needs-decision"})
        self.assertEqual(labels[3], {"kraken-task"})
        self.assertEqual(labels[5], {"kraken-task", "in-progress"})


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


class WatchFailureActionTests(unittest.TestCase):
    """The watcher's fail-loud gate (watch_failure_action): a run of failed queue
    reads must be audible on stderr — spaced, not one line per poll — and must
    eventually kill the watcher instead of leaving it alive and deaf."""

    def test_first_failure_speaks_immediately(self):
        self.assertEqual(kraken.watch_failure_action(1, 10, 60), "warn")

    def test_quiet_between_thresholds(self):
        for failures in (2, 3, 9, 11):
            self.assertIsNone(kraken.watch_failure_action(failures, 10, 60),
                              "failure %d must not re-warn" % failures)

    def test_warns_again_every_spacing(self):
        self.assertEqual(kraken.watch_failure_action(10, 10, 60), "warn")
        self.assertEqual(kraken.watch_failure_action(20, 10, 60), "warn")

    def test_ceiling_gives_up(self):
        self.assertEqual(kraken.watch_failure_action(60, 10, 60), "die")

    def test_ceiling_wins_over_a_warn_on_the_same_count(self):
        # 60 is both a multiple of the spacing and the ceiling: dying outranks
        # warning, or the watcher would announce and then keep polling.
        self.assertEqual(kraken.watch_failure_action(60, 10, 60), "die")
        self.assertEqual(kraken.watch_failure_action(61, 10, 60), "die")

    def test_zero_ceiling_disables_the_exit(self):
        # Opt-out for an operator who prefers a watcher that rides out any
        # outage: the warnings stay, the exit never comes.
        self.assertEqual(kraken.watch_failure_action(9999, 10, 0), None)
        self.assertEqual(kraken.watch_failure_action(10, 10, 0), "warn")

    def test_no_failures_is_silent(self):
        self.assertIsNone(kraken.watch_failure_action(0, 10, 60))


class WatchFailureLoopTests(unittest.TestCase):
    """cmd_watch's poll loop around that gate: a failed read is counted and
    reported on stderr (never on stdout, which is the wake channel), the counter
    resets on the first successful read, and the ceiling exits non-zero."""

    def setUp(self):
        orig_snapshot = kraken.snapshot_state
        self.addCleanup(setattr, kraken, "snapshot_state", orig_snapshot)

    def _setenv(self, name, value):
        os.environ[name] = value
        self.addCleanup(os.environ.pop, name, None)

    def _run(self, snapshots, env=None):
        """Drive the loop over a scripted snapshot_state sequence; the loop is
        infinite, so an exhausted script interrupts it."""
        pending = list(snapshots)

        def scripted(repo, project):
            if not pending:
                raise KeyboardInterrupt
            return pending.pop(0)
        kraken.snapshot_state = scripted

        self._setenv("KRAKEN_WATCH_POLL_SECONDS", "0")  # no real waiting
        for name, value in (env or {}).items():
            self._setenv(name, value)

        args = SimpleNamespace(repo="OWNER/tasks", project="app")
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = kraken.cmd_watch(args)
            except KeyboardInterrupt:
                rc = "polling"
        return rc, out.getvalue(), err.getvalue()

    def test_failed_read_is_reported_on_stderr(self):
        rc, out, err = self._run([None])
        self.assertEqual(rc, "polling", "one failed read must not kill the watcher")
        self.assertIn("kraken-watch:", err, "a failed queue read was swallowed")
        self.assertNotIn("kraken-watch:", out,
                         "the diagnostic must not ride the wake channel (stdout)")

    def test_success_resets_the_counter(self):
        # Two failures, a success, then a failure: the last one is failure #1
        # again, so it warns — proving the run was reset rather than accumulated.
        rc, out, err = self._run([None, None, "7:startable", None],
                                 {"KRAKEN_WATCH_MAX_FAILURES": "3"})
        self.assertEqual(rc, "polling", "the ceiling was reached across a reset")
        self.assertEqual(err.count("kraken-watch:"), 2,
                         "expected one warning per failure run, got: %r" % err)
        self.assertIn("kraken-queue:", out, "the successful poll emitted no wake")

    def test_ceiling_exits_transport(self):
        rc, out, err = self._run([None, None, None],
                                 {"KRAKEN_WATCH_MAX_FAILURES": "3"})
        self.assertEqual(rc, kraken.EXIT_TRANSPORT,
                         "a watcher that can never read the queue must die loudly")
        self.assertIn("giving up", err)
        self.assertIn("3", err, "the give-up line must name the failure count")


class BundledAssetTests(unittest.TestCase):
    """Every asset init installs must actually ship next to the module."""

    def test_bundled_asset_covers_every_init_asset(self):
        for name, _dest, _msg in kraken.INIT_ASSETS:
            self.assertTrue(kraken.bundled_asset(name),
                            "%s has no bundled bytes to install" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
