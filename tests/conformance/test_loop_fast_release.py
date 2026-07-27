#!/usr/bin/env python3
"""`kraken.py loop` — the supervising driver, and its fast claim-release.

The loop invokes the agent only when a task is startable, and `kraken.py claim`
writes $KRAKEN_STATE_DIR/claim-<worker>.json while every terminal transition
removes it. So a state file that OUTLIVED the agent process is proof the drain
died holding the lease (crash, kill, rate-limit abort) and the loop releases it
on the spot — as does its teardown when the loop itself is interrupted. Scoped
to its OWN worker: a co-located worker's live claim must survive. A clean drain
is a strict no-op. Best-effort throughout: the lease expiry is what actually
recovers a task, this only makes it immediate.

These cases were driven against scripts/kraken-loop.sh until the loop moved into
the package; the agent is now passed explicitly after `--`, so nothing here has
to install a fake binary on PATH."""
import os
import signal
import subprocess
import time
import unittest

from harness import KRAKEN, KrakenConformanceTest

TASKS = "acme/tasks"


class LoopFastReleaseTests(KrakenConformanceTest):
    def agent(self, body):
        """A fake agent command: a bash script with the given body, invoked as
        `bash <script> {prompt}`. The placeholder is what the loop substitutes
        the drain instruction into, and `loop` refuses an argv without it."""
        path = os.path.join(self.state, "agent.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n" + body + "\n")
        return ["bash", path, "{prompt}"]

    def claims_then(self, tail, issue=7, worker="w1"):
        """An agent body that wins a real claim and then does `tail` — the
        shape of a drain that died before any terminal transition."""
        return self.agent(
            'python3 "%s" claim "%s" %d %s >/dev/null\n%s'
            % (KRAKEN, TASKS, issue, worker, tail))

    def run_loop(self, agent, worker="w1", extra=()):
        return subprocess.run(
            ["python3", KRAKEN, "loop", TASKS, "app", worker, "--once",
             *extra, "--", *agent],
            cwd=os.path.dirname(KRAKEN), env=self.base_env(),
            capture_output=True, text=True,
        )

    def test_agent_death_mid_drain_releases_the_claim(self):
        self.mk_issue(7, "abandoned mid-drain", "kraken-task", "project:app")

        proc = self.run_loop(self.claims_then("exit 1"))
        self.assertEqual(proc.returncode, 0,
                         "loop --once must exit 0 (stderr: %s)" % proc.stderr)

        self.assertFalse(self.has_label(7, "in-progress"),
                         "loop did not drop in-progress after the agent died")
        self.assertFalse(os.path.isfile(self.claim_state_file("w1")),
                         "loop did not delete the claim state file")
        self.assertIn(
            '<!-- kraken {"type":"released","worker":"w1","reason":"agent exited mid-drain"} -->',
            self.last_comment(7),
            "loop did not release via kraken.py (released marker missing)")
        self.assertFalse(self.claim_ref_exists(7),
                         "loop release left the claim ref behind")

        # The released task is claimable again — end to end.
        self.assertEqual(self.kraken("claim", TASKS, 7, "w2").rc, 0,
                         "task re-claimable after the loop released it")

    def test_clean_drain_is_a_strict_noop(self):
        # The agent exits without holding a claim (it delivered, or never
        # claimed): the loop must write nothing to the queue.
        self.mk_issue(7, "untouched task", "kraken-task", "project:app")
        before = self.comment_count(7)

        proc = self.run_loop(self.agent("exit 0"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.comment_count(7), before,
                         "clean drain must post no comment")
        self.assertTrue(self.has_label(7, "kraken-task"),
                        "labels must be untouched")

    def test_release_is_scoped_to_the_loops_own_worker(self):
        # Another worker's live claim on the same machine must survive this
        # loop's cleanup — only claim-<own-worker>.json is released.
        self.mk_issue(7, "startable bait", "kraken-task", "project:app")
        self.mk_issue(8, "someone else's task", "kraken-task", "project:app")
        self.kraken("claim", TASKS, 8, "other")
        self.assertTrue(os.path.isfile(self.claim_state_file("other")),
                        "setup: claim did not write state file for 'other'")
        before8 = self.comment_count(8)

        proc = self.run_loop(self.agent("exit 1"), worker="w1")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.isfile(self.claim_state_file("other")),
                        "loop released a claim belonging to another worker")
        self.assertTrue(self.has_label(8, "in-progress"),
                        "loop dropped another worker's in-progress label")
        self.assertEqual(self.comment_count(8), before8,
                         "loop posted on another worker's issue")

    def test_idle_queue_never_invokes_the_agent(self):
        # The whole point of polling outside the model: nothing startable, no
        # token spent. The agent would create this file if it ever ran.
        self.mk_issue(7, "held task", "kraken-task", "project:app",
                      "needs-decision")
        ran = os.path.join(self.state, "agent-ran")

        proc = self.run_loop(self.agent('touch "%s"' % ran))
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.isfile(ran),
                         "the loop invoked the agent on an idle queue")

    def test_an_unreadable_queue_is_not_an_idle_queue(self):
        # The bug inherited from the shell: a failed read used to print
        # "queue idle" and skip the model, indistinguishably from real idleness.
        self.mk_issue(7, "startable", "kraken-task", "project:app")
        ran = os.path.join(self.state, "agent-ran")
        self.knobs.set_fail("POST /graphql")

        proc = self.run_loop(self.agent('touch "%s"' % ran))
        self.assertEqual(proc.returncode, 20,
                         "a queue that cannot be read must exit 20, not 0")
        self.assertFalse(os.path.isfile(ran),
                         "the loop drained on a queue read that never landed")
        self.assertIn("kraken-loop:", proc.stderr,
                      "the failed queue read was swallowed")

    def test_an_agent_command_that_cannot_run_stops_the_loop(self):
        # A missing binary never fixes itself; polling forever in front of one
        # is worse than stopping.
        self.mk_issue(7, "startable", "kraken-task", "project:app")

        proc = self.run_loop(["definitely-not-a-real-binary", "{prompt}"])
        self.assertEqual(proc.returncode, 2,
                         "an unrunnable agent must be a usage error")
        self.assertIn("cannot run the agent command", proc.stderr)

    def test_sigint_on_the_polling_loop_releases_the_claim(self):
        # Ctrl-C story: the polling loop (no --once) is interrupted while the
        # agent holds the claim; the teardown must release before exit.
        self.mk_issue(7, "interrupted task", "kraken-task", "project:app")
        # The stub announces itself only once `claim` has RETURNED, so the
        # signal cannot land on the still-running claim write.
        ready = os.path.join(self.state, "agent-ready")
        agent = self.claims_then('touch "%s"\nsleep 60\n' % ready)

        proc = subprocess.Popen(
            ["python3", KRAKEN, "loop", TASKS, "app", "w1", "--", *agent],
            cwd=os.path.dirname(KRAKEN), env=self.base_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if os.path.isfile(ready):
                    break
                time.sleep(0.1)
            else:
                self.fail("the agent stub never finished its claim")
            self.assertTrue(os.path.isfile(self.claim_state_file("w1")),
                            "setup: stub claim did not write the state file")
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)

        self.assertFalse(os.path.isfile(self.claim_state_file("w1")),
                         "interrupted loop did not delete the claim state file")
        self.assertFalse(self.has_label(7, "in-progress"),
                         "interrupted loop did not drop in-progress")
        self.assertIn('"type":"released","worker":"w1"', self.last_comment(7),
                      "interrupted loop did not post the released marker")
        self.assertFalse(self.claim_ref_exists(7),
                         "interrupted loop left the claim ref behind")

    def test_sigterm_releases_the_claim_too(self):
        # SIGTERM kills an unhandled python process outright, which would strand
        # the lease for a full TTL — the handler exists so the teardown runs.
        self.mk_issue(7, "terminated task", "kraken-task", "project:app")
        ready = os.path.join(self.state, "agent-ready")
        agent = self.claims_then('touch "%s"\nsleep 60\n' % ready)

        proc = subprocess.Popen(
            ["python3", KRAKEN, "loop", TASKS, "app", "w1", "--", *agent],
            cwd=os.path.dirname(KRAKEN), env=self.base_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if os.path.isfile(ready):
                    break
                time.sleep(0.1)
            else:
                self.fail("the agent stub never finished its claim")
            proc.terminate()
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)

        self.assertFalse(os.path.isfile(self.claim_state_file("w1")),
                         "terminated loop did not delete the claim state file")
        self.assertFalse(self.claim_ref_exists(7),
                         "terminated loop left the claim ref behind")


if __name__ == "__main__":
    unittest.main()
