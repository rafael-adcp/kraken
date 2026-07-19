#!/usr/bin/env python3
"""init --upgrade conformance: the drift-repair path, proven against the gh stub
with no LLM.

The plugin's bundled bytes are the single source of truth — there is no manifest
of past release hashes. An asset is either `unchanged` (byte-identical to the
bundled copy) or `drifted` (anything else). `init --upgrade` re-syncs every
drifted asset to the bundled copy; plain `init` REPORTS the drift but writes
nothing over it. Drift is generated synthetically here (the bundled bytes plus a
trailing marker line), so the test needs no vendored fixture of a past release.
"""
import filecmp
import os
import unittest

from harness import KrakenConformanceTest, SCRIPTS

# (bundled src name, destination path) for the six assets init manages.
ASSETS = [
    ("task-template.yml", ".github/ISSUE_TEMPLATE/task.yml"),
    ("kraken.py", ".github/kraken.py"),
    ("reclaim-stale.yml", ".github/workflows/reclaim-stale.yml"),
    ("cleanup-closed.yml", ".github/workflows/cleanup-closed.yml"),
    ("requeue-on-reply.yml", ".github/workflows/requeue-on-reply.yml"),
    ("validate-task.yml", ".github/workflows/validate-task.yml"),
]
DST = dict(ASSETS)
BUNDLED = {name: os.path.join(SCRIPTS, name) for name, _ in ASSETS}

# Two assets are seeded drifted; the rest are seeded byte-identical to bundled.
DRIFTED = ("kraken.py", "reclaim-stale.yml")
IN_SYNC = ("task-template.yml", "cleanup-closed.yml",
           "requeue-on-reply.yml", "validate-task.yml")


class InitUpgradeTests(KrakenConformanceTest):
    def _contents(self, dst):
        return os.path.join(self.state, "contents", dst)

    def _put_lines_for(self, dst):
        return [l for l in self.log_lines()
                if ("contents/%s" % dst) in l and "PUT" in l]

    def _drifted_src(self, name):
        """A temp copy of the bundled asset with a trailing marker line — so it
        differs from the bundled bytes (drift) while staying a plausible stale
        vendored copy. --upgrade restores the exact bundled bytes over it."""
        with open(BUNDLED[name], "r", encoding="utf-8") as f:
            text = f.read()
        src = os.path.join(self.state, "drifted-" + name)
        self._write(src, text + "\n# stale vendored copy (drifted)\n")
        return src

    def _seed_drift(self):
        """Seed an existing coordination repo mid-drift:
          - kraken.py + reclaim-stale.yml = bundled bytes + a marker -> DRIFTED
          - the remaining four            = the current bundled bytes -> UNCHANGED
        so a run touches only the two drifted files and nothing is `absent`."""
        self.mk_repo("OWNER/tasks")
        for name in DRIFTED:
            self.mk_content(DST[name], self._drifted_src(name))
        for name in IN_SYNC:
            self.mk_content(DST[name], BUNDLED[name])

    def test_upgrade_resyncs_every_drifted_asset(self):
        self._seed_drift()
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks", "--upgrade")
        self.assertEqual(r.rc, 0, "init --upgrade exit")

        # Each drifted asset is re-synced to the bundled copy and reported.
        for name in DRIFTED:
            dst = DST[name]
            self.assertIn("init: asset %s (upgraded)" % dst, r.out,
                          "%s not reported upgraded" % dst)
            self.assertTrue(filecmp.cmp(self._contents(dst), BUNDLED[name], shallow=False),
                            "%s not re-synced to the bundled copy" % dst)
            # A contents-API update MUST carry the blob sha or GitHub rejects it.
            puts = self._put_lines_for(dst)
            self.assertTrue(puts, "no PUT issued to upgrade %s" % dst)
            self.assertTrue(any("sha=" in l for l in puts),
                            "upgrade PUT for %s omitted the required blob sha" % dst)

        # The in-sync assets are silently unchanged — no PUT.
        for name in IN_SYNC:
            dst = DST[name]
            self.assertIn("init: asset %s (unchanged)" % dst, r.out,
                          "%s not reported unchanged" % dst)
            self.assertEqual(self._put_lines_for(dst), [],
                             "a PUT was issued over an in-sync asset")
        self.assertIn("assets_upgraded=2", r.out)
        self.assertIn("assets_drifted=0", r.out)

    def test_plain_init_reports_drift_without_writing(self):
        self._seed_drift()
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "plain init exit")

        # Plain init classifies drift but never overwrites.
        for name in DRIFTED:
            dst = DST[name]
            self.assertIn("init: asset %s (drifted)" % dst, r.out,
                          "%s not reported drifted by plain init" % dst)
            # Untouched: still the drifted bytes, never the bundled copy.
            self.assertFalse(filecmp.cmp(self._contents(dst), BUNDLED[name], shallow=False),
                             "%s was overwritten by a plain (create-only) init" % dst)

        # Nothing at all is written: no create (all six exist), no upgrade.
        self.assertNotIn("-X PUT", self.log_text(),
                         "plain init wrote an asset (a PUT) when it must only report")
        self.assertIn("assets_drifted=2", r.out)
        self.assertIn("assets_upgraded=0", r.out)
        # The report points the operator at the repair command.
        self.assertIn("init --upgrade", r.out,
                      "plain init did not hint at --upgrade for the drifted assets")


if __name__ == "__main__":
    unittest.main()
