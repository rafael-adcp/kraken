#!/usr/bin/env python3
"""Harness e2e on Claude Code: the real `claude`, pointed at the scripted fake
model over the Anthropic Messages wire (ANTHROPIC_BASE_URL) — see e2e_base.py
for the shared world.

Claude Code's side of the wiring: the plugin loaded with --plugin-dir, the
`/kraken:unleash` slash command expanding SKILL.md into the model's prompt
(the invocation an operator — and tests/agent/lib-agent.sh's drive_claude —
runs), the Bash tool under --dangerously-skip-permissions, and both lifecycle
hooks in hooks.json: SessionEnd, and StopFailure on a rate limit, which the
fake model triggers with a scripted HTTP 429.

StopFailure is exercised both in an INTERACTIVE session (a pseudo-terminal),
the case the hook exists for — a usage limit kills the turn but not the session
— and under `claude -p`, where the process exits on the failed turn and kills
hooks still running (verified on 2.1.283: a 2-second StopFailure hook never
finished). Both hooks survive that only because they release DETACHED, and the
SessionEnd test runs the stub at a real API's pace to hold them to it: Claude
Code gives SessionEnd hooks 1.5s in total, and a plugin cannot raise it.

Needs `claude` on PATH (npm install -g @anthropic-ai/claude-code). No login and
no API key: the key it is handed is fake, and only the fake model sees it.
"""
import json
import os
import select
import subprocess
import time
import unittest

from e2e_base import (COORD, ISSUE, NEXT_ACTION, PROJECT, ROOT, WORKER, E2ETest,
                      deliver_script, model_server, require_cli)

COAUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"
UNLEASH = "/kraken:unleash %s --worker-name %s --project %s --once" % (COORD, WORKER, PROJECT)
# A line of SKILL.md's body: in the model's prompt only if the plugin loaded and
# the slash command expanded the skill.
SKILL_MARK = "You are a **tentacle**"
RATE_LIMIT = {"fail": {"status": 429, "type": "rate_limit_error",
                       "message": "This request would exceed your account's rate limit."}}
CLAUDE = None


def _fake_api(url, home):
    """Claude Code on the fake model: a fake key, a model id this CLI knows (so
    it applies its real defaults), no retries (a scripted 429 must end the turn
    now, not after minutes of backoff), no non-essential traffic, own config."""
    return {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": "fake-key",
            "ANTHROPIC_MODEL": "claude-sonnet-5", "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1",
            "HOME": home, "CLAUDE_CONFIG_DIR": os.path.join(home, ".claude")}


def setUpModule():
    global CLAUDE
    CLAUDE = require_cli("claude", _fake_api, lambda cli: [cli, "-p", "probe"])


class ClaudeE2ETest(E2ETest):
    def cli_env(self, model_url):
        env = _fake_api(model_url, self.home)
        # Only the fake key may authenticate: an inherited OAuth token or
        # provider switch would route the run around the fake model.
        for var in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            env[var] = ""
        return env

    def unleash(self, turns, prompt=UNLEASH):
        """One `--once` drain, invoked the way an operator (and the agent
        harness's drive_claude) does: the slash command, the repo as plugin."""
        return self.play(turns, [CLAUDE, "-p", prompt, "--plugin-dir", ROOT,
                                 "--dangerously-skip-permissions", "--max-turns", "20"])

    def unleash_interactive(self, turns, until, timeout=60):
        """The same slash command in an INTERACTIVE session on a pseudo-terminal,
        its first-run dialogs (theme, folder trust, API key, bypass mode)
        pre-answered in the isolated config. Runs until `until()` holds or
        `timeout` passes, then ends the session. Returns (requests, screen)."""
        httpd, url = model_server.start(turns, self.record)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        env = self.env(url)
        cfg = env["CLAUDE_CONFIG_DIR"]
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as f:
            json.dump({"hasCompletedOnboarding": True, "theme": "dark",
                       "bypassPermissionsModeAccepted": True,
                       "customApiKeyResponses": {"approved": [env["ANTHROPIC_API_KEY"][-20:]],
                                                 "rejected": []},
                       "projects": {os.path.realpath(self.work): {
                           "hasTrustDialogAccepted": True,
                           "hasCompletedProjectOnboarding": True}}}, f)
        with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as f:
            json.dump({"skipDangerousModePermissionPrompt": True}, f)
        argv = [CLAUDE, "--plugin-dir", ROOT, "--dangerously-skip-permissions", UNLEASH]
        # openpty + Popen, not pty.fork(): this process runs server threads, and
        # a bare fork of a threaded process can deadlock the child.
        fd, tty = os.openpty()
        proc = subprocess.Popen(argv, cwd=self.work, env=env, stdin=tty, stdout=tty, stderr=tty,
                                start_new_session=True)
        os.close(tty)
        screen = b""
        deadline = time.time() + timeout
        try:
            while time.time() < deadline and not until():
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        screen += os.read(fd, 65536)
                    except OSError:
                        break
        finally:
            proc.kill()
            proc.wait()
            os.close(fd)
        self.transcript = screen.decode("utf-8", "replace")
        return model_server.read_record(self.record), self.transcript


