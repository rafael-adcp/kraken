#!/usr/bin/env python3
"""init conformance: the bootstrap kraken.py init mechanizes — verify or create
the coordination repo PRIVATE, install the bundled assets via the contents API
(create / skip-unchanged / flag-customized), and upsert the canonical labels —
proven against the gh stub with no LLM."""
import filecmp
import os
import unittest

from harness import KrakenConformanceTest, SCRIPTS

ASSET_SRCS = ["task-template.yml", "kraken.py", "reclaim-stale.yml",
              "cleanup-closed.yml", "requeue-on-reply.yml", "validate-task.yml"]
ASSET_DSTS = [
    ".github/ISSUE_TEMPLATE/task.yml",
    ".github/kraken.py",
    ".github/workflows/reclaim-stale.yml",
    ".github/workflows/cleanup-closed.yml",
    ".github/workflows/requeue-on-reply.yml",
    ".github/workflows/validate-task.yml",
]


class InitTests(KrakenConformanceTest):
    def _contents(self, dst):
        return os.path.join(self.state, "contents", dst)

    def test_init(self):
        # --- 1. fresh bootstrap: absent repo created private, assets + labels --
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks", "--project", "app")
        self.assertEqual(r.rc, 0, "fresh init exit")
        self.assertIn("repo create OWNER/tasks --private", self.log_text(),
                      "fresh init did not create the repo PRIVATE")

        for src_name, dst in zip(ASSET_SRCS, ASSET_DSTS):
            src = os.path.join(SCRIPTS, src_name)
            self.assertTrue(filecmp.cmp(self._contents(dst), src, shallow=False),
                            "asset %s not installed byte-identical to bundled %s" % (dst, src_name))
            self.assertIn("init: asset %s (created)" % dst, r.out, "asset %s not reported created" % dst)

        for lbl in ("kraken-task", "in-progress", "needs-decision", "awaiting-merge", "project:app"):
            self.assertTrue(os.path.isfile(os.path.join(self.state, "labels-meta", lbl)),
                            "label %s not upserted" % lbl)
        with open(os.path.join(self.state, "labels-meta", "kraken-task"), encoding="utf-8") as f:
            self.assertIn("color=1D76DB", f.read(), "kraken-task label lost its canonical color")
        with open(os.path.join(self.state, "labels-meta", "project:app"), encoding="utf-8") as f:
            self.assertIn("color=5319E7", f.read(), "project:app label lost its canonical purple")

        # --- 2. idempotent re-run: no repo create, no PUT --------------------
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks", "--project", "app")
        self.assertEqual(r.rc, 0, "idempotent re-run exit")
        self.assertNotIn("repo create", self.log_text(), "re-run wrongly re-created the repo")
        self.assertNotIn("-X PUT", self.log_text(), "re-run wrongly re-wrote an asset (PUT on unchanged file)")
        self.assertIn("init: asset %s (unchanged)" % ASSET_DSTS[0], r.out,
                      "re-run did not report the task template as unchanged")

        # --- 3. flag-don't-clobber: a customized asset is reported, not overwritten
        self.truncate_log()
        custom = os.path.join(self.state, "custom.yml")
        self._write(custom, "name: my customized reaper\n")
        self.mk_content(".github/workflows/reclaim-stale.yml", custom)
        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "flag-don't-clobber exit")
        self.assertIn("init: asset .github/workflows/reclaim-stale.yml (customized)", r.out,
                      "customized asset not flagged")
        self.assertTrue(filecmp.cmp(self._contents(".github/workflows/reclaim-stale.yml"), custom, shallow=False),
                        "customized asset was overwritten — flag-don't-clobber violated")
        self.assertNotIn("-X PUT", self.log_text(),
                         "a PUT was issued during a run where every asset already exists")


if __name__ == "__main__":
    unittest.main()
