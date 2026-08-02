#!/usr/bin/env python3
"""Unit tests for kraken.py — the parts the gh-stub conformance suite cannot
exercise in isolation: the protocol/4 claim-ref helpers (the CAS, the batched
commit/issue meta reads, the liveness decode), marker decoding edge cases, and
the comment pagination the status/validator paths still rely on.

Stdlib only (unittest), no network, no gh. Run: python3 tests/unit/test_kraken.py
"""

import os
import re
import time
import datetime
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
from fakes import FakeApi, FakeQueue, recording_api  # noqa: E402


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
    liveness decode. Each test hands the code under test its own `Api`, so only
    the arg-building and result-parsing are exercised — and there is no global
    to restore, which is why this class needs no setUp/tearDown."""

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

        api = FakeApi("o/tasks", request=fake_request)
        sha = kraken.Refs(api).commit({"type": "claim", "worker": "w1"})
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

        api = FakeApi("o/tasks", request=fake_request)
        sha = kraken.Refs(api).commit({"type": "claim", "worker": "w1"})
        self.assertEqual(sha, "def456")
        self.assertEqual(trees, [kraken.EMPTY_TREE_SHA, "headtree"],
                         "must retry with the HEAD tree after the empty-tree 422")

    def test_create_claim_commit_returns_none_on_transport_fault(self):
        api = FakeApi(request=lambda m, p, body=None: (
            kraken.STATUS_NETWORK_FAILURE, ""))
        self.assertIsNone(
            kraken.Refs(api).commit({"type": "claim", "worker": "w"}))

    def test_claim_ref_create_maps_the_cas_outcomes(self):
        api = FakeApi(request=lambda m, p, body=None: (201, "{}"))
        self.assertEqual(kraken.Refs(api).create(7, 1, "sha"), "won")

        # HTTP 422 IS the CAS-lost signal — an integer status, not a stderr scrape.
        api = FakeApi(request=lambda m, p, body=None: (422, ""))
        self.assertEqual(kraken.Refs(api).create(7, 1, "sha"), "lost")

        api = FakeApi(request=lambda m, p, body=None: (500, ""))
        self.assertEqual(kraken.Refs(api).create(7, 1, "sha"), "fail")

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

        api = FakeApi(request=fake_request)
        verdict, gen, sha = kraken.Refs(api).advance(
            7, 4, {"type": "claim", "worker": "w1"})
        self.assertEqual((verdict, gen, sha), ("won", 5, "fresh"))
        self.assertEqual(created["ref"], "refs/kraken/claims/7/5")
        self.assertEqual(created["sha"], "fresh")

    def test_advance_lease_reports_the_cas_loss(self):
        def fake_request(method, path, body=None):
            if path.endswith("/git/commits"):
                return (201, json.dumps({"sha": "fresh"}))
            return (422, "")

        api = FakeApi(request=fake_request)
        self.assertEqual(kraken.Refs(api).advance(7, 1, {"type": "claim"}),
                         ("lost", None, None))

    def test_claim_ref_delete_tolerates_a_missing_ref(self):
        api = FakeApi(request=lambda m, p, body=None: (204, ""))
        self.assertTrue(kraken.Refs(api).delete(7, 1))
        # Already gone (422) is success — the delete is idempotent.
        api = FakeApi(request=lambda m, p, body=None: (422, ""))
        self.assertTrue(kraken.Refs(api).delete(7, 1))
        # A real transport fault is not tolerated.
        api = FakeApi(request=lambda m, p, body=None: (
            kraken.STATUS_NETWORK_FAILURE, ""))
        self.assertFalse(kraken.Refs(api).delete(7, 1))

    def test_dropping_generations_reports_a_partial_failure(self):
        seen = []

        def fake_request(method, path, body=None):
            seen.append(path)
            return (204, "") if path.endswith("/1") else (500, "")

        api = FakeApi(request=fake_request)
        self.assertFalse(kraken.Refs(api).drop(7, [1, 2]))
        self.assertEqual(len(seen), 2, "a failed delete must not stop the rest")

    def test_claim_ref_list_groups_generations_per_issue(self):
        api = FakeApi(request=lambda m, p, body=None: (200, json.dumps([
            {"ref": "refs/kraken/claims/7", "object": {"sha": "sha7g0"}},
            {"ref": "refs/kraken/claims/7/2", "object": {"sha": "sha7g2"}},
            {"ref": "refs/kraken/claims/12/1", "object": {"sha": "sha12"}},
            {"ref": "refs/heads/main", "object": {"sha": "nope"}},
        ])))
        self.assertEqual(kraken.Refs(api).all(),
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
        api = FakeApi(request=lambda m, p, body=None: (200, json.dumps([
            {"ref": "refs/kraken/claims/12/1", "object": {"sha": "mine"}},
            {"ref": "refs/kraken/claims/120/9", "object": {"sha": "theirs"}},
        ])))
        self.assertEqual(kraken.Refs(api).of(12), (True, [(1, "mine")]))

    def test_claim_refs_of_separates_absent_from_unreadable(self):
        api = FakeApi(request=lambda m, p, body=None: (200, "[]"))
        self.assertEqual(kraken.Refs(api).of(7), (True, []))
        api = FakeApi(request=lambda m, p, body=None: (500, ""))
        self.assertEqual(kraken.Refs(api).of(7), (False, []))

    def test_claim_ref_list_transport_failure_is_none(self):
        api = FakeApi(request=lambda m, p, body=None: (500, ""))
        self.assertIsNone(kraken.Refs(api).all())

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

        api = FakeApi(request=present, graphql=lambda q: {"data": {"repository": {
            "c0": {"committedDate": "t", "message": clm("w1")}}}})
        self.assertEqual(kraken.Refs(api).owner(7), "w1")
        self.assertEqual(kraken.Refs(api).owner("7"), "w1")
        # No lease at all → None (treated as not-ours by the caller).
        self.assertIsNone(kraken.Refs(api).owner(9))
        # Transport failure → None, never a guessed owner — an ambiguous read
        # must never turn a real CAS loss into a false win.
        api = FakeApi(request=lambda m, p, body=None: (500, ""))
        self.assertIsNone(kraken.Refs(api).owner(7))
        api = FakeApi(request=lambda m, p, body=None: (
            kraken.STATUS_NETWORK_FAILURE, ""))
        self.assertIsNone(kraken.Refs(api).owner(7))

    def test_resolve_commit_meta_batches_and_parses(self):
        captured = {}

        def fake_graphql(q):
            captured["q"] = q
            return {"data": {"repository": {
                "c0": {"committedDate": "2026-07-01T00:00:00Z", "message": clm("w1")}}}}

        api = FakeApi("o/tasks", graphql=fake_graphql)
        meta = kraken.Refs(api).commit_meta(["sha1"])
        self.assertIn('object(oid: "sha1")', captured["q"])
        self.assertEqual(meta["sha1"]["message"], clm("w1"))
        self.assertEqual(meta["sha1"]["committedDate"], "2026-07-01T00:00:00Z")

    def test_resolve_commit_meta_empty_input_is_no_call(self):
        api = FakeApi(graphql=lambda q: self.fail(
            "graphql must not be called for []"))
        self.assertEqual(kraken.Refs(api).commit_meta([]), {})

    def test_resolve_commit_meta_chunks_and_keeps_its_aliases_unique(self):
        # More claims than one query may carry. The alias is the SHA's index, so
        # numbering that restarted per chunk would collapse two `c0`s in the
        # merge — and a lease read back under the wrong SHA names the wrong
        # worker and the wrong timestamp.
        queries = []

        def fake(q):
            queries.append(q)
            return {"data": {"repository": {
                alias: {"committedDate": "2026-07-01T00:00:00Z",
                        "message": clm("w-" + alias)}
                for alias in re.findall(r"(c[0-9]+): object", q)}}}

        total = kraken.GRAPHQL_ALIAS_CHUNK * 2 + 1
        shas = ["%040x" % i for i in range(total)]
        meta = kraken.Refs(FakeApi("o/t", graphql=fake)).commit_meta(shas)
        self.assertEqual(len(queries), 3, "chunked, not one query per claim")
        self.assertEqual(len(meta), total)
        for i, sha in enumerate(sorted(shas)):
            self.assertEqual(meta[sha]["message"], clm("w-c%d" % i),
                             "each SHA must carry back its OWN commit")

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

    def test_boundary_prints_the_spec_section_verbatim(self):
        # What a driver pastes into a subagent that will hold push access. It
        # must be the SPEC's words, not a paraphrase: the heading proves which
        # section, and the MUST NOT sentence is the whole point of the section.
        lines = self._run("boundary")
        self.assertTrue(lines[0].startswith("## 11. "),
                        "boundary must open on PROTOCOL.md's own §11 heading: %r"
                        % lines[:1])
        body = "\n".join(lines)
        self.assertIn("MUST NOT merge", body, "the prohibition did not survive")
        self.assertNotIn("## 12.", body, "the section bled into the next one")


class ProtocolSectionTests(unittest.TestCase):
    """Reading one section out of the spec — the `contract boundary` half that
    touches the filesystem."""

    def test_an_unreadable_spec_yields_nothing_rather_than_prose(self):
        # A caller pastes this into a prompt. Empty is the only answer it can
        # tell apart from real rules, so a missing spec must not degrade into a
        # placeholder sentence.
        self.assertEqual(kraken.protocol_section(11, doc="/nonexistent/PROTOCOL.md"),
                         [])

    def test_an_absent_section_yields_nothing(self):
        self.assertEqual(kraken.protocol_section(999), [])

    def test_a_section_stops_at_the_next_one(self):
        section = kraken.protocol_section(9)   # §9 Release, followed by §10
        self.assertTrue(section[0].startswith("## 9. "))
        self.assertNotIn("## 10.", "\n".join(section))
        self.assertTrue(section[-1].strip(),
                        "trailing blank lines are not part of the section")


class PlaceholderSlugTests(unittest.TestCase):
    """The doc placeholder, refused by the program instead of by three copies of
    a paragraph in three SKILL.md files."""

    def _main(self, *argv):
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            return kraken.main(list(argv)), buf.getvalue()

    def test_the_template_slug_is_refused_before_anything_is_read(self):
        for slug in ("OWNER/tasks", "<coordination-repo>", "me/<repo>"):
            rc, out = self._main("status", slug)
            self.assertEqual(rc, kraken.EXIT_USAGE,
                             "%r was not refused" % slug)
            self.assertIn("placeholder", out,
                          "the refusal must say WHY: %r" % out)

    def test_a_real_slug_is_not_refused_by_the_guard(self):
        self.assertFalse(kraken.placeholder_slug("acme/tasks"))
        self.assertFalse(kraken.placeholder_slug("OWNERS/tasks"))
        self.assertTrue(kraken.placeholder_slug("OWNER/anything"))

    def test_contracts_placeholder_option_is_not_the_guards_business(self):
        # `contract` defaults --repo to <coordination-repo> ON PURPOSE — it
        # prints doc lines and talks to nothing. The guard is keyed on the
        # positional slug, which is the one that reaches GitHub.
        rc, out = self._main("contract", "task-trailer")
        self.assertEqual(rc, kraken.EXIT_OK, out)
        self.assertIn("<coordination-repo>", out)


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

    @staticmethod
    def _paged(recs):
        """A `request` stand-in that serves `recs` as REST comment pages, keyed
        on the per_page/page query `Api.paginated` walks."""
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

        FakeApi("acme/tasks", request=fake).comment_records("42")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertIn("repos/acme/tasks/issues/42/comments", calls[0][1])
        self.assertIn("per_page=", calls[0][1])
        self.assertIn("page=", calls[0][1])

    def test_returns_records_past_one_hundred(self):
        # The validator's debounce is the last reader of a whole thread, and a
        # single-page read would silently truncate it — the finding it looks for
        # may be comment 130.
        recs = [{"body": f"c {i}", "created_at": f"2026-07-01T00:{i % 60:02d}:00Z"}
                for i in range(150)]
        recs[129] = {"body": "the one that matters",
                     "created_at": "2026-07-09T00:00:00Z"}
        result = FakeApi(request=self._paged(recs)).comment_records("42")
        self.assertEqual(len(result), 150)
        self.assertEqual(result[129]["body"], "the one that matters")

    def test_maps_created_at_to_createdat(self):
        recs = [
            {"body": "first", "created_at": "2026-07-01T00:00:00Z"},
            {"body": "just prose", "created_at": "2026-07-01T05:00:00Z"},
        ]
        result = FakeApi(request=self._paged(recs)).comment_records("42")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["createdAt"], "2026-07-01T00:00:00Z")
        self.assertEqual(result[0]["body"], "first")

    def test_transport_failure_returns_none(self):
        api = FakeApi(request=lambda m, p, body=None: (500, ""))
        self.assertIsNone(api.comment_records("42"))


class BatchedAliasTests(unittest.TestCase):
    """`Api.aliased`: the one fan-out shape, and its ceiling.

    Batching is what stops a fan-out becoming a request per item; the CHUNK is
    what stops it becoming a single query that grows without limit. An
    over-large GraphQL query is not slow, it is rejected — so an uncapped batch
    fails every item in it at once, on exactly the busy queue that needed it to
    work."""

    def _recorder(self, node=None):
        """A FakeApi that records every query and answers each alias it sees."""
        self.queries = []

        def fake(q):
            self.queries.append(q)
            return {"data": {"repository": {
                alias: (node if node is not None else {"seen": alias})
                for alias in re.findall(r"(a[0-9]+): x", q)}}}
        return FakeApi("o/t", graphql=fake)

    def test_an_empty_ask_is_no_call(self):
        api = FakeApi("o/t", graphql=lambda q: self.fail(
            "graphql must not be called for []"))
        self.assertEqual(api.aliased([]), {})

    def test_fields_within_the_chunk_are_one_call(self):
        api = self._recorder()
        got = api.aliased([f"a{i}: x" for i in range(kraken.GRAPHQL_ALIAS_CHUNK)])
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(len(got), kraken.GRAPHQL_ALIAS_CHUNK)
        self.assertIn('repository(owner: "o", name: "t")', self.queries[0])

    def test_beyond_the_chunk_splits_and_merges(self):
        api = self._recorder()
        total = kraken.GRAPHQL_ALIAS_CHUNK * 2 + 1
        got = api.aliased([f"a{i}: x" for i in range(total)])
        self.assertEqual(len(self.queries), 3, "chunked, not one call per field")
        # Every alias survives the merge — a chunk boundary must not eat one.
        self.assertEqual(sorted(got), sorted(f"a{i}" for i in range(total)))

    def test_a_failed_chunk_takes_the_whole_read_down(self):
        # Half a dict is worse than none: the caller cannot tell an item the
        # server omitted from one it never asked about.
        calls = []

        def fake(q):
            calls.append(q)
            return None if len(calls) > 1 else {"data": {"repository": {"a0": {}}}}

        api = FakeApi("o/t", graphql=fake)
        self.assertIsNone(
            api.aliased([f"a{i}: x" for i in range(kraken.GRAPHQL_ALIAS_CHUNK + 1)]))

    def test_a_null_repository_is_an_empty_answer_not_a_crash(self):
        api = FakeApi("o/t", graphql=lambda q: {"data": {"repository": None}})
        self.assertEqual(api.aliased(["a0: x"]), {})


class ClaimNextIterationTests(unittest.TestCase):
    """The deterministic claim loop in `acquire_next`: skip-on-held,
    skip-on-lost, forward-only, stop-on-transport, honest-empty.

    The subject is the ITERATION, so the two expensive collaborators — the
    candidate list and the per-candidate CAS — are passed in, and the queue read
    behind them is an empty FakeApi. `acquire_next` answers `(rc, won)` as data;
    how `claim-next` PRINTS that is not this class's business and is pinned
    end-to-end in tests/conformance/test_claim_next.py, against the real binary."""

    def setUp(self):
        self.attempted = []

    @staticmethod
    def _candidate(number, title, state, body):
        """One classified candidate. It carries its whole `Task`, which is how
        claim-next re-derives the requeue verdict off the very task the filter
        used — the read no longer has to be seeded with a parallel node list."""
        return kraken.Candidate(
            kraken.Task({"number": number, "title": title,
                         "createdAt": "2026-01-01", "body": body,
                         "labels": {"nodes": [{"name": "kraken-task"}]},
                         "comments": {"nodes": []},
                         "blockedBy": {"nodes": []}}),
            state)

    def _run(self, rows, claim_results, states=None):
        # rows=None models a failed queue read, which surfaces at Queue.read.
        tasks = [c.task for c in (rows or [])]

        def fake_claim_step(api, issue, worker, record=None, lease=None,
                            ttl=None, probe_lease=False):
            self.attempted.append(issue)
            return claim_results[issue]

        # The preflight reads the repo's label set; the queue read is scripted.
        api = FakeApi(paginated=lambda path: [{"name": "project:app"}])
        buf = StringIO()
        with redirect_stdout(buf):
            rc, won = kraken.acquire_next(
                api, "app", "w1",
                queue=FakeQueue(
                    api,
                    read=lambda now=None, ttl=None: (
                        None if rows is None
                        else kraken.QueueRead(tasks, {}, {}, states or {})),
                    candidates=lambda p, read=None: rows),
                claim_step=fake_claim_step)
        return rc, won, buf.getvalue()

    def test_claims_first_startable(self):
        rows = [self._candidate(7, "oldest", "startable", "body-7")]
        rc, won, _ = self._run(rows, {7: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7])
        self.assertEqual(won, {"issue": 7, "title": "oldest", "body": "body-7",
                               "bounced": False, "pr": None})

    def test_the_won_payload_carries_the_records_bounce_and_pr(self):
        """A task with no record is a fresh one; a task whose held record the
        thread has run past is coming BACK, and carries where it was delivered.

        Both come off the record the loop already read to guard the claim, which
        is the point: `next-action` emits them, and no reader downstream has to
        re-derive a bounce from the thread or hunt the earlier PR in the body."""
        rows = [self._candidate(7, "bounced back", "startable", "b7")]
        rows[0].task.comment_total = 5
        states = {7: kraken.TaskState(state="awaiting-merge", worker="w0",
                                      comments=4, recorded=True,
                                      pr="https://github.com/o/r/pull/3")}
        _rc, won, _ = self._run(rows, {7: kraken.EXIT_OK}, states=states)
        self.assertTrue(won["bounced"],
                        "a comment past the record's anchor is a bounce (§6)")
        self.assertEqual(won["pr"], "https://github.com/o/r/pull/3",
                         "the delivery the task is coming back from was dropped")

    def test_a_task_whose_record_still_holds_is_not_bounced(self):
        # Same record, nothing said since: the anchor equals the live total, so
        # the derivation is false and the agent is told this is not rework.
        rows = [self._candidate(7, "fresh claim", "startable", "b7")]
        rows[0].task.comment_total = 4
        states = {7: kraken.TaskState(state="awaiting-merge", worker="w0",
                                      comments=4, recorded=True)}
        _rc, won, _ = self._run(rows, {7: kraken.EXIT_OK}, states=states)
        self.assertFalse(won["bounced"], "nothing was said past the anchor")
        self.assertIsNone(won["pr"], "a record with no delivery has no PR")

    def test_skips_held_rows_without_attempting_them(self):
        rows = [
            self._candidate(5, "held one", "held", "b5"),
            self._candidate(7, "startable", "startable", "b7"),
        ]
        rc, _, _ = self._run(rows, {7: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7])

    def test_skip_on_lost_cas_moves_forward_never_back(self):
        # THE §5 invariant: a lost CAS on the oldest candidate moves on to the
        # next — it must never retry the issue it just lost.
        rows = [
            self._candidate(7, "lost this", "startable", "b7"),
            self._candidate(9, "win this", "startable", "b9"),
        ]
        rc, won, _ = self._run(rows, {7: kraken.EXIT_LOST, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7, 9])
        self.assertEqual(self.attempted.count(7), 1)
        self.assertEqual(won["issue"], 9)

    def test_skip_on_held_since_listing_moves_to_next(self):
        rows = [
            self._candidate(7, "now held", "startable", "b7"),
            self._candidate(9, "clear", "startable", "b9"),
        ]
        rc, _, _ = self._run(rows, {7: kraken.EXIT_NOT_CLEAR, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.attempted, [7, 9])

    def test_empty_queue_is_honest_none(self):
        rc, won, out = self._run([], {})
        self.assertEqual(rc, kraken.EXIT_NONE)
        self.assertIsNone(won)
        self.assertEqual(self.attempted, [])
        self.assertIn("claim-next: none project:app", out)

    def test_all_candidates_lost_or_held_is_none(self):
        rows = [
            self._candidate(7, "a", "startable", "b7"),
            self._candidate(9, "b", "startable", "b9"),
        ]
        rc, won, _ = self._run(rows, {7: kraken.EXIT_LOST, 9: kraken.EXIT_NOT_CLEAR})
        self.assertEqual(rc, kraken.EXIT_NONE)
        self.assertIsNone(won)
        self.assertEqual(self.attempted, [7, 9])

    def test_transport_during_claim_stops_immediately(self):
        rows = [
            self._candidate(7, "ambiguous", "startable", "b7"),
            self._candidate(9, "untouched", "startable", "b9"),
        ]
        rc, won, out = self._run(rows, {7: kraken.EXIT_TRANSPORT, 9: kraken.EXIT_OK})
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertIsNone(won)
        self.assertEqual(self.attempted, [7])
        self.assertIn("state unknown", out)

    def test_transport_during_listing_is_twenty(self):
        rc, won, out = self._run(None, {})
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertIsNone(won)
        self.assertEqual(self.attempted, [])
        self.assertIn("claim-next: gh-failure stage=list", out)


class VerifyProjectTests(unittest.TestCase):
    """The project preflight, isolated from transport: a worker pointed at a
    `project:<name>` label the coordination repo does not carry is permanently
    deaf — every task is filtered out client-side, so the queue reads as empty
    forever. The check refuses on a genuinely absent label, and never declares a
    project missing from a label read that merely failed."""

    def _verify(self, projects, project="app"):
        # Faked at the Api, not at `Queue.projects`: the real label read and
        # the real `project:` stripping stay under test, and a failed read is a
        # `paginated` answering None — what the transport actually does.
        labels = (None if projects is None
                  else [{"name": f"project:{p}"} for p in projects])
        api = FakeApi(paginated=lambda path: labels)
        return kraken.Queue(api).verify_project(project)

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
    """The drain's project preflight: an unconfigured project refuses with
    EXIT_UNKNOWN_PROJECT before the queue is read and before any write."""

    def setUp(self):
        self.classified = []

    def _candidates(self, project, read=None):
        self.classified.append(project)
        return []

    def _run(self, labels):
        # The verdict comes from the real preflight over a scripted label set:
        # a list of names, [] for a repo with none, None for a failed read.
        api = FakeApi(paginated=lambda path: labels)
        buf = StringIO()
        with redirect_stdout(buf):
            rc, _won = kraken.acquire_next(
                api, "app", "w-gate",
                queue=FakeQueue(api,
                                read=lambda now=None, ttl=None: kraken.QueueRead([], {}, {}),
                                candidates=self._candidates))
        return rc, buf.getvalue()

    def test_unknown_project_refuses_before_reading_the_queue(self):
        rc, out = self._run([{"name": "project:other"}])
        self.assertEqual(rc, kraken.EXIT_UNKNOWN_PROJECT)
        self.assertEqual(self.classified, [], "a refused drain still read the queue")
        self.assertIn("unknown project", out)

    def test_transport_failure_during_the_check_is_twenty(self):
        rc, out = self._run(None)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertEqual(self.classified, [], "an unverified project still read the queue")
        self.assertIn("gh-failure", out)

    def test_configured_project_proceeds_to_the_queue(self):
        rc, out = self._run([{"name": "project:app"}])
        self.assertEqual(rc, kraken.EXIT_NONE)
        self.assertEqual(self.classified, ["app"])


class WatchProjectGateTests(unittest.TestCase):
    """cmd_watch's startup preflight: a watcher armed on a project label that
    does not exist would poll forever without ever waking anyone, so it refuses
    before the first poll. A failed label read is NOT a refusal — the poll loop
    already tolerates transport faults, so the watcher warns and arms anyway."""

    def setUp(self):
        self.polled = []

    def _snapshot(self, api, project):
        # The poll loop is infinite; the first poll is all this needs to see.
        self.polled.append(project)
        raise KeyboardInterrupt

    def _run(self, labels):
        # The verdict comes from the real preflight over a scripted label set.
        api = FakeApi(paginated=lambda path: labels)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = kraken.watch(api, "app", snapshot_reader=self._snapshot)
            except KeyboardInterrupt:
                rc = "polled"
        return rc, err.getvalue()

    def test_unknown_project_refuses_to_arm(self):
        rc, err = self._run([{"name": "project:other"}])
        self.assertEqual(rc, kraken.EXIT_UNKNOWN_PROJECT)
        self.assertEqual(self.polled, [], "a refused watcher still polled the queue")
        self.assertIn("unknown project", err)

    def test_configured_project_arms(self):
        rc, err = self._run([{"name": "project:app"}])
        self.assertEqual(rc, "polled")
        self.assertEqual(self.polled, ["app"])

    def test_transport_failure_warns_but_still_arms(self):
        rc, err = self._run(None)
        self.assertEqual(rc, "polled", "a failed label read must not kill the watcher")
        self.assertIn("gh-failure", err)


class StatusHelperTests(unittest.TestCase):
    """The status console's pure helpers, isolated from any transport: PR-URL
    parsing, ISO parsing, and age formatting."""

    def _rec(self, body, created):
        return {"body": body, "createdAt": created}

    def test_the_delivery_url_is_a_field_of_the_record(self):
        # protocol/9: the console reads `pr` off the state record (§8), so there
        # is nothing to parse out of a thread at all. The whole `parse_pr_url`
        # family — the newest-marker scan, the free-text fallback, and the
        # `legacy-free-text` source it reported — went with it.
        record = kraken.TaskState(state="awaiting-merge", worker="w1",
                                  comments=4, pr="https://github.com/o/r/pull/42",
                                  recorded=True)
        self.assertEqual(record.pr, "https://github.com/o/r/pull/42")
        self.assertFalse(hasattr(kraken, "parse_pr_url"),
                         "the prose fallback must not come back")

    def test_a_delivery_with_no_pr_records_none(self):
        # §8 carries the field "when there is one": a work repo that takes no
        # push delivers the diff on the thread, and the issue IS the review
        # target. `pr: None` says that, and the marker omits the key entirely.
        record = kraken.NO_RECORD.moved_to("awaiting-merge", "w1", 3)
        self.assertIsNone(record.pr)
        self.assertNotIn("pr", record.payload())

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

    def test_non_github_url_is_false_without_any_gh_call(self):
        calls = []
        api = FakeApi(request=lambda m, p, body=None: (
            calls.append(1), (500, ""))[1])
        gitlab = "https://gitlab.com/group/project/-/merge_requests/2313"
        self.assertFalse(kraken.pr_is_merged(api, gitlab))
        self.assertEqual(calls, [], "a non-GitHub URL must never hit gh")

    def test_unparseable_url_is_false(self):
        api = FakeApi(request=lambda m, p, body=None: self.fail(
            "an unparseable URL must never hit gh"))
        self.assertFalse(kraken.pr_is_merged(api, "not a url"))
        self.assertFalse(kraken.pr_is_merged(api, None))

    def test_github_transport_failure_is_none(self):
        api = FakeApi(request=lambda m, p, body=None: (500, ""))
        self.assertIsNone(kraken.pr_is_merged(api, "https://github.com/o/r/pull/1"))

    def test_github_merged_pr_is_true(self):
        api = FakeApi(request=lambda m, p, body=None: (
            200, json.dumps({"merged_at": "2026-07-01T00:00:00Z"})))
        self.assertTrue(kraken.pr_is_merged(api, "https://github.com/o/r/pull/1"))

    def test_github_open_pr_is_false(self):
        api = FakeApi(request=lambda m, p, body=None: (
            200, json.dumps({"merged_at": None, "merged": False, "state": "open"})))
        self.assertFalse(kraken.pr_is_merged(api, "https://github.com/o/r/pull/1"))


class StatusComputeTests(unittest.TestCase):
    """StatusReport: the whole report assembled from queue nodes, the claim
    refs (in-flight worker/age/msg) and an injected Api (comment threads, the
    label set) — grouping, project filter, orphan flagging, and transport-failure
    propagation, all with no gh."""

    NOW = kraken.parse_iso("2026-07-01T10:00:00Z")

    def _node(self, number, title, labels, created="2026-07-01T00:00:00Z"):
        return kraken.Task({
            "number": number, "title": title, "createdAt": created,
            "labels": {"nodes": [{"name": n} for n in labels]},
        })

    @staticmethod
    def _delivered(pr=None, comments=0):
        """The state record a delivery writes (§3.1, §8) — where the console now
        reads the PR link from. It used to paginate the whole comment thread of
        every awaiting-merge task looking for a `delivered` marker."""
        return kraken.TaskState(state="awaiting-merge", worker="w", pr=pr,
                                comments=comments, recorded=True)

    def _call(self, nodes, project="", claim_refs=None, commit_meta=None,
              states=None, merged=None, projects=None, ttl=None):
        merged = merged or {}
        commit_meta = commit_meta or {}
        # Tests seed {issue: sha}; the lease view wants the generation ladder.
        refs = {n: [(1, sha)] for n, sha in (claim_refs or {}).items()}
        # `paginated` is faked rather than `Queue.projects`, so the real
        # project:<name> stripping is under test too.
        api = FakeApi(
            "o/tasks",
            paginated=lambda path: [{"name": f"project:{p}"}
                                    for p in (projects or [])],
        )
        leases = kraken.lease_state(refs, commit_meta, self.NOW,
                                    kraken.lease_ttl_seconds(ttl))
        return kraken.StatusReport(
            api, project, self.NOW,
            pr_merged=lambda u: merged.get(u, False),
        ).of(kraken.QueueRead(nodes, leases, commit_meta, dict(states or {})))

    def test_groups_by_held_label_with_ref_liveness(self):
        nodes = [
            self._node(88, "review", ["kraken-task", "project:app", "awaiting-merge"]),
            self._node(97, "decide", ["kraken-task", "project:app", "needs-decision"]),
            self._node(99, "running", ["kraken-task", "project:app", "in-progress"]),
            self._node(12, "queued", ["kraken-task", "project:app"]),
        ]
        states = {88: self._delivered("https://x/pull/1"),
                  97: kraken.TaskState(state="needs-decision", worker="w",
                                       recorded=True)}
        claim_refs = {99: "s99"}
        commit_meta = {"s99": {"committedDate": "2026-07-01T09:00:00Z",
                               "message": hb("w1", "still going")}}
        report = self._call(nodes, claim_refs=claim_refs, commit_meta=commit_meta,
                            states=states, projects=["app"])
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

    def test_in_progress_label_without_a_ref_is_not_in_flight(self):
        # A stale badge (label, no ref). Under protocol/6 the console showed it
        # as in flight with no worker and no age, and the reaper requeued it.
        # Under protocol/7 nothing repairs the label, so reading it would strand
        # that "worker unknown" row forever — the lease alone says who is running.
        nodes = [self._node(99, "silent", ["kraken-task", "project:app", "in-progress"])]
        report = self._call(nodes, projects=["app"])
        self.assertEqual(report["in_flight"], [])

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
        states = {88: self._delivered("https://x/pull/5"),
                  91: self._delivered("https://x/pull/6")}
        merged = {"https://x/pull/5": True, "https://x/pull/6": False}
        report = self._call(nodes, states=states, merged=merged, projects=["app"])
        self.assertEqual(report["orphans"], [88])
        flags = {r["number"]: r["orphan"] for r in report["review_queue"]}
        self.assertTrue(flags[88])
        self.assertFalse(flags[91])

    def test_non_github_delivery_url_is_never_a_transport_failure(self):
        # #121: a GitLab MR delivery must not turn `status` into gh-failure
        # stage=read — it's neither an orphan nor a transport failure, just
        # unverifiable. End-to-end through the real kraken.pr_is_merged (not
        # the injected lambda) so the fix's actual call path is exercised.
        calls = []
        nodes = [self._node(88, "gitlab delivery",
                            ["kraken-task", "project:app", "awaiting-merge"])]
        gitlab_mr = "https://gitlab.com/group/project/-/merge_requests/2313"
        api = FakeApi(
            "o/tasks",
            request=lambda m, p, body=None: (calls.append(1), (500, ""))[1],
            paginated=lambda path: [{"name": "project:app"}],
        )
        report = kraken.StatusReport(api, "", self.NOW).of(
            kraken.QueueRead(nodes, {}, {}, {88: self._delivered(gitlab_mr)}))
        self.assertIsNotNone(report, "a non-GitHub delivery must not be gh-failure")
        self.assertEqual(report["orphans"], [])
        self.assertEqual(calls, [], "gh must never be asked about a non-GitHub URL")
        item = report["review_queue"][0]
        self.assertFalse(item["orphan"])
        self.assertTrue(item["merge_state_unknown"])

    def test_merge_state_unknown_false_for_a_verified_github_delivery(self):
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        report = self._call(nodes,
                            states={88: self._delivered("https://github.com/o/r/pull/5")},
                            merged={"https://github.com/o/r/pull/5": False}, projects=["app"])
        self.assertFalse(report["review_queue"][0]["merge_state_unknown"])

    def test_the_review_row_reads_the_pr_off_the_record(self):
        # protocol/9 (§8): the link is a field of the state record, so a PR a
        # human quoted in the thread cannot outrank it — and a delivery with no
        # PR reports `pr_url: null`, which the console renders as "review on the
        # thread" rather than sending the operator hunting for a link.
        nodes = [
            self._node(88, "delivered with a PR",
                       ["kraken-task", "project:app", "awaiting-merge"]),
            self._node(92, "no PR at all",
                       ["kraken-task", "project:app", "awaiting-merge"],
                       created="2026-07-01T02:00:00Z"),
        ]
        states = {88: self._delivered("https://github.com/o/r/pull/5"),
                  92: self._delivered(None)}
        report = self._call(nodes, states=states, projects=["app"])
        items = {r["number"]: r for r in report["review_queue"]}
        self.assertEqual(items[88]["pr_url"], "https://github.com/o/r/pull/5")
        self.assertIsNone(items[92]["pr_url"])
        self.assertNotIn("pr_source", items[88],
                         "the legacy-vs-marker distinction has no source left")

    def test_the_review_queue_costs_no_comment_read(self):
        # The cost claim protocol/9 is paying for: a console over N delivered
        # tasks used to paginate N comment threads. Any comment read here is a
        # regression, so the fake refuses one outright.
        nodes = [self._node(n, "d", ["kraken-task", "project:app", "awaiting-merge"],
                            created="2026-07-01T0%d:00:00Z" % n)
                 for n in range(1, 6)]
        api = FakeApi("o/tasks",
                      comment_records=lambda i: self.fail(
                          "status must not read a comment thread"),
                      paginated=lambda path: [{"name": "project:app"}])
        report = kraken.StatusReport(api, "", self.NOW, pr_merged=lambda u: False).of(
            kraken.QueueRead(nodes, {}, {},
                             {n: self._delivered("https://x/pull/%d" % n)
                              for n in range(1, 6)}))
        self.assertEqual(len(report["review_queue"]), 5)

    def test_a_failed_pr_read_still_propagates_none(self):
        # The console has one transport call left inside the report — the merge
        # state of a delivery PR — and a failure there must still be exit 20
        # rather than a report that quietly says "not merged".
        nodes = [self._node(88, "x", ["kraken-task", "project:app", "awaiting-merge"])]
        report = kraken.StatusReport(
            FakeApi("o/tasks"), "", self.NOW, pr_merged=lambda u: None,
        ).of(kraken.QueueRead(nodes, {}, {},
                              {88: self._delivered("https://github.com/o/r/pull/5")}))
        self.assertIsNone(report)


class QueueHygieneTests(unittest.TestCase):
    """The read-side twin of the arrival-time validator (PROTOCOL.md §2.1): the
    same three checks — project label, Goal, Acceptance — decided from the bodies
    the queue walk already carries, so the console reports them for the whole
    queue at once and nothing has to run in the coordination repo."""

    GOOD = "### Goal\n\nShip it.\n\n### Acceptance\n\n`npm test` passes.\n"

    def _node(self, number, labels, body, created="2026-07-01T00:00:00Z"):
        return kraken.Task(
            {"number": number, "title": "t%d" % number, "createdAt": created,
             "body": body, "labels": {"nodes": [{"name": l} for l in labels]}})

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


def _task_node(number, labels, comment_total=0):
    """A minimal open task the way `Queue.open_tasks` returns it — the only issue
    input reconcile_plan reads. `comment_total` is the thread's live comment
    count, which the requeue derivation compares against the state record's
    anchor (§3.1, §6)."""
    return kraken.Task(
        {"number": number, "title": "t%d" % number, "createdAt": "2026-01-01",
         "body": "", "labels": {"nodes": [{"name": l} for l in labels]},
         "comments": {"totalCount": comment_total}})


def _at(hours_ago):
    dt = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(hours=hours_ago))
    return {"committedDate": dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "message": clm("w")}


def _seconds_ago(seconds, worker="w"):
    dt = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(seconds=seconds))
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
    return kraken.lease_state(refs, meta, time.time(),
                              kraken.LEASE_DEFAULT_TTL_SECONDS)


class LeaseStateTests(unittest.TestCase):
    """The lease clock as a pure decision (PROTOCOL.md §5): the ref's commit date
    is the lease timestamp, the TTL is how long it holds, and the READER applies
    the expiry — so this is the one place expiry is decided for every consumer."""

    def test_a_fresh_lease_is_live(self):
        leases = _leases(i1=10)
        self.assertTrue(leases[1].live)
        self.assertEqual(leases[1].worker, "w")
        self.assertLess(leases[1].age, 60)

    def test_the_holder_is_the_highest_generation(self):
        # Ownership is the top of the ladder: a superseded generation left
        # behind by a failed cleanup decides nothing, and its stale date must
        # not make a live lease read as expired.
        old, new = "s-old", "s-new"
        meta = {old: _seconds_ago(kraken.LEASE_DEFAULT_TTL_SECONDS + 600, "w-dead"),
                new: _seconds_ago(5, "w-live")}
        leases = kraken.lease_state({1: [(1, old), (2, new)]}, meta,
                                    time.time(),
                                    kraken.LEASE_DEFAULT_TTL_SECONDS)
        self.assertEqual(leases[1].gen, 2)
        self.assertEqual(leases[1].worker, "w-live")
        self.assertTrue(leases[1].live)
        self.assertEqual(leases[1].gens, (1, 2),
                         "the ladder must stay visible so a release drops it all")

    def test_a_lease_past_the_ttl_is_not_live(self):
        self.assertFalse(_leases(i1=kraken.LEASE_DEFAULT_TTL_SECONDS + 5)[1].live)

    def test_the_ttl_boundary_expires(self):
        # Exactly at the TTL the lease is over — `age < ttl` is the live test, so
        # the boundary belongs to the thief, never to a holder that stopped
        # renewing precisely one TTL ago.
        self.assertFalse(_leases(i1=kraken.LEASE_DEFAULT_TTL_SECONDS)[1].live)
        self.assertTrue(_leases(i1=kraken.LEASE_DEFAULT_TTL_SECONDS - 5)[1].live)

    def test_an_unreadable_commit_fails_open_to_the_steal(self):
        # Nothing proves the holder alive, so the lease must not hold the task
        # forever behind a clock nobody can read.
        lease = _leases(i1=None)[1]
        self.assertIsNone(lease.age)
        self.assertFalse(lease.live)

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


class QueueWalkQueryTests(unittest.TestCase):
    """What the hot read asks the server for. The walk runs once a minute per
    worker over the WHOLE repo, so every field on it is paid for by every task —
    which is why it carries the comment COUNT and not a single comment body."""

    def setUp(self):
        self.queries = []

        def fake(q):
            self.queries.append(q)
            return {"data": {"repository": {"issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": ""}, "nodes": []}}}}
        self.api = FakeApi("o/tasks", graphql=fake)

    def test_the_walk_carries_the_count_and_no_comment_body(self):
        kraken.Queue(self.api).open_tasks()
        q = self.queries[0]
        self.assertIn("comments { totalCount }", q,
                      "the requeue derivation needs the count on the walk (§6)")
        self.assertNotIn("comments(last:", q,
                         "the queue walk must not carry comment threads")
        self.assertNotIn("nodes { body", q)
        # The fields it does need, still on the one walk.
        for field in ("body", "labels(first:", "blockedBy(first:",
                      'labels: ["kraken-task"]', "states: OPEN"):
            self.assertIn(field, q)


class DependsOnBatchTests(unittest.TestCase):
    """The `depends-on: #N` fallback resolve. One field per distinct target in
    one query, chunked — the whole point of the fallback is that a queue full of
    text dependencies costs round trips in the number of CALLS, not targets."""

    def _recorder(self, open_numbers=()):
        self.queries = []

        def fake(q):
            self.queries.append(q)
            return {"data": {"repository": {
                "i" + n: {"state": "OPEN" if int(n) in open_numbers else "CLOSED"}
                for n in re.findall(r"i([0-9]+): issue", q)}}}
        return FakeApi("o/t", graphql=fake)

    def test_an_empty_ask_is_no_call(self):
        api = FakeApi("o/t", graphql=lambda q: self.fail(
            "graphql must not be called for []"))
        self.assertEqual(kraken.Queue(api)._depends_on([]), {})

    def test_targets_within_the_chunk_are_one_call(self):
        api = self._recorder(open_numbers={2})
        got = kraken.Queue(api)._depends_on([1, 2, 3])
        self.assertEqual(len(self.queries), 1, "three targets must cost ONE call")
        self.assertEqual(got, {1: False, 2: True, 3: False})

    def test_chunks_beyond_the_ceiling(self):
        total = kraken.GRAPHQL_ALIAS_CHUNK * 2 + 1
        api = self._recorder(open_numbers=set(range(1, total + 1)))
        got = kraken.Queue(api)._depends_on(range(1, total + 1))
        self.assertEqual(len(self.queries), 3, "chunked, not one call per target")
        self.assertEqual(len(got), total)
        self.assertTrue(all(got.values()))

    def test_a_generator_survives_being_read_twice(self):
        # The targets are read once to build the fields and once to read the
        # answers back. Consuming them on the first pass answers {} for every
        # target — every dependency silently "closed", every task startable.
        api = self._recorder(open_numbers={7})
        self.assertEqual(kraken.Queue(api)._depends_on(n for n in (7, 8)),
                         {7: True, 8: False})

    def test_an_issue_the_reply_omits_is_not_open(self):
        api = FakeApi("o/t", graphql=lambda q: {"data": {"repository": {}}})
        self.assertEqual(kraken.Queue(api)._depends_on([5]), {5: False})

    def test_transport_failure_propagates_as_none(self):
        api = FakeApi("o/t", graphql=lambda q: None)
        self.assertIsNone(kraken.Queue(api)._depends_on([1]))


class ExpiryCountTests(unittest.TestCase):
    """The repeat-expiry guard's counter, now a FIELD of the state record
    (PROTOCOL.md §3.1). Through protocol/8 it was counted from the
    `lease-expired` markers in a trailing comment window, which undercounted
    once the window scrolled past the oldest steals — a task that had burned
    five workers could read as having burned two."""

    def test_a_steal_increments_the_count_exactly(self):
        record = kraken.NO_RECORD
        for expected in (1, 2, 3, 4, 5):
            record = record.stolen("thief-%d" % expected)
            self.assertEqual(record.expiries, expected)
        self.assertEqual(record.worker, "thief-5", "the record names its writer")

    def test_a_task_with_no_record_has_burned_nobody(self):
        self.assertEqual(kraken.NO_RECORD.expiries, 0)

    def test_a_steal_carries_the_state_and_anchor_forward(self):
        # Taking over a lease does not change what the task IS — only how many
        # workers it has burned. A delivery bounced back to a worker that then
        # dies must still read as delivered against the same anchor.
        delivered = kraken.TaskState(state="awaiting-merge", worker="w1",
                                     comments=7, pr="https://x/pull/1",
                                     recorded=True)
        stolen = delivered.stolen("w2")
        self.assertEqual((stolen.state, stolen.comments, stolen.pr),
                         ("awaiting-merge", 7, "https://x/pull/1"))

    def test_a_transition_carries_the_count_forward(self):
        # `expiries` is cumulative over the task's whole life: a delivery does
        # not forgive the workers the task already killed.
        record = kraken.NO_RECORD.stolen("w1").stolen("w2")
        moved = record.moved_to("awaiting-merge", "w3", 9)
        self.assertEqual(moved.expiries, 2)


class ReconcilerPlanTests(unittest.TestCase):
    """reconcile_plan's rules as a PURE decision (PROTOCOL.md §6) — no transport
    at all, because the whole point of protocol/5 is that the rules run off the
    queue read a reader already has. `nodes` carries only OPEN kraken-task
    issues, which is what makes rule 1 free: a ref whose issue is absent from that
    walk is a lock over a closed (or no-longer-task) issue.

    Two things are deliberately NOT in here. An EXPIRED lease (protocol/6): it is
    unheld, the claim path steals it, and the reconciler writes nothing. And the
    `in-progress` LABEL in any state (protocol/7): it is write-only, so neither a
    missing one nor an orphan one is a repair anybody is owed."""

    def _rules(self, plan):
        return sorted((a["rule"], a["issue"]) for a in plan)

    @staticmethod
    def _held(number, state, comments=0):
        return {number: kraken.TaskState(state=state, worker="w",
                                         comments=comments, recorded=True)}

    def test_rule1_orphan_lock_on_held_or_absent_issue(self):
        # #4 escalated but its ref lingered; #6 is closed, so it is not in nodes.
        # "Already left the claim" is now the RECORD's verdict, not the label's.
        nodes = [_task_node(4, ["kraken-task", "needs-decision"])]
        plan = kraken.reconcile_plan(nodes, _leases(i4=10, i6=10),
                                     self._held(4, "needs-decision"))
        self.assertEqual(self._rules(plan),
                         [("orphan-lock", 4), ("orphan-lock", 6)])

    def test_rule1_leaves_a_requeued_record_alone(self):
        # The record says needs-decision but the operator has since commented,
        # so the task is NOT held — the lease on it is a live claim somebody is
        # working, not a lock the transition forgot to drop.
        nodes = [_task_node(4, ["kraken-task", "needs-decision"], comment_total=5)]
        plan = kraken.reconcile_plan(nodes, _leases(i4=10),
                                     self._held(4, "needs-decision", comments=4))
        self.assertEqual(plan, [])

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

    @staticmethod
    def _expiries(number, count):
        return {number: kraken.TaskState(state=kraken.QUEUED, worker="w",
                                         expiries=count, recorded=True)}

    def test_rule2_reclaim_after_repeated_expiries(self):
        # A task nobody can finish: past the threshold it stops circulating and
        # becomes the operator's call. The count is the record's, and it is
        # exact — the marker count it replaces undercounted a long history.
        nodes = [_task_node(1, ["kraken-task", "in-progress"])]
        expired_age = kraken.LEASE_DEFAULT_TTL_SECONDS + 60
        plan = kraken.reconcile_plan(
            nodes, _leases(i1=expired_age),
            self._expiries(1, kraken.LEASE_EXPIRY_ESCALATE))
        self.assertEqual(self._rules(plan), [("reclaim", 1)])
        self.assertTrue(plan[0]["held"], "the in-progress label must be swapped off")
        self.assertIn(str(kraken.LEASE_EXPIRY_ESCALATE), plan[0]["reason"])

    def test_one_expiry_below_the_threshold_still_gets_stolen(self):
        nodes = [_task_node(1, ["kraken-task", "in-progress"])]
        expired_age = kraken.LEASE_DEFAULT_TTL_SECONDS + 60
        self.assertEqual(
            kraken.reconcile_plan(nodes, _leases(i1=expired_age),
                                  self._expiries(1, kraken.LEASE_EXPIRY_ESCALATE - 1)),
            [])

    def test_a_live_lease_is_never_reclaimed_however_many_expiries(self):
        # The count is about a dead task, not a busy one: whoever holds a LIVE
        # lease is working, and the history of past steals must not evict them.
        nodes = [_task_node(1, ["kraken-task", "in-progress"])]
        self.assertEqual(
            kraken.reconcile_plan(nodes, _leases(i1=10),
                                  self._expiries(1, kraken.LEASE_EXPIRY_ESCALATE + 2)),
            [])

    def test_a_live_lease_with_no_label_plans_nothing(self):
        # protocol/6 healed this (rule 3: re-add the badge a crashed claim never
        # wrote). Nothing reads the badge now, and the lease already holds the
        # task, so the repair buys a prettier issue list and one write.
        nodes = [_task_node(5, ["kraken-task"])]
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i5=10)), [])

    def test_an_orphan_in_progress_label_plans_nothing(self):
        # protocol/6 requeued this (rule 4: a label with no ref behind it). Under
        # protocol/7 the task is ALREADY queued — the label never held it — so
        # there is nothing to requeue and no comment to post.
        self.assertEqual(
            kraken.reconcile_plan([_task_node(3, ["kraken-task", "in-progress"])], {}),
            [])

    def test_a_queue_with_nothing_to_repair_plans_nothing(self):
        nodes = [_task_node(2, ["kraken-task", "in-progress"]),
                 _task_node(9, ["kraken-task"]),
                 _task_node(10, ["kraken-task", "awaiting-merge"])]
        states = self._held(10, "awaiting-merge")
        self.assertEqual(kraken.reconcile_plan(nodes, _leases(i2=10), states), [],
                         "a queue holding one live lease must cost zero writes")

    def test_rule3_migrates_a_held_label_with_no_record(self):
        # THE protocol/9 migration (§6 rule 3): a queue written by an older
        # revision wears held labels and has no records at all. One pass writes
        # what each label already means, and the queue heals on the first drain
        # after the upgrade — no `migrate` command, nothing installed in the repo.
        nodes = [_task_node(3, ["kraken-task", "awaiting-merge"], comment_total=6),
                 _task_node(4, ["kraken-task", "needs-decision"], comment_total=2)]
        plan = kraken.reconcile_plan(nodes, {}, {})
        self.assertEqual(self._rules(plan), [("migrate", 3), ("migrate", 4)])
        by_issue = {a["issue"]: a for a in plan}
        self.assertEqual(by_issue[3]["state"], "awaiting-merge")
        self.assertEqual(by_issue[3]["comments"], 6,
                         "the anchor is the thread as it stands right now")
        self.assertEqual(by_issue[4]["state"], "needs-decision")

    def test_rule3_is_a_one_shot(self):
        # Once the record exists the rule is silent — otherwise every drain
        # would rewrite it and no requeue would ever be derivable.
        nodes = [_task_node(3, ["kraken-task", "awaiting-merge"], comment_total=6)]
        self.assertEqual(
            kraken.reconcile_plan(nodes, {}, self._held(3, "awaiting-merge", 6)),
            [])

    def test_rule3_ignores_a_task_no_label_holds(self):
        nodes = [_task_node(3, ["kraken-task"]),
                 _task_node(4, ["kraken-task", "in-progress"])]
        self.assertEqual(kraken.reconcile_plan(nodes, {}, {}), [])

    def test_rule4_re_anchors_a_record_whose_thread_shrank(self):
        # §6 rule 4. The record anchored at 5 and the thread carries 3, so two
        # comments were deleted: the anchor is now unreachable and every reply
        # after it would be absorbed by `total > anchor`. The plan proposes the
        # repair; nothing here decides it (the applier confirms over REST).
        nodes = [_task_node(8, ["kraken-task", "awaiting-merge"], comment_total=3)]
        plan = kraken.reconcile_plan(nodes, {}, self._held(8, "awaiting-merge", 5))
        self.assertEqual(self._rules(plan), [("re-anchor", 8)])
        self.assertIn("5", plan[0]["reason"])
        self.assertIn("3", plan[0]["reason"])

    def test_rule4_leaves_a_reachable_anchor_alone(self):
        # Equal is the normal state of a freshly held task, and greater is a
        # requeue — neither is a thread that shrank, and repairing either would
        # rewrite a record every drain.
        for total in (5, 6):
            nodes = [_task_node(8, ["kraken-task", "awaiting-merge"],
                                comment_total=total)]
            self.assertEqual(
                kraken.reconcile_plan(nodes, {}, self._held(8, "awaiting-merge", 5)),
                [], "total=%d is not a shrunken thread" % total)

    def test_rule4_ignores_a_queued_record(self):
        # A `queued` record holds nothing, so its anchor is meaningless (§3.1)
        # and there is no derivation for a deletion to break.
        nodes = [_task_node(8, ["kraken-task"], comment_total=1)]
        self.assertEqual(
            kraken.reconcile_plan(nodes, {}, self._held(8, kraken.QUEUED, 5)), [])

    def test_rule4_is_idempotent(self):
        # After the repair the anchor IS the total, which is the case above that
        # plans nothing — so a second pass over the repaired queue is free.
        nodes = [_task_node(8, ["kraken-task", "awaiting-merge"], comment_total=3)]
        repaired = {8: self._held(8, "awaiting-merge", 5)[8].re_anchored(3)}
        self.assertEqual(kraken.reconcile_plan(nodes, {}, repaired), [])

    def test_an_orphan_state_record_is_swept(self):
        # A record on an issue the walk no longer carries is state over nothing —
        # the same orphan rule a leftover lock gets, so the namespace does not
        # grow one ref per task the queue has ever closed (§10).
        plan = kraken.reconcile_plan([], {}, self._held(42, "awaiting-merge"))
        self.assertEqual(self._rules(plan), [("orphan-state", 42)])


class ReconcilerApplyTests(unittest.TestCase):
    """apply_reconcile's write ordering and project_reconcile's in-memory fold.
    Every write is recorded, none real."""

    def setUp(self):
        self.writes = []
        self.api = self._api()

    def _api(self, swap=None):
        def request(method, path, body=None):
            # The ref writes go through the real gateways, so they show up here
            # as the HTTP calls they actually are rather than as faked functions:
            # the claim-ref DELETE, and the orphan commit + create a state record
            # is written with (§3.1).
            if method == "GET":
                return (200, "[]")   # the record read a transition does first
            if path.endswith("/git/commits"):
                return (201, json.dumps({"sha": "state-sha"}))
            if path.endswith("/git/refs"):
                self.writes.append(("record", (body or {}).get("ref", "")))
                return (201, "{}")
            ref = path.split("/git/", 1)[1]
            claim = kraken.parse_claim_ref(ref)
            self.writes.append(
                ("del-ref", claim[0] if claim else kraken.parse_state_ref(ref)))
            return (204, "")

        return FakeApi(
            "o/tasks",
            request=request,
            # The anchor a reclaim records, read back after its comment lands.
            comment_count=lambda n: 7,
            swap_labels=swap or (lambda n, remove=None, add=None: (
                self.writes.append(("labels", n, remove, add)) or True)),
            post_comment=lambda n, body: (
                self.writes.append(("comment", n, body)) or True),
        )

    def _apply(self, plan):
        buf = StringIO()
        with redirect_stdout(buf):
            counts = kraken.apply_reconcile(self.api, plan, "w-reaper")
        return counts, buf.getvalue()

    def test_reclaim_deletes_the_ref_last(self):
        counts, _ = self._apply([{"rule": "reclaim", "issue": 1,
                                  "reason": "the lease expired 3 times",
                                  "held": True, "gens": [1]}])
        self.assertEqual(counts["reclaim"], 1)
        kinds = [w[0] for w in self.writes]
        self.assertEqual(kinds, ["comment", "record", "labels", "del-ref"],
                         "§3.1 ordering: comment, record, labels, ref delete last")
        self.assertIn(("labels", 1, "in-progress", "needs-decision"), self.writes)
        self.assertIn(("record", "refs/kraken/state/1"), self.writes)

    def test_migrate_writes_only_the_record(self):
        # §6 rule 3: the label is already there and already holding, so this
        # writes down what it means and touches nothing else — no comment, no
        # label swap, no ref delete.
        counts, _ = self._apply([{"rule": "migrate", "issue": 8, "reason": "x",
                                  "state": "awaiting-merge", "comments": 4}])
        self.assertEqual(counts["migrate"], 1)
        self.assertEqual(self.writes, [("record", "refs/kraken/state/8")])

    def test_orphan_state_deletes_the_record(self):
        counts, _ = self._apply([{"rule": "orphan-state", "issue": 9,
                                  "reason": "x"}])
        self.assertEqual(counts["orphan-state"], 1)
        self.assertEqual(self.writes, [("del-ref", 9)])

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

    def _re_anchor_api(self, *, record, live_total):
        """An api whose `States.of` answers `record` and whose REST comment
        count answers `live_total` — the two reads the re-anchor rule confirms
        itself against before it writes."""
        def paginated(path):
            return [{"ref": "refs/kraken/state/8", "object": {"sha": "s8"}}]

        return FakeApi(
            "o/tasks",
            request=self.api.request,
            paginated=paginated,
            aliased=lambda fields: {
                "c0": {"committedDate": "2026-01-01T00:00:00Z",
                       "message": kraken.make_marker(record.payload())}},
            comment_count=lambda n: live_total,
        )

    @staticmethod
    def _plan_re_anchor(issue=8):
        return [{"rule": "re-anchor", "issue": issue, "reason": "shrank"}]

    def test_re_anchor_writes_only_the_record(self):
        # §6 rule 4: the anchor moves and nothing else does — no comment, no
        # label swap, no ref delete. The hold survives the repair on purpose.
        api = self._re_anchor_api(
            record=kraken.TaskState(state="awaiting-merge", worker="w0",
                                    comments=5, recorded=True),
            live_total=3)
        buf = StringIO()
        with redirect_stdout(buf):
            counts = kraken.apply_reconcile(api, self._plan_re_anchor(), "w-reaper")
        self.assertEqual(counts["re-anchor"], 1)
        self.assertEqual(self.writes, [("record", "refs/kraken/state/8")])
        self.assertIn("anchor 5 -> 3", buf.getvalue())

    def test_re_anchor_confirms_over_rest_before_writing(self):
        # The walk PROPOSES; the read-back DECIDES. A GraphQL walk that failed
        # to select `totalCount` reports 0 for every task, which would otherwise
        # re-anchor a whole queue to zero and requeue it on the next comment.
        api = self._re_anchor_api(
            record=kraken.TaskState(state="awaiting-merge", worker="w0",
                                    comments=5, recorded=True),
            live_total=9)
        buf = StringIO()
        with redirect_stdout(buf):
            counts = kraken.apply_reconcile(api, self._plan_re_anchor(), "w-reaper")
        self.assertEqual(counts["re-anchor"], 0, "a refuted proposal is not a repair")
        self.assertEqual(self.writes, [], "the walk talked the reaper into a write")
        self.assertIn("skipped", buf.getvalue())

    def test_re_anchor_refuses_on_an_unreadable_count(self):
        # Ambiguous is never a decision: an unread count is exit 20, never a 0.
        api = self._re_anchor_api(
            record=kraken.TaskState(state="awaiting-merge", worker="w0",
                                    comments=5, recorded=True),
            live_total=None)
        buf, err = StringIO(), StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            counts = kraken.apply_reconcile(api, self._plan_re_anchor(), "w")
        self.assertIsNone(counts)
        self.assertIn("gh-failure stage=count issue=8", err.getvalue())
        self.assertEqual(self.writes, [])

    def test_re_anchor_keeps_everything_but_the_anchor(self):
        # The state, the worker, the expiry count and the delivery URL are not
        # the deletion's business — only the comparison was broken.
        before = kraken.TaskState(state="awaiting-merge", worker="w0",
                                  comments=5, expiries=2, recorded=True,
                                  pr="https://github.com/o/r/pull/1")
        after = before.re_anchored(3)
        self.assertEqual(after.comments, 3)
        self.assertEqual(
            (after.state, after.worker, after.expiries, after.pr),
            (before.state, before.worker, before.expiries, before.pr))
        self.assertTrue(after.held_state, "the repair must not lift the hold")
        self.assertFalse(after.requeued(3), "re-anchoring is not a requeue")

    def test_transport_failure_answers_none(self):
        api = self._api(swap=lambda n, remove=None, add=None: False)
        buf, err = StringIO(), StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            counts = kraken.apply_reconcile(
                api,
                [{"rule": "reclaim", "issue": 5, "reason": "x", "held": True,
                  "gens": [1]}], "w")
        self.assertIsNone(counts)
        self.assertIn("gh-failure stage=labels issue=5", err.getvalue())

    def test_project_reconcile_folds_the_plan_into_the_read(self):
        # The drain must see the state it just repaired without re-fetching it.
        tasks = [_task_node(1, ["kraken-task", "in-progress"]),
                 _task_node(5, ["kraken-task"])]
        leases = _leases(i1=10, i5=10, i6=10)
        plan = [{"rule": "reclaim", "issue": 1, "reason": "x", "held": True,
                 "gens": [1]},
                {"rule": "orphan-lock", "issue": 6, "reason": "x", "gens": [1]}]
        kraken.project_reconcile(plan, tasks, leases)
        self.assertEqual(sorted(leases), [5],
                         "reclaimed/orphan leases must be dropped")
        labels = {t.number: t.labels for t in tasks}
        self.assertEqual(labels[1], {"kraken-task", "needs-decision"})
        # #5's live lease is untouched, badge or no badge: the fold mirrors the
        # writes, and protocol/7 plans no label write for it.
        self.assertEqual(labels[5], {"kraken-task"})


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
    """The poll loop around that gate: a failed read is counted and reported on
    stderr (never on stdout, which is the wake channel), the counter resets on
    the first successful read, and the ceiling exits non-zero."""

    def _setenv(self, name, value):
        os.environ[name] = value
        self.addCleanup(os.environ.pop, name, None)

    def _run(self, snapshots, env=None):
        """Drive the loop over a scripted snapshot sequence; the loop is
        infinite, so an exhausted script interrupts it."""
        pending = list(snapshots)

        def scripted(api, project):
            if not pending:
                raise KeyboardInterrupt
            return pending.pop(0)

        self._setenv("KRAKEN_WATCH_POLL_SECONDS", "0")  # no real waiting
        for name, value in (env or {}).items():
            self._setenv(name, value)

        api = FakeApi(paginated=lambda path: [{"name": "project:app"}])
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = kraken.watch(api, "app", snapshot_reader=scripted)
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


# --- next-action -------------------------------------------------------------
# The verdict is a pure function of what was observed, so every branch is
# exercised here without a stub: what a recorded claim is worth, and what the
# envelope promises for it. The wire-level behaviour lives in
# tests/conformance/test_next_action.py.

def rec(issue="7", repo="acme/tasks", worker="w1"):
    """A claim-<worker>.json record as open_claim_record returns it."""
    return {"repo": repo, "issue": str(issue), "worker": worker}


def ref_head(worker="w1", epoch=1000.0, gen=1):
    """What claim_ref_head hands resume_verdict: ownership and the clock, with
    the expiry verdict left undecided (no TTL is known at that read)."""
    return kraken.Lease(gen=gen, sha="abc", worker=worker, epoch=epoch,
                        gens=tuple(range(1, gen + 1)))


def issue_obj(state="open", labels=("kraken-task", "in-progress"), comments=0):
    return {"state": state, "title": "t", "body": "b", "comments": comments,
            "labels": [{"name": n} for n in labels]}


def task_state(state, comments, **kw):
    """A state record as a transition would have written it (PROTOCOL.md §3.1)."""
    return kraken.TaskState(state=state, worker="w1", comments=comments,
                            recorded=True, **kw)


class ResumeVerdictTests(unittest.TestCase):
    """PROTOCOL.md §5.3 as a decision table: who owns the lease decides whether a
    write is legal, and an ambiguous read is never a decision."""

    # None is a meaningful VALUE for obj ("the read failed"), so its default
    # needs a sentinel of its own. `head` no longer needs one: the two
    # no-ladder outcomes are objects (NO_LEASE / UNREADABLE_LEASE).
    DEFAULT = object()

    def verdict(self, *, record=None, repo="acme/tasks", worker="w1",
                head=DEFAULT, obj=DEFAULT, state=kraken.NO_RECORD):
        return kraken.resume_verdict(
            record or rec(), repo, worker,
            ref_head() if head is self.DEFAULT else head,
            issue_obj() if obj is self.DEFAULT else obj, state)

    def test_live_lease_of_ours_resumes(self):
        v, detail = self.verdict(head=ref_head(epoch=1900.0))
        self.assertEqual(v, "execute", "our own live lease must resume")
        self.assertEqual(detail["generation"], 1, "generation not reported")

    def test_expired_but_ours_still_resumes(self):
        # §5.3: expired-but-unstolen is still ours — completing the transition
        # frees it honestly. The caller is told to renew, not to stop. No clock
        # is passed because the verdict does not consult one: ownership decides.
        v, _ = self.verdict(head=ref_head(epoch=1.0))
        self.assertEqual(v, "execute",
                         "an expired lease nobody took is still ours to finish")

    def test_stolen_lease_abandons(self):
        v, detail = self.verdict(head=ref_head(worker="w-thief", gen=2))
        self.assertEqual(v, "abandon", "a stolen lease must not be written through")
        self.assertEqual(detail["reason"], "lease-stolen")
        self.assertIn("w-thief", detail["detail"], "the new holder is not named")

    def test_missing_ref_on_an_open_task_abandons(self):
        v, detail = self.verdict(head=kraken.NO_LEASE)
        self.assertEqual(v, "abandon", "a vanished lease holds nothing")
        self.assertEqual(detail["reason"], "lease-gone")

    def test_unreadable_lease_is_a_retry_not_a_loss(self):
        v, detail = self.verdict(head=kraken.UNREADABLE_LEASE)
        self.assertEqual(v, "retry",
                         "an ambiguous read must never be turned into a verdict")
        self.assertEqual(detail["reason"], "lease-unreadable")

    def test_unreadable_issue_is_a_retry(self):
        v, detail = self.verdict(obj=None)
        self.assertEqual(v, "retry", "a failed issue read is ambiguous, not a loss")
        self.assertEqual(detail["reason"], "issue-unreadable")

    def test_terminal_label_without_our_lease_is_resolved(self):
        # The transition landed; only the local scratch file lagged. Keep
        # draining rather than report a loss.
        v, detail = self.verdict(
            head=kraken.NO_LEASE,
            obj=issue_obj(labels=("kraken-task", "awaiting-merge")))
        self.assertEqual(v, "resolved", "a delivered task is not an abandonment")
        self.assertEqual(detail["reason"], "already-resolved")

    def test_closed_task_under_our_own_lease_is_resolved(self):
        v, detail = self.verdict(obj=issue_obj(state="closed"))
        self.assertEqual(v, "resolved", "a closed task has nothing to resume")
        self.assertEqual(detail["reason"], "task-finished")

    def test_a_requeued_delivery_under_our_lease_resumes(self):
        # §3.1 through the resume path. We delivered #7 and the ref delete was
        # lost, so the lease is still ours; the operator then replied, which §6
        # requeues. The badge still reads `awaiting-merge`, and reading it made
        # this "resolved" — the worker dropped a task the queue had just handed
        # back, silently, with nobody else holding it.
        v, _ = self.verdict(
            obj=issue_obj(labels=("kraken-task", "awaiting-merge"), comments=5),
            state=task_state("awaiting-merge", 4))
        self.assertEqual(v, "execute",
                         "a requeued delivery is live work, not a finished task")

    def test_an_unrequeued_delivery_under_our_lease_is_resolved(self):
        # The other direction, unchanged: nothing was said after the delivery,
        # so the record still holds and the turn really is over.
        v, detail = self.verdict(
            obj=issue_obj(labels=("kraken-task", "awaiting-merge"), comments=4),
            state=task_state("awaiting-merge", 4))
        self.assertEqual(v, "resolved", "an untouched delivery is finished")
        self.assertEqual(detail["reason"], "task-finished")

    def test_a_stale_badge_the_record_contradicts_resumes(self):
        # The record says queued — a release whose label swap was lost. The
        # badge must not end a turn the record says is still open.
        v, _ = self.verdict(
            obj=issue_obj(labels=("kraken-task", "awaiting-merge"), comments=9),
            state=task_state(kraken.QUEUED, 9))
        self.assertEqual(v, "execute", "a badge the record contradicts decides nothing")

    def test_a_record_holds_even_when_the_badge_never_landed(self):
        # The inverse: the transition wrote the record and lost the label swap.
        v, detail = self.verdict(
            obj=issue_obj(labels=("kraken-task", "in-progress"), comments=4),
            state=task_state("needs-decision", 4))
        self.assertEqual(v, "resolved", "the record is the state, badge or not")
        self.assertEqual(detail["reason"], "task-finished")

    def test_no_record_still_reads_the_badge(self):
        # §3.1's sanctioned fallback — and the proof this change left a
        # pre-protocol/9 queue behaving exactly as it did.
        v, detail = self.verdict(
            head=kraken.NO_LEASE,
            obj=issue_obj(labels=("kraken-task", "awaiting-merge"),
                          comments=999))
        self.assertEqual(v, "resolved", "a task with no record is read off its badge")
        self.assertEqual(detail["reason"], "already-resolved")

    def test_claim_in_another_repo_blocks(self):
        # One task at a time is repo-independent, and it is decided before any
        # read — hence the "nothing was observed" lease and no issue.
        v, detail = kraken.resume_verdict(
            rec(repo="acme/other"), "acme/tasks", "w1",
            kraken.UNREADABLE_LEASE, None)
        self.assertEqual(v, "blocked", "a claim elsewhere must block this drain")
        self.assertEqual(detail["reason"], "claim-elsewhere")
        self.assertIn("acme/other#7", detail["detail"],
                      "the blocking claim is not named")


class NextActionEnvelopeTests(unittest.TestCase):
    """The envelope is the contract the agent reads, so its shape is pinned."""

    def test_execute_carries_fully_interpolated_commands(self):
        env = kraken.next_action_envelope(
            "execute", "acme/tasks", "env-1", issue=12, resumed=False,
            script="/plugins/with space/kraken.py")
        self.assertEqual(set(env["then"]),
                         {"renew", "note", "escalate", "deliver", "release"},
                         "the legal next writes drifted")
        for name, command in env["then"].items():
            self.assertIn("acme/tasks 12 env-1", command,
                          "then.%s is not interpolated" % name)
            self.assertIn('"/plugins/with space/kraken.py"', command,
                          "then.%s does not quote a path containing spaces" % name)

    def test_verdicts_with_no_legal_write_offer_none(self):
        for action in ("idle", "abandon", "stop", "retry"):
            env = kraken.next_action_envelope(action, "acme/tasks", "env-1",
                                              issue=12, reason="x")
            self.assertNotIn("then", env,
                             "%s must offer no next write — none is legal" % action)

    def test_blocked_names_the_held_claim_and_targets_it(self):
        # The held claim may live in ANOTHER repo. Pairing the top-level repo
        # with its issue number would name a task that does not exist, and a
        # worker acting on it would write to the wrong repo's issue.
        env = kraken.next_action_envelope(
            "blocked", "acme/tasks", "env-1", reason="claim-elsewhere",
            holding={"repo": "acme/other", "issue": "7"},
            script="/plugins/kraken.py")
        self.assertNotIn("issue", env,
                         "blocked must not carry a bare top-level issue — it "
                         "would read against the repo being drained")
        self.assertEqual(env["holding"], {"repo": "acme/other", "issue": 7},
                         "the held claim is not named")
        # A write IS legal here — it is what resolves the block — so the commands
        # must be present and must target the HELD claim.
        for name in ("deliver", "escalate", "release"):
            self.assertIn("acme/other 7 env-1", env["then"][name],
                          "then.%s does not target the held claim" % name)
            self.assertNotIn("acme/tasks", env["then"][name],
                             "then.%s targets the drained repo, not the held "
                             "claim" % name)

    def test_every_action_has_an_exit_code(self):
        self.assertEqual(set(kraken.NEXT_ACTIONS), set(kraken.NEXT_ACTION_EXIT),
                         "an action without an exit code is unreachable from a "
                         "shell, and an exit code without an action is dead")

    def test_lease_block_reports_the_renewal_contract(self):
        block = kraken.lease_block(1000.0, 1200.0, 1800, generation=3)
        self.assertEqual(block["generation"], 3)
        self.assertEqual(block["seconds_remaining"], 1600, "TTL math is wrong")
        self.assertFalse(block["renew_now"], "a live lease does not need renewing")
        self.assertEqual(block["renew_every_seconds"],
                         kraken.lease_renew_seconds(1800),
                         "the cadence must come from the TTL, never a literal")
        self.assertEqual(block["expires_at"], kraken.format_iso(2800.0),
                         "expires_at is not the anchor plus the TTL")

    def test_lease_block_flags_an_expired_lease(self):
        block = kraken.lease_block(1000.0, 9999.0, 1800)
        self.assertTrue(block["renew_now"], "an expired lease must say so")
        self.assertLess(block["seconds_remaining"], 0,
                        "an expired lease reports the overrun honestly")

    def test_format_iso_round_trips_through_parse_iso(self):
        self.assertEqual(kraken.parse_iso(kraken.format_iso(1700000000.0)),
                         1700000000.0, "a timestamp we emit must read back")


class TaskBriefTests(unittest.TestCase):
    def test_sections_are_split_and_placeholders_read_as_empty(self):
        body = ("### Goal\nship it\n\n### Acceptance\nmake check\n\n"
                "### Notes\n_No response_\n")
        brief = kraken.task_brief("a title", body)
        self.assertEqual(brief["goal"], "ship it")
        self.assertEqual(brief["acceptance"], "make check")
        self.assertEqual(brief["notes"], "",
                         "the issue-form placeholder must not reach the agent as "
                         "if it were a note")
        self.assertEqual(brief["body"], body,
                         "the raw body must survive for hand-written issues that "
                         "carry no headings")

    def test_hand_written_issue_keeps_its_body(self):
        brief = kraken.task_brief("t", "just some prose")
        self.assertEqual(brief["goal"], "", "no heading means no Goal section")
        self.assertEqual(brief["body"], "just some prose")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StateRecordParseTests(unittest.TestCase):
    """Decoding a state record off its ref commit (PROTOCOL.md §3.1, §4). Foreign
    data: a record that does not decode must never become a half-record, because
    every reader of it is deciding whether a task is held."""

    @staticmethod
    def _commit(**fields):
        return kraken.make_marker({"type": "state", **fields})

    def test_round_trips_every_field(self):
        written = kraken.TaskState(state="awaiting-merge", worker="env-1",
                                   comments=7, expiries=2, pr="https://x/pull/1",
                                   recorded=True)
        read = kraken.parse_state_commit(kraken.make_marker(written.payload()))
        self.assertEqual(
            (read.state, read.worker, read.comments, read.expiries, read.pr),
            ("awaiting-merge", "env-1", 7, 2, "https://x/pull/1"))
        self.assertTrue(read.recorded)

    def test_a_marker_of_another_type_is_not_a_record(self):
        self.assertIsNone(kraken.parse_state_commit(
            kraken.make_marker({"type": "claim", "worker": "w1"})))

    def test_an_unknown_state_is_refused(self):
        # A reader that accepted it would have to decide whether it holds, and
        # there is no honest answer to that.
        self.assertIsNone(kraken.parse_state_commit(
            self._commit(state="in-progress", worker="w1")))
        self.assertIsNone(kraken.parse_state_commit(
            self._commit(state="", worker="w1")))

    def test_undecodable_json_is_not_a_record(self):
        self.assertIsNone(kraken.parse_state_commit("<!-- kraken not-json -->"))
        self.assertIsNone(kraken.parse_state_commit(""))

    def test_a_junk_counter_reads_as_zero(self):
        # Fail-open: 0 requeues (any comment is newer than none) and never
        # escalates early. The opposite default would bury a delivery.
        for junk in (None, "seven", True, -3, [1]):
            record = kraken.parse_state_commit(
                self._commit(state="needs-decision", worker="w1",
                             comments=junk, expiries=junk))
            self.assertEqual((record.comments, record.expiries), (0, 0), junk)

    def test_a_missing_pr_is_none_and_is_not_serialized(self):
        record = kraken.parse_state_commit(
            self._commit(state="awaiting-merge", worker="w1", pr="   "))
        self.assertIsNone(record.pr)
        self.assertNotIn("pr", record.payload())

    def test_state_view_drops_what_it_cannot_decode(self):
        # Absence is the fail-open answer (§3.1), and dropping is what makes a
        # corrupted record self-healing: §6 rule 3 sees a held label with no
        # record and writes a fresh one.
        meta = {"good": {"message": self._commit(state="queued", worker="w1")},
                "junk": {"message": "not a marker"}}
        view = kraken.state_view({1: "good", 2: "junk", 3: "missing"}, meta)
        self.assertEqual(sorted(view), [1])


class StateRefNameTests(unittest.TestCase):
    """The ref name, parsed strictly for the same reason a claim ref is:
    matching-refs is a plain PREFIX match, so `state/12` also returns
    `state/120` and the caller must filter on what it parsed."""

    def test_round_trip(self):
        self.assertEqual(kraken.state_ref(12), "refs/kraken/state/12")
        self.assertEqual(kraken.parse_state_ref("refs/kraken/state/12"), 12)

    def test_rejects_anything_else(self):
        for ref in ("refs/kraken/claims/12", "refs/kraken/state/", "refs/heads/x",
                    "refs/kraken/state/12/3", "refs/kraken/state/abc"):
            self.assertIsNone(kraken.parse_state_ref(ref), ref)

    def test_the_prefix_match_is_filtered_by_the_parse(self):
        items = [{"ref": "refs/kraken/state/12", "object": {"sha": "a"}},
                 {"ref": "refs/kraken/state/120", "object": {"sha": "b"}},
                 {"ref": "refs/kraken/claims/12/1", "object": {"sha": "c"}}]
        self.assertEqual(kraken.state_ref_shas(items), {12: "a", 120: "b"})

    def test_one_namespace_read_serves_both_families(self):
        # The call-count property protocol/9 rests on: claims and records come
        # back from ONE matching-refs read, each parsing its own names out.
        items = [{"ref": "refs/kraken/claims/7/2", "object": {"sha": "c7"}},
                 {"ref": "refs/kraken/state/7", "object": {"sha": "s7"}}]
        api = FakeApi("o/t", paginated=lambda path: self.fail(
            "the payload was already fetched"))
        self.assertEqual(kraken.Refs(api).all(items), {7: [(2, "c7")]})
        self.assertEqual(kraken.States(api).all(items), {7: "s7"})


class StatesGatewayTests(unittest.TestCase):
    """Writing a record: create, and force-update when it is already there.

    Force is correct here and would be wrong on a claim ref — there is no ladder
    to climb and no race to arbitrate, because the lease already serialized the
    writers (§3.1)."""

    def _api(self, ref_exists):
        self.calls = []

        def request(method, path, body=None):
            self.calls.append((method, path.rsplit("/git/", 1)[-1]))
            if path.endswith("/git/commits"):
                return (201, json.dumps({"sha": "deadbeef"}))
            if path.endswith("/git/refs"):
                return (422, "") if ref_exists else (201, "{}")
            return (200, "{}")
        return FakeApi("o/t", request=request)

    def test_a_first_write_is_one_create(self):
        api = self._api(ref_exists=False)
        self.assertTrue(kraken.States(api).write(9, kraken.NO_RECORD))
        self.assertEqual([m for m, _ in self.calls], ["POST", "POST"],
                         "the common case must be commit + create, nothing more")

    def test_an_existing_record_is_force_updated(self):
        api = self._api(ref_exists=True)
        self.assertTrue(kraken.States(api).write(9, kraken.NO_RECORD))
        self.assertEqual(self.calls[-1], ("PATCH", "refs/kraken/state/9"))

    def test_a_failed_commit_is_a_failed_write(self):
        api = FakeApi("o/t", request=lambda m, p, body=None: (500, ""))
        self.assertFalse(kraken.States(api).write(9, kraken.NO_RECORD))

    def test_delete_tolerates_a_missing_record(self):
        # Idempotent under retries, the same tolerance a claim-ref delete has.
        api = FakeApi("o/t", request=lambda m, p, body=None: (422, ""))
        self.assertTrue(kraken.States(api).delete(9))

    def test_a_failed_ref_read_is_unknown_never_absent(self):
        # "No record" and "the read did not land" demand opposite answers: the
        # first offers the task, the second refuses to write at all.
        api = FakeApi("o/t", request=lambda m, p, body=None: (500, ""))
        self.assertTrue(kraken.States(api).of(9).unknown)

    def test_no_ref_is_no_record(self):
        api = FakeApi("o/t", request=lambda m, p, body=None: (200, "[]"))
        record = kraken.States(api).of(9)
        self.assertFalse(record.recorded)
        self.assertFalse(record.unknown)
        self.assertEqual(record.state, kraken.QUEUED)
