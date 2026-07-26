#!/usr/bin/env python3
"""Unit tests for the coordination passes kraken.py owns: the §6 reconcile and
requeue derivation, plus the validate and cleanup subcommands. These began as
coordination-repo workflows (issues #37, #39), moved out of jq/grep/awk/bash into
kraken.py so one parser drives them all; protocol/5 then moved the reconcile and
the requeue onto the reader, where they are decided rather than executed.

Two layers: the pure parsing/decision helpers (no transport at all), and the
cmd_* entry points with their gh transport mocked — exactly the pattern
ClaimNextIterationTests uses, so only the workflow logic is under test.

Stdlib only (unittest), no network, no gh.
"""

import os
import sys
import datetime
import unittest
from types import SimpleNamespace
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(HERE, "..", "..", "skills", "unleash")
sys.path.insert(0, os.path.abspath(SKILL_DIR))

import kraken  # noqa: E402


def disclaimer_body(worker, *rest):
    """A real worker comment: the attribution disclaimer heading followed by
    optional extra lines (composed the way a worker's comment actually is)."""
    parts = [kraken.disclaimer(worker)]
    parts.extend(rest)
    return "\n\n".join(parts)


# --- human-vs-worker discrimination ------------------------------------------

class WorkerCommentTests(unittest.TestCase):
    """The requeue derivation's discriminator: a comment is a worker's iff it
    carries a hidden kraken marker on any line (PROTOCOL.md §4), with a first-line
    attribution disclaimer as a legacy fallback. Both are derived from kraken.py
    constants, never a second copy."""

    def test_disclaimer_headed_comment_is_a_worker(self):
        self.assertTrue(kraken.is_worker_comment(disclaimer_body("env-1", "some prose")))

    def test_marker_bearing_comment_is_a_worker(self):
        # The structural discriminator: any hidden marker → worker, even without a
        # leading disclaimer line.
        body = "prose\n\n" + kraken.make_marker({"type": "delivered", "worker": "w1"})
        self.assertTrue(kraken.is_worker_comment(body))

    def test_note_comment_is_a_worker(self):
        # compose_note now carries a `note` marker, so a note is worker-authored
        # structurally — not merely because of its disclaimer line.
        self.assertTrue(kraken.is_worker_comment(kraken.compose_note("env-1", "assuming X")))

    def test_operator_pasting_a_raw_marker_is_read_as_worker(self):
        # Accepted edge (PROTOCOL.md §4): a raw kraken marker pasted by an
        # operator reads as a worker comment; hand-removing the label is the
        # escape hatch.
        self.assertTrue(kraken.is_worker_comment(
            "bounce it\n\n" + kraken.make_marker({"type": "requeue"})))

    def test_bare_human_comment_is_not_a_worker(self):
        self.assertFalse(kraken.is_worker_comment("option B, go"))

    def test_malformed_marker_does_not_classify_as_worker(self):
        # A line that is not a decodable kraken marker (no string type) is inert.
        self.assertFalse(kraken.is_worker_comment("here is <!-- kraken not-json -->"))

    def test_disclaimer_mid_body_still_a_worker_via_no_marker_is_human(self):
        # An operator quoting the disclaimer mid-reply, with no marker anywhere,
        # is still a human — only the opening line counts for the disclaimer path.
        body = "answering below:\n\n" + kraken.disclaimer("env-1") + "\n\noption B"
        self.assertFalse(kraken.is_worker_comment(body))

    def test_name_agnostic(self):
        # Any worker name matches — the disclaimer fallback keys on the prefix up
        # to the opening backtick, not a specific name.
        for name in ("env-1", "kraken-copilot-9", "a"):
            self.assertTrue(kraken.is_worker_comment(disclaimer_body(name)))

    def test_crlf_first_line_still_matches(self):
        body = kraken.disclaimer("env-1") + "\r\n\r\nprose"
        self.assertTrue(kraken.is_worker_comment(body))


# --- requeue-directive detection ---------------------------------------------

