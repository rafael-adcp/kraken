#!/usr/bin/env python3
"""Harness e2e on GitHub Copilot CLI: the real `copilot`, in BYOK offline mode
(COPILOT_PROVIDER_BASE_URL + COPILOT_OFFLINE=true), against the scripted fake
model — see e2e_base.py for the shared world.

Copilot's side of the wiring: scripts/kraken-loop.sh and the prompt and deny
rules it takes from scripts/lib-copilot-drain.sh (the test_agent_harness_copilot
conformance test pins only the argv a fake `copilot` receives; this runs the
real one), Copilot's shell tool and permission layer, and the plugin's
hooks.json loaded with --plugin-dir, as scripts/kraken-copilot.sh does when no
kraken plugin is installed.

Needs `copilot` on PATH (npm install -g @github/copilot).
"""
import os
import subprocess
import unittest

from e2e_base import (COORD, ISSUE, NEXT_ACTION, PROJECT, ROOT, WORK_SLUG, WORKER,
                      E2ETest, deliver_script, model_server, require_cli)

LOOP = os.path.join(ROOT, "scripts", "kraken-loop.sh")
LIB_DRAIN = os.path.join(ROOT, "scripts", "lib-copilot-drain.sh")
COAUTHOR = "Co-Authored-By: GitHub Copilot <noreply@github.com>"
COPILOT = None


def _byok(url, home):
    """Copilot in BYOK offline mode: the fake model, no GitHub login, no
    telemetry, no built-in GitHub MCP server, no auto-update, own config dir."""
    return {"COPILOT_OFFLINE": "true", "COPILOT_PROVIDER_BASE_URL": url + "/v1",
            "COPILOT_PROVIDER_API_KEY": "fake-key", "COPILOT_MODEL": model_server.MODEL,
            "HOME": home, "COPILOT_HOME": os.path.join(home, ".copilot")}


def setUpModule():
    global COPILOT
    COPILOT = require_cli("copilot", _byok, lambda cli: [cli, "-p", "probe", "--no-auto-update"])


class CopilotE2ETest(E2ETest):
    def cli_env(self, model_url):
        return _byok(model_url, self.home)

    def run_loop_once(self, turns):
        """One drain pass exactly as an operator runs it: scripts/kraken-loop.sh."""
        return self.play(turns, ["bash", LOOP, COORD, "--worker-name", WORKER,
                                 "--project", PROJECT, "--work-dir", self.work, "--once"])

    def run_with_plugin(self, turns, prompt):
        return self.play(turns, [COPILOT, "-p", prompt, "--plugin-dir", ROOT, "--add-dir", ROOT,
                                 "--allow-all-tools", "--no-ask-user", "--no-auto-update"])

    def expected_prompt(self):
        return subprocess.run(
            ["bash", "-c", '. "$1"; kraken_copilot_prompt "$2" "$3" "$4" "$5" "$6"', "_",
             LIB_DRAIN, COORD, PROJECT, WORKER, os.path.realpath(self.work), ROOT],
            capture_output=True, text=True, check=True).stdout.strip()


class LoopDrainTests(CopilotE2ETest):
    """scripts/kraken-loop.sh --once, a whole task, claim to draft PR."""

    def test_drain_pass_carries_a_task_to_a_draft_pr(self):
        proc, requests = self.run_loop_once(deliver_script(COAUTHOR))
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertGreaterEqual(len(requests), 5, "copilot stopped before the script ended" + self.detail(requests))
        # The prompt the model saw carries the loop's, byte for byte (Copilot
        # prepends its own <current_datetime> block to the user turn).
        self.assertIn(self.expected_prompt(), self.user_text(requests[0]),
                      "the drain prompt reaching the model drifted from lib-copilot-drain.sh")
        self.assert_delivered(requests, COAUTHOR)

    def test_loop_releases_a_lease_the_drain_abandoned(self):
        # The model claims, then walks away. The loop's own release-on-exit
        # (not a hook — the loop runs no plugin) must hand the task back.
        proc, requests = self.run_loop_once([{"bash": NEXT_ACTION}, {"say": "Stopping here."}])
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assert_claimed_then_released("copilot exited mid-drain", requests)


class DenyRuleTests(CopilotE2ETest):
    """SKILL.md's Authorization boundaries, enforced by Copilot's permission
    layer under the loop's own flags — even with --allow-all-tools."""

    FORBIDDEN = [
        ("gh pr merge 1 --admin --squash", "pr merge"),
        ("gh repo delete %s --yes" % WORK_SLUG, "repo delete"),
        ("gh issue close %d --repo %s" % (ISSUE, COORD), "issue close"),
    ]

    def test_forbidden_commands_never_reach_gh(self):
        self.mk_pr(1, "OPEN")
        turns = [{"bash": cmd} for cmd, _ in self.FORBIDDEN]
        # A control: an allowed gh call in the same session does reach the stub,
        # so "nothing in the log" means denied, not "the stub was never on PATH".
        turns.append({"bash": "gh pr view https://github.com/%s/pull/1 --json state" % WORK_SLUG})
        turns.append({"say": "done"})
        self.truncate_log()
        proc, requests = self.run_loop_once(turns)
        self.assertEqual(proc.returncode, 0, self.detail(requests))

        log = self.log_text()
        self.assertIn("pr view", log, "control gh call never reached the stub" + self.detail(requests))
        for cmd, needle in self.FORBIDDEN:
            self.assertNotIn(needle, log, "%r ran despite the deny rule" % cmd + self.detail(requests))
        results = self.tool_results(requests)
        for i in range(1, len(self.FORBIDDEN) + 1):
            self.assertRegex(results.get("call_%d" % i, ""), r"(?i)denied|not allowed|permission",
                             "call %d was not reported as denied" % i + self.detail(requests))
        self.assertEqual(self.labels(ISSUE), ["kraken-task", "project:" + PROJECT])


class PluginHookTests(CopilotE2ETest):
    """The plugin's hooks.json, loaded by Copilot CLI, fires SessionEnd with the
    session id kraken.py recorded (#173)."""

    def test_session_end_hook_releases_the_sessions_claim(self):
        # At a real API's pace the release takes seconds — longer than a
        # harness gives a SessionEnd hook it knows nothing about.
        self.at_real_api_speed()
        proc, requests = self.run_with_plugin([{"bash": NEXT_ACTION}, {"say": "Stopping here."}],
                                              "Do one kraken drain pass.")
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assert_claimed_then_released("session ended", requests)

    def test_session_end_leaves_another_sessions_claim_alone(self):
        # A claim made outside this Copilot session (a bare shell: no session
        # recorded) must survive the session's end — the lease TTL covers it.
        self.assertEqual(self.kraken("claim", COORD, ISSUE, WORKER).rc, 0)
        proc, requests = self.run_with_plugin([{"say": "Nothing to do."}], "Say hi.")
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertTrue(self.has_label(ISSUE, "in-progress"),
                        "SessionEnd released a claim it does not own" + self.detail(requests))
        self.assertTrue(os.path.exists(self.claim_state_file(WORKER)))


if __name__ == "__main__":
    unittest.main()
