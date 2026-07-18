#!/usr/bin/env python3
"""Unit tests for the vendored-workflow subcommands (issues #37, #39): reap,
requeue-check, validate, and cleanup. These are the coordination-repo workflows'
logic, moved out of jq/grep/awk/bash and into kraken.py so one parser (with one
set of unit tests) drives all four.

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
from contextlib import redirect_stdout

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
    """requeue-on-reply's discriminator: a comment is a worker's iff its FIRST
    line is the attribution disclaimer (PROTOCOL.md §4). Derived from the
    DISCLAIMER constant, so it is name-agnostic and never a second copy."""

    def test_disclaimer_headed_comment_is_a_worker(self):
        self.assertTrue(kraken.is_worker_comment(disclaimer_body("env-1", "some prose")))

    def test_bare_human_comment_is_not_a_worker(self):
        self.assertFalse(kraken.is_worker_comment("option B, go"))

    def test_disclaimer_mid_body_is_not_a_worker(self):
        # An operator quoting the disclaimer mid-reply is still a human — only
        # the opening line classifies.
        body = "answering below:\n\n" + kraken.disclaimer("env-1") + "\n\noption B"
        self.assertFalse(kraken.is_worker_comment(body))

    def test_name_agnostic(self):
        # Any worker name matches — the filter keys on the prefix up to the
        # opening backtick, not a specific name.
        for name in ("env-1", "kraken-copilot-9", "a"):
            self.assertTrue(kraken.is_worker_comment(disclaimer_body(name)))

    def test_crlf_first_line_still_matches(self):
        body = kraken.disclaimer("env-1") + "\r\n\r\nprose"
        self.assertTrue(kraken.is_worker_comment(body))


# --- requeue-directive detection ---------------------------------------------

class RequeueDirectiveTests(unittest.TestCase):
    """awaiting-merge (delivered work) bounces back ONLY on an explicit,
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
    """The reaper's reclaim comment: prose + a stale-claim marker, no disclaimer
    (it is coordination automation, not a worker)."""

    def test_carries_reason_prose_and_marker(self):
        body = kraken.stale_claim_body("no worker heartbeat for 8h")
        self.assertIn("gone silent (no worker heartbeat for 8h)", body)
        self.assertIn('<!-- kraken {"type":"stale-claim","reason":"no worker heartbeat for 8h"} -->', body)

    def test_carries_no_worker_disclaimer(self):
        body = kraken.stale_claim_body("no worker heartbeat on record")
        self.assertNotIn("automated comment from a kraken tentacle", body)


# --- cmd_reap: staleness anchoring, transport mocked -------------------------

def _iso(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReapCommandTests(unittest.TestCase):
    """cmd_reap's decision loop with transport mocked: which in-progress issues
    get reclaimed, anchored to the newest liveness marker."""

    NOW = 1_800_000_000.0

    def setUp(self):
        self._orig = {
            "open_issue_numbers": kraken.open_issue_numbers,
            "comment_records": kraken.comment_records,
            "swap_labels": kraken.swap_labels,
            "post_comment": kraken.post_comment,
            "time": kraken.time.time,
        }
        self.swaps = []
        self.posts = []
        kraken.swap_labels = lambda repo, issue, remove=None, add=None: (
            self.swaps.append((issue, remove, add)) or True)
        kraken.post_comment = lambda repo, issue, body: (
            self.posts.append((issue, body)) or True)
        kraken.time.time = lambda: self.NOW

    def tearDown(self):
        kraken.open_issue_numbers = self._orig["open_issue_numbers"]
        kraken.comment_records = self._orig["comment_records"]
        kraken.swap_labels = self._orig["swap_labels"]
        kraken.post_comment = self._orig["post_comment"]
        kraken.time.time = self._orig["time"]

    def _claim(self, worker, hours_ago):
        return {"body": kraken.make_marker({"type": "claim", "worker": worker}),
                "createdAt": _iso(self.NOW - hours_ago * 3600)}

    def _hb(self, worker, hours_ago):
        return {"body": kraken.make_marker({"type": "heartbeat", "worker": worker}),
                "createdAt": _iso(self.NOW - hours_ago * 3600)}

    def _op(self, hours_ago):
        return {"body": "just an operator note",
                "createdAt": _iso(self.NOW - hours_ago * 3600)}

    def _run(self, issue_comments, max_hours=6):
        kraken.open_issue_numbers = lambda repo, label: list(issue_comments)
        kraken.comment_records = lambda repo, issue: issue_comments[issue]
        args = SimpleNamespace(repo="OWNER/tasks", max_hours=max_hours)
        buf = StringIO()
        with redirect_stdout(buf):
            rc = kraken.cmd_reap(args)
        return rc, buf.getvalue()

    def test_dead_worker_reclaimed(self):
        rc, _ = self._run({1: [self._claim("dead", 8), self._op(0)]})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [(1, "in-progress", "needs-decision")])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("stale-claim", self.posts[0][1])
        self.assertIn("for 8h", self.posts[0][1])

    def test_live_worker_left_alone(self):
        rc, _ = self._run({2: [self._claim("live", 9), self._hb("live", 0)]})
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])

    def test_no_liveness_marker_is_infinitely_stale(self):
        rc, _ = self._run({3: [self._op(0)]})
        self.assertEqual(self.swaps, [(3, "in-progress", "needs-decision")])
        self.assertIn("on record", self.posts[0][1])

    def test_operator_comment_never_resets_the_clock(self):
        # A fresh operator comment on a dead worker's issue must not spare it.
        rc, _ = self._run({4: [self._claim("dead", 8), self._op(0), self._op(0)]})
        self.assertEqual(self.swaps, [(4, "in-progress", "needs-decision")])

    def test_boundary_exactly_at_max_hours_is_reclaimed(self):
        rc, _ = self._run({5: [self._claim("edge", 6)]}, max_hours=6)
        self.assertEqual(self.swaps, [(5, "in-progress", "needs-decision")])

    def test_just_under_max_hours_is_spared(self):
        rc, _ = self._run({6: [self._claim("edge", 5)]}, max_hours=6)
        self.assertEqual(self.swaps, [])

    def test_transport_failure_on_list_is_twenty(self):
        kraken.open_issue_numbers = lambda repo, label: None
        args = SimpleNamespace(repo="OWNER/tasks", max_hours=6)
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_reap(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)

    def test_env_max_hours_fallback(self):
        # max_hours=None -> read MAX_HOURS from the env (the workflow's channel).
        kraken.open_issue_numbers = lambda repo, label: [7]
        kraken.comment_records = lambda repo, issue: [self._claim("w", 4)]
        os.environ["MAX_HOURS"] = "3"
        try:
            args = SimpleNamespace(repo="OWNER/tasks", max_hours=None)
            with redirect_stdout(StringIO()):
                rc = kraken.cmd_reap(args)
        finally:
            del os.environ["MAX_HOURS"]
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [(7, "in-progress", "needs-decision")])