class RequeueDirectiveTests(unittest.TestCase):
    """awaiting-merge (delivered work) rejoins the queue ONLY on an explicit,
    structured requeue directive: a protocol/3 requeue marker or a standalone
    requeue/requeue: line. A prose sentence must never bounce a ready branch."""

    def test_structured_marker_is_a_directive(self):
        body = "bounce it\n\n" + kraken.make_marker({"type": "requeue"})
        self.assertTrue(kraken.has_requeue_directive(body))

    def test_standalone_requeue_line(self):
        self.assertTrue(kraken.has_requeue_directive("requeue"))
        self.assertTrue(kraken.has_requeue_directive("requeue:"))

    def test_standalone_requeue_line_among_others(self):
        self.assertTrue(kraken.has_requeue_directive("requeue:\nfix the typo first"))

    def test_case_insensitive_standalone_line(self):
        self.assertTrue(kraken.has_requeue_directive("REQUEUE"))
        self.assertTrue(kraken.has_requeue_directive("  Requeue:  "))

    def test_prose_starting_with_requeue_is_not_a_directive(self):
        # THE accidental-collision fix.
        self.assertFalse(kraken.has_requeue_directive(
            "requeue: is something I considered, but hold off until Monday"))

    def test_absent_directive(self):
        self.assertFalse(kraken.has_requeue_directive("looks good, merging tomorrow"))

    def test_non_requeue_marker_is_not_a_directive(self):
        body = kraken.make_marker({"type": "heartbeat", "worker": "w1"})
        self.assertFalse(kraken.has_requeue_directive(body))


# --- section validation ------------------------------------------------------

class SectionParsingTests(unittest.TestCase):
    """validate-task's section detection: the trimmed content under an issue-form
    heading, and the empty/`_No response_` rule."""

    GOOD = ("### Goal\n\nShip it.\n\n### Acceptance\n\n`npm test` passes.\n\n"
            "### Notes\n\n_No response_")

    def test_extracts_section_content(self):
        self.assertIn("Ship it.", kraken.section_body(self.GOOD, "Goal"))
        self.assertIn("npm test", kraken.section_body(self.GOOD, "Acceptance"))

    def test_section_stops_at_next_heading(self):
        self.assertNotIn("Acceptance", kraken.section_body(self.GOOD, "Goal"))
        self.assertNotIn("npm test", kraken.section_body(self.GOOD, "Goal"))

    def test_missing_heading_yields_empty(self):
        self.assertEqual(kraken.section_body("no headings here", "Goal"), "")
        self.assertTrue(kraken.is_empty_section(kraken.section_body("no headings", "Goal")))

    def test_no_response_placeholder_is_empty(self):
        self.assertTrue(kraken.is_empty_section(kraken.section_body(self.GOOD, "Notes")))

    def test_blank_only_section_is_empty(self):
        self.assertTrue(kraken.is_empty_section("   \n\n  \n"))

    def test_populated_section_is_not_empty(self):
        self.assertFalse(kraken.is_empty_section(kraken.section_body(self.GOOD, "Goal")))

    def test_crlf_headings_match(self):
        body = "### Goal\r\n\r\nShip it.\r\n\r\n### Acceptance\r\n\r\n_No response_"
        self.assertFalse(kraken.is_empty_section(kraken.section_body(body, "Goal")))
        self.assertTrue(kraken.is_empty_section(kraken.section_body(body, "Acceptance")))


class ValidationBodyTests(unittest.TestCase):
    """The validator's actionable comment and the debounce anchor."""

    def test_body_carries_marker_and_items(self):
        body = kraken.validation_body([kraken.VALIDATE_PROJECT_MISSING,
                                        kraken.VALIDATE_GOAL_MISSING])
        self.assertIn('<!-- kraken {"type":"validation"} -->', body)
        self.assertIn("project:<name>", body)
        self.assertIn("**Goal**", body)
        self.assertTrue(body.startswith("> 🐙 **Kraken task validator**"))

    def test_latest_validation_comment_picks_newest(self):
        recs = [
            {"body": kraken.validation_body([kraken.VALIDATE_PROJECT_MISSING])},
            {"body": "an operator note (no marker)"},
            {"body": kraken.validation_body([kraken.VALIDATE_ACCEPTANCE_MISSING])},
        ]
        latest = kraken.latest_validation_comment(recs)
        self.assertIn("**Acceptance**", latest)

    def test_latest_validation_comment_none_when_absent(self):
        recs = [{"body": "just chatter"}, {"body": "more chatter"}]
        self.assertIsNone(kraken.latest_validation_comment(recs))


