#!/usr/bin/env python3
"""The Copilot-harness fast claim-release (scripts/kraken-loop.sh): the
SessionEnd/StopFailure hooks only fire inside a Claude Code session, so the
loop itself is the Copilot worker's self-heal. `kraken.py claim` writes
$KRAKEN_STATE_DIR/claim-<worker>.json and every terminal transition removes it,
so a file that survives the `copilot` process is proof the drain died holding
the claim — the loop releases it on the spot (and its EXIT/INT/TERM trap covers
the loop itself being killed), scoped to its OWN worker only. A clean drain is
a strict no-op. Best-effort: a failed release falls back to the reaper."""
import os
import signal
import stat
import subprocess
import time
import unittest

from harness import KrakenConformanceTest, ROOT

LOOP = os.path.join(ROOT, "scripts", "kraken-loop.sh")
# kraken-loop.sh rejects the OWNER/* placeholder shape; the stub ignores the
# slug, so any real-looking one works.
TASKS = "acme/tasks"


class LoopFastReleaseTests(KrakenConformanceTest):
    def write_copilot_stub(self, body):
        """Install a fake `copilot` on PATH whose behavior is the given bash
        body (the loop invokes it with -p/--allow-all-tools/--no-ask-user)."""
        bin_dir = os.path.join(self.state, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        path = os.path.join(bin_dir, "copilot")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        return bin_dir

    def loop_env(self, bin_dir):
        env = self.base_env()
        env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        return env

    def run_loop_once(self, bin_dir, worker="w1"):
        proc = subprocess.run(
            ["bash", LOOP, TASKS, "--worker-name", worker, "--project", "app", "--once"],
            cwd=ROOT,
            env=self.loop_env(bin_dir),
            capture_output=True,
            text=True,
        )
        return proc

    def test_copilot_death_mid_drain_releases_the_claim(self):
        # The stub claims and then dies without any terminal transition — a
        # crash / kill / rate-limit abort mid-drain.
        self.mk_issue(7, "abandoned mid-drain", "kraken-task", "project:app")
        bin_dir = self.write_copilot_stub(
            'python3 "%s" claim "%s" 7 w1 >/dev/null\nexit 1\n'
            % (os.path.join(ROOT, "skills", "unleash", "kraken.py"), TASKS)
        )

        proc = self.run_loop_once(bin_dir)
        self.assertEqual(proc.returncode, 0,
                         "loop --once must exit 0 (stderr: %s)" % proc.stderr)

        self.assertFalse(self.has_label(7, "in-progress"),
                         "loop did not drop in-progress after copilot died mid-drain")
        self.assertFalse(os.path.isfile(self.claim_state_file("w1")),
                         "loop did not delete the claim state file")
        self.assertIn('<!-- kraken {"type":"released","worker":"w1","reason":"copilot exited mid-drain"} -->',
                      self.last_comment(7),
                      "loop did not release via kraken.py (released marker missing)")
        self.assertFalse(self.claim_ref_exists(7), "loop release left the claim ref behind")

        # The released task is claimable again — end to end.
        r = self.kraken("claim", TASKS, 7, "w2")
        self.assertEqual(r.rc, 0, "task re-claimable after the loop released it")

    def test_clean_drain_is_a_strict_noop(self):
        # The stub exits without holding a claim (the model delivered/never
        # claimed): the loop must write nothing to the queue.
        self.mk_issue(7, "untouched task", "kraken-task", "project:app")
        before = self.comment_count(7)
        bin_dir = self.write_copilot_stub("exit 0")

        proc = self.run_loop_once(bin_dir)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.comment_count(7), before,
                         "clean drain must post no comment")
        self.assertTrue(self.has_label(7, "kraken-task"), "labels must be untouched")

    def test_release_is_scoped_to_the_loops_own_worker(self):
        # Another worker's live claim on the same machine must survive this
        # loop's cleanup — only claim-<own-worker>.json is released.
        self.mk_issue(7, "startable bait", "kraken-task", "project:app")
        self.mk_issue(8, "someone else's task", "kraken-task", "project:app")
        self.kraken("claim", TASKS, 8, "other")
        self.assertTrue(os.path.isfile(self.claim_state_file("other")),
                        "setup: claim did not write state file for 'other'")
        before8 = self.comment_count(8)
        bin_dir = self.write_copilot_stub("exit 1")

        proc = self.run_loop_once(bin_dir, worker="w1")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.isfile(self.claim_state_file("other")),
                        "loop released a claim belonging to another worker")
        self.assertTrue(self.has_label(8, "in-progress"),
                        "loop dropped another worker's in-progress label")
        self.assertEqual(self.comment_count(8), before8,
                         "loop posted on another worker's issue")

    def test_sigint_on_the_polling_loop_releases_the_claim(self):
        # Ctrl-C story: the polling loop (no --once) is interrupted while the
        # copilot process holds the claim; the trap must release before exit.
        self.mk_issue(7, "interrupted task", "kraken-task", "project:app")
        bin_dir = self.write_copilot_stub(
            'python3 "%s" claim "%s" 7 w1 >/dev/null\nsleep 60\n'
            % (os.path.join(ROOT, "skills", "unleash", "kraken.py"), TASKS)
        )

        proc = subprocess.Popen(
            ["bash", LOOP, TASKS, "--worker-name", "w1", "--project", "app"],
            cwd=ROOT,
            env=self.loop_env(bin_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            # Deterministic ordering: only interrupt once the claim provably
            # exists (the stub wrote the state file).
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if os.path.isfile(self.claim_state_file("w1")):
                    break
                time.sleep(0.1)
            else:
                self.fail("copilot stub never claimed (state file missing)")
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


if __name__ == "__main__":
    unittest.main()
