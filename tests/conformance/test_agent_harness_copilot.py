#!/usr/bin/env python3
"""The agent-behavior harness's Copilot driver (tests/agent/lib-agent.sh with
KRAKEN_AGENT_CLI=copilot), proven token-free with a fake `copilot` on PATH.

The real-model scenarios only mean something if the harness drives Copilot the
way an operator does, so this pins the wiring itself: the SAME prompt and flags
scripts/kraken-loop.sh uses (single-sourced in scripts/lib-copilot-drain.sh),
run in the scratch work repo, with the gh-stub still first on PATH — and with
GH_TOKEN/GITHUB_TOKEN stripped, because Copilot CLI would authenticate the model
with the stub's fake token instead of the operator's stored login."""
import os
import stat
import subprocess
import unittest

from harness import KrakenConformanceTest, ROOT

LIB_AGENT = os.path.join(ROOT, "tests", "agent", "lib-agent.sh")
LIB_DRAIN = os.path.join(ROOT, "scripts", "lib-copilot-drain.sh")

FAKE_COPILOT = r'''#!/usr/bin/env bash
pwd > "$RECORD/cwd"
printf '%s\0' "$@" > "$RECORD/args"
{ echo "GH_TOKEN=${GH_TOKEN-<unset>}"; echo "GITHUB_TOKEN=${GITHUB_TOKEN-<unset>}"; } > "$RECORD/tokens"
command -v gh > "$RECORD/gh-path"
gh auth token > "$RECORD/gh-token"
echo "$KRAKEN_STATE_DIR" > "$RECORD/state-dir"
'''


class AgentHarnessCopilotDriverTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.record = os.path.join(self.state, "record")
        os.makedirs(self.record)
        self.bin_dir = os.path.join(self.state, "bin")
        os.makedirs(self.bin_dir)
        fake = os.path.join(self.bin_dir, "copilot")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_COPILOT)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        # A tripwire `claude`: this suite is token-free, so a driver that falls
        # back to Claude must fail loudly here, never reach a real model.
        tripwire = os.path.join(self.bin_dir, "claude")
        with open(tripwire, "w", encoding="utf-8") as f:
            f.write('#!/usr/bin/env bash\ntouch "$RECORD/claude-called"\nexit 97\n')
        os.chmod(tripwire, os.stat(tripwire).st_mode | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)

    def drive(self):
        """Source lib-agent.sh with the Copilot driver and run one scenario's
        invocation; print what the harness itself believes it set up."""
        script = (
            '. "%s"\n'
            'setup_work_repo\n'
            'run_unleash "EXTRA-CONTEXT-LINE"\n'
            'echo "rc=$?"\n'
            'echo "work=$WORK_DIR"\n'
            'echo "stub=$GH_STUB"\n'
            'echo "scratch=$SCRATCH"\n'
            'kraken_copilot_prompt "$COORD" "$PROJECT" "$WORKER" "$WORK_DIR" "$ROOT" > "$RECORD/expected-prompt"\n'
            % LIB_AGENT)
        env = self.base_env({"KRAKEN_AGENT_CLI": "copilot", "RECORD": self.record,
                             "GITHUB_TOKEN": "operator-token"})
        env["PATH"] = self.bin_dir + os.pathsep + env["PATH"]
        env.pop("KRAKEN_STATE_DIR", None)
        proc = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=120)
        facts = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        return proc, facts

    def tearDown(self):
        self.assertFalse(os.path.exists(os.path.join(self.record, "claude-called")),
                         "the Copilot driver invoked claude")
        super().tearDown()

    def read(self, name):
        with open(os.path.join(self.record, name), encoding="utf-8") as f:
            return f.read()

    def args(self):
        return self.read("args").split("\0")[:-1]

    def test_copilot_driver_runs_the_loops_invocation_in_the_work_repo(self):
        proc, facts = self.drive()
        self.assertTrue(os.path.isfile(os.path.join(self.record, "args")),
                        "the harness never invoked copilot: " + proc.stdout + proc.stderr)
        self.assertEqual(facts.get("rc"), "0", proc.stdout + proc.stderr)
        self.assertEqual(os.path.realpath(self.read("cwd").strip()),
                         os.path.realpath(facts["work"]),
                         "copilot must run in the scratch work repo")

        args = self.args()
        prompt = args[args.index("-p") + 1]
        expected = self.read("expected-prompt").rstrip("\n")
        self.assertTrue(prompt.startswith(expected),
                        "the harness prompt is not the loop's prompt (lib-copilot-drain.sh)")
        self.assertIn("EXTRA-CONTEXT-LINE", prompt, "the scenario's extra context was dropped")
        self.assertIn("--add-dir", args)
        self.assertEqual(os.path.realpath(args[args.index("--add-dir") + 1]),
                         os.path.realpath(ROOT), "--add-dir must grant the kraken checkout")
        for flag in ("--allow-all-tools", "--no-ask-user"):
            self.assertIn(flag, args, "the loop's permission flag %s is missing" % flag)
        # Copilot's built-in GitHub MCP server talks to real GitHub with the
        # operator's login — it must not route around the seeded stub.
        self.assertIn("--disable-builtin-mcps", args)

    def test_copilot_driver_strips_tokens_and_keeps_the_stub_first(self):
        _proc, facts = self.drive()
        tokens = self.read("tokens")
        self.assertIn("GH_TOKEN=<unset>", tokens,
                      "GH_TOKEN would override copilot's stored login")
        self.assertIn("GITHUB_TOKEN=<unset>", tokens,
                      "GITHUB_TOKEN would override copilot's stored login")
        self.assertEqual(os.path.dirname(self.read("gh-path").strip()), facts["stub"],
                         "the gh-stub must shadow the real gh for the model's commands")
        # With no token in the env, kraken.py falls back to `gh auth token` —
        # which the stub must answer, or every transition goes out tokenless.
        self.assertEqual(self.read("gh-token").strip(), "stub-token")

    def test_claim_state_is_isolated_in_the_scratch(self):
        # A real run's claim-<worker>.json must never land in the operator's
        # ~/.kraken, where their own SessionEnd hook or loop would act on it.
        _proc, facts = self.drive()
        state_dir = self.read("state-dir").strip()
        self.assertTrue(state_dir.startswith(facts["scratch"] + os.sep),
                        "KRAKEN_STATE_DIR (%r) is not inside the scenario scratch" % state_dir)

    def test_claude_stays_the_default_driver(self):
        with open(LIB_AGENT, encoding="utf-8") as f:
            self.assertIn('KRAKEN_AGENT_CLI:-claude', f.read())

    def test_unknown_driver_fails_loudly(self):
        env = self.base_env({"KRAKEN_AGENT_CLI": "gemini", "RECORD": self.record})
        env["PATH"] = self.bin_dir + os.pathsep + env["PATH"]
        proc = subprocess.run(["bash", "-c", '. "%s"; run_unleash; echo "reached=$?"' % LIB_AGENT],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("reached=", proc.stdout, "an unknown driver must not fall through")
        self.assertIn("unknown KRAKEN_AGENT_CLI", proc.stderr)


if __name__ == "__main__":
    unittest.main()