class StaleClaimBodyTests(unittest.TestCase):
    """The reconciler's comment — the reclaim note, and since protocol/7 the only
    one it writes. Under protocol/5 a WORKER posts it (the reconcile rides the
    claim path), so unlike protocol/4's Actions-bot version it carries the §4
    attribution disclaimer."""

    def test_carries_reason_prose_and_marker(self):
        body = kraken.stale_claim_body("w1", "the lease expired 3 times")
        self.assertIn("Nobody is finishing this task (the lease expired 3 times)",
                      body)
        self.assertIn('<!-- kraken {"type":"stale-claim","reason":"the lease expired 3 times"} -->', body)

    def test_carries_the_worker_disclaimer(self):
        body = kraken.stale_claim_body("w1", "no worker heartbeat on record")
        self.assertTrue(body.startswith(kraken.disclaimer("w1")))


# --- cmd_reap: the stand-alone reconcile pass, transport mocked --------------

def _iso(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReapCommandTests(unittest.TestCase):
    """cmd_reap wires the queue read to the pure planner and the applier. The
    rules themselves are pinned in test_kraken.ReconcilerPlanTests; what is under
    test here is the wiring, the lease clock reaching it, and the failure staging
    of each read."""

    NOW = 1_800_000_000.0

    def setUp(self):
        self._orig = {
            "read_queue": kraken.read_queue,
            "resolve_commit_meta": kraken.resolve_commit_meta,
            "drop_generations": kraken.drop_generations,
            "swap_labels": kraken.swap_labels,
            "post_comment": kraken.post_comment,
            "time": kraken.time.time,
        }
        self.swaps = []
        self.posts = []
        self.deleted = []
        kraken.swap_labels = lambda repo, issue, remove=None, add=None: (
            self.swaps.append((issue, remove, add)) or True)
        kraken.post_comment = lambda repo, issue, body: (
            self.posts.append((issue, body)) or True)
        kraken.drop_generations = lambda repo, issue, gens: (
            self.deleted.append(issue) or True)
        kraken.time.time = lambda: self.NOW

    def tearDown(self):
        for k, v in self._orig.items():
            if k == "time":
                kraken.time.time = v
            else:
                setattr(kraken, k, v)

    def _meta(self, seconds_ago):
        return {"committedDate": _iso(self.NOW - seconds_ago),
                "message": kraken.make_marker({"type": "claim", "worker": "w"})}

    @staticmethod
    def _node(number, labels, comments=()):
        return {"number": number, "title": "t", "createdAt": "2026-01-01",
                "body": "", "labels": {"nodes": [{"name": l} for l in labels]},
                "comments": {"nodes": [{"body": b, "createdAt": "2026-01-01"}
                                       for b in comments]}}

    def _run(self, nodes, refs, commit_meta, ttl=None, worker="reconciler"):
        # Tests seed {issue: sha}; a lease is a generation ladder.
        ladders = {n: [(1, sha)] for n, sha in refs.items()}
        kraken.read_queue = lambda repo, now=None, ttl=None: (
            nodes, kraken.lease_state(ladders, commit_meta, self.NOW,
                                      kraken.lease_ttl_seconds(ttl))
        )
        kraken.resolve_commit_meta = lambda repo, shas: commit_meta
        args = SimpleNamespace(repo="OWNER/tasks", worker=worker, ttl=ttl)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.cmd_reap(args)
        return rc, buf.getvalue()

    @staticmethod
    def _expiries(n):
        return [kraken.make_marker({"type": "lease-expired", "worker": "w%d" % i})
                for i in range(n)]

    def test_a_repeatedly_expired_task_is_reclaimed(self):
        # The only escalation left: the task has already been stolen
        # LEASE_EXPIRY_ESCALATE times and still nobody finished it.
        node = self._node(1, ["kraken-task", "in-progress"],
                          comments=self._expiries(kraken.LEASE_EXPIRY_ESCALATE))
        rc, out = self._run([node], {1: "s1"},
                            {"s1": self._meta(kraken.LEASE_DEFAULT_TTL_SECONDS + 60)})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertIn((1, "in-progress", "needs-decision"), self.swaps)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("stale-claim", self.posts[0][1])
        self.assertIn(1, self.deleted)
        self.assertIn("reap: done leases=1 reclaimed=1", out)

    def test_an_expired_lease_is_left_for_the_thief(self):
        # protocol/6: reap does NOT free an expired lease. The reader already
        # treats it as unheld, so a pass that "repaired" it would only escalate a
        # task the next drain would have picked up by itself.
        rc, out = self._run([self._node(2, ["kraken-task", "in-progress"])],
                            {2: "s2"},
                            {"s2": self._meta(kraken.LEASE_DEFAULT_TTL_SECONDS + 60)})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.deleted, [])
        self.assertIn("reclaimed=0 orphan_locks=0", out)

    def test_live_worker_left_alone(self):
        rc, out = self._run([self._node(2, ["kraken-task", "in-progress"])],
                            {2: "s2"}, {"s2": self._meta(0)})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.deleted, [])
        self.assertIn("reclaimed=0 orphan_locks=0", out)

    def test_reclaim_is_attributed_to_the_named_worker(self):
        node = self._node(1, ["kraken-task", "in-progress"],
                          comments=self._expiries(kraken.LEASE_EXPIRY_ESCALATE))
        self._run([node], {1: "s1"},
                  {"s1": self._meta(kraken.LEASE_DEFAULT_TTL_SECONDS + 60)},
                  worker="drain-1")
        self.assertTrue(self.posts[0][1].startswith(kraken.disclaimer("drain-1")))

    def test_the_ttl_flag_reaches_the_read(self):
        # --ttl 60 makes a 5-minute-old lease expired, so the repeat guard fires
        # on a task a default TTL would still consider held.
        node = self._node(7, ["kraken-task", "in-progress"],
                          comments=self._expiries(kraken.LEASE_EXPIRY_ESCALATE))
        rc, _ = self._run([node], {7: "s7"}, {"s7": self._meta(300)}, ttl=60)
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertIn((7, "in-progress", "needs-decision"), self.swaps)

    def test_transport_failure_on_the_queue_read_is_twenty(self):
        kraken.read_queue = lambda repo, now=None, ttl=None: None
        args = SimpleNamespace(repo="OWNER/tasks", worker="w", ttl=None)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            rc = kraken.cmd_reap(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)


