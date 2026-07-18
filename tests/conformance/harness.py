"""Shared harness for the Python conformance suite (tests/conformance/test_*.py).

This is the Python port of the old bash `tests/lib.sh` + `tests/t/*.sh` drivers.
It reuses the already-Python `gh` stub (tests/gh-stub/gh) unchanged: each test
gets a fresh stub state directory, the stub dir is prepended to PATH so the
program under test (skills/unleash/kraken.py) calls the stub and never the real
`gh`, and the test drives `python3 kraken.py <subcommand>` as a subprocess and
asserts on the resulting stub state (labels, comments, log).

The suite's defining property is preserved from the bash original: the scripts'
own `--jq` expressions are still evaluated by the *real* `jq` inside the stub, so
the filters under test are the shipped ones. jq/python3 are therefore required;
`tests/run.py` skips the whole suite cleanly when either is absent.

A test subclasses `KrakenConformanceTest`. `setUp` creates the scratch state and
env; the `mk_*` methods seed the stub the way `lib.sh`'s helpers did; `kraken()`
invokes a kraken.py subcommand; and the `assert_*`/inspection helpers replace
`lib.sh`'s shell assertions.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(ROOT, "skills", "unleash")
KRAKEN = os.path.join(SCRIPTS, "kraken.py")
GH_STUB_DIR = os.path.join(ROOT, "tests", "gh-stub")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago_iso(hours):
    """ISO timestamp `hours` hours in the past (lib.sh's ago_iso)."""
    return iso(utcnow() - timedelta(hours=hours))


def now_iso():
    return iso(utcnow())


class RunResult:
    """The outcome of a kraken.py subprocess run.

    `.out` is stdout with trailing newlines stripped, matching bash `$(...)`
    command substitution (which most tests compare against). `.out_raw` and
    `.err` keep the untouched streams.
    """

    def __init__(self, rc, out_raw, err):
        self.rc = rc
        self.out_raw = out_raw
        self.err = err
        self.out = out_raw.rstrip("\n")

    @property
    def lines(self):
        return self.out.split("\n") if self.out else []


class KrakenConformanceTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="kraken-conf-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        # Isolate the claim state dir into this test's scratch so kraken.py never
        # reads or writes the real ~/.kraken (the one-task-at-a-time guard reads
        # it). Tests may reassign self.kraken_state_dir before a run.
        self.kraken_state_dir = os.path.join(self.state, "kraken")
        os.makedirs(os.path.join(self.state, "issues"), exist_ok=True)
        open(os.path.join(self.state, "log"), "w").close()

    # --- environment ---------------------------------------------------------

    def base_env(self, extra=None):
        env = dict(os.environ)
        env["GH_STUB_STATE"] = self.state
        env["KRAKEN_STATE_DIR"] = self.kraken_state_dir
        env["PATH"] = GH_STUB_DIR + os.pathsep + env.get("PATH", "")
        if extra:
            env.update({k: v for k, v in extra.items() if v is not None})
        return env

    # --- running kraken.py ---------------------------------------------------

    def kraken(self, *args, env=None, fail=None, stdin=None):
        """Run `python3 kraken.py <args>`; return a RunResult.

        `fail` sets GH_STUB_FAIL (the stub's injected-failure regex). `env`
        merges extra environment variables for this call only.
        """
        extra = dict(env or {})
        if fail is not None:
            extra["GH_STUB_FAIL"] = fail
        proc = subprocess.run(
            ["python3", KRAKEN, *[str(a) for a in args]],
            cwd=ROOT,
            env=self.base_env(extra),
            input=stdin,
            capture_output=True,
            text=True,
        )
        return RunResult(proc.returncode, proc.stdout, proc.stderr)

    def run_concurrent(self, argsets, env=None):
        """Run several kraken.py invocations concurrently (for the race tests).

        `argsets` is a list of arg tuples. Returns a list of RunResult in the
        same order. All share the given `env` (e.g. GH_STUB_BARRIER).
        """
        results = [None] * len(argsets)

        def worker(i, args):
            proc = subprocess.run(
                ["python3", KRAKEN, *[str(a) for a in args]],
                cwd=ROOT,
                env=self.base_env(env),
                capture_output=True,
                text=True,
            )
            results[i] = RunResult(proc.returncode, proc.stdout, proc.stderr)

        threads = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(argsets)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def run_hook(self, hook_rel, stdin, env=None):
        """Run a bundled bash hook (hooks/*.sh) with a JSON event on stdin."""
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, hook_rel)],
            cwd=ROOT,
            env=self.base_env(env),
            input=stdin,
            capture_output=True,
            text=True,
        )
        return RunResult(proc.returncode, proc.stdout, proc.stderr)

    # --- stub state seeding (lib.sh mk_* ports) ------------------------------

    def issue_dir(self, n):
        return os.path.join(self.state, "issues", str(n))

    def _write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def mk_issue(self, n, title, *labels):
        """Create an open issue. createdAt derives from N, so a higher number is
        always a younger task (keeps oldest-first assertions readable)."""
        d = self.issue_dir(n)
        os.makedirs(os.path.join(d, "comments"), exist_ok=True)
        self._write(os.path.join(d, "title"), title + "\n")
        self._write(os.path.join(d, "state"), "open\n")
        self._write(os.path.join(d, "createdAt"),
                    "2026-07-01T%02d:%02d:00Z\n" % (n // 60, n % 60))
        self._write(os.path.join(d, "labels"),
                    "".join(l + "\n" for l in labels))

    def mk_comment(self, n, body, at=None):
        """Append a comment in server order. Optional ISO `at` is written to a
        .at sidecar (the reaper's staleness anchor reads comment createdAt)."""
        cdir = os.path.join(self.issue_dir(n), "comments")
        os.makedirs(cdir, exist_ok=True)
        seq = len([f for f in os.listdir(cdir) if f.endswith(".md")]) + 1
        self._write(os.path.join(cdir, "%04d.md" % seq), body + "\n")
        if at is not None:
            self._write(os.path.join(cdir, "%04d.at" % seq), at + "\n")

    def mk_blocked_by(self, n, *blockers):
        self._write(os.path.join(self.issue_dir(n), "blocked_by"),
                    "".join(str(b) + "\n" for b in blockers))

    def mk_body(self, n, text):
        self._write(os.path.join(self.issue_dir(n), "body"), text + "\n")

    def mk_pr(self, n, state, merged_at=None):
        merged = ('"%s"' % merged_at) if merged_at is not None else "null"
        self._write(os.path.join(self.state, "pr", "%04d.json" % n),
                    '{"state":"%s","mergedAt":%s}\n' % (state, merged))

    def mk_label(self, label):
        path = os.path.join(self.state, "labels")
        with open(path, "a", encoding="utf-8") as f:
            f.write(label + "\n")

    def mk_repo(self, slug):
        self._write(os.path.join(self.state, "repo", "nameWithOwner"), slug + "\n")

    def mk_content(self, path, src_file):
        dst = os.path.join(self.state, "contents", path)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src_file, dst)

    # --- direct state manipulation (things tests do inline in bash) ----------

    def set_issue_state(self, n, state):
        self._write(os.path.join(self.issue_dir(n), "state"), state + "\n")

    def labels(self, n):
        path = os.path.join(self.issue_dir(n), "labels")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.rstrip("\n")]

    def set_labels(self, n, labels):
        self._write(os.path.join(self.issue_dir(n), "labels"),
                    "".join(l + "\n" for l in labels))

    def remove_label(self, n, label):
        self.set_labels(n, [l for l in self.labels(n) if l != label])

    def log_text(self):
        with open(os.path.join(self.state, "log"), encoding="utf-8") as f:
            return f.read()

    def log_lines(self):
        return [l for l in self.log_text().split("\n") if l]

    def truncate_log(self):
        open(os.path.join(self.state, "log"), "w").close()

    # --- inspection ----------------------------------------------------------

    def has_label(self, n, label):
        return label in self.labels(n)

    def comment_count(self, n):
        cdir = os.path.join(self.issue_dir(n), "comments")
        if not os.path.isdir(cdir):
            return 0
        return len([f for f in os.listdir(cdir) if f.endswith(".md")])

    def last_comment(self, n):
        c = self.comment_count(n)
        if c == 0:
            return ""
        with open(os.path.join(self.issue_dir(n), "comments", "%04d.md" % c),
                  encoding="utf-8") as f:
            return f.read().rstrip("\n")

    def claim_state_file(self, worker):
        return os.path.join(self.kraken_state_dir, "claim-%s.json" % worker)

    def disclaimer_line(self, worker):
        """The authoritative attribution blockquote for `worker`, derived from
        kraken.py's DISCLAIMER constant (the single source of truth)."""
        return self.kraken("contract", "disclaimer", "--worker", worker).out

    # --- assertions (lib.sh port) --------------------------------------------

    def assert_disclaimer(self, n, worker):
        expected = self.disclaimer_line(worker)
        actual = self.last_comment(n).split("\n")[0] if self.last_comment(n) else ""
        self.assertEqual(actual, expected,
                         "disclaimer blockquote missing or drifted on #%s" % n)

    def assert_marker(self, n, json_str):
        marker = "<!-- kraken %s -->" % json_str
        self.assertIn(marker, self.last_comment(n),
                      "protocol/3 marker %r missing from #%s latest comment" % (marker, n))

    def marker_count(self, n):
        return sum(1 for l in self.last_comment(n).split("\n") if "<!-- kraken " in l)


def make_marker(payload):
    """Compact-JSON marker exactly as kraken.py emits it — for seeding threads."""
    return "<!-- kraken %s -->" % json.dumps(payload, separators=(",", ":"))
