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
import json
import time
import datetime
import unittest
from types import SimpleNamespace
from io import StringIO
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.join(HERE, "..", "..", "skills", "unleash")
sys.path.insert(0, os.path.abspath(SKILL_DIR))

import kraken  # noqa: E402
from fakes import FakeApi, FakeQueue  # noqa: E402


def disclaimer_body(worker, *rest):
    """A real worker comment: the attribution disclaimer heading followed by
    optional extra lines (composed the way a worker's comment actually is)."""
    parts = [kraken.disclaimer(worker)]
    parts.extend(rest)
    return "\n\n".join(parts)


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
        self._orig_time = time.time
        self.swaps = []
        self.posts = []
        self.deleted = []
        self.records = []
        time.time = lambda: self.NOW

    def tearDown(self):
        time.time = self._orig_time

    def _api(self):
        def request(method, path, body=None):
            # Beyond labels and comments the applier reaches for two kinds of ref
            # write: the claim-ref delete, and the state record a reclaim or a
            # migrate writes (§3.1). Record each as what it is.
            if path.endswith("/git/commits"):
                return (201, json.dumps({"sha": "state-sha"}))
            if path.endswith("/git/refs"):
                self.records.append((body or {}).get("ref", ""))
                return (201, "{}")
            parsed = kraken.parse_claim_ref(path.split("/git/", 1)[1])
            if parsed is not None:
                self.deleted.append(parsed[0])
            return (204, "")

        return FakeApi(
            "acme/tasks",
            request=request,
            # The record's anchor: read back off the issue after the pass posts
            # its comment (§3.1). The applier's wiring is what is under test, not
            # the count, so it answers a constant.
            comment_count=lambda issue: 0,
            swap_labels=lambda issue, remove=None, add=None: (
                self.swaps.append((issue, remove, add)) or True),
            post_comment=lambda issue, body: (
                self.posts.append((issue, body)) or True))

    def _meta(self, seconds_ago):
        return {"committedDate": _iso(self.NOW - seconds_ago),
                "message": kraken.make_marker({"type": "claim", "worker": "w"})}

    @staticmethod
    def _node(number, labels, total=0):
        return kraken.Task(
            {"number": number, "title": "t", "createdAt": "2026-01-01",
             "body": "", "labels": {"nodes": [{"name": l} for l in labels]},
             "comments": {"totalCount": total}})

    def _run(self, nodes, refs, commit_meta, ttl=None, worker="reconciler",
             states=None):
        # Tests seed {issue: sha}; a lease is a generation ladder. The queue read
        # is the injected collaborator — it has its own tests; what is under test
        # here is the wiring from that read to the plan and the applier.
        ladders = {n: [(1, sha)] for n, sha in refs.items()}

        def read(now=None, ttl=None):
            return kraken.QueueRead(
                nodes,
                kraken.lease_state(ladders, commit_meta, self.NOW,
                                   kraken.lease_ttl_seconds(ttl)),
                commit_meta,
                dict(states or {}))

        buf = StringIO()
        with redirect_stdout(buf):
            rc, counts = kraken.reconcile_pass(
                self._api(), worker, ttl, queue=FakeQueue(read=read))
        return rc, counts, buf.getvalue()

    @staticmethod
    def _expired(n, expiries):
        """The state record of a task whose lease has been stolen `expiries`
        times — the count §6 rule 2 reads (§3.1). Through protocol/8 it was
        counted from `lease-expired` markers in a comment window."""
        return {n: kraken.TaskState(state=kraken.QUEUED, worker="w",
                                    expiries=expiries, recorded=True)}

    def test_a_repeatedly_expired_task_is_reclaimed(self):
        # The only escalation left: the task has already been stolen
        # LEASE_EXPIRY_ESCALATE times and still nobody finished it.
        node = self._node(1, ["kraken-task", "in-progress"])
        rc, counts, _ = self._run(
            [node], {1: "s1"},
            {"s1": self._meta(kraken.LEASE_DEFAULT_TTL_SECONDS + 60)},
            states=self._expired(1, kraken.LEASE_EXPIRY_ESCALATE))
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertIn((1, "in-progress", "needs-decision"), self.swaps)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("stale-claim", self.posts[0][1])
        self.assertIn(1, self.deleted)
        self.assertEqual((counts["leases"], counts["reclaim"]), (1, 1))

    def test_an_expired_lease_is_left_for_the_thief(self):
        # protocol/6: reap does NOT free an expired lease. The reader already
        # treats it as unheld, so a pass that "repaired" it would only escalate a
        # task the next drain would have picked up by itself.
        rc, counts, _ = self._run(
            [self._node(2, ["kraken-task", "in-progress"])],
            {2: "s2"},
            {"s2": self._meta(kraken.LEASE_DEFAULT_TTL_SECONDS + 60)})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.deleted, [])
        self.assertEqual((counts["reclaim"], counts["orphan-lock"]), (0, 0))

    def test_live_worker_left_alone(self):
        rc, counts, _ = self._run(
            [self._node(2, ["kraken-task", "in-progress"])],
            {2: "s2"}, {"s2": self._meta(0)})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.deleted, [])
        self.assertEqual((counts["reclaim"], counts["orphan-lock"]), (0, 0))

    def test_reclaim_is_attributed_to_the_named_worker(self):
        node = self._node(1, ["kraken-task", "in-progress"])
        self._run([node], {1: "s1"},
                  {"s1": self._meta(kraken.LEASE_DEFAULT_TTL_SECONDS + 60)},
                  worker="drain-1",
                  states=self._expired(1, kraken.LEASE_EXPIRY_ESCALATE))
        self.assertTrue(self.posts[0][1].startswith(kraken.disclaimer("drain-1")))

    def test_the_ttl_flag_reaches_the_read(self):
        # --ttl 60 makes a 5-minute-old lease expired, so the repeat guard fires
        # on a task a default TTL would still consider held.
        node = self._node(7, ["kraken-task", "in-progress"])
        rc, _, _ = self._run([node], {7: "s7"}, {"s7": self._meta(300)}, ttl=60,
                             states=self._expired(7, kraken.LEASE_EXPIRY_ESCALATE))
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertIn((7, "in-progress", "needs-decision"), self.swaps)

    def test_transport_failure_on_the_queue_read_is_twenty(self):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            rc, counts = kraken.reconcile_pass(
                self._api(), "w", None,
                queue=FakeQueue(read=lambda now=None, ttl=None: None))
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)
        self.assertIsNone(counts)

    def test_cmd_reap_renders_the_summary(self):
        # The one test that drives the argv-shaped entry point, so the summary
        # line stays pinned. An empty queue needs no injection: the FakeApi
        # answers the real walk with no issues and no claim refs.
        empty_walk = {"data": {"repository": {"issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": ""}, "nodes": []}}}}
        api = FakeApi("acme/tasks", graphql=lambda q: empty_walk,
                      paginated=lambda path: [])
        args = SimpleNamespace(repo="acme/tasks", worker="w", ttl=None, api=api)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.cmd_reap(args)
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertIn("reap: done leases=0 reclaimed=0 orphan_locks=0 "
                      "orphan_states=0 migrated=0 re_anchored=0", buf.getvalue())


