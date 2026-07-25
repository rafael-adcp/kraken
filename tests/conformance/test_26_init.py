#!/usr/bin/env python3
"""init conformance: the bootstrap kraken.py init mechanizes — verify or create
the coordination repo PRIVATE, install the bundled assets via the contents API
(create / skip-unchanged / flag-drifted), PRUNE the assets this protocol
revision retired, and upsert the canonical labels — proven against the gh stub
with no LLM."""
import filecmp
import json
import os
import unittest

from harness import KrakenConformanceTest, SCRIPTS

# protocol/5 installs ONE file: the issue form. Nothing in the coordination repo
# is executed, so there is nothing for a vendored transition program to run.
ASSET_SRCS = ["task-template.yml"]
ASSET_DSTS = [".github/ISSUE_TEMPLATE/task.yml"]
# ... and deletes everything an earlier release installed to be executed,
# including the vendored program itself.
RETIRED_DSTS = [".github/workflows/reclaim-stale.yml",
                ".github/workflows/requeue-on-reply.yml",
                ".github/workflows/cleanup-closed.yml",
                ".github/workflows/validate-task.yml",
                ".github/kraken.py"]
RETIRED_DST = RETIRED_DSTS[0]


class InitTests(KrakenConformanceTest):
    def _contents(self, dst):
        return os.path.join(self.state, "contents", dst)

    def test_init(self):
        # --- 1. fresh bootstrap: absent repo created private, assets + labels --
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks", "--project", "app")
        self.assertEqual(r.rc, 0, "fresh init exit")
        self.assertIn("POST /user/repos", self.log_text(),
                      "fresh init did not create the repo")
        with open(os.path.join(self.state, "repo", "create.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["private"], "repo was not created PRIVATE")

        for src_name, dst in zip(ASSET_SRCS, ASSET_DSTS):
            src = os.path.join(SCRIPTS, src_name)
            self.assertTrue(filecmp.cmp(self._contents(dst), src, shallow=False),
                            "asset %s not installed byte-identical to bundled %s" % (dst, src_name))
            self.assertIn("init: asset %s (created)" % dst, r.out, "asset %s not reported created" % dst)

        for lbl in ("kraken-task", "in-progress", "needs-decision", "awaiting-merge",
                    "priority:high", "project:app"):
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
        self.assertNotIn("POST /user/repos", self.log_text(), "re-run wrongly re-created the repo")
        self.assertNotIn("PUT ", self.log_text(), "re-run wrongly re-wrote an asset (PUT on unchanged file)")
        self.assertIn("init: asset %s (present)" % ASSET_DSTS[0], r.out,
                      "re-run did not report the task template as already present")

        # --- 3. create-only: an edited asset is left exactly as it is ---------
        # Nothing in the coordination repo is executed under protocol/5, so a
        # template the operator tuned is their business — init reports it present
        # and writes nothing over it.
        self.truncate_log()
        custom = os.path.join(self.state, "custom.yml")
        self._write(custom, "name: my hand-edited task form\n")
        self.mk_content(ASSET_DSTS[0], custom)
        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "create-only exit")
        self.assertIn("init: asset %s (present)" % ASSET_DSTS[0], r.out)
        self.assertTrue(filecmp.cmp(self._contents(ASSET_DSTS[0]), custom, shallow=False),
                        "plain init overwrote an edited asset — create-only violated")
        self.assertNotIn("PUT ", self.log_text(),
                         "a PUT was issued during a plain run where every asset already exists")

    def test_init_prunes_every_retired_workflow(self):
        """Re-running init on a repo stood up by an older release is the
        migration: each retired workflow is deleted, so no server-side job keeps
        mutating labels under semantics the workers no longer implement."""
        self.mk_repo("OWNER/tasks")
        for i, dst in enumerate(RETIRED_DSTS):
            vendored = os.path.join(self.state, "old-%d.yml" % i)
            # The bytes an older init installed: the bundled header is the
            # sentinel that proves the file is kraken's own to delete.
            if dst.endswith(".py"):
                # The vendored transition program: its own module docstring is
                # the sentinel that proves the file is kraken's own.
                self._write(vendored,
                            "#!/usr/bin/env python3\n\"\"\"kraken.py \u2014 the bundled "
                            "worker-side transitions.\"\"\"\n")
            else:
                self._write(vendored,
                            "# Something for kraken. Installed by `init` into the "
                            "coordination repo as\n# %s\nname: retired\n" % dst)
            self.mk_content(dst, vendored)

        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "init exit with retired assets present")
        for dst in RETIRED_DSTS:
            self.assertIn("init: asset %s (removed)" % dst, r.out,
                          "%s was not reported removed" % dst)
            self.assertFalse(os.path.isfile(self._contents(dst)),
                             "%s survived the prune" % dst)
        self.assertIn("assets_removed=%d" % len(RETIRED_DSTS), r.out)

        # Idempotent: a second run finds nothing to prune and says removed=0.
        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "second init exit")
        self.assertIn("assets_removed=0", r.out, "the prune is not idempotent")

    def test_prune_spares_a_file_that_is_not_krakens(self):
        """The prune is gated on kraken's own header. An operator who wrote their
        own workflow at that path keeps it — a bootstrap command must never
        delete something it did not install."""
        self.mk_repo("OWNER/tasks")
        mine = os.path.join(self.state, "mine.yml")
        self._write(mine, "name: my own nightly job\non:\n  schedule: []\n")
        self.mk_content(RETIRED_DST, mine)

        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "init exit")
        self.assertIn("assets_removed=0", r.out)
        self.assertTrue(filecmp.cmp(self._contents(RETIRED_DST), mine, shallow=False),
                        "init deleted a workflow it did not install")


if __name__ == "__main__":
    unittest.main()