# --- cmd_requeue_check: held-state rules, transport mocked -------------------

class RequeueCheckCommandTests(unittest.TestCase):

    def setUp(self):
        self._orig = {
            "issue_label_names": kraken.issue_label_names,
            "swap_labels": kraken.swap_labels,
            "post_comment": kraken.post_comment,
        }
        self.swaps = []
        self.posts = []
        kraken.swap_labels = lambda repo, issue, remove=None, add=None: (
            self.swaps.append((issue, remove, add)) or True)
        kraken.post_comment = lambda repo, issue, body: (
            self.posts.append((issue, body)) or True)

    def tearDown(self):
        kraken.issue_label_names = self._orig["issue_label_names"]
        kraken.swap_labels = self._orig["swap_labels"]
        kraken.post_comment = self._orig["post_comment"]
        for k in ("COMMENT_BODY", "COMMENT_AUTHOR_TYPE"):
            os.environ.pop(k, None)

    def _run(self, issue, labels, body, author_type):
        kraken.issue_label_names = lambda repo, i: labels
        os.environ["COMMENT_BODY"] = body
        os.environ["COMMENT_AUTHOR_TYPE"] = author_type
        args = SimpleNamespace(repo="OWNER/tasks", issue=str(issue))
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_requeue_check(args)
        return rc

    def test_bot_comment_is_a_noop(self):
        rc = self._run(1, ["needs-decision"], "stale-claim: ...", "Bot")
        self.assertEqual(rc, kraken.EXIT_OK)
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])

    def test_worker_comment_is_a_noop(self):
        rc = self._run(1, ["needs-decision"],
                       disclaimer_body("w1", "which option?"), "User")
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])

    def test_needs_decision_requeues_on_bare_reply(self):
        rc = self._run(1, ["kraken-task", "needs-decision"], "option B", "User")
        self.assertEqual(self.swaps, [(str(1), "needs-decision", None)])
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(self.posts[0][1].startswith("requeue: operator reply detected"))

    def test_awaiting_merge_bare_comment_stays_held(self):
        rc = self._run(1, ["awaiting-merge"], "merging tomorrow", "User")
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])

    def test_awaiting_merge_requeues_on_directive(self):
        rc = self._run(1, ["awaiting-merge"], "requeue:", "User")
        self.assertEqual(self.swaps, [(str(1), "awaiting-merge", None)])
        self.assertTrue(self.posts[0][1].startswith("requeue: explicit requeue"))

    def test_awaiting_merge_requeues_on_marker(self):
        body = "bounce\n\n" + kraken.make_marker({"type": "requeue"})
        rc = self._run(1, ["awaiting-merge"], body, "User")
        self.assertEqual(self.swaps, [(str(1), "awaiting-merge", None)])

    def test_no_held_label_is_a_noop(self):
        rc = self._run(1, ["kraken-task"], "nice work", "User")
        self.assertEqual(self.swaps, [])
        self.assertEqual(self.posts, [])

    def test_transport_failure_on_labels_is_twenty(self):
        kraken.issue_label_names = lambda repo, i: None
        os.environ["COMMENT_BODY"] = "option B"
        os.environ["COMMENT_AUTHOR_TYPE"] = "User"
        args = SimpleNamespace(repo="OWNER/tasks", issue="1")
        with redirect_stdout(StringIO()):
            rc = kraken.cmd_requeue_check(args)
        self.assertEqual(rc, kraken.EXIT_TRANSPORT)


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
        }
        self.removed = []
        kraken.swap_labels = lambda repo, issue, remove=None, add=None: (
            self.removed.append((issue, remove, add)) or True)

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