# --- the requeue derivation: one integer against another, no transport -------

def _record(state, comments, **kw):
    """A state record as a transition would have written it (PROTOCOL.md §3.1)."""
    return kraken.TaskState(state=state, worker="w1", comments=comments,
                            recorded=True, **kw)


class RequeueByCountTests(unittest.TestCase):
    """protocol/9's requeue derivation (§3.1, §6): the thread's live comment
    total against the count the record froze when the transition put the task
    down. Anything added since is news the task has not been read against.

    No transport, and no comment: through protocol/8 this classified a window of
    comment BODIES into worker-authored and operator-authored and compared their
    positions. The classifier, the window and the discriminator are all gone —
    what is left is `total > record.comments`."""

    def test_a_new_comment_requeues(self):
        rec = _record("needs-decision", 3)
        self.assertTrue(rec.requeued(4))
        self.assertFalse(rec.holds(4))

    def test_nothing_new_stays_held(self):
        rec = _record("needs-decision", 3)
        self.assertFalse(rec.requeued(3))
        self.assertTrue(rec.holds(3))

    def test_the_same_rule_holds_for_delivered_work(self):
        # protocol/8 made the rule symmetric; the counter makes it symmetric for
        # free, because neither held state has anything left to read.
        rec = _record("awaiting-merge", 7)
        self.assertTrue(rec.requeued(8))
        self.assertFalse(rec.holds(8))

    def test_a_queued_record_has_nothing_to_lift(self):
        # `comments` is inert on a queued record — the derivation ranges over
        # held states only — so a busy thread does not "requeue" a task that is
        # already in the queue.
        rec = _record(kraken.QUEUED, 0)
        self.assertFalse(rec.requeued(5))
        self.assertFalse(rec.holds(5))

    def test_a_transition_records_its_own_comment(self):
        # The reason no comment needs classifying: an escalation posts its
        # question and then records the count INCLUDING it, so its own words
        # never requeue the task it just held. The operator's answer is the
        # first thing that moves the counter past it.
        rec = _record("needs-decision", 4)   # question posted, thread now at 4
        self.assertTrue(rec.holds(4))
        self.assertFalse(rec.holds(5))       # the operator answered

    def test_a_congratulatory_comment_requeues_too(self):
        # The cost side of the trade, pinned so it stays a decision and not a
        # surprise: any comment counts, and the worker that picks the task up
        # escalates rather than inventing rework (§6, skills/unleash/SKILL.md).
        self.assertTrue(_record("awaiting-merge", 2).requeued(3))

    def test_a_deleted_comment_does_not_requeue(self):
        # A total BELOW the anchor means comments were removed, not added.
        # Nothing was said, so nothing is lifted.
        self.assertTrue(_record("awaiting-merge", 9).holds(8))

    def test_a_missing_count_reads_as_zero_and_requeues(self):
        # Fail-open, deliberately (`_as_int`): a record whose counter did not
        # decode offers the task, and the worker that finds nothing actionable
        # escalates. The opposite default would bury a delivery.
        rec = kraken.parse_state_commit(kraken.make_marker(
            {"type": "state", "state": "awaiting-merge", "worker": "w1"}))
        self.assertEqual(rec.comments, 0)
        self.assertTrue(rec.requeued(1))


