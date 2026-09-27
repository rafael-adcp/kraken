#!/usr/bin/env python3
"""scripts/queue-from-issues.sh — feed the queue from plain issues.

The script reads open issues from a SOURCE repo and creates kraken tasks in the
coordination (DEST) repo, then marks each source issue so a re-run never
duplicates it. The gh-stub models one repo, so this suite puts a small fake `gh`
first on PATH instead: it serves source issues from a fixture, records every
call it gets, and evaluates the script's own `--jq` filters with the real jq —
so the filters under test are the shipped ones, as in the gh-stub."""
import json
import os
import shutil
import stat
import subprocess
import unittest

from harness import ROOT

SCRIPT = os.path.join(ROOT, "scripts", "queue-from-issues.sh")

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
state = os.environ["FAKE_GH_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls.jsonl"), "a") as f:
    f.write(json.dumps(args) + "\n")
fx = json.load(open(os.path.join(state, "fixture.json")))

def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default

def emit(obj):
    jq = opt("--jq")
    text = json.dumps(obj)
    if jq:
        text = subprocess.run(["jq", "-r", jq], input=text, capture_output=True,
                              text=True, check=True).stdout
        sys.stdout.write(text)
    else:
        print(text)

cmd = args[:2]
if cmd == ["auth", "status"]:
    sys.exit(0 if fx.get("authed", True) else 1)
if cmd[:1] == ["api"] and args[1] == "user":
    emit({"login": fx["me"]})
elif cmd == ["repo", "view"]:
    emit({"nameWithOwner": fx["current_repo"]})
elif cmd == ["issue", "list"]:
    author = opt("--author")
    emit([{"number": i["number"], "labels": [{"name": l} for l in i["labels"]]}
          for i in fx["issues"] if i["author"] == author])
elif cmd == ["issue", "view"]:
    i = next(i for i in fx["issues"] if i["number"] == int(args[2]))
    emit({"title": i["title"], "url": "https://github.com/%s/issues/%d" % (opt("-R"), i["number"]),
          "body": i.get("body", "")})
elif cmd == ["issue", "create"]:
    n = sum(1 for c in open(os.path.join(state, "calls.jsonl")) if '"create"' in c and '"issue"' in c)
    print("https://github.com/%s/issues/%d" % (opt("-R"), 100 + n))
elif cmd in (["issue", "edit"], ["label", "create"]):
    pass
else:
    sys.exit("fake gh: unsupported: %r" % args)