# --- the requeue derivation: held-state rules, no transport at all -----------

def _cmt(body):
    return {"body": body, "createdAt": "2026-07-01T00:00:00Z"}


def _worker_cmt(worker="w1", mtype="needs-decision"):
    return _cmt(kraken.compose_comment(worker, "a question",
                                       {"type": mtype, "worker": worker}))


class RequeueVerdictTests(unittest.TestCase):
    """protocol/5's read filter (PROTOCOL.md §6). Through protocol/4 an operator
    reply MUTATED the label via a workflow; now the same fact — an operator
    comment newer than the last worker comment — is DERIVED on the read. No
    network is involved, which is the point: the verdict is a property of the
    thread, identical for every reader."""

    def test_bare_reply_requeues_needs_decision(self):
        comments = [_worker_cmt(), _cmt("go with option B")]
        self.assertTrue(kraken.requeue_verdict(("needs-decision",), comments))

    def test_no_reply_stays_held(self):
        self.assertFalse(kraken.requeue_verdict(("needs-decision",),
                                                [_worker_cmt()]))

    def test_empty_thread_stays_held(self):
        self.assertFalse(kraken.requeue_verdict(("needs-decision",), []))

    def test_a_worker_comment_after_the_reply_re_holds(self):
        # The operator answered, a worker picked it up and escalated AGAIN: the
        # newest worker comment outranks the older reply, so it is held again.
        comments = [_worker_cmt(), _cmt("go with option B"), _worker_cmt()]
        self.assertFalse(kraken.requeue_verdict(("needs-decision",), comments))

    def test_worker_comments_never_requeue(self):
        # Every kraken-authored comment carries a marker — including the
        # reconciler's stale-claim note and the validator's — so none of them can
        # requeue the escalation they just posted. This is what retires the
        # protocol/4 `user.type == Bot` gate.
        for body in (kraken.stale_claim_body("w1", "silent"),
                     kraken.validation_body([kraken.VALIDATE_GOAL_MISSING]),
                     kraken.compose_note("w1", "still working")):
            comments = [_worker_cmt(), _cmt(body)]
            self.assertFalse(
                kraken.requeue_verdict(("needs-decision",), comments),
                "a kraken-authored comment requeued the task: %s" % body[:60])

    def test_awaiting_merge_needs_an_explicit_directive(self):
        delivered = [_worker_cmt(mtype="delivered")]
        self.assertFalse(kraken.requeue_verdict(
            ("awaiting-merge",), delivered + [_cmt("nice, thanks")]))
        self.assertTrue(kraken.requeue_verdict(
            ("awaiting-merge",), delivered + [_cmt("please fix the naming\nrequeue")]))

    def test_a_prose_requeue_does_not_bounce_a_ready_branch(self):
        delivered = [_worker_cmt(mtype="delivered")]
        self.assertFalse(kraken.requeue_verdict(
            ("awaiting-merge",),
            delivered + [_cmt("requeue: only if CI is red, otherwise merge it")]))

    def test_the_directive_may_arrive_in_a_later_reply(self):
        delivered = [_worker_cmt(mtype="delivered")]
        self.assertTrue(kraken.requeue_verdict(
            ("awaiting-merge",),
            delivered + [_cmt("hmm"), _cmt("requeue")]))

    def test_a_truncated_window_cannot_mislead(self):
        # A window holding NO worker comment means the last one is older than the
        # window — so every operator comment in it is newer, which is the correct
        # verdict. This is why a fixed comment window is safe.
        self.assertTrue(kraken.requeue_verdict(
            ("needs-decision",), [_cmt("reply %d" % i) for i in range(25)]))