class UnleashDrainTests(ClaudeE2ETest):
    def test_unleash_once_carries_a_task_to_a_draft_pr(self):
        proc, requests = self.unleash(deliver_script(COAUTHOR))
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        agent = [r for r in requests if r.get("tools")]
        self.assertGreaterEqual(len(agent), 5, "claude stopped before the script ended" + self.detail(requests))
        first = self.user_text(agent[0])
        self.assertIn(SKILL_MARK, first,
                      "SKILL.md never reached the model: the plugin or the slash command broke")
        self.assertIn("--worker-name %s --project %s --once" % (WORKER, PROJECT), first,
                      "the /kraken:unleash arguments never reached the model")
        self.assertIn("Bash", [t.get("name") for t in agent[0]["tools"]])
        self.assert_delivered(requests, COAUTHOR)


class PluginHookTests(ClaudeE2ETest):
    def test_session_end_hook_releases_the_sessions_claim(self):
        # At a real API's pace the release takes seconds — longer than a
        # harness gives a SessionEnd hook it knows nothing about.
        self.at_real_api_speed()
        proc, requests = self.unleash([{"bash": NEXT_ACTION}, {"say": "Stopping here."}])
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assert_claimed_then_released("session ended", requests)

    def test_session_end_leaves_another_sessions_claim_alone(self):
        # A claim made outside this session (a bare shell: no session recorded)
        # must survive the session's end — the lease TTL covers it.
        self.assertEqual(self.kraken("claim", COORD, ISSUE, WORKER).rc, 0)
        proc, requests = self.unleash([{"say": "Nothing to do."}], prompt="Say hi.")
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertTrue(self.has_label(ISSUE, "in-progress"),
                        "SessionEnd released a claim it does not own" + self.detail(requests))
        self.assertTrue(os.path.exists(self.claim_state_file(WORKER)))

    def test_stop_failure_headless_releases_before_the_process_exits(self):
        # Under `claude -p` a rate-limited turn ends the process at once; the
        # detached release must land anyway.
        self.at_real_api_speed()
        proc, requests = self.unleash([{"bash": NEXT_ACTION}, RATE_LIMIT])
        self.assertNotEqual(proc.returncode, 0, "a rate-limited turn must not exit clean" + self.detail())
        self.assertIn("429", self.transcript)
        self.assert_claimed_then_released("usage limit", requests)
        self.assertTrue(os.path.isfile(os.path.join(self.kraken_state_dir, "wake-retry")),
                        "StopFailure did not stamp the wake-retry flag" + self.detail(requests))

    def test_stop_failure_on_rate_limit_releases_and_stamps_the_retry_flag(self):
        # The model claims, then the account hits its usage limit mid-drain: the
        # turn dies with a 429, which fires StopFailure (matcher rate_limit) —
        # while the session itself lives on, so SessionEnd cannot be what frees it.
        flag = os.path.join(self.kraken_state_dir, "wake-retry")
        requests, screen = self.unleash_interactive(
            [{"bash": NEXT_ACTION}, RATE_LIMIT],
            until=lambda: os.path.isfile(flag) and not os.path.exists(self.claim_state_file(WORKER)))
        self.assertIn("429", screen, "the scripted rate limit never surfaced" + self.detail(requests))
        self.assert_claimed_then_released("usage limit", requests)
        self.assertTrue(os.path.isfile(flag),
                        "StopFailure did not stamp the wake-retry flag" + self.detail(requests))

if __name__ == "__main__":
    unittest.main()
