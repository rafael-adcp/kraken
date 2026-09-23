#!/usr/bin/env python3
"""Where the Copilot loop (scripts/kraken-loop.sh) runs the worker: `copilot`
runs in the WORK DIR — the work repo's checkout, where the code, the branch and
the draft PR live — never in the kraken checkout the script sits in. The work
dir defaults to the directory the loop was launched from; `--work-dir` (or
KRAKEN_WORK_DIR) points it elsewhere. The contract files stay in the kraken
checkout, so the prompt names them by absolute path and `--add-dir` grants
copilot read access to that checkout."""
import os
import stat
import subprocess
import unittest

from harness import KrakenConformanceTest, KRAKEN, ROOT

LOOP = os.path.join(ROOT, "scripts", "kraken-loop.sh")
# kraken-loop.sh rejects the OWNER/* placeholder shape; the stub ignores the
# slug, so any real-looking one works.
TASKS = "acme/tasks"


class LoopWorkDirTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        # The stub records where it ran and what it was handed, so every test
        # asserts on the invocation the loop actually made.
        self.cwd_file = os.path.join(self.state, "copilot-cwd")
        self.args_file = os.path.join(self.state, "copilot-args")
        bin_dir = os.path.join(self.state, "bin")
        os.makedirs(bin_dir)
        stub = os.path.join(bin_dir, "copilot")
        with open(stub, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n"
                    'pwd > "%s"\n'
                    "printf '%%s\\0' \"$@\" > \"%s\"\n" % (self.cwd_file, self.args_file))
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        self.bin_dir = bin_dir
        self.work = os.path.join(self.state, "work-repo")
        os.makedirs(self.work)
        # A startable task, so the drain pass actually reaches `copilot`.
        self.mk_issue(7, "startable task", "kraken-task", "project:app")

    def run_loop(self, *flags, cwd=ROOT, env=None):
        full_env = self.base_env(env)
        full_env["PATH"] = self.bin_dir + os.pathsep + full_env["PATH"]
        if not env or "KRAKEN_WORK_DIR" not in env:
            full_env.pop("KRAKEN_WORK_DIR", None)
        return subprocess.run(
            ["bash", LOOP, TASKS, "--worker-name", "w1", "--project", "app", "--once", *flags],
            cwd=cwd, env=full_env, capture_output=True, text=True,
        )

    def copilot_cwd(self):
        with open(self.cwd_file, encoding="utf-8") as f:
            return f.read().strip()

    def copilot_args(self):
        with open(self.args_file, encoding="utf-8") as f:
            return f.read().split("\0")[:-1]

    def assert_ran_in(self, expected):
        self.assertTrue(os.path.isfile(self.cwd_file), "the loop never invoked copilot")
        self.assertEqual(os.path.realpath(self.copilot_cwd()), os.path.realpath(expected))

    def test_default_work_dir_is_the_launch_directory(self):
        # Launched by absolute path from the work repo: copilot must run there,
        # not in the kraken checkout the script lives in.
        proc = self.run_loop(cwd=self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_ran_in(self.work)

    def test_work_dir_flag_wins_over_the_launch_directory(self):
        proc = self.run_loop("--work-dir", self.work, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_ran_in(self.work)

    def test_work_dir_env_fallback_and_flag_precedence(self):
        other = os.path.join(self.state, "other-repo")
        os.makedirs(other)
        proc = self.run_loop(cwd=ROOT, env={"KRAKEN_WORK_DIR": self.work})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_ran_in(self.work)

        proc = self.run_loop("--work-dir", other, cwd=ROOT, env={"KRAKEN_WORK_DIR": self.work})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_ran_in(other)

    def test_relative_work_dir_resolves_against_the_launch_directory(self):
        proc = self.run_loop("--work-dir", "work-repo", cwd=self.state)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assert_ran_in(self.work)
        # The prompt carries the resolved absolute path, never the relative one.
        prompt = self.copilot_args()[self.copilot_args().index("-p") + 1]
        self.assertIn("Your working directory, %s," % self.copilot_cwd(), prompt)

    def test_prompt_points_at_the_contract_by_absolute_path(self):
        proc = self.run_loop("--work-dir", self.work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.copilot_args()
        self.assertIn("-p", args)
        prompt = args[args.index("-p") + 1]
        for path in (os.path.join(ROOT, "AGENTS.md"),
                     os.path.join(ROOT, "skills", "unleash", "SKILL.md"),
                     os.path.join(ROOT, "PROTOCOL.md")):
            self.assertTrue(os.path.isfile(path), "setup: %s is missing" % path)
            self.assertIn(path, prompt)
        self.assertIn('python3 "%s" next-action %s app w1' % (KRAKEN, TASKS), prompt)
        self.assertIn("--add-dir", args)
        self.assertEqual(os.path.realpath(args[args.index("--add-dir") + 1]),
                         os.path.realpath(ROOT),
                         "copilot must be granted read access to the kraken checkout")

    def test_missing_work_dir_refuses_before_invoking_copilot(self):
        proc = self.run_loop("--work-dir", os.path.join(self.state, "no-such-dir"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--work-dir", proc.stderr)
        self.assertFalse(os.path.isfile(self.cwd_file),
                         "copilot ran although the work dir does not exist")
        self.assertTrue(self.has_label(7, "kraken-task"), "the queue must be untouched")
        self.assertFalse(self.has_label(7, "in-progress"), "the queue must be untouched")


if __name__ == "__main__":
    unittest.main()
