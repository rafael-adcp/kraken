#!/usr/bin/env python3
"""Harness e2e on Pi: the real `pi`, pointed at the scripted fake model through
its own `models.json` + `settings.json` in an isolated PI_CODING_AGENT_DIR — see
e2e_base.py for the shared world.

Pi's side of the wiring: scripts/kraken-loop.sh --harness pi (its argv, the
`/skill:unleash` invocation, `-e` of the extension), Pi's bash tool, and the
kraken extension (extensions/pi/) — the deny check on `gh` commands and the
session_shutdown release scoped by PI_SESSION_ID. The extension is exercised on
both of its load paths: the loop's `-e <file>`, and a directory `-e <kraken>`,
which follows Pi's package discovery exactly as `pi install git:...` does — the
path #188 found silently empty before extensions/pi/index.ts existed.

Needs `pi` on PATH (npm install -g @earendil-works/pi-coding-agent).
"""
import json
import os
import unittest

from e2e_base import (COORD, ISSUE, NEXT_ACTION, ROOT, WORK_SLUG, WORKER,
                      E2ETest, deliver_script, model_server, require_cli)

LOOP = os.path.join(ROOT, "scripts", "kraken-loop.sh")
PROVIDER = "fake"
COAUTHOR = "Co-Authored-By: Pi (%s) <noreply@pi.dev>" % model_server.MODEL
PI = None


