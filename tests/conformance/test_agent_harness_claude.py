#!/usr/bin/env python3
"""The agent-behavior harness's Claude Code driver (tests/agent/lib-agent.sh,
the default KRAKEN_AGENT_CLI=claude), proven token-free with a fake `claude`
on PATH.

The real-model scenarios only mean something if the harness drives Claude the
way an operator does, so this pins the wiring: the SAME slash command and
permission flags `scripts/kraken-loop.sh --harness claude` runs (single-sourced
in scripts/lib-claude-drain.sh), this checkout as the plugin, run in the
scratch work repo — never `--dangerously-skip-permissions`."""
import os
import stat
import subprocess
import unittest

from harness import KrakenConformanceTest, ROOT

LIB_AGENT = os.path.join(ROOT, "tests", "agent", "lib-agent.sh")
LIB_CLAUDE = os.path.join(ROOT, "scripts", "lib-claude-drain.sh")

# Reaches the stub once (`gh auth token`), so the harness does not skip the run
# as one whose model never touched the seeded queue.
FAKE_CLAUDE = r'''#!/usr/bin/env bash
pwd > "$RECORD/cwd"
printf '%s\0' "$@" > "$RECORD/args"
gh auth token >/dev/null
'''


def claude_flags():
    """KRAKEN_CLAUDE_FLAGS as bash expands it — the single source."""
    out = subprocess.run(
        ["bash", "-c", '. "$0"; printf "%s\\0" "${KRAKEN_CLAUDE_FLAGS[@]}"', LIB_CLAUDE],
        capture_output=True, text=True, check=True).stdout
    return out.split("\0")[:-1]


class AgentHarnessClaudeDriverTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.state, "record")
        os.makedirs(self.record)
        self.bin_dir = os.path.join(self.state, "bin")
        os.makedirs(self.bin_dir)
        fake = os.path.join(self.bin_dir, "claude")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)

    def read(self, name):
        with open(os.path.join(self.record, name), encoding="utf-8") as f:
            return f.read()

    def test_claude_driver_runs_the_loops_invocation_in_the_work_repo(self):
        script = ('. "%s"\n'
                  'setup_work_repo\n'
                  'run_unleash "EXTRA-CONTEXT-LINE"\n'
                  'echo "rc=$?"\n'
                  'echo "work=$WORK_DIR"\n' % LIB_AGENT)
        env = self.base_env({"KRAKEN_AGENT_CLI": "claude", "RECORD": self.record})
        env["PATH"] = self.bin_dir + os.pathsep + env["PATH"]
        env.pop("KRAKEN_STATE_DIR", None)
        proc = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        facts = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        self.assertTrue(os.path.isfile(os.path.join(self.record, "args")),
                        "the harness never invoked claude: " + proc.stdout + proc.stderr)
        self.assertEqual(facts.get("rc"), "0", proc.stdout + proc.stderr)
        self.assertEqual(os.path.realpath(self.read("cwd").strip()),
                         os.path.realpath(facts["work"]),
                         "claude must run in the scratch work repo")

        args = self.read("args").split("\0")[:-1]
        self.assertEqual(args[0], "-p")
        self.assertEqual(
            args[1],
            "/kraken:unleash stub-owner/tasks --worker-name %s --project %s --once"
            "\n\nEXTRA-CONTEXT-LINE" % (self.harness_var("WORKER"), self.harness_var("PROJECT")))
        self.assertEqual(args[2:4], ["--plugin-dir", ROOT])
        flags = claude_flags()
        self.assertEqual(args[4:4 + len(flags)], flags,
                         "the harness drifted from lib-claude-drain.sh's flags")
        self.assertNotIn("--dangerously-skip-permissions", args)

    def harness_var(self, name):
        """A value lib-agent.sh sets, as it sets it."""
        return subprocess.run(["bash", "-c", '. "$0" >/dev/null 2>&1; printf %%s "$%s"' % name,
                               LIB_AGENT], cwd=ROOT, env=self.base_env(),
                              capture_output=True, text=True).stdout


if __name__ == "__main__":
    unittest.main()