class HoldingStateTests(unittest.TestCase):
    """`Task.holding` / `holding_state`: the record decides, and the held LABEL
    is consulted in exactly one case — a task that has no record (§3.1)."""

    @staticmethod
    def _task(number, labels, total=0):
        return kraken.Task(
            {"number": number, "title": "t", "createdAt": "2026-01-01",
             "body": "", "labels": {"nodes": [{"name": l} for l in labels]},
             "comments": {"totalCount": total},
             "blockedBy": {"nodes": []}})

    def test_the_record_holds_even_with_no_badge(self):
        # A delivery whose label swap never landed is still delivered: the
        # record is the state, and the badge is projection that nothing repairs.
        task = self._task(1, ["kraken-task"], total=4)
        self.assertEqual(task.holding({1: _record("awaiting-merge", 4)}),
                         "awaiting-merge")

    def test_a_stale_held_badge_cannot_hold_a_recorded_queued_task(self):
        # The inverse, and the reason the label is not consulted where a record
        # exists: a release left `awaiting-merge` behind, the record says queued.
        task = self._task(1, ["kraken-task", "awaiting-merge"], total=9)
        self.assertIsNone(task.holding({1: _record(kraken.QUEUED, 9)}))

    def test_a_new_comment_lifts_the_record(self):
        task = self._task(1, ["kraken-task", "awaiting-merge"], total=8)
        self.assertIsNone(task.holding({1: _record("awaiting-merge", 7)}))

    def test_no_record_falls_back_to_the_label(self):
        task = self._task(1, ["kraken-task", "needs-decision"], total=12)
        self.assertEqual(task.holding({}), "needs-decision")

    def test_no_record_derives_no_requeue_however_long_the_thread(self):
        # The migration's safety property (§3.1, §6 rule 3): a queue written by
        # an older revision reads as held until the reconcile writes a record.
        # Deriving a requeue here instead would hand every pre-upgrade delivery
        # back to a worker with nothing to do.
        task = self._task(1, ["kraken-task", "awaiting-merge"], total=999)
        self.assertEqual(task.holding({}), "awaiting-merge")

    def test_in_progress_holds_nothing(self):
        task = self._task(1, ["kraken-task", "in-progress"], total=3)
        self.assertIsNone(task.holding({}))

    def test_an_unheld_task_holds_nothing(self):
        self.assertIsNone(self._task(1, ["kraken-task"], total=5).holding({}))