class RequeuedLabelsTests(unittest.TestCase):
    """requeued_labels: the derivation as the classifier and the claim both see
    it — which held labels an operator reply has already lifted."""

    @staticmethod
    def _node(number, labels, comments=()):
        return {"number": number, "title": "t", "createdAt": "2026-01-01",
                "body": "", "labels": {"nodes": [{"name": l} for l in labels]},
                "comments": {"nodes": list(comments)},
                "blockedBy": {"nodes": []}}

    def test_answered_needs_decision_is_lifted(self):
        node = self._node(1, ["kraken-task", "needs-decision"],
                          [_worker_cmt(), _cmt("do option B")])
        self.assertEqual(kraken.requeued_labels(node, {}), ("needs-decision",))

    def test_unanswered_stays_held(self):
        node = self._node(1, ["kraken-task", "needs-decision"], [_worker_cmt()])
        self.assertEqual(kraken.requeued_labels(node, {}), ())

    def test_a_live_claim_ref_outranks_the_thread(self):
        # A comment on a CLAIMED task is context for its worker, never a requeue:
        # the lock is the truth (§5), so the timeline is not consulted at all.
        node = self._node(1, ["kraken-task", "needs-decision"],
                          [_worker_cmt(), _cmt("do option B")])
        self.assertEqual(kraken.requeued_labels(node, {1: "sha"}), ())

    def test_in_progress_needs_no_lifting(self):
        # Nothing to lift: the badge is write-only (§3), so the task is already
        # queued and the derivation has no held label to report.
        node = self._node(1, ["kraken-task", "in-progress"],
                          [_worker_cmt(), _cmt("any news?")])
        self.assertEqual(kraken.requeued_labels(node, {}), ())

    def test_a_stale_badge_cannot_suppress_a_requeue(self):
        # Both labels at once — a crashed escalate that left the badge on. Under
        # protocol/6 the in-progress label short-circuited the whole derivation,
        # so the operator's answer was silently ignored. It holds nothing now.
        node = self._node(1, ["kraken-task", "in-progress", "needs-decision"],
                          [_worker_cmt(), _cmt("do option B")])
        self.assertEqual(kraken.requeued_labels(node, {}), ("needs-decision",))

    def test_an_unheld_task_lifts_nothing(self):
        node = self._node(1, ["kraken-task"], [_cmt("just chatter")])
        self.assertEqual(kraken.requeued_labels(node, {}), ())


