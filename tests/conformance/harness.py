"""Shared harness for the Python conformance suite (tests/conformance/test_*.py).

Phase-2 transport (issue #53) makes kraken.py talk pure HTTP over stdlib urllib,
so the conformance seam is the API base, not a `gh` binary on PATH: each test
gets a fresh state directory and an in-process HTTP stub (tests/gh-stub/server.py)
serving GitHub's REST + GraphQL surface over that state; the harness points the
program under test (skills/unleash/kraken.py) at it via `GITHUB_API_URL`, sets a
stub `GH_TOKEN` so no `gh auth token` spawn is needed, and prepends a tripwire
`gh` to PATH that fails loudly — proving the transport never shells out to `gh`.
The test drives `python3 kraken.py <subcommand>` as a subprocess and asserts on
the resulting stub state (labels, comments, refs, log).

The stub serves the SAME on-disk state layout the retired `gh` CLI stub used, so
every `mk_*` seeding helper is unchanged. It needs no `jq`: kraken.py parses JSON
itself now, so there are no `--jq` filters crossing the transport.

A test subclasses `KrakenConformanceTest`. `setUp` creates the scratch state,
starts the stub, and builds the env; the `mk_*` methods seed the stub; `kraken()`
invokes a kraken.py subcommand; and the `assert_*`/inspection helpers replace
`lib.sh`'s shell assertions.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(ROOT, "skills", "unleash")
KRAKEN = os.path.join(SCRIPTS, "kraken.py")
GH_STUB_DIR = os.path.join(ROOT, "tests", "gh-stub")
TRIPWIRE_DIR = os.path.join(GH_STUB_DIR, "no-gh")

sys.path.insert(0, GH_STUB_DIR)
import server as stub_server  # noqa: E402  (path set above)

# The program under test still runs as a SUBPROCESS — that is the whole point of
# this suite. This import exists only to read its constants (the lease clock), so
# no test hardcodes a number kraken.py owns.
sys.path.insert(0, SCRIPTS)
import kraken as kraken_module  # noqa: E402  (path set above)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago_iso(hours=0, seconds=0):
    """ISO timestamp `hours`/`seconds` in the past. The lease clock is measured
    in minutes (PROTOCOL.md §5), so seconds is the resolution that matters now;
    hours stays for the tests that predate it."""
    return iso(utcnow() - timedelta(hours=hours, seconds=seconds))


def lease_ttl():
    """The default lease TTL, read off the program under test rather than
    hardcoded, so retuning the clock never means editing a pile of tests."""
    return kraken_module.LEASE_DEFAULT_TTL_SECONDS


def expiry_marker(worker="w-thief", previous="w-dead"):
    """A `lease-expired` comment body — what a steal leaves behind, and what the
    repeat-expiry guard (§6) counts."""
    stale = kraken_module.Lease(gen=1, sha="stale", worker=previous, epoch=None,
                                gens=(1,), age=99)
    return kraken_module.lease_expired_body(worker, stale)


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
        os.makedirs(os.path.join(self.state, "issues"), exist_ok=True)
        open(os.path.join(self.state, "log"), "w").close()
        # The in-process HTTP stub — the transport seam. Torn down after the test.
        self.httpd, self.api_url, self.knobs = stub_server.start(self.state)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        # Isolate the claim state dir into this test's scratch so kraken.py never
        # reads or writes the real ~/.kraken (the one-task-at-a-time guard reads
        # it). Tests may reassign self.kraken_state_dir before a run.
        self.kraken_state_dir = os.path.join(self.state, "kraken")
        # How far the stub server's clock sits from this machine's. Zero for
        # every test but the skew ones; see `set_server_clock`.
        self.server_clock_offset = 0

    # --- the server's clock --------------------------------------------------

    def set_server_clock(self, offset_seconds):
        """Move the stub server's clock `offset_seconds` away from this
        machine's — positive for a server ahead of the worker, negative for one
        behind. Every timestamp the server stamps or reports moves with it, and
        so does every age seeded through `mk_claim_ref`, because a lease's age
        is defined against the server's clock (PROTOCOL.md §5.1)."""
        self.server_clock_offset = offset_seconds
        self.knobs.set_clock_offset(offset_seconds)

    def server_ago_iso(self, hours=0, seconds=0):
        """ISO timestamp `hours`/`seconds` before the SERVER's now — the anchor
        every seeded lease age is measured from, so a test that skews the clock
        seeds the age it means rather than the age this machine would compute."""
        return iso(utcnow()
                   + timedelta(seconds=self.server_clock_offset)
                   - timedelta(hours=hours, seconds=seconds))

    # --- environment ---------------------------------------------------------

    def _apply_knobs(self, mapping):
        """Route the test's injection knobs to the in-process stub. GH_STUB_FAIL
        and GH_STUB_BARRIER are no longer subprocess env vars the stub reads —
        the stub shares this process, so they are set on `self.knobs` here."""
        self.knobs.set_fail(mapping.get("GH_STUB_FAIL"))
        self.knobs.set_barrier(mapping.get("GH_STUB_BARRIER") or 0)
        self.knobs.set_cas_barrier(mapping.get("GH_STUB_CAS_BARRIER") or 0)

    def base_env(self, extra=None):
        env = dict(os.environ)
        env["GITHUB_API_URL"] = self.api_url
        # A token from the env means kraken.py never spawns `gh auth token`.
        env["GH_TOKEN"] = "stub-token"
        env["KRAKEN_STATE_DIR"] = self.kraken_state_dir
        # A tripwire `gh` first on PATH: any surviving `gh` spawn fails loudly.
        env["PATH"] = TRIPWIRE_DIR + os.pathsep + env.get("PATH", "")
        if extra:
            env.update({k: v for k, v in extra.items() if v is not None})
        return env

    # --- running kraken.py ---------------------------------------------------

    def kraken(self, *args, env=None, fail=None, stdin=None):
        """Run `python3 kraken.py <args>`; return a RunResult.

        `fail` sets the stub's injected-failure regex (matched against
        "METHOD path [graphql-query]"). `env` merges extra environment variables
        for this call only.
        """
        extra = dict(env or {})
        if fail is not None:
            extra["GH_STUB_FAIL"] = fail
        self._apply_knobs(extra)
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
        self._apply_knobs(dict(env or {}))
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
        self._apply_knobs(dict(env or {}))
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
        .at sidecar (absent = derived from the issue's createdAt)."""
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

    def mk_claim_ref(self, n, worker, age_hours=0, msg=None, mtype="claim",
                     age_seconds=0, orphan_commit=False, gen=1):
        """Seed a claim ref for issue `n` — the lock, and its LEASE: the
        refs file for generation `gen` plus its commit object, committedDate
        backdated by `age_hours`/`age_seconds` (the lease clock, PROTOCOL.md §5).
        The commit message is the standard kraken marker, exactly as
        create_claim_commit writes it. Returns the seeded sha.

        `gen=0` seeds the bare protocol/5 ref name, which readers take as the
        lowest generation — the shape a queue predating protocol/6 carries.

        `orphan_commit=True` writes the ref WITHOUT its commit object — a lease
        whose clock cannot be read, which §5 requires readers to treat as
        expired (fail open to the steal) rather than as holding forever."""
        payload = {"type": mtype, "worker": worker}
        if msg is not None:
            payload["msg"] = msg
        sha = hashlib.sha1(
            ("seed-%s-%s-%s" % (n, worker, gen)).encode("utf-8")).hexdigest()
        if not orphan_commit:
            self._write(os.path.join(self.state, "objects", sha + ".json"),
                        json.dumps({"message": make_marker(payload),
                                    "committedDate": self.server_ago_iso(
                                        age_hours, age_seconds)}))
        name = str(n) if int(gen) == 0 else "%d-%d" % (int(n), int(gen))
        self._write(os.path.join(self.state, "refs", name), sha + "\n")
        return sha

    def mk_expired_lease(self, n, worker, **kwargs):
        """A lease seeded just past the TTL — the steal's precondition."""
        return self.mk_claim_ref(n, worker, age_seconds=lease_ttl() + 60, **kwargs)

    def mk_state_record(self, n, state, worker="w1", comments=None,
                        expiries=0, pr=None):
        """Seed the state record of issue `n` (PROTOCOL.md §3.1) — the ref plus
        the orphan commit whose message is the `state` marker, exactly as
        `States.write` writes it. Returns the seeded sha.

        `comments` defaults to the thread AS IT STANDS, which is what a
        transition records: it reads the count back after posting its own
        comment, so a freshly delivered task holds until something else is said.
        Passing a lower number models a thread that has moved on since."""
        payload = {"type": "state", "state": state, "worker": worker,
                   "comments": (self.comment_count(n) if comments is None
                                else int(comments)),
                   "expiries": int(expiries)}
        if pr:
            payload["pr"] = pr
        sha = hashlib.sha1(
            ("state-%s-%s-%s" % (n, state, payload["comments"])).encode("utf-8")
        ).hexdigest()
        self._write(os.path.join(self.state, "objects", sha + ".json"),
                    json.dumps({"message": make_marker(payload),
                                "committedDate": self.server_ago_iso(0)}))
        self._write(os.path.join(self.state, "refs", "state-%d" % int(n)),
                    sha + "\n")
        return sha

    def state_record(self, n):
        """The seeded state record's decoded marker payload, or None when the
        task has no record. How a test asserts on what a transition wrote."""
        ref = os.path.join(self.state, "refs", "state-%d" % int(n))
        if not os.path.isfile(ref):
            return None
        with open(ref, encoding="utf-8") as f:
            sha = f.read().strip()
        obj = os.path.join(self.state, "objects", sha + ".json")
        if not os.path.isfile(obj):
            return None
        with open(obj, encoding="utf-8") as f:
            message = json.load(f).get("message", "")
        return parse_marker(message)

    def state_record_exists(self, n):
        return os.path.isfile(
            os.path.join(self.state, "refs", "state-%d" % int(n)))

    def deliver_state(self, n, worker="w1", pr=None):
        """A task in the state a delivery leaves it: the `delivered` comment, the
        `awaiting-merge` label, and the record anchored on the thread including
        that comment. The three-line preamble almost every review-side test
        needs, so no test has to remember the order (§8)."""
        self.mk_comment(n, "%s\n\ndone\n\n%s" % (
            self.disclaimer_line(worker),
            make_marker({"type": "delivered", "worker": worker,
                         **({"pr": pr} if pr else {})})))
        self.set_labels(n, self.labels(n) + ["awaiting-merge"])
        return self.mk_state_record(n, "awaiting-merge", worker=worker, pr=pr)

    def claim_generations(self, n):
        """[(generation, sha), …] for issue `n`, sorted — the lease ladder."""
        rdir = os.path.join(self.state, "refs")
        if not os.path.isdir(rdir):
            return []
        out = []
        for name in os.listdir(rdir):
            issue, _, gen = name.partition("-")
            if not issue.isdigit() or int(issue) != int(n):
                continue
            if gen and not gen.isdigit():
                continue
            with open(os.path.join(rdir, name), encoding="utf-8") as f:
                out.append((int(gen or 0), f.read().strip()))
        return sorted(out)

    def claim_ref(self, n):
        """The sha of the ref that HOLDS issue `n` — its highest generation — or
        None when the task is unlocked. Ownership is the top of the ladder, so
        this is what every assertion about "who has the lease" reads."""
        refs = self.claim_generations(n)
        return refs[-1][1] if refs else None

    def claim_ref_exists(self, n):
        return self.claim_ref(n) is not None

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

    def comment_bodies(self, n):
        """Every comment on the issue, oldest first — for the transitions that
        write more than one (a steal posts its audit note and then its claim)."""
        cdir = os.path.join(self.issue_dir(n), "comments")
        if not os.path.isdir(cdir):
            return []
        bodies = []
        for name in sorted(f for f in os.listdir(cdir) if f.endswith(".md")):
            with open(os.path.join(cdir, name), encoding="utf-8") as f:
                bodies.append(f.read().rstrip("\n"))
        return bodies

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


def parse_marker(text):
    """The marker payload out of a commit message or comment body, or None —
    how a test reads back what a transition wrote into a state record."""
    return kraken_module.parse_marker(text)