'''


@unittest.skipUnless(shutil.which("jq"), "jq not found (the fake gh evaluates --jq with it)")
class QueueFromIssuesTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.state = tempfile.mkdtemp(prefix="kraken-qfi-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        bin_dir = os.path.join(self.state, "bin")
        os.makedirs(bin_dir)
        gh = os.path.join(bin_dir, "gh")
        with open(gh, "w", encoding="utf-8") as f:
            f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, FAKE_GH_STATE=self.state,
                        PATH=bin_dir + os.pathsep + os.environ["PATH"])
        self.env.pop("KRAKEN_TASKS_REPO", None)
        self.fixture({"me": "me", "current_repo": "me/notes", "issues": [
            {"number": 3, "author": "me", "title": "Fix the flaky login", "labels": [],
             "body": "It fails on Mondays."},
            {"number": 1, "author": "me", "title": "Bump deps", "labels": []},
            {"number": 2, "author": "me", "title": "Already queued", "labels": ["queued"]},
            {"number": 4, "author": "other", "title": "Someone else's", "labels": []},
        ]})

    def fixture(self, fx):
        with open(os.path.join(self.state, "fixture.json"), "w", encoding="utf-8") as f:
            json.dump(fx, f)

    def run_script(self, *args):
        open(os.path.join(self.state, "calls.jsonl"), "w").close()
        return subprocess.run(["bash", SCRIPT, *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def calls(self, *prefix):
        with open(os.path.join(self.state, "calls.jsonl"), encoding="utf-8") as f:
            calls = [json.loads(l) for l in f if l.strip()]
        return [c for c in calls if c[:len(prefix)] == list(prefix)]

    def test_queues_each_unmarked_issue_as_a_task_and_marks_it(self):
        r = self.run_script("--dest", "me/tasks", "--project", "app")
        self.assertEqual(r.returncode, 0, r.stderr)
        creates = self.calls("issue", "create")
        # Oldest number first; the marked issue and another author's are skipped.
        self.assertEqual([c[c.index("--title") + 1] for c in creates],
                         ["Bump deps", "Fix the flaky login"])
        for c in creates:
            self.assertEqual(c[c.index("-R") + 1], "me/tasks")
            labels = [c[i + 1] for i, a in enumerate(c) if a == "--label"]
            self.assertEqual(labels, ["kraken-task", "project:app"])
        body = creates[1][creates[1].index("--body") + 1]
        self.assertIn("### Goal\n\nIt fails on Mondays.", body)
        self.assertIn("### Acceptance", body)
        self.assertIn("Translated from me/notes#3 — https://github.com/me/notes/issues/3", body)
        # An issue with no body: its title is the goal.
        self.assertIn("### Goal\n\nBump deps", creates[0][creates[0].index("--body") + 1])
        # Each source issue is marked, so a re-run never duplicates it.
        self.assertEqual(sorted(c[2] for c in self.calls("issue", "edit")), ["1", "3"])
        for c in self.calls("issue", "edit"):
            self.assertEqual(c[c.index("--add-label") + 1], "queued")
        self.assertIn("done: 2 issue(s) queued.", r.stdout)

    def test_source_and_author_default_to_the_current_repo_and_you(self):
        r = self.run_script("--dest", "me/tasks")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("source: me/notes (authors: me, skip label: queued)", r.stdout)
        self.assertIn("project:kraken", r.stdout)
        self.assertTrue(self.calls("repo", "view") and self.calls("api", "user"))

    def test_several_authors_union_without_duplicates(self):
        r = self.run_script("--dest", "me/tasks", "--author", "me, other", "--author", "me",
                            "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        queued = [l for l in r.stdout.splitlines() if l.startswith("would queue:")]
        self.assertEqual([l.split()[2] for l in queued], ["me/notes#1", "me/notes#3", "me/notes#4"])

    def test_dry_run_writes_nothing(self):
        r = self.run_script("--dest", "me/tasks", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mode:   DRY RUN", r.stdout)
        self.assertIn("done: 2 issue(s) would be queued.", r.stdout)
        for write in (("issue", "create"), ("issue", "edit"), ("label", "create")):
            self.assertEqual(self.calls(*write), [], "dry run called gh %s %s" % write)

    def test_custom_marker_is_what_skips_and_what_marks(self):
        self.fixture({"me": "me", "current_repo": "me/notes", "issues": [
            {"number": 1, "author": "me", "title": "a", "labels": ["queued"]},
            {"number": 2, "author": "me", "title": "b", "labels": ["in-kraken"]}]})
        r = self.run_script("--dest", "me/tasks", "--marker", "in-kraken")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c[2] for c in self.calls("issue", "edit")], ["1"])
        self.assertEqual(self.calls("issue", "edit")[0][-1], "in-kraken")

    def test_nothing_to_queue_is_a_clean_exit(self):
        self.fixture({"me": "me", "current_repo": "me/notes", "issues": []})
        r = self.run_script("--dest", "me/tasks")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to queue.", r.stdout)
        self.assertEqual(self.calls("label", "create"), [])

    def test_dest_falls_back_to_the_env_var(self):
        self.env["KRAKEN_TASKS_REPO"] = "me/from-env"
        r = self.run_script("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dest:   me/from-env", r.stdout)

    def test_refuses_to_run_without_what_it_needs(self):
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no destination repo", r.stderr)

        r = self.run_script("--dest", "me/tasks", "--bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown argument: --bogus", r.stderr)

        self.fixture({"authed": False, "me": "me", "current_repo": "me/notes", "issues": []})
        r = self.run_script("--dest", "me/tasks")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("gh is not authenticated", r.stderr)

    def test_help(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Usage:", r.stdout)


if __name__ == "__main__":
    unittest.main()
