#!/usr/bin/env python3
"""Unit tests for extensions/pi/deny.py — the matcher the kraken Pi extension's
`tool_call` handler runs on every `bash` command. It refuses the three shell
commands Copilot CLI's deny rules refuse (scripts/lib-copilot-drain.sh):
`gh pr merge`, `gh repo delete`, `gh issue close`. Defense in depth, not a
sandbox — the "not seen" cases below are pinned so a change to them is a
decision, not an accident.

Stdlib only, no Node: the TypeScript side is an adapter that pipes the command
in and maps exit 2 to `{ block: true, reason }`.
"""
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DENY = os.path.abspath(os.path.join(HERE, "..", "..", "extensions", "pi", "deny.py"))
sys.path.insert(0, os.path.dirname(DENY))

import deny  # noqa: E402

BLOCKED = [
    "gh pr merge",  # no number still merges the current branch's PR
    "gh pr merge 1",
    "gh pr merge 1 --admin --squash",
    "gh repo delete acme/app --yes",
    "gh issue close 7",
    "  gh   pr    merge   1  ",
    "\tgh\tissue\tclose 7",
    "GH_TOKEN=x gh pr merge 1",
    "A=1 B=2 gh repo delete acme/app",
    "env GH_TOKEN=x gh pr merge 1",
    "env -i PATH=/usr/bin gh issue close 7",
    "command gh pr merge 1",
    "cd work && gh pr merge 1",
    "git push; gh pr merge 1",
    "false || gh issue close 7",
    "echo y | gh repo delete acme/app",
    "sleep 1 & gh pr merge 1",
    "echo ok\ngh issue close 7",
    "/usr/bin/gh pr merge 1",
    "gh.exe pr merge 1",
    '"gh" pr merge 1',
    "(gh pr merge 1)",
    "echo $(gh pr merge 1)",
    "echo `gh issue close 7`",
    "if gh pr merge 1; then echo merged; fi",
    'bash -c "gh pr merge 1"',
    "sh -lc 'cd x && gh repo delete acme/app --yes'",
    "bash -c \"sh -c 'gh issue close 7'\"",
    "echo a#b; gh pr merge 1",
    'gh pr merge "unbalanced',
]

ALLOWED = [
    "",
    "gh pr view 1",
    "gh pr create --draft --title t --body b",
    "gh issue view 7",
    "gh issue list --label kraken-task",
    "gh repo view acme/app",
    'echo "gh pr merge 1"',
    "git commit -m 'never gh pr merge here'",
    "gh pr comment 1 --body 'please gh pr merge when ready'",
    "echo hi # gh pr merge 1",
    "grep -rn 'gh issue close' .",
    "python3 kraken.py release acme/tasks 7 w1 'gh repo delete'",
    "ghx pr merge 1",
    "gh pr",
]

NOT_SEEN = [
    'GH=gh; $GH pr merge 1',  # a variable
    'echo "$(gh pr merge 1)"',  # substitution inside double quotes
    "eval 'gh pr merge 1'",
    "python3 -c 'import os; os.system(\"gh pr merge 1\")'",
    "gh api -X PUT repos/acme/app/pulls/1/merge",
]


class DenyMatcherTests(unittest.TestCase):
    def test_blocked(self):
        for command in BLOCKED:
            with self.subTest(command=command):
                self.assertIsNotNone(deny.denied(command), "not blocked")

    def test_allowed(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(deny.denied(command), "wrongly blocked")

    def test_not_a_sandbox(self):
        # Documented blind spots (deny.py docstring). SKILL.md, the human who
        # merges and the lease are the real controls.
        for command in NOT_SEEN:
            with self.subTest(command=command):
                self.assertIsNone(deny.denied(command))

    def test_reason_names_the_command(self):
        self.assertIn("`gh pr merge`", deny.denied("gh pr merge 1"))
        self.assertIn("`gh repo delete`", deny.denied("gh repo delete x"))
        self.assertIn("`gh issue close`", deny.denied("gh issue close 1"))

    def test_same_commands_as_copilots_deny_rules(self):
        # Keep in step with KRAKEN_COPILOT_FLAGS' three shell(...) rules.
        lib = os.path.join(HERE, "..", "..", "scripts", "lib-copilot-drain.sh")
        with open(lib, encoding="utf-8") as f:
            text = f.read()
        rules = set(re.findall(r'--deny-tool=shell\(gh (\w+) (\w+):\*\)', text))
        self.assertEqual(rules, set(deny.DENIED))

    def run_cli(self, command):
        return subprocess.run([sys.executable, DENY], input=command,
                              capture_output=True, text=True)

    def test_cli_contract(self):
        # What kraken.ts relies on: exit 0 allows, exit 2 blocks with the
        # reason on stdout.
        ok = self.run_cli("gh pr view 1")
        self.assertEqual((ok.returncode, ok.stdout), (0, ""))
        no = self.run_cli("cd w && gh pr merge 1 --admin")
        self.assertEqual(no.returncode, 2)
        self.assertIn("`gh pr merge`", no.stdout)


if __name__ == "__main__":
    unittest.main()