# --- cmd_validate: gate + debounce, transport mocked ------------------------

class ValidateCommandTests(unittest.TestCase):

    GOOD = ("### Goal\n\nShip it.\n\n### Acceptance\n\n`npm test` passes.\n\n"
            "### Notes\n\n_No response_")

    def setUp(self):
        self._orig = {
            "issue_label_names": kraken.issue_label_names,
            "issue_body": kraken.issue_body,
            "comment_records": kraken.comment_records,
            "post_comment": kraken.post_comment,
        }
        self.posts = []
        kraken.post_comment = lambda repo, issue, body: (
            self.posts.append((issue, body)) or True)
        kraken.comment_records = lambda repo, issue: []

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(kraken, k, v)

    def _run(self, issue, labels, body, prior_records=None):
        kraken.issue_label_names = lambda repo, i: labels
        kraken.issue_body = lambda repo, i: body
        if prior_records is not None:
            kraken.comment_records = lambda repo, i: prior_records
        args = SimpleNamespace(repo="OWNER/tasks", issue=str(issue))
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_validate(args)
        return rc

    def test_non_kraken_task_is_a_noop(self):
        rc = self._run(1, ["project:app"], self.GOOD)
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.posts, [])

    def test_missing_project_label_flags(self):
        rc = self._run(1, ["kraken-task"], self.GOOD)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("project:<name>", self.posts[0][1])

    def test_missing_acceptance_flags(self):
        body = "### Goal\n\nShip it.\n\n### Acceptance\n\n_No response_"
        rc = self._run(1, ["kraken-task", "project:app"], body)
        self.assertIn("**Acceptance**", self.posts[0][1])
        self.assertNotIn("project:<name>", self.posts[0][1])

    def test_compliant_task_gets_no_comment(self):
        rc = self._run(1, ["kraken-task", "project:app"], self.GOOD)
        self.assertEqual(self.posts, [])

    def test_debounce_skips_identical_prior(self):
        prior = kraken.validation_body([kraken.VALIDATE_PROJECT_MISSING])
        # A prior comment byte-identical to what we'd post (with a transport
        # trailing newline) must debounce.
        rc = self._run(1, ["kraken-task"], self.GOOD,
                       prior_records=[{"body": prior + "\n"}])
        self.assertEqual(self.posts, [])

    def test_changed_missing_set_posts_again(self):
        # A prior validation comment about a DIFFERENT missing set is not the
        # same body, so a new flag posts.
        prior = kraken.validation_body([kraken.VALIDATE_ACCEPTANCE_MISSING])
        rc = self._run(1, ["kraken-task"], self.GOOD,
                       prior_records=[{"body": prior}])
        self.assertEqual(len(self.posts), 1)

    def test_transport_failure_on_labels_is_twenty(self):
        kraken.issue_label_names = lambda repo, i: None
        args = SimpleNamespace(repo="OWNER/tasks", issue="1")
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_validate(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)


# --- cleanup-closed: the identity-label rule --------------------------------

