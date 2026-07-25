#!/usr/bin/env python3
"""`kraken.py work` — the supervisor owns the life cycle, the agent only judges.

Every case is driven by a STUB AGENT: a shell script standing in for
`claude -p …` / `copilot -p …`, which is the whole point of the design — if a
three-line shell script can be a conforming worker, then porting to a real agent
is a flag rather than a document.

What is pinned here is the supervisor's side of the contract:

  - it claims, briefs the agent with files, and performs the transition itself;
  - a delivered verdict delivers, a needs-decision verdict escalates;
  - EVERY degraded outcome (crash, no verdict, junk verdict, hang, verdict with
    no result) releases honestly rather than looking like a success;
  - a hang is killed at the timeout — the only thing that can catch one, since a
    hung agent's lease is being renewed and looks alive to the queue;
  - an agent that performs its own transition (the interactive skill's path) is
    respected, not double-written;
  - the claim is released on the way out of every exit path.
"""
import json
import os
import signal
import stat
import subprocess
import time
import unittest

from harness import KRAKEN, ROOT, KrakenConformanceTest


class WorkSupervisorTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.mk_label("project:app")

    # --- stub agents ---------------------------------------------------------

    def agent(self, script):
        """Write a stub agent and return its path. It receives the briefing in
        $KRAKEN_TASK_DIR, like any real agent would."""
        path = os.path.join(self.state, "agent-%d.sh" % len(os.listdir(self.state)))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\nset -u\n" + script + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def verdict_agent(self, verdict, result="did the work", pr=None, extra=""):
        payload = {"verdict": verdict}
        if pr:
            payload["pr"] = pr
        return self.agent(
            'printf %%s "%s" > "$KRAKEN_TASK_DIR/result.md"\n'
            "cat > \"$KRAKEN_TASK_DIR/verdict.json\" <<'JSON'\n%s\nJSON\n%s"
            % (result, json.dumps(payload), extra))

    def work(self, agent_path, *extra, worker="w-sup"):
        return self.kraken("work", "OWNER/tasks", "--project", "app",
                           "--worker-name", worker, "--agent", agent_path,
                           "--once", *extra)

    # --- the happy paths -----------------------------------------------------

    def test_a_delivered_verdict_delivers(self):
        self.mk_issue(1, "ship it", "kraken-task", "project:app")

        r = self.work(self.verdict_agent(
            "delivered", result="built and pushed",
            pr="https://github.com/OWNER/work/pull/7"))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertTrue(self.has_label(1, "awaiting-merge"), "not delivered")
        self.assertFalse(self.has_label(1, "in-progress"))
        self.assertFalse(self.claim_ref_exists(1), "the lease was not released")
        last = self.last_comment(1)
        self.assertIn('"type":"delivered"', last)
        self.assertIn("built and pushed", last, "the agent's result was not posted")
        self.assertIn("https://github.com/OWNER/work/pull/7", last)
        self.assert_disclaimer(1, "w-sup")

    def test_a_needs_decision_verdict_escalates(self):
        self.mk_issue(2, "ambiguous", "kraken-task", "project:app")

        r = self.work(self.verdict_agent(
            "needs-decision", result="A or B? I recommend B."))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertTrue(self.has_label(2, "needs-decision"))
        self.assertFalse(self.claim_ref_exists(2))
        self.assertIn("A or B? I recommend B.", self.last_comment(2))
        self.assertIn('"type":"needs-decision"', self.last_comment(2))

    def test_a_released_verdict_requeues_the_task(self):
        self.mk_issue(3, "cannot host", "kraken-task", "project:app")

        r = self.work(self.verdict_agent(
            "released", result="this environment has no database"))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertFalse(self.has_label(3, "in-progress"), "still held")
        self.assertFalse(self.claim_ref_exists(3))
        self.assertIn('"type":"released"', self.last_comment(3))
        self.assertIn("no database", self.last_comment(3))

    # --- the briefing --------------------------------------------------------

    def test_the_agent_is_briefed_by_file_including_the_thread(self):
        # A review bounce leaves its feedback ONLY on the thread, and a
        # supervised agent may have no gh at all — so the thread has to travel
        # with the briefing.
        self.mk_issue(4, "bounced back", "kraken-task", "project:app")
        self.mk_body(4, "### Goal\nmake it fast")
        self.mk_comment(4, "the p99 is still bad, try the index")

        # The agent proves what it was given by copying it into the result.
        r = self.work(self.agent(
            'cp "$KRAKEN_TASK_DIR/task.json" "$KRAKEN_TASK_DIR/seen.json"\n'
            'cp "$KRAKEN_TASK_DIR/task.md" "$KRAKEN_TASK_DIR/result.md"\n'
            'echo \'{"verdict":"released"}\' > "$KRAKEN_TASK_DIR/verdict.json"'))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        task_dir = self._task_dir_from(r.out)
        with open(os.path.join(task_dir, "seen.json"), encoding="utf-8") as fh:
            brief = json.load(fh)
        self.assertEqual(brief["issue"], 4)
        self.assertEqual(brief["repo"], "OWNER/tasks")
        self.assertEqual(brief["worker"], "w-sup")
        self.assertEqual(brief["project"], "app")
        self.assertIn("make it fast", brief["body"])
        self.assertIn("project:app", brief["labels"])
        self.assertTrue(any("try the index" in c["body"] for c in brief["comments"]),
                        "the thread did not reach the agent: %s" % brief["comments"])
        # The renewal cadence travels too, so an agent never hardcodes it.
        self.assertGreater(brief["lease_renew_seconds"], 0)

    def test_the_task_body_never_reaches_the_agent_command(self):
        # SECURITY.md: a task body is untrusted input. It travels by file, so a
        # body full of shell must be inert — never interpolated into --agent.
        self.mk_issue(5, "$(touch /tmp/pwned)", "kraken-task", "project:app")
        self.mk_body(5, '`touch "$KRAKEN_TASK_DIR/pwned"`; $(id > "$KRAKEN_TASK_DIR/pwned2")')

        r = self.work(self.verdict_agent("released", result="nope"))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        task_dir = self._task_dir_from(r.out)
        self.assertFalse(os.path.exists(os.path.join(task_dir, "pwned")),
                         "a task body was executed by the shell")
        self.assertFalse(os.path.exists(os.path.join(task_dir, "pwned2")),
                         "a task body was executed by the shell")

    # --- every degraded outcome is honest ------------------------------------

    def test_a_crashing_agent_releases_rather_than_looking_delivered(self):
        self.mk_issue(6, "will crash", "kraken-task", "project:app")

        r = self.work(self.agent("echo 'boom' >&2\nexit 3"))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertFalse(self.has_label(6, "awaiting-merge"), "a crash delivered")
        self.assertFalse(self.has_label(6, "in-progress"), "a crash left it held")
        self.assertFalse(self.claim_ref_exists(6))
        self.assertIn('"type":"released"', self.last_comment(6))
        self.assertIn("exited 3", self.last_comment(6))

    def test_a_silent_agent_is_never_a_success(self):
        # Exit 0 having done nothing must not be indistinguishable from a
        # delivery — this is the case the file-based verdict exists for.
        self.mk_issue(7, "does nothing", "kraken-task", "project:app")

        r = self.work(self.agent("exit 0"))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertFalse(self.has_label(7, "awaiting-merge"), "silence delivered")
        self.assertIn('"type":"released"', self.last_comment(7))
        self.assertIn("no verdict", self.last_comment(7))

    def test_a_junk_verdict_releases(self):
        self.mk_issue(8, "malformed", "kraken-task", "project:app")

        r = self.work(self.agent(
            'echo "done" > "$KRAKEN_TASK_DIR/result.md"\n'
            'echo "I have delivered it, boss" > "$KRAKEN_TASK_DIR/verdict.json"'))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertFalse(self.has_label(8, "awaiting-merge"), "junk delivered")
        self.assertIn('"type":"released"', self.last_comment(8))

    def test_a_verdict_with_no_result_releases(self):
        # Inventing a body would put words on the thread nobody wrote.
        self.mk_issue(9, "no body", "kraken-task", "project:app")

        r = self.work(self.agent(
            'echo \'{"verdict":"delivered"}\' > "$KRAKEN_TASK_DIR/verdict.json"'))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertFalse(self.has_label(9, "awaiting-merge"))
        self.assertIn('"type":"released"', self.last_comment(9))
        self.assertIn("wrote no result", self.last_comment(9))

    def test_a_hanging_agent_is_killed_at_the_timeout_and_the_task_freed(self):
        # THE case lease expiry cannot catch: the supervisor keeps renewing
        # while the agent hangs, so to the queue it looks perfectly alive.
        self.mk_issue(10, "hangs forever", "kraken-task", "project:app")

        r = self.work(self.agent("sleep 300"), "--timeout", "2")
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertFalse(self.has_label(10, "in-progress"), "a hang stayed held")
        self.assertFalse(self.claim_ref_exists(10), "a hang kept the lease")
        self.assertIn('"type":"released"', self.last_comment(10))
        self.assertIn("timeout", self.last_comment(10))

    # --- interoperating with an agent that drives its own transitions --------

    def test_an_agent_that_transitions_itself_is_respected(self):
        # The interactive skill's path: the agent already ran `kraken.py
        # deliver`. The supervisor must not write a second time.
        self.mk_issue(11, "self-driving", "kraken-task", "project:app")

        kraken_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))),
            "skills", "unleash", "kraken.py")
        script = (
            'printf %%s "self delivered" > "$KRAKEN_TASK_DIR/result.md"\n'
            'python3 "%s" deliver OWNER/tasks 11 w-sup '
            '"$KRAKEN_TASK_DIR/result.md" https://github.com/OWNER/work/pull/9'
            % kraken_py)
        r = self.work(self.agent(script))
        self.assertEqual(r.rc, 0, "work exit: %s%s" % (r.out, r.err))

        self.assertTrue(self.has_label(11, "awaiting-merge"))
        self.assertIn("resolved issue=11 itself", r.out,
                      "the supervisor did not notice the agent's own transition")
        delivered = [b for b in self.comment_bodies(11) if '"type":"delivered"' in b]
        self.assertEqual(len(delivered), 1,
                         "the supervisor wrote a second delivery: %s" % delivered)

    # --- the loop ------------------------------------------------------------

    def test_an_idle_queue_is_exit_three_and_never_runs_the_agent(self):
        marker = os.path.join(self.state, "ran")
        r = self.work(self.agent('touch "%s"' % marker))
        self.assertEqual(r.rc, 3, "an idle queue should be honest-empty")
        self.assertFalse(os.path.exists(marker), "the agent ran on an idle queue")

    def test_an_unknown_project_refuses_before_any_write(self):
        r = self.kraken("work", "OWNER/tasks", "--project", "nope",
                        "--worker-name", "w-sup", "--agent", "true", "--once")
        self.assertEqual(r.rc, 13, "unknown project should refuse: %s" % r.out)

    def test_a_live_lease_on_the_only_task_is_left_alone(self):
        self.mk_issue(12, "someone else has it", "kraken-task", "project:app",
                      "in-progress")
        held = self.mk_claim_ref(12, "w-other", age_seconds=5)

        r = self.work(self.agent("exit 0"))
        self.assertEqual(r.rc, 3, "a live lease was taken: %s%s" % (r.out, r.err))
        self.assertEqual(self.claim_ref(12), held)

    # --- release on the way out, and the renewer ----------------------------

    def test_sigterm_mid_task_releases_the_claim(self):
        """The promise that makes `hooks/` and `kraken-loop.sh` redundant: the
        supervisor frees the task on the way out, on ANY harness, because it is
        a `finally` in code rather than a lifecycle event some agents happen to
        have. `docker stop` is exactly this signal."""
        self.mk_issue(13, "interrupted", "kraken-task", "project:app")

        proc = self._work_background(self.agent("sleep 300"))
        self._wait_for(lambda: self.claim_ref_exists(13),
                       "the supervisor never claimed the task")

        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("the supervisor ignored SIGTERM")

        self.assertFalse(self.claim_ref_exists(13),
                         "SIGTERM left the lease held")
        self.assertFalse(self.has_label(13, "in-progress"),
                         "SIGTERM left the task in-progress")
        self.assertIn('"type":"released"', self.last_comment(13))
        self.assertIn("supervisor exited", self.last_comment(13))

    def test_the_lease_is_renewed_while_the_agent_works(self):
        """A task that outlives its TTL must not be stolen out from under a
        working agent. The renewer thread is what prevents it, and with a short
        TTL the effect is observable: the lease climbs generations while the
        agent is still running."""
        self.mk_issue(14, "slow but alive", "kraken-task", "project:app")

        proc = self._work_background(self.agent("sleep 300"),
                                     env={"KRAKEN_LEASE_TTL_SECONDS": "3"})
        self._wait_for(lambda: self.claim_ref_exists(14),
                       "the supervisor never claimed the task")
        first = self.claim_generations(14)
        self._wait_for(lambda: self.claim_generations(14) != first,
                       "the lease was never renewed", timeout=25)

        gens = self.claim_generations(14)
        self.assertGreater(max(g for g, _s in gens), max(g for g, _s in first),
                           "the renewal did not climb a generation")
        # And it stays a single holder: the superseded rung is collected.
        self.assertEqual(len(gens), 1,
                         "renewal left a superseded generation behind: %s" % gens)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)

    # --- helpers -------------------------------------------------------------

    def _work_background(self, agent_path, env=None):
        """Start `work` (no --once) as a real process we can signal."""
        proc = subprocess.Popen(
            ["python3", KRAKEN, "work", "OWNER/tasks", "--project", "app",
             "--worker-name", "w-sup", "--agent", agent_path],
            cwd=ROOT, env=self.base_env(env or {}),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    def _wait_for(self, predicate, message, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        self.fail(message)


    def _task_dir_from(self, out):
        for line in out.split("\n"):
            if line.startswith("work: claimed ") and " dir=" in line:
                return line.split(" dir=", 1)[1].strip()
        self.fail("no task dir on stdout:\n%s" % out)


if __name__ == "__main__":
    unittest.main()
