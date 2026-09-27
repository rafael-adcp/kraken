"""Shared world for the harness e2e suite (tests/e2e/test_*_e2e.py): a REAL
agent CLI — GitHub Copilot CLI or Claude Code — driven by the scripted fake
model in tests/fake-model/model_server.py.

The conformance suite proves kraken.py's transitions with no agent in the loop;
the agent-behavior harness (tests/agent/) runs a real CLI with a real model but
spends tokens and never runs in CI. This suite pins the seam between them,
token-free: the CLI's real prompt assembly, plugin/skill loading, shell tool,
permission layer and hooks, carrying a drain against kraken.py and the gh-stub
into a real git work repo with a local bare remote. The "model" only plays a
script, so the model's judgment is never under test — a failure means the
wiring broke.

Each CLI module calls `require_cli(name, probe)` from its setUpModule: a CLI
that is missing, or cannot reach a localhost model at all, skips the module —
unless KRAKEN_E2E_REQUIRE=1 (CI sets it), where it fails, so a broken install
can never pass as a green skip.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "conformance"))
sys.path.insert(0, os.path.join(HERE, "..", "fake-model"))

from harness import GH_STUB_DIR, KRAKEN, ROOT, KrakenConformanceTest  # noqa: E402,F401
import model_server  # noqa: E402

COORD = "stub-owner/tasks"
WORK_SLUG = "stub-owner/work"
PROJECT = "x"
WORKER = "t1"
ISSUE = 1
BRANCH = "kraken/task-1"
TIMEOUT = int(os.environ.get("KRAKEN_E2E_TIMEOUT", "180"))
REQUIRED = os.environ.get("KRAKEN_E2E_REQUIRE") == "1"
# A real GitHub API round trip, per request. The stub answers in milliseconds,
# which hides any hook that only fits its harness's deadline on a fast API:
# `kraken.py release` makes ~9 calls, so at this latency it takes ~3s.
REAL_API_LATENCY = 0.3

# The scripted steps every CLI's drain shares: its shell tool runs them, so they
# are plain commands, identical whichever CLI carries them.
NEXT_ACTION = "python3 %s next-action %s %s %s" % (KRAKEN, COORD, PROJECT, WORKER)


def _unusable(reason):
    if REQUIRED:
        raise RuntimeError("KRAKEN_E2E_REQUIRE=1 but " + reason)
    raise unittest.SkipTest(reason)


def require_cli(name, probe_env, probe_argv):
    """Skip (or, under KRAKEN_E2E_REQUIRE=1, fail) unless `name` and git are on
    PATH and one probe run of the CLI reaches the fake model. A CLI that cannot
    reach a localhost model — e.g. a Windows-side build seen from WSL — would
    otherwise fail every test for a reason that is not the wiring under test.
    `probe_env(url, home)` returns the probe's extra env; `probe_argv(cli)` its argv."""
    missing = [t for t in (name, "git") if not shutil.which(t)]
    if missing:
        _unusable("%s not on PATH" % ", ".join(missing))
    cli = shutil.which(name)
    with tempfile.TemporaryDirectory(prefix="kraken-e2e-probe-") as tmp:
        record = os.path.join(tmp, "probe.jsonl")
        httpd, url = model_server.start([{"say": "ok"}], record)
        try:
            env = dict(os.environ, **probe_env(url, tmp))
            subprocess.run(probe_argv(cli), cwd=tmp, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            _unusable("%s did not run: %s" % (cli, e))
        finally:
            httpd.shutdown()
            httpd.server_close()
        if not model_server.read_record(record):
            _unusable("%s never reached the local fake model (a Windows-side %s under WSL? "
                      "install the Linux one)" % (cli, name))
    return cli


def deliver_script(coauthor):
    """Claim, branch + commit with both trailers + push, open a draft PR,
    deliver — a whole task, as scripted shell steps."""
    trailer = "$(python3 %s contract task-trailer --repo %s --issue %d)" % (KRAKEN, COORD, ISSUE)
    commit_msg = "feat: greet in feature.txt\n\n%s\n%s" % (coauthor, trailer)
    return [
        {"bash": NEXT_ACTION},
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


def _text(content):
    """A message's content as plain text, whichever wire shaped it."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text(p.get("content")) if p.get("type") == "tool_result"
                       else p.get("text", "") for p in content if isinstance(p, dict))
    return ""


class E2ETest(KrakenConformanceTest):
    """One scratch world per test: gh-stub state (HTTP API + `gh` CLI over the
    same files), a work repo with a bare remote, an isolated HOME, and a fake
    model playing the test's script. Subclasses add the CLI's own env."""

    def setUp(self):
        super().setUp()
        self.scratch = os.path.join(self.state, "e2e")
        self.home = os.path.join(self.scratch, "home")
        self.work = os.path.join(self.scratch, "work")
        self.bare = os.path.join(self.scratch, "work.git")
        self.record = os.path.join(self.scratch, "model-requests.jsonl")
        os.makedirs(self.home)
        self.transcript = ""
        self.mk_issue(ISSUE, "Add greet() to feature.txt", "kraken-task", "project:" + PROJECT)
        self.mk_body(ISSUE, "## Goal\nAdd a greet line.\n\n## Acceptance\n- feature.txt greets.")
        self.setup_work_repo()

    # --- the world ----------------------------------------------------------

    def cli_env(self, model_url):
        """The CLI's own variables: point it at the fake model. Per subclass."""
        return {}

    def env(self, model_url):
        env = self.base_env({
            # The gh CLI stub — not base_env's tripwire — answers the model's
            # own `gh` commands, over the same state the HTTP stub serves.
            "GH_STUB_STATE": self.state,
            # Never the operator's CLI config, installed plugins or git identity.
            "HOME": self.home,
            "XDG_CONFIG_HOME": os.path.join(self.home, ".config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Kraken Worker",
            "GIT_AUTHOR_EMAIL": "worker@example.invalid",
            "GIT_COMMITTER_NAME": "Kraken Worker",
            "GIT_COMMITTER_EMAIL": "worker@example.invalid",
        })
        # This suite may itself run inside an agent session; the CLI under test
        # must start its own, never inherit one.
        for var in list(env):
            if var.startswith(("CLAUDE_CODE_", "CLAUDECODE", "COPILOT_")):
                env.pop(var)
        env.update(self.cli_env(model_url))
        path = env["PATH"].split(os.pathsep)
        env["PATH"] = os.pathsep.join([GH_STUB_DIR] + [p for p in path if p and "no-gh" not in p])
        return env

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.work, capture_output=True, text=True,
                              env=self.env("http://unused"))

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

    def play(self, turns, argv):
        """Serve `turns` as the model, run `argv` (the CLI, or a script that
        runs it) in the work repo, and return (proc, requests the model got)."""
        httpd, url = model_server.start(turns, self.record)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        proc = subprocess.run(argv, cwd=self.work, env=self.env(url), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=TIMEOUT)
        self.transcript = proc.stdout + proc.stderr
        return proc, model_server.read_record(self.record)

    # --- reading what came back ---------------------------------------------

    def tool_results(self, requests):
        """The tool outputs the CLI sent back to the model, keyed by the
        scripted call id (call_N) — what the "model" read after each step."""
        out = {}
        for req in requests:
            for m in req.get("messages", []):
                if m.get("role") == "tool":  # OpenAI wire
                    out[m.get("tool_call_id")] = _text(m.get("content"))
                elif isinstance(m.get("content"), list):  # Anthropic wire
                    for block in m["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            cid = block.get("tool_use_id", "").replace("toolu_", "", 1)
                            out[cid] = _text(block.get("content"))
        return out

    def user_text(self, request):
        """Everything the user turns of one request said, as text."""
        return "\n".join(_text(m.get("content")) for m in request.get("messages", [])
                         if m.get("role") == "user")

    def detail(self, requests=None):
        out = "\n--- CLI transcript ---\n" + self.transcript[-4000:]
        if requests is not None:
            out += "\n--- tool results ---\n" + json.dumps(self.tool_results(requests), indent=1)[-4000:]
        return out

    # --- shared assertions --------------------------------------------------

    def assert_delivered(self, requests, coauthor):
        """The whole task landed: queue, PR and work repo all say so."""
        detail = self.detail(requests)
        # Tool calls ran in the work repo, not the kraken checkout (#175).
        with open(os.path.join(self.work, ".e2e-cwd"), encoding="utf-8") as f:
            self.assertEqual(os.path.realpath(f.read().strip()), os.path.realpath(self.work))
        # The envelope came back to the model through the CLI's shell tool.
        self.assertIn('"action"', self.tool_results(requests).get("call_1", ""),
                      "next-action's envelope never reached the model" + detail)

        self.assertTrue(self.has_label(ISSUE, "awaiting-merge"), detail)
        self.assertFalse(self.has_label(ISSUE, "in-progress"))
        self.assertIn('"type":"delivered"', self.last_comment(ISSUE))
        self.assertIn("/pull/1", self.last_comment(ISSUE), "delivered marker lacks the PR")
        self.assert_disclaimer(ISSUE, WORKER)
        self.assertFalse(os.path.exists(self.claim_state_file(WORKER)),
                         "a delivered task left its claim state file behind")

        with open(os.path.join(self.state, "pr", "0001.json"), encoding="utf-8") as f:
            pr = json.load(f)
        self.assertTrue(pr["draft"], "the PR is not a draft")
        self.assertEqual((pr["repo"], pr["head"]), (WORK_SLUG, BRANCH))

        log = self.git("--git-dir", self.bare, "log", "-1", "--format=%B", BRANCH).stdout
        self.assertIn(coauthor, log)
        self.assertIn("Kraken-Task:", log)
        self.assertEqual(self.git("--git-dir", self.bare, "rev-parse", "main").stdout.strip(),
                         self.main_baseline, "the default branch moved")

    def at_real_api_speed(self):
        """Answer every stub request at REAL_API_LATENCY from now on."""
        self.knobs.set_latency(REAL_API_LATENCY)
        self.addCleanup(self.knobs.set_latency, 0)

    def wait_released(self, timeout=30):
        """The hooks release DETACHED, so the release may still be landing
        after the CLI exits: wait for all of it — the label, the lock, and the
        local claim state file, which `release` removes last."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (not self.has_label(ISSUE, "in-progress") and not self.claim_ref_exists(ISSUE)
                    and not os.path.exists(self.claim_state_file(WORKER))):
                return
            time.sleep(0.2)

    def assert_claimed_then_released(self, reason, requests):
        self.wait_released()
        detail = self.detail(requests)
        self.assertIn('"type":"claim"', "\n".join(self.comment_bodies(ISSUE)),
                      "the scripted claim never landed" + detail)
        self.assertIn('"type":"released"', self.last_comment(ISSUE), detail)
        self.assertIn(reason, self.last_comment(ISSUE), detail)
        self.assertFalse(self.has_label(ISSUE, "in-progress"))
        self.assertFalse(self.claim_ref_exists(ISSUE), "the lock survived the release")
        self.assertFalse(os.path.exists(self.claim_state_file(WORKER)))