class IdentityLabelTests(unittest.TestCase):
    """cleanup-closed's keep/strip rule (PROTOCOL.md §10): the only labels a
    closed task keeps are kraken-task and its project:<name> routing label;
    every state-machine or unrelated label is stripped."""

    def test_kraken_task_is_kept(self):
        self.assertTrue(kraken.is_identity_label("kraken-task"))

    def test_project_label_is_kept(self):
        self.assertTrue(kraken.is_identity_label("project:app"))
        self.assertTrue(kraken.is_identity_label("project:some-other"))

    def test_state_labels_are_stripped(self):
        for lbl in ("in-progress", "needs-decision", "awaiting-merge"):
            self.assertFalse(kraken.is_identity_label(lbl))

    def test_unrelated_labels_are_stripped(self):
        self.assertFalse(kraken.is_identity_label("priority:high"))
        self.assertFalse(kraken.is_identity_label("bug"))


class CleanupCommandTests(unittest.TestCase):
    """cmd_cleanup with its gh transport mocked: on a closed kraken-task issue it
    removes every non-identity label (one --remove-label at a time), keeps
    kraken-task and project:<name>, no-ops on a non-task issue, and maps a
    transport failure to exit 20."""

    def setUp(self):
        self._orig = {
            "issue_label_names": kraken.issue_label_names,
            "swap_labels": kraken.swap_labels,
            "claim_refs_of": kraken.claim_refs_of,
            "drop_generations": kraken.drop_generations,
        }
        self.removed = []
        self.ref_deletes = []
        kraken.swap_labels = lambda repo, issue, remove=None, add=None: (
            self.removed.append((issue, remove, add)) or True)
        # cleanup also drops a leftover lease — every generation of it. Mock the
        # read and the delete so the unit tests never reach the network.
        kraken.claim_refs_of = lambda repo, issue: (True, [(1, "sha")])
        kraken.drop_generations = lambda repo, issue, gens: (
            self.ref_deletes.append((issue, list(gens))) or True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(kraken, k, v)

    def _run(self, issue, labels):
        kraken.issue_label_names = lambda repo, i: labels
        args = SimpleNamespace(repo="OWNER/tasks", issue=str(issue))
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_cleanup(args)
        return rc

    def test_strips_state_label_keeps_identity(self):
        rc = self._run(1, ["kraken-task", "project:app", "in-progress"])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.removed, [("1", "in-progress", None)])

    def test_strips_all_non_identity_labels(self):
        rc = self._run(2, ["kraken-task", "project:web", "awaiting-merge",
                           "needs-decision", "priority:high"])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(
            self.removed,
            [("2", "awaiting-merge", None),
             ("2", "needs-decision", None),
             ("2", "priority:high", None)],
        )

    def test_already_clean_is_a_noop(self):
        rc = self._run(3, ["kraken-task", "project:app"])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.removed, [])

    def test_deletes_every_leftover_generation(self):
        # Even a label-clean closed task must not leave its lock behind: cleanup
        # drops the whole ladder, including a generation a steal failed to
        # collect (idempotent — a missing ref is fine).
        kraken.claim_refs_of = lambda repo, issue: (True, [(1, "a"), (2, "b")])
        rc = self._run(3, ["kraken-task", "project:app"])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.ref_deletes, [("3", [1, 2])])

    def test_an_unreadable_ref_read_is_twenty(self):
        kraken.claim_refs_of = lambda repo, issue: (False, [])
        rc = self._run(3, ["kraken-task", "project:app"])
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    def test_non_kraken_task_is_a_noop(self):
        # The workflow's if: gate is re-checked here: a non-task issue strips
        # nothing even when it carries a state label.
        rc = self._run(4, ["needs-decision", "priority:high"])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.removed, [])

    def test_transport_failure_on_labels_is_twenty(self):
        kraken.issue_label_names = lambda repo, i: None
        args = SimpleNamespace(repo="OWNER/tasks", issue="1")
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_cleanup(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    def test_transport_failure_on_remove_is_twenty(self):
        kraken.swap_labels = lambda repo, issue, remove=None, add=None: False
        args = SimpleNamespace(repo="OWNER/tasks", issue="1")
        kraken.issue_label_names = lambda repo, i: ["kraken-task", "in-progress"]
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_cleanup(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