class ClaimIsMootTests(unittest.TestCase):
    """`claim_is_moot`: whether a claim ref of ours locks a task whose turn is
    already over (§3.1). The same `holding_state` rule `Task.holding` and §6's
    rule 1 apply, which is the point — this used to read the held LABELS
    outright and so returned a different verdict than the reconciler for the
    same task."""

    def test_a_recorded_held_state_makes_the_ref_moot(self):
        # The ordinary case: we delivered, the record landed, the ref delete
        # did not. The ref locks nothing and must not brick this worker.
        self.assertTrue(
            kraken.claim_is_moot(_record("awaiting-merge", 4), 4,
                                 ["kraken-task", "awaiting-merge"]))

    def test_a_requeued_delivery_is_not_moot(self):
        # The divergence this change exists to close. The operator replied to a
        # delivered task, so §6 requeued it — but the badge still says
        # `awaiting-merge` until somebody claims it. Reading the badge called
        # this moot while `reconcile_plan` called it live.
        self.assertFalse(
            kraken.claim_is_moot(_record("awaiting-merge", 4), 5,
                                 ["kraken-task", "awaiting-merge"]))

    def test_a_stale_badge_the_record_contradicts_is_not_moot(self):
        # A release left the badge behind; the record says queued. The label
        # would have called our live claim moot and let this worker take a
        # second task, which §5 forbids.
        self.assertFalse(
            kraken.claim_is_moot(_record(kraken.QUEUED, 9), 9,
                                 ["kraken-task", "awaiting-merge"]))

    def test_no_record_still_falls_back_to_the_badge(self):
        # §3.1's one sanctioned label read, and what keeps a pre-protocol/9
        # queue behaving exactly as it did before this change.
        self.assertTrue(
            kraken.claim_is_moot(kraken.NO_RECORD, 12,
                                 ["kraken-task", "needs-decision"]))

    def test_an_unreadable_comment_count_errs_toward_live(self):
        # `comment_total_of` answers None for an issue object whose field is
        # missing. Erring toward "not moot" refuses a second claim and resumes
        # a task; erring the other way would bury a delivery the operator
        # reopened — the same direction `_comment_total` floors a lost field to.
        self.assertFalse(
            kraken.claim_is_moot(_record("awaiting-merge", 4), None,
                                 ["kraken-task", "awaiting-merge"]))

    def test_in_progress_never_makes_a_ref_moot(self):
        self.assertFalse(
            kraken.claim_is_moot(kraken.NO_RECORD, 3,
                                 ["kraken-task", "in-progress"]))


# --- cmd_validate: gate + debounce, transport mocked ------------------------

