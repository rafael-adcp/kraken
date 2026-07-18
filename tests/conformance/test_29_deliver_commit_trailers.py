#!/usr/bin/env python3
"""PROTOCOL.md §8: "Every delivered commit MUST carry the attribution trailers"
— the agent's own Co-Authored-By and the authoritative Kraken-Task line. Every
other conformance test models `gh` but no git, so this is the one case that
needs real git: it stands up a throwaway local work repo, builds delivery
commits the way a worker does (both trailers, the Kraken-Task line taken
verbatim from `kraken.py contract task-trailer`), and asserts every commit on
the delivered branch carries both, read through git's own trailer parser. A
negative commit proves the assertion fails when a trailer is missing."""
import os
import re
import shutil
import subprocess
import unittest

from harness import KrakenConformanceTest

REPO_SLUG = "OWNER/tasks"
ISSUE = 7
WORKER = "w1"
# The agent's own identity line (this suite's driver is GitHub Copilot).
COAUTHOR = "Co-Authored-By: GitHub Copilot <noreply@github.com>"


@unittest.skipUnless(shutil.which("git"), "git not found")
class DeliverCommitTrailerTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.state, "work")
        os.makedirs(self.work)
        home = os.path.join(self.state, "home")
        os.makedirs(home)
        empty_global = os.path.join(home, ".gitconfig-empty")
        empty_system = os.path.join(home, ".gitconfig-empty-system")
        open(empty_global, "w").close()
        open(empty_system, "w").close()
        # A fully isolated git identity/config, so we never read or write the
        # machine's real git identity or hooks.
        self.git_env = dict(os.environ)
        self.git_env.update({
            "HOME": home,
            "GIT_CONFIG_GLOBAL": empty_global,
            "GIT_CONFIG_SYSTEM": empty_system,
            "GIT_AUTHOR_NAME": "Test Worker",
            "GIT_AUTHOR_EMAIL": "worker@example.invalid",
            "GIT_COMMITTER_NAME": "Test Worker",
            "GIT_COMMITTER_EMAIL": "worker@example.invalid",
        })

    def git(self, *args, stdin=None):
        return subprocess.run(["git", "-C", self.work, *args], env=self.git_env,
                              input=stdin, capture_output=True, text=True)

    def commit_delivered(self, path, line, trailer):
        with open(os.path.join(self.work, path), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.git("add", "-A")
        msg = "feat: %s change\n\nPart of the delivered work.\n\n%s\n%s\n" % (path, COAUTHOR, trailer)
        self.git("commit", "-q", "-F", "-", stdin=msg)

    def trailer_value(self, sha, key):
        out = self.git("log", "-1", "--format=%%(trailers:key=%s,valueonly)" % key, sha).stdout
        return out.split("\n")[0] if out else ""

    def assert_delivered_commit(self, sha, trailer):
        kt = self.trailer_value(sha, "Kraken-Task")
        ca = self.trailer_value(sha, "Co-Authored-By")
        self.assertTrue(ca, "commit %s: Co-Authored-By trailer missing or not git-parseable" % sha)
        self.assertTrue(kt, "commit %s: Kraken-Task trailer missing or not git-parseable" % sha)
        self.assertRegex(
            "Kraken-Task: " + kt,
            r"^Kraken-Task: [^ ]+#[0-9]+ \(worker: [^,]+, kraken@[^ )]+\)$",
            "commit %s: Kraken-Task malformed [%s]" % (sha, kt))
        self.assertNotIn("kraken@unknown", kt, "commit %s: Kraken-Task version is 'unknown' [%s]" % (sha, kt))
        self.assertIn("worker: %s," % WORKER, kt, "commit %s: worker name not '%s' [%s]" % (sha, WORKER, kt))
        self.assertEqual("Kraken-Task: " + kt, trailer,
                         "commit %s Kraken-Task matches kraken.py contract" % sha)
        self.assertRegex(ca, r"<[^>]+@[^>]+>", "commit %s: Co-Authored-By lacks an email [%s]" % (sha, ca))

    def test_delivered_commits_carry_trailers(self):
        # The Kraken-Task trailer is NOT hand-written: it comes verbatim from
        # kraken.py, the single source of its format and its kraken@<version>.
        trailer = self.kraken("contract", "task-trailer", "--repo", REPO_SLUG,
                              "--issue", ISSUE, "--worker", WORKER).out
        self.assertTrue(trailer, "kraken.py contract task-trailer produced nothing")

        self.git("init", "-q")
        self.git("checkout", "-q", "-b", "main")
        # The repo bootstrap commit — exempt from the trailer rule.
        with open(os.path.join(self.work, "README"), "w", encoding="utf-8") as f:
            f.write("baseline\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: bootstrap")
        base = self.git("rev-parse", "HEAD").stdout.strip()

        # A delivered branch of MORE THAN ONE commit — §8 says *every* delivered
        # commit carries the trailers, so check them all, not just the tip.
        self.git("checkout", "-q", "-b", "tasks-%d-delivery" % ISSUE)
        self.commit_delivered("src.txt", "first slice", trailer)
        self.commit_delivered("docs.txt", "second slice", trailer)

        delivered = [s for s in self.git("rev-list", "%s..HEAD" % base).stdout.split("\n") if s]
        self.assertTrue(delivered, "no delivered commits between base and tip")
        for sha in delivered:
            self.assert_delivered_commit(sha, trailer)
        self.assertGreaterEqual(len(delivered), 2, "expected >=2 delivered commits checked")

        # Negative control: a commit made WITHOUT the discipline must be caught.
        with open(os.path.join(self.work, "oops.txt"), "a", encoding="utf-8") as f:
            f.write("untrailed\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feat: forgot the trailers")
        bad = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(AssertionError,
                               msg="assertion passed a commit with NO trailers"):
            self.assert_delivered_commit(bad, trailer)

        # Also catch a malformed Kraken-Task (version stamp dropped).
        msg = "feat: malformed trailer\n\n%s\nKraken-Task: %s#%d (worker: %s)\n" % (
            COAUTHOR, REPO_SLUG, ISSUE, WORKER)
        self.git("commit", "-q", "--allow-empty", "-F", "-", stdin=msg)
        malformed = self.git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(AssertionError,
                               msg="assertion passed a malformed Kraken-Task trailer"):
            self.assert_delivered_commit(malformed, trailer)


if __name__ == "__main__":
    unittest.main()
