#!/usr/bin/env python3
"""The lifecycle hooks release DETACHED: the hook returns at once and the
release runs on in its own session, outliving the hook — and whatever the
harness does to the hook once its deadline passes.

Why: a harness bounds how long a hook may run. Claude Code gives SessionEnd
hooks 1.5s in total (a plugin's own "timeout" does not raise it, verified on
2.1.283) and, under `claude -p`, kills a StopFailure hook as the process exits;
a release against the real API makes ~9 calls and takes seconds. Run inside the
hook, the release was killed half-done: the released marker posted, but
in-progress and the lock still held until the TTL.

Here the stub answers at a real API's pace, the hook runs in its own process
group, and the group is SIGKILLed the moment the hook returns — the harsh end
of what a harness may do. The release must still land, in full. And a harness
that exits signals its hooks while they still run (`claude -p` does so ~15ms
after starting a StopFailure hook): a hook SIGTERMed mid-run must still hand
the release off."""
import os
import signal
import subprocess
import time
import unittest

from harness import KrakenConformanceTest, ROOT

API_LATENCY = 0.3   # per request: a release (~9 calls) takes ~3s
DEADLINE = 1.5      # Claude Code's SessionEnd budget
# A `date` that takes a second: holds the StopFailure hook mid-run (it stamps
# the wake-retry flag with `date`) while the harness signals it.
SLOW_DATE = """#!/usr/bin/env bash
sleep 1
PATH="${PATH#*:}" exec date "$@"
"""


class DetachedHookReleaseTests(KrakenConformanceTest):
    def run_hook_like_a_harness(self, hook, stdin, term=False, env=None):
        """Run `hook` as a harness would: its own process group, killed once it
        returns. With `term`, the group is SIGTERMed while the hook is still
        running — as an exiting harness does — and only then fed its event: the
        hook is either blocked reading it (SessionEnd) or held in a slow step
        (see SLOW_DATE). Returns how long the hook itself took."""
        env = self.base_env(env)
        env.pop("KRAKEN_HOOK_WAIT", None)
        start = time.time()
        proc = subprocess.Popen(["bash", os.path.join(ROOT, hook)], cwd=ROOT, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        if term:
            time.sleep(0.3)
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(stdin.encode(), timeout=30)
        except BrokenPipeError:
            proc.wait(timeout=30)
        took = time.time() - start
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return took

    def wait_released(self, issue, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.has_label(issue, "in-progress") and not self.claim_ref_exists(issue):
                return True
            time.sleep(0.2)
        return False

    def assert_detached_release(self, hook, stdin, reason, term=False, env=None):
        self.knobs.set_latency(API_LATENCY)
        self.addCleanup(self.knobs.set_latency, 0)
        took = self.run_hook_like_a_harness(hook, stdin, term, env)
        if not term:
            self.assertLess(took, DEADLINE, "the hook ran the release inline (%.1fs)" % took)
        self.assertTrue(self.wait_released(7),
                        "the release did not outlive the hook: task left half-released")
        self.assertIn('"type":"released"', self.last_comment(7))
        self.assertIn(reason, self.last_comment(7))
        self.assertFalse(os.path.isfile(self.claim_state_file("w1")))

    def test_session_end_release_outlives_the_hook(self):
        self.mk_issue(7, "task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "w1", env={"CLAUDE_CODE_SESSION_ID": "sess-A"})
        self.assert_detached_release("hooks/session-end-release.sh",
                                     '{"hook_event_name":"SessionEnd","session_id":"sess-A"}',
                                     "session ended")

    def test_stop_failure_release_outlives_the_hook(self):
        self.mk_issue(7, "task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "w1")
        self.assert_detached_release("hooks/stop-failure-release.sh",
                                     '{"hook_event_name":"StopFailure","error":"rate_limit"}',
                                     "usage limit")
        # The wake-retry flag is stamped inline, before the hook returns.
        self.assertTrue(os.path.isfile(os.path.join(self.kraken_state_dir, "wake-retry")))


    def test_session_end_survives_the_harness_signalling_it(self):
        self.mk_issue(7, "task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "w1", env={"CLAUDE_CODE_SESSION_ID": "sess-A"})
        self.assert_detached_release("hooks/session-end-release.sh", '{"session_id":"sess-A"}',
                                     "session ended", term=True)

    def test_stop_failure_survives_the_harness_signalling_it(self):
        self.mk_issue(7, "task", "kraken-task", "project:app")
        self.kraken("claim", "acme/tasks", 7, "w1")
        slow = os.path.join(self.state, "slow-bin")
        os.makedirs(slow)
        with open(os.path.join(slow, "date"), "w", encoding="utf-8") as f:
            f.write(SLOW_DATE)
        os.chmod(os.path.join(slow, "date"), 0o755)
        self.assert_detached_release("hooks/stop-failure-release.sh", '{"error":"rate_limit"}',
                                     "usage limit", term=True,
                                     env={"PATH": slow + os.pathsep + os.environ["PATH"]})


if __name__ == "__main__":
    unittest.main()