def _pi_env(url, home):
    """Pi's own config, isolated: the fake model as an OpenAI-compatible
    provider and the startup default (the loop passes no --model), no
    network, no version check, no telemetry."""
    agent_dir = os.path.join(home, ".pi", "agent")
    os.makedirs(agent_dir, exist_ok=True)
    model = {"id": model_server.MODEL, "name": model_server.MODEL, "reasoning": False,
             "input": ["text"], "contextWindow": 128000, "maxTokens": 4096,
             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    with open(os.path.join(agent_dir, "models.json"), "w", encoding="utf-8") as f:
        json.dump({"providers": {PROVIDER: {"baseUrl": url + "/v1", "api": "openai-completions",
                                            "apiKey": "fake-key", "models": [model]}}}, f)
    with open(os.path.join(agent_dir, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({"defaultProvider": PROVIDER, "defaultModel": model_server.MODEL}, f)
    return {"HOME": home, "PI_CODING_AGENT_DIR": agent_dir, "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}


def setUpModule():
    global PI
    PI = require_cli("pi", _pi_env, lambda cli: [cli, "-p", "--no-session", "probe"])


class PiE2ETest(E2ETest):
    def setUp(self):
        super().setUp()
        # Pi strips an inherited PI_SESSION_ID itself; the suite may run
        # inside a Pi session, and the CLI under test must start its own.
        os.environ.pop("PI_SESSION_ID", None)

    def cli_env(self, model_url):
        return _pi_env(model_url, self.home)

    def run_loop_once(self, turns):
        """One drain pass exactly as an operator runs it: scripts/kraken-loop.sh."""
        return self.play(turns, ["bash", LOOP, COORD, "--worker-name", WORKER, "--project", "x",
                                 "--work-dir", self.work, "--harness", "pi", "--once"])

    def run_as_package(self, turns, prompt):
        """Pi with the kraken checkout as a DIRECTORY extension: Pi's package
        discovery (docs/packages.md), the shape of `pi install git:...`."""
        return self.play(turns, [PI, "-p", "--session-dir", os.path.join(self.scratch, "sessions"),
                                 "-e", ROOT, prompt])


class LoopDrainTests(PiE2ETest):
    """scripts/kraken-loop.sh --harness pi --once, a whole task, claim to draft PR."""

    def test_drain_pass_carries_a_task_to_a_draft_pr(self):
        proc, requests = self.run_loop_once(deliver_script(COAUTHOR))
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertGreaterEqual(len(requests), 5, "pi stopped before the script ended" + self.detail(requests))
        # The skill was loaded and its three arguments reached the model (1.0).
        first = self.user_text(requests[0])
        self.assertIn("--worker-name %s" % WORKER, first, self.detail(requests))
        self.assertIn(COORD, first)
        self.assert_delivered(requests, COAUTHOR)

    def test_an_abandoned_lease_is_released_when_pi_exits(self):
        # Two releases race here, both correct: the extension's session_shutdown
        # (the loop loads it with -e; it releases detached, "session ended") and
        # the loop's own release-on-exit ("pi exited mid-drain"). Whichever
        # lands first frees the task; the other finds nothing left to free.
        proc, requests = self.run_loop_once([{"bash": NEXT_ACTION}, {"say": "Stopping here."}])
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assert_claimed_then_released("", requests)
        self.assertRegex(self.last_comment(ISSUE), r"session ended|pi exited mid-drain")


class DenyRuleTests(PiE2ETest):
    """SKILL.md's Authorization boundaries, enforced by the extension's
    tool_call check (extensions/pi/deny.py) — the shell half of the rules
    Copilot gets from KRAKEN_COPILOT_FLAGS."""

    FORBIDDEN = [
        ("gh pr merge 1 --admin --squash", "pr merge"),
        ("cd /tmp && FOO=1 gh  repo delete %s --yes" % WORK_SLUG, "repo delete"),
        ("gh issue close %d --repo %s" % (ISSUE, COORD), "issue close"),
    ]

    def assert_denied(self, proc, requests):
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        log = self.log_text()
        # The control: an allowed gh call does reach the stub, so an empty log
        # means "refused", not "the stub was never on PATH".
        self.assertIn("pr view", log, "control gh call never reached the stub" + self.detail(requests))
        for cmd, needle in self.FORBIDDEN:
            self.assertNotIn(needle, log, "%r ran despite the deny check" % cmd + self.detail(requests))
        results = self.tool_results(requests)
        for i in range(1, len(self.FORBIDDEN) + 1):
            self.assertIn("kraken", results.get("call_%d" % i, ""),
                          "call %d was not refused by the kraken extension" % i + self.detail(requests))
        self.assertEqual(self.labels(ISSUE), ["kraken-task", "project:x"])

    def turns(self):
        turns = [{"bash": cmd} for cmd, _ in self.FORBIDDEN]
        turns.append({"bash": "gh pr view https://github.com/%s/pull/1 --json state" % WORK_SLUG})
        turns.append({"say": "done"})
        return turns

    def test_the_loop_refuses_forbidden_commands(self):
        self.mk_pr(1, "OPEN")
        self.truncate_log()
        self.assert_denied(*self.run_loop_once(self.turns()))

    def test_a_package_install_refuses_forbidden_commands(self):
        # #188: through index.ts, not the kraken.ts path the loop names.
        self.mk_pr(1, "OPEN")
        self.truncate_log()
        self.assert_denied(*self.run_as_package(self.turns(), "Do one kraken drain pass."))


class SessionShutdownTests(PiE2ETest):
    """The extension's session_shutdown release, on the package-install path,
    scoped to the claims this Pi session recorded (#173, #188)."""

    def test_session_end_releases_the_sessions_claim(self):
        self.at_real_api_speed()
        proc, requests = self.run_as_package([{"bash": NEXT_ACTION}, {"say": "Stopping here."}],
                                             "Do one kraken drain pass.")
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assert_claimed_then_released("session ended", requests)

    def test_session_end_leaves_another_sessions_claim_alone(self):
        # A claim from a bare shell records no session: nothing session-scoped
        # may free it; the lease TTL covers it.
        self.assertEqual(self.kraken("claim", COORD, ISSUE, WORKER).rc, 0)
        proc, requests = self.run_as_package([{"say": "Nothing to do."}], "Say hi.")
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertTrue(self.has_label(ISSUE, "in-progress"),
                        "session_shutdown released a claim it does not own" + self.detail(requests))
        self.assertTrue(os.path.exists(self.claim_state_file(WORKER)))


if __name__ == "__main__":
    unittest.main()
