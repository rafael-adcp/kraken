#!/usr/bin/env python3
"""Harness e2e: the REAL GitHub Copilot CLI, driven by a scripted fake model.

The conformance suite proves kraken.py's transitions with no agent in the loop,
and pins the Copilot wiring with a fake `copilot` that only records its argv
(test_agent_harness_copilot.py). The agent-behavior harness (tests/agent/) runs
the real CLI with a real model, but spends requests and is never run in CI. What
neither covers is the seam between them — does the CLI an operator runs actually
carry the drain through? This suite pins exactly that seam, token-free:

  * Copilot CLI runs in BYOK offline mode (COPILOT_PROVIDER_BASE_URL +
    COPILOT_OFFLINE=true) against tests/fake-model/model_server.py, which plays a
    fixed script of tool calls. No credentials, no network, deterministic.
  * Everything else is real: scripts/kraken-loop.sh, the prompt and deny rules
    in scripts/lib-copilot-drain.sh, Copilot's shell tool and permission layer,
    the plugin's hooks.json, kraken.py against the HTTP gh-stub, and a real git
    work repo with a local bare remote.

The model's judgment is NOT under test here — the script decides every step.
A failure means the wiring broke: the prompt never reached the model, a tool
call ran somewhere else than the work repo, the stub was not the `gh` the shell
saw, a deny rule stopped matching, or a hook stopped firing.

Needs `copilot` on PATH (npm install -g @github/copilot) and git. Absent either,
the suite skips — unless KRAKEN_E2E_REQUIRE=1 (CI sets it), where a missing CLI
is a failure, so a broken install can never pass as a green skip.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "conformance"))
sys.path.insert(0, os.path.join(HERE, "..", "fake-model"))

from harness import GH_STUB_DIR, KRAKEN, ROOT, KrakenConformanceTest  # noqa: E402
import model_server  # noqa: E402

LOOP = os.path.join(ROOT, "scripts", "kraken-loop.sh")
LIB_DRAIN = os.path.join(ROOT, "scripts", "lib-copilot-drain.sh")

COORD = "stub-owner/tasks"
WORK_SLUG = "stub-owner/work"
PROJECT = "x"
WORKER = "t1"
ISSUE = 1
BRANCH = "kraken/task-1"
COAUTHOR = "Co-Authored-By: GitHub Copilot <noreply@github.com>"
TIMEOUT = int(os.environ.get("KRAKEN_E2E_TIMEOUT", "180"))

COPILOT = shutil.which("copilot")
REQUIRED = os.environ.get("KRAKEN_E2E_REQUIRE") == "1"


def _unusable(reason):
    if REQUIRED:
        raise RuntimeError("KRAKEN_E2E_REQUIRE=1 but " + reason)
    raise unittest.SkipTest(reason)


def setUpModule():
    missing = [t for t in ("copilot", "git") if not shutil.which(t)]
    if missing:
        _unusable("%s not on PATH" % ", ".join(missing))
    # A `copilot` that cannot reach a localhost model at all — e.g. the Windows
    # build VS Code exposes to WSL — would fail every test for a reason that is
    # not the wiring under test. Probe once and say so instead.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="kraken-e2e-probe-") as tmp:
        record = os.path.join(tmp, "probe.jsonl")
        httpd, url = model_server.start([{"say": "ok"}], record)
        try:
            env = dict(os.environ, COPILOT_OFFLINE="true", COPILOT_PROVIDER_BASE_URL=url,
                       COPILOT_PROVIDER_API_KEY="fake-key", COPILOT_MODEL=model_server.MODEL,
                       HOME=tmp, COPILOT_HOME=os.path.join(tmp, ".copilot"))
            subprocess.run([COPILOT, "-p", "probe", "--no-auto-update"], cwd=tmp, env=env,
                           capture_output=True, timeout=TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            _unusable("%s did not run: %s" % (COPILOT, e))
        finally:
            httpd.shutdown()
            httpd.server_close()
        if not model_server.read_record(record):
            _unusable("%s never reached the local fake model (a Windows-side copilot "
                      "under WSL? install the Linux one: npm install -g @github/copilot)" % COPILOT)


class CopilotE2ETest(KrakenConformanceTest):
    """One scratch world per test: gh-stub state (HTTP API + `gh` CLI over the
    same files), a work repo with a bare remote, an isolated HOME/COPILOT_HOME,
    and a fake model playing the test's script."""

    def setUp(self):
        super().setUp()
        self.scratch = os.path.join(self.state, "e2e")
        self.home = os.path.join(self.scratch, "home")
        self.work = os.path.join(self.scratch, "work")
        self.bare = os.path.join(self.scratch, "work.git")
        self.record = os.path.join(self.scratch, "model-requests.jsonl")
        os.makedirs(self.home)
        self.mk_issue(ISSUE, "Add greet() to feature.txt", "kraken-task",
                      "project:" + PROJECT)
        self.mk_body(ISSUE, "## Goal\nAdd a greet line.\n\n## Acceptance\n- feature.txt greets.")
        self.setup_work_repo()

    # --- the world ----------------------------------------------------------

    def env(self, model_url):
        env = self.base_env({
            # The gh CLI stub — not base_env's tripwire — answers the model's
            # own `gh` commands, over the same state the HTTP stub serves.
            "GH_STUB_STATE": self.state,
            # Copilot in BYOK offline mode: the fake model, no GitHub login,
            # no telemetry, no built-in GitHub MCP server, no auto-update.
            "COPILOT_OFFLINE": "true",
            "COPILOT_PROVIDER_BASE_URL": model_url,
            "COPILOT_PROVIDER_API_KEY": "fake-key",
            "COPILOT_MODEL": model_server.MODEL,
            # Never the operator's Copilot config, installed plugins or git.
            "HOME": self.home,
            "COPILOT_HOME": os.path.join(self.home, ".copilot"),
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Kraken Worker",
            "GIT_AUTHOR_EMAIL": "worker@example.invalid",
            "GIT_COMMITTER_NAME": "Kraken Worker",
            "GIT_COMMITTER_EMAIL": "worker@example.invalid",
        })
        path = env["PATH"].split(os.pathsep)
        env["PATH"] = os.pathsep.join([GH_STUB_DIR] + [p for p in path if p and "no-gh" not in p])
        return env

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.work, capture_output=True,
                              text=True, env=self.env("http://unused"))

    def setup_work_repo(self):
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.bare], check=True,
                       env=self.env("http://unused"))
        os.makedirs(self.work)
        self.git("init", "-q", "-b", "main")
        with open(os.path.join(self.work, "feature.txt"), "w", encoding="utf-8") as f:
            f.write("placeholder\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed: initial commit")
        self.git("remote", "add", "origin", self.bare)
        self.git("push", "-q", "-u", "origin", "main")
        self.main_baseline = self.git("--git-dir", self.bare, "rev-parse", "main").stdout.strip()

    def play(self, turns, argv, cwd=None):
        """Serve `turns` as the model, run `argv` (the CLI or a script that runs
        it), and return (proc, requests-the-model-received)."""
        httpd, url = model_server.start(turns, self.record)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        proc = subprocess.run(argv, cwd=cwd or self.work, env=self.env(url),
                              capture_output=True, text=True, timeout=TIMEOUT)
        self.transcript = proc.stdout + proc.stderr
        return proc, model_server.read_record(self.record)

    def run_loop_once(self, turns):
        """One drain pass exactly as an operator runs it: scripts/kraken-loop.sh."""
        return self.play(turns, ["bash", LOOP, COORD, "--worker-name", WORKER,
                                 "--project", PROJECT, "--work-dir", self.work, "--once"])

    # --- reading what came back ---------------------------------------------

    def tool_results(self, requests):
        """The tool outputs Copilot sent back to the model, keyed by call id —
        what the "model" would have read after each scripted step."""
        out = {}
        for req in requests:
            for m in req.get("messages", []):
                if m.get("role") == "tool":
                    content = m.get("content")
                    if isinstance(content, list):
                        content = "".join(p.get("text", "") for p in content)
                    out[m.get("tool_call_id")] = content or ""
        return out

    def user_prompt(self, requests):
        msgs = requests[0]["messages"]
        user = [m for m in msgs if m["role"] == "user"][0]["content"]
        return user if isinstance(user, str) else "".join(p.get("text", "") for p in user)

    def expected_prompt(self):
        return subprocess.run(
            ["bash", "-c", '. "$1"; kraken_copilot_prompt "$2" "$3" "$4" "$5" "$6"', "_",
             LIB_DRAIN, COORD, PROJECT, WORKER, os.path.realpath(self.work), ROOT],
            capture_output=True, text=True, check=True).stdout.strip()

    def detail(self, requests=None):
        out = "\n--- copilot transcript ---\n" + self.transcript[-4000:]
        if requests is not None:
            out += "\n--- tool results ---\n" + json.dumps(self.tool_results(requests), indent=1)[-4000:]
        return out


