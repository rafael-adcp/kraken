#!/usr/bin/env python3
"""kraken.py next-action: the driver loop as one call.

The envelope is the machine contract, so this pins its SHAPE (stdout is JSON and
nothing but JSON — every diagnostic goes to stderr) and every verdict a worker
can be handed: a fresh claim, resuming the task it already holds without
re-claiming it, the steal it must NOT write through, the finished task whose
scratch file lagged, an empty queue, and a project label the repo does not
carry."""
import json
import os
import unittest

from harness import KrakenConformanceTest


class NextActionTests(KrakenConformanceTest):
    def envelope(self, *args, **kwargs):
        """Run next-action and parse stdout. Parsing is itself the assertion that
        stdout carries the envelope alone — a stray diagnostic line breaks it."""
        r = self.kraken("next-action", *args, **kwargs)
        try:
            return r, json.loads(r.out_raw)
        except ValueError:
            self.fail("next-action stdout is not pure JSON:\n%r" % r.out_raw)

    def test_fresh_claim(self):
        self.mk_issue(7, "oldest task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it\n\n### Acceptance\nmake check passes")

        r, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "a won claim exits 0")
        self.assertEqual(env["action"], "execute", "expected an execute verdict")
        self.assertEqual(env["issue"], 7, "wrong task in the envelope")
        self.assertFalse(env["resumed"], "a first claim is not a resume")
        self.assertEqual(env["brief"]["goal"], "ship it", "Goal not split out")
        self.assertEqual(env["brief"]["acceptance"], "make check passes",
                         "Acceptance not split out")
        self.assertEqual(env["brief"]["notes"], "",
                         "an absent Notes section must read as empty, not as the "
                         "issue-form placeholder")
        self.assertTrue(self.has_label(7, "in-progress"), "claim not projected")

        # The lease block is the renewal contract as numbers, not as an
        # instruction the model has to remember.
        lease = env["lease"]
        self.assertEqual(lease["source"], "estimated",
                         "a freshly won claim is timed off the local clock")
        self.assertFalse(lease["renew_now"], "a fresh lease does not need renewing")
        self.assertGreater(lease["seconds_remaining"], 0, "fresh lease already expired")
        self.assertGreater(lease["renew_every_seconds"], 0, "no renewal cadence")

        # `then` is the whole point: fully interpolated argv, so the agent never
        # assembles one. The script path is absolute and quoted (it may contain
        # spaces) and every command names this repo, issue and worker.
        for name in ("renew", "note", "escalate", "deliver", "release"):
            command = env["then"][name]
            self.assertIn("OWNER/tasks 7 w1", command,
                          "then.%s is not interpolated: %r" % (name, command))
            self.assertIn('python3 "/', command,
                          "then.%s does not carry a quoted absolute path: %r"
                          % (name, command))

    def test_resume_does_not_reclaim(self):
        self.mk_issue(7, "a resumable task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it")
        _, first = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(first["action"], "execute", "setup claim failed")
        comments = self.comment_count(7)
        generations = self.claim_generations(7)

        r, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "resuming a held task exits 0")
        self.assertEqual(env["action"], "execute", "expected to resume")
        self.assertEqual(env["issue"], 7, "resumed the wrong task")
        self.assertTrue(env["resumed"], "resumed flag not set")
        self.assertEqual(env["brief"]["goal"], "ship it",
                         "the brief is re-read on a resume")
        self.assertEqual(env["brief"]["title"], "a resumable task",
                         "a resumed brief must carry the title too — the agent "
                         "gets no second fetch to look it up")
        self.assertEqual(env["lease"]["source"], "claim-ref",
                         "a resumed lease is timed off the ref's commit date")
        self.assertIsNotNone(env["lease"]["generation"],
                             "a resumed lease reports its generation")

        # Resuming is a READ. Nothing may be claimed, commented or advanced.
        self.assertEqual(self.comment_count(7), comments,
                         "resuming posted a comment")
        self.assertEqual(self.claim_generations(7), generations,
                         "resuming advanced the claim ref")

    def test_stolen_lease_is_abandoned_without_writing(self):
        self.mk_issue(7, "task", "kraken-task", "project:app")
        self.mk_body(7, "### Goal\nship it")
        _, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(env["action"], "execute", "setup claim failed")
        comments = self.comment_count(7)

        # A thief climbs one generation: ownership is the HIGHEST generation, so
        # w1's own ref surviving proves nothing (PROTOCOL.md §5.3).
        self.mk_claim_ref(7, "w-thief", gen=2)

        r, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 10, "a stolen lease exits 10")
        self.assertEqual(env["action"], "abandon", "expected an abandon verdict")
        self.assertEqual(env["reason"], "lease-stolen", "wrong abandon reason")
        self.assertEqual(env["issue"], 7, "abandoned the wrong task")
        self.assertNotIn("then", env,
                         "abandon must offer no next write — nothing is legal here")
        self.assertEqual(self.comment_count(7), comments,
                         "abandon wrote to the task it no longer holds")

        # The stale scratch file is dropped, so the one-task-at-a-time guard
        # stops refusing forever on a claim this worker provably lost.
        self.assertFalse(os.path.exists(self.claim_state_file("w1")),
                         "the claim state file survived a provable loss")

    def test_finished_task_falls_through_to_the_next(self):
        # A terminal transition landed but the local scratch file lagged (a crash
        # between the write and the cleanup). The drain must continue, not stall.
        self.mk_issue(7, "delivered task", "kraken-task", "project:app",
                      "awaiting-merge")
        self.mk_issue(9, "next task", "kraken-task", "project:app")
        self.mk_body(9, "### Goal\nthe next one")
        os.makedirs(self.kraken_state_dir, exist_ok=True)
        with open(self.claim_state_file("w1"), "w", encoding="utf-8") as fh:
            json.dump({"repo": "OWNER/tasks", "issue": "7", "worker": "w1"}, fh)

        r, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 0, "a resolved claim must not stall the drain")
        self.assertEqual(env["action"], "execute", "expected to move on")
        self.assertEqual(env["issue"], 9, "did not move on to the next task")
        self.assertFalse(env["resumed"], "the next task is a fresh claim")
        self.assertFalse(self.has_label(7, "in-progress"),
                         "the finished task was re-claimed")

    def test_blocked_by_a_claim_in_another_repo(self):
        # The one-task-at-a-time rule is repo-independent, so the claim that
        # blocks this drain can live somewhere else entirely. The envelope has to
        # say WHERE, or a worker resolves it against the wrong repo.
        self.mk_issue(9, "a task here", "kraken-task", "project:app")
        os.makedirs(self.kraken_state_dir, exist_ok=True)
        with open(self.claim_state_file("w1"), "w", encoding="utf-8") as fh:
            json.dump({"repo": "OWNER/other", "issue": "42", "worker": "w1"}, fh)

        r, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 11, "a claim held elsewhere exits 11")
        self.assertEqual(env["action"], "blocked", "expected a blocked verdict")
        self.assertEqual(env["holding"], {"repo": "OWNER/other", "issue": 42},
                         "the blocking claim is not named")
        self.assertNotIn("issue", env,
                         "a bare top-level issue would read against OWNER/tasks")
        self.assertIn("OWNER/other 42 w1", env["then"]["release"],
                      "then.release must target the held claim, not the drain")
        self.assertFalse(self.has_label(9, "in-progress"),
                         "a blocked worker must claim nothing")

    def test_idle_and_unknown_project(self):
        self.mk_label("project:app")
        r, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertEqual(r.rc, 3, "an empty queue exits 3")
        self.assertEqual(env["action"], "idle", "expected an idle verdict")
        self.assertEqual(env["reason"], "queue-empty", "wrong idle reason")
        self.assertNotIn("then", env, "idle offers no next write")

        r, env = self.envelope("OWNER/tasks", "nosuch", "w1")
        self.assertEqual(r.rc, 13, "an unknown project exits 13")
        self.assertEqual(env["action"], "stop", "expected a stop verdict")
        self.assertEqual(env["reason"], "unknown-project", "wrong stop reason")

    def test_every_action_is_in_the_published_vocabulary(self):
        vocabulary = set(self.kraken("contract", "next-actions").lines)
        self.mk_issue(7, "task", "kraken-task", "project:app")
        _, env = self.envelope("OWNER/tasks", "app", "w1")
        self.assertIn(env["action"], vocabulary,
                      "next-action emitted a verdict `contract next-actions` "
                      "does not publish")


if __name__ == "__main__":
    unittest.main()