class ValidateCommandTests(unittest.TestCase):

    GOOD = ("### Goal\n\nShip it.\n\n### Acceptance\n\n`npm test` passes.\n\n"
            "### Notes\n\n_No response_")

    def setUp(self):
        self.posts = []

    def _api(self, **methods):
        defaults = {
            "post_comment": lambda issue, body: (
                self.posts.append((issue, body)) or True),
            "comment_records": lambda issue: [],
            # No state ref: the default queue entry has never been put down, so
            # the §3.1 anchor refresh finds no record and writes nothing.
            "paginated": lambda path, **kw: [],
        }
        defaults.update(methods)
        return FakeApi("acme/tasks", **defaults)

    def _run(self, issue, labels, body, prior_records=None):
        methods = {"issue_label_names": lambda i: labels,
                   "issue_body": lambda i: body}
        if prior_records is not None:
            methods["comment_records"] = lambda i: prior_records
        args = SimpleNamespace(repo="acme/tasks", issue=str(issue),
                               api=self._api(**methods))
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
        args = SimpleNamespace(
            repo="acme/tasks", issue="1",
            api=self._api(issue_label_names=lambda i: None))
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_validate(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    # --- the §3.1 anchor refresh this pass's own comment owes ----------------

    def _flagged(self, record=None, total=9, **methods):
        """Run a validate pass over a task that WILL be flagged (no project
        label), against a state ref that resolves to `record` through the REAL
        `States.of` path — one matching-refs read, one batched commit read.
        `record=None` is a task that has none. Ref writes land in `self.writes`;
        returns the exit code."""
        self.writes = []

        def request(verb, path, body=None):
            self.writes.append((verb, path, body))
            # The commit create is the one write whose RESPONSE is read back
            # (Refs.commit wants the sha it then points the ref at).
            return (201, json.dumps({"sha": "new-sha"}))

        def paginated(path, **kw):
            if "matching-refs" in path and record is not None:
                return [{"ref": "refs/kraken/state/1", "object": {"sha": "s1"}}]
            return []

        defaults = {
            "issue_label_names": lambda i: ["kraken-task"],
            "issue_body": lambda i: self.GOOD,
            "paginated": paginated,
            "aliased": lambda fields: {
                "c0": {"committedDate": "2026-07-01T09:00:00Z",
                       "message": kraken.make_marker(record.payload())}}
            if record is not None else (lambda fields: {}),
            "comment_count": lambda issue: total,
            "request": request,
        }
        defaults.update(methods)
        args = SimpleNamespace(repo="acme/tasks", issue="1",
                               api=self._api(**defaults))
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return kraken.cmd_validate(args)

    def _written_record(self):
        """The state marker this run committed, decoded — or None if it wrote
        no commit at all."""
        for _verb, path, body in self.writes:
            if path.endswith("/git/commits"):
                return kraken.parse_state_commit((body or {}).get("message", ""))
        return None

    def test_its_own_comment_does_not_read_as_an_operator_reply(self):
        """THE point of the refresh (PROTOCOL.md §3.1). §6 derives a requeue
        from `total > record.comments` and counts EVERY comment, so a validation
        comment left un-anchored makes a held task read as bounced: the next
        worker claims rework nobody asked for, finds an automated nag, and
        escalates it back as a question the operator then has to answer."""
        held = kraken.TaskState(state="awaiting-merge", worker="w0",
                                comments=4, recorded=True,
                                pr="https://github.com/o/r/pull/3")
        rc = self._flagged(held, total=5)
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(len(self.posts), 1, "the flag itself still posts")
        written = self._written_record()
        self.assertIsNotNone(written, "the anchor was never refreshed")
        self.assertEqual(written.comments, 5,
                         "the anchor must clear this pass's own comment")
        self.assertFalse(written.requeued(5),
                         "after the refresh the task must NOT read as bounced")

    def test_the_refresh_moves_the_anchor_and_nothing_else(self):
        """`re_anchored`, not `moved_to`: validate informs, so the hold, the
        worker, the expiry count and the delivery URL all stand. A refresh that
        lifted the hold would hand an answered task back to the queue."""
        held = kraken.TaskState(state="needs-decision", worker="w0", comments=4,
                                expiries=2, recorded=True,
                                pr="https://github.com/o/r/pull/3")
        self._flagged(held, total=5)
        written = self._written_record()
        self.assertEqual(written.state, "needs-decision", "the hold was lifted")
        self.assertEqual(written.worker, "w0")
        self.assertEqual(written.expiries, 2, "the expiry count is cumulative")
        self.assertEqual(written.pr, "https://github.com/o/r/pull/3")

    def test_a_task_with_no_record_is_not_given_one(self):
        """The common case: a new queue entry has never been put down, so there
        is no anchor to move and no record this pass may invent."""
        rc = self._flagged(None)
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(len(self.posts), 1, "the flag itself still posts")
        self.assertIsNone(self._written_record(),
                          "a task with no record must not be given one")

    def test_a_compliant_task_pays_for_no_refresh(self):
        """No comment, no anchor to clear. The refresh is owed by the POST, so
        the happy path must not read the record at all."""
        reads = []
        rc = self._flagged(
            None,
            issue_label_names=lambda i: ["kraken-task", "project:app"],
            paginated=lambda path, **kw: reads.append(path) or [])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.posts, [], "a compliant task is not flagged")
        self.assertEqual(
            [p for p in reads if "matching-refs" in p], [],
            "a compliant task must not pay for a state-ref read")

    def test_an_unreadable_record_is_transport_not_a_silent_stale_anchor(self):
        held = kraken.TaskState(state="awaiting-merge", worker="w0",
                                comments=4, recorded=True)
        rc = self._flagged(held, paginated=lambda path, **kw: None)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    def test_an_unreadable_count_is_transport(self):
        held = kraken.TaskState(state="awaiting-merge", worker="w0",
                                comments=4, recorded=True)
        rc = self._flagged(held, comment_count=lambda issue: None)
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
        self.removed = []
        self.ref_deletes = []

    def _api(self, labels=None, swap=None, gens=(1,), refs_readable=True):
        """cleanup also drops a leftover lease — every generation of it. The ref
        ladder and its deletes are scripted at the transport, so the real
        `claim_refs_of` prefix filter and the real delete path stay under test."""
        issue_holder = {}

        def request(method, path, body=None):
            if "/matching-refs/" in path:
                if not refs_readable:
                    return (500, "")
                issue_holder["n"] = path.rsplit("/", 1)[1].split("?")[0]
                return (200, json.dumps([
                    {"ref": f"refs/kraken/claims/{issue_holder['n']}/{g}",
                     "object": {"sha": f"sha{g}"}} for g in gens]))
            issue, gen = kraken.parse_claim_ref(path.split("/git/", 1)[1])
            self.ref_deletes.append((issue, gen))
            return (204, "")

        return FakeApi(
            "acme/tasks",
            request=request,
            issue_label_names=lambda i: labels,
            swap_labels=swap or (lambda issue, remove=None, add=None: (
                self.removed.append((issue, remove, add)) or True)),
        )

    def _run(self, issue, labels, **api_kwargs):
        args = SimpleNamespace(repo="acme/tasks", issue=str(issue),
                               api=self._api(labels, **api_kwargs))
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
        rc = self._run(3, ["kraken-task", "project:app"], gens=(1, 2))
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.ref_deletes, [(3, 1), (3, 2)])

    def test_an_unreadable_ref_read_is_twenty(self):
        rc = self._run(3, ["kraken-task", "project:app"], refs_readable=False)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    def test_non_kraken_task_is_a_noop(self):
        # The workflow's if: gate is re-checked here: a non-task issue strips
        # nothing even when it carries a state label.
        rc = self._run(4, ["needs-decision", "priority:high"])
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.removed, [])

    def test_transport_failure_on_labels_is_twenty(self):
        args = SimpleNamespace(repo="acme/tasks", issue="1",
                               api=self._api(labels=None))
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_cleanup(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    def test_transport_failure_on_remove_is_twenty(self):
        api = self._api(labels=["kraken-task", "in-progress"],
                        swap=lambda issue, remove=None, add=None: False)
        args = SimpleNamespace(repo="acme/tasks", issue="1", api=api)
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_cleanup(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