class LoopDrainTests(CopilotE2ETest):
    """scripts/kraken-loop.sh --once, a whole task, claim to draft PR."""

    def deliver_script(self):
        trailer = "$(python3 %s contract task-trailer --repo %s --issue %d)" % (KRAKEN, COORD, ISSUE)
        commit_msg = "feat: greet in feature.txt\n\n%s\n%s" % (COAUTHOR, trailer)
        return [
            {"bash": "python3 %s next-action %s %s %s" % (KRAKEN, COORD, PROJECT, WORKER)},
            {"bash": ('pwd > .e2e-cwd && git checkout -q -b %s && echo "greet() { echo hello; }" '
                      '>> feature.txt && git commit -q -am "%s" && git push -q -u origin %s'
                      % (BRANCH, commit_msg, BRANCH))},
            {"bash": ('gh pr create --draft --repo %s --head %s --base main --title "Add greet" '
                      '--body "Closes the kraken task." > .e2e-pr-url' % (WORK_SLUG, BRANCH))},
            {"bash": ('printf "Added greet() to feature.txt." > "$HOME/result.md" && '
                      'python3 %s deliver %s %d %s "$HOME/result.md" "$(cat .e2e-pr-url)"'
                      % (KRAKEN, COORD, ISSUE, WORKER))},
            {"say": "Delivered task #%d as a draft PR." % ISSUE},
        ]

    def test_drain_pass_carries_a_task_to_a_draft_pr(self):
        proc, requests = self.run_loop_once(self.deliver_script())
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertGreaterEqual(len(requests), 5, "copilot stopped before the script ended" + self.detail(requests))

        # The prompt the model saw carries the loop's, byte for byte (Copilot
        # prepends its own <current_datetime> block to the user turn).
        self.assertIn(self.expected_prompt(), self.user_prompt(requests),
                         "the drain prompt reaching the model drifted from lib-copilot-drain.sh")
        # Tool calls ran in the work repo, not the kraken checkout (#175).
        with open(os.path.join(self.work, ".e2e-cwd"), encoding="utf-8") as f:
            self.assertEqual(os.path.realpath(f.read().strip()), os.path.realpath(self.work))
        # The envelope came back to the model through Copilot's shell tool.
        envelope = self.tool_results(requests).get("call_1", "")
        self.assertIn('"action"', envelope, "next-action's envelope never reached the model" + self.detail(requests))

        # Queue: delivered, with attribution, and the lease handed back.
        self.assertTrue(self.has_label(ISSUE, "awaiting-merge"), self.detail(requests))
        self.assertFalse(self.has_label(ISSUE, "in-progress"))
        self.assertIn('"type":"delivered"', self.last_comment(ISSUE))
        self.assertIn("/pull/1", self.last_comment(ISSUE), "delivered marker lacks the PR")
        self.assert_disclaimer(ISSUE, WORKER)
        self.assertFalse(os.path.exists(self.claim_state_file(WORKER)),
                         "a delivered task left its claim state file behind")

        # The PR: a draft, on the work repo, from the work branch.
        with open(os.path.join(self.state, "pr", "0001.json"), encoding="utf-8") as f:
            pr = json.load(f)
        self.assertTrue(pr["draft"], "the PR is not a draft")
        self.assertEqual((pr["repo"], pr["head"]), (WORK_SLUG, BRANCH))

        # The work repo: branch pushed with both trailers, main untouched.
        log = self.git("--git-dir", self.bare, "log", "-1", "--format=%B", BRANCH).stdout
        self.assertIn(COAUTHOR, log)
        self.assertIn("Kraken-Task:", log)
        self.assertEqual(self.git("--git-dir", self.bare, "rev-parse", "main").stdout.strip(),
                         self.main_baseline, "the default branch moved")

    def test_loop_releases_a_lease_the_drain_abandoned(self):
        # The model claims, then walks away. The loop's own release-on-exit
        # (not a hook — the loop runs no plugin) must hand the task back.
        proc, requests = self.run_loop_once([
            {"bash": "python3 %s next-action %s %s %s" % (KRAKEN, COORD, PROJECT, WORKER)},
            {"say": "Stopping here."},
        ])
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertIn('"type":"claim"', "\n".join(self.comment_bodies(ISSUE)),
                      "the scripted claim never landed" + self.detail(requests))
        self.assertIn('"type":"released"', self.last_comment(ISSUE), self.detail(requests))
        self.assertIn("copilot exited mid-drain", self.last_comment(ISSUE))
        self.assertFalse(self.has_label(ISSUE, "in-progress"))
        self.assertFalse(self.claim_ref_exists(ISSUE), "the lock survived the release")


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
    """The plugin's hooks.json, loaded by Copilot CLI (--plugin-dir, as the
    interactive launcher scripts/kraken-copilot.sh does when no kraken plugin is
    installed), fires SessionEnd with the session id kraken.py recorded (#173)."""

    def test_session_end_hook_releases_the_sessions_claim(self):
        proc, requests = self.play(
            [{"bash": "python3 %s next-action %s %s %s" % (KRAKEN, COORD, PROJECT, WORKER)},
             {"say": "Stopping here."}],
            [COPILOT, "-p", "Do one kraken drain pass.", "--plugin-dir", ROOT, "--add-dir", ROOT,
             "--allow-all-tools", "--no-ask-user", "--no-auto-update"])
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        claim = [b for b in self.comment_bodies(ISSUE) if '"type":"claim"' in b]
        self.assertTrue(claim, "the scripted claim never landed" + self.detail(requests))
        self.assertIn('"type":"released"', self.last_comment(ISSUE),
                      "SessionEnd did not release the session's claim" + self.detail(requests))
        self.assertIn("session ended", self.last_comment(ISSUE))
        self.assertFalse(self.has_label(ISSUE, "in-progress"))
        self.assertFalse(os.path.exists(self.claim_state_file(WORKER)))

    def test_session_end_leaves_another_sessions_claim_alone(self):
        # A claim made outside this Copilot session (a bare shell: no session
        # recorded) must survive the session's end — the lease TTL covers it.
        self.assertEqual(self.kraken("claim", COORD, ISSUE, WORKER).rc, 0)
        proc, requests = self.play(
            [{"say": "Nothing to do."}],
            [COPILOT, "-p", "Say hi.", "--plugin-dir", ROOT, "--allow-all-tools",
             "--no-ask-user", "--no-auto-update"])
        self.assertEqual(proc.returncode, 0, self.detail(requests))
        self.assertTrue(self.has_label(ISSUE, "in-progress"),
                        "SessionEnd released a claim it does not own" + self.detail(requests))
        self.assertTrue(os.path.exists(self.claim_state_file(WORKER)))


if __name__ == "__main__":
    unittest.main()
