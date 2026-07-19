#!/usr/bin/env python3
"""init --upgrade conformance: the drift-repair path, proven against the gh stub
with no LLM.

`init --upgrade` replaces an installed asset whose bytes match a KNOWN prior
release (recorded in kraken.py's ASSET_MANIFEST, so it was shipped and never
hand-edited) with the bundled copy, while leaving a `customized` asset — one
matching no release — untouched. Plain `init` over the same repo REPORTS
`outdated` vs `customized` but writes nothing over either. The real protocol/3
(v0.4.0) asset bytes are used as the "older release", so the manifest hashes
under test are the shipped ones.
"""
import filecmp
import os
import unittest

from harness import KrakenConformanceTest, SCRIPTS

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "protocol3")

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


class InitUpgradeTests(KrakenConformanceTest):
    def _contents(self, dst):
        return os.path.join(self.state, "contents", dst)

    def _put_lines_for(self, dst):
        return [l for l in self.log_lines()
                if ("contents/%s" % dst) in l and "PUT" in l]

    def _seed_drift(self):
        """Seed an existing coordination repo mid-drift:
          - kraken.py + reclaim-stale.yml  = the v0.4.0 (protocol/3) release  -> OUTDATED
          - cleanup-closed.yml             = a hand edit matching no release  -> CUSTOMIZED
          - the remaining three            = the current bundled bytes        -> UNCHANGED
        so a run touches only the two outdated files and nothing is `absent`."""
        self.mk_repo("OWNER/tasks")
        self.mk_content(DST["kraken.py"],
                        os.path.join(FIXTURES, "kraken.py.txt"))
        self.mk_content(DST["reclaim-stale.yml"],
                        os.path.join(FIXTURES, "reclaim-stale.yml.txt"))
        custom = os.path.join(self.state, "custom-cleanup.yml")
        self._write(custom, "name: my hand-edited cleanup\non: {}\n")
        self.mk_content(DST["cleanup-closed.yml"], custom)
        self.custom_cleanup = custom
        for name in ("task-template.yml", "requeue-on-reply.yml", "validate-task.yml"):
            self.mk_content(DST[name], BUNDLED[name])

    def test_upgrade_replaces_outdated_leaves_customized(self):
        self._seed_drift()
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks", "--upgrade")
        self.assertEqual(r.rc, 0, "init --upgrade exit")

        # The two outdated assets are replaced by the bundled copy and reported.
        for name in ("kraken.py", "reclaim-stale.yml"):
            dst = DST[name]
            self.assertIn("init: asset %s (upgraded)" % dst, r.out,
                          "%s not reported upgraded" % dst)
            self.assertTrue(filecmp.cmp(self._contents(dst), BUNDLED[name], shallow=False),
                            "%s not replaced with the bundled copy" % dst)
            # A contents-API update MUST carry the blob sha or GitHub rejects it.
            puts = self._put_lines_for(dst)
            self.assertTrue(puts, "no PUT issued to upgrade %s" % dst)
            self.assertTrue(any("sha=" in l for l in puts),
                            "upgrade PUT for %s omitted the required blob sha" % dst)

        # The customized asset is flagged and left byte-for-byte untouched.
        cc = DST["cleanup-closed.yml"]
        self.assertIn("init: asset %s (customized)" % cc, r.out,
                      "hand-edited asset not flagged customized")
        self.assertTrue(filecmp.cmp(self._contents(cc), self.custom_cleanup, shallow=False),
                        "customized asset was overwritten — flag-don't-clobber violated")
        self.assertEqual(self._put_lines_for(cc), [],
                         "a PUT was issued over a customized asset")

        # The three current assets are silently unchanged.
        for name in ("task-template.yml", "requeue-on-reply.yml", "validate-task.yml"):
            self.assertIn("init: asset %s (unchanged)" % DST[name], r.out,
                          "%s not reported unchanged" % DST[name])
        self.assertIn("assets_upgraded=2", r.out)
        self.assertIn("assets_customized=1", r.out)

    def test_plain_init_reports_outdated_and_customized_without_writing(self):
        self._seed_drift()
        self.truncate_log()
        r = self.kraken("init", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "plain init exit")

        # Plain init classifies but never overwrites: outdated vs customized.
        for name in ("kraken.py", "reclaim-stale.yml"):
            dst = DST[name]
            self.assertIn("init: asset %s (outdated)" % dst, r.out,
                          "%s not reported outdated by plain init" % dst)
            # Untouched: still the v0.4.0 bytes, never the bundled copy.
            self.assertFalse(filecmp.cmp(self._contents(dst), BUNDLED[name], shallow=False),
                             "%s was overwritten by a plain (create-only) init" % dst)
        self.assertIn("init: asset %s (customized)" % DST["cleanup-closed.yml"], r.out,
                      "customized asset not reported by plain init")

        # Nothing at all is written: no create (all six exist), no upgrade.
        self.assertNotIn("-X PUT", self.log_text(),
                         "plain init wrote an asset (a PUT) when it must only report")
        self.assertIn("assets_outdated=2", r.out)
        self.assertIn("assets_upgraded=0", r.out)
        # The report points the operator at the repair command.
        self.assertIn("init --upgrade", r.out,
                      "plain init did not hint at --upgrade for the outdated assets")


if __name__ == "__main__":
    unittest.main()
