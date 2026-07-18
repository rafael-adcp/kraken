#!/usr/bin/env python3
"""kraken.py status: the read-only operator console, mechanized. Pins the
review/decision/in-flight queues, the heartbeat-age anchored to the worker's
last liveness marker, the merged-PR-but-open-issue orphan heuristic (flag, never
act), the launch recon — plus the hard guarantee that the whole thing is
read-only."""
import json
import os
import re
import unittest

from harness import KrakenConformanceTest, ago_iso, now_iso


class StatusTests(KrakenConformanceTest):
    def _queue_snapshot(self):
        rows = []
        issues_dir = os.path.join(self.state, "issues")
        for name in sorted(os.listdir(issues_dir)):
            rows.append("%s|labels=%s|comments=%d" % (
                name, ",".join(self.labels(int(name))), self.comment_count(int(name))))
        return "\n".join(rows)

    def test_status(self):
        # Review queue: #88 delivered with a MERGED PR (orphan), #91 OPEN PR.
        self.mk_issue(88, "orphan candidate", "kraken-task", "project:app", "awaiting-merge")
        self.mk_comment(88, '> d\n\n<!-- kraken {"type":"delivered","worker":"w1","pr":"https://github.com/OWNER/work/pull/5"} -->\n\nlanded')
        self.mk_pr(5, "MERGED", now_iso())
        self.mk_issue(91, "healthy delivery", "kraken-task", "project:app", "awaiting-merge")
        self.mk_comment(91, '> d\n\n<!-- kraken {"type":"delivered","worker":"w2","pr":"https://github.com/OWNER/work/pull/6"} -->')
        self.mk_pr(6, "OPEN")

        # Decision queue.
        self.mk_issue(97, "needs a human call", "kraken-task", "project:app", "needs-decision")

        # In flight: #99 DEAD (claimed 8h ago, operator poked now), #100 ALIVE.
        self.mk_issue(99, "dead worker, operator poked it", "kraken-task", "project:app", "in-progress")
        self.mk_comment(99, '> d\n\n<!-- kraken {"type":"claim","worker":"dead-worker"} -->\n', ago_iso(8))
        self.mk_comment(99, "any progress? — the operator", ago_iso(0))
        self.mk_issue(100, "live worker", "kraken-task", "project:app", "in-progress")
        self.mk_comment(100, '> d\n\n<!-- kraken {"type":"claim","worker":"live-worker"} -->', ago_iso(9))
        self.mk_comment(100, '> d\n\n<!-- kraken {"type":"heartbeat","worker":"live-worker"} -->', ago_iso(0))

        # A startable task in another project, and an empty registered project.
        self.mk_issue(12, "queued elsewhere", "kraken-task", "project:web")
        self.mk_label("project:idle")

        snapshot = self._queue_snapshot()

        # --- 1. human console ------------------------------------------------
        r = self.kraken("status", "OWNER/tasks")
        self.assertEqual(r.rc, 0, "status human exit")
        out = r.out
        self.assertIn("#88  orphan candidate", out, "review item #88 missing")
        self.assertIn("https://github.com/OWNER/work/pull/5", out, "PR link for #88 missing")
        self.assertIn("#97  needs a human call", out, "decision item #97 missing")

        # Heartbeat age anchors to the machine line.
        self.assertRegex(out, r"#99 .*worker dead-worker .*last heartbeat 8h ago",
                         "in-flight #99 heartbeat age not anchored (expected 8h)")
        self.assertRegex(out, r"#100 .*worker live-worker", "in-flight #100 missing worker")
        self.assertFalse(re.search(r"#100 .*last heartbeat 8h ago", out),
                         "in-flight #100 read the stale claim, not the fresh heartbeat")

        # Orphan heuristic: #88 (merged PR) flagged; #91 (open PR) not.
        self.assertIn("possible orphan(s): #88", out, "#88 not flagged as an orphan")
        self.assertFalse(re.search(r"orphan.*#91", out), "#91 (open PR) wrongly flagged as an orphan")

        # Launch recon lists every project: label, incl. the empty one.
        for p in ("app", "idle", "web"):
            self.assertIn("--project %s" % p, out, "launch recon missing project:%s" % p)
        self.assertIn("OWNER/tasks --worker-name <worker-name>", out, "launch line shape wrong")

        # --- 2. THE read-only guarantee: nothing was written -----------------
        self.assertEqual(self._queue_snapshot(), snapshot, "status mutated the queue — must be read-only")
        self.assertTrue(self.has_label(88, "awaiting-merge"), "status changed #88's label")

        # --- 3. --json: the documented stable schema -------------------------
        js = json.loads(self.kraken("status", "OWNER/tasks", "--json").out)
        self.assertEqual(len(js["review_queue"]), 2, "json review_queue wrong length")
        self.assertIn(97, [d["number"] for d in js["decision_queue"]], "json decision_queue missing #97")
        self.assertEqual(js["orphans"], [88], "json orphans != [88]")
        self.assertTrue(next(d for d in js["review_queue"] if d["number"] == 88)["orphan"],
                        "json #88 orphan flag not true")
        self.assertFalse(next(d for d in js["review_queue"] if d["number"] == 91)["orphan"],
                         "json #91 orphan flag not false")
        f99 = next(d for d in js["in_flight"] if d["number"] == 99)
        self.assertEqual(f99["worker"], "dead-worker", "json in_flight #99 worker wrong")
        self.assertGreaterEqual(f99["heartbeat_age_seconds"], 28000, "json in_flight #99 age not anchored")
        self.assertEqual(js["projects"], ["app", "idle", "web"], "json projects list wrong")

        # --- 4. --project scopes every list to that project ------------------
        scoped = json.loads(self.kraken("status", "OWNER/tasks", "--project", "web", "--json").out)
        self.assertEqual(scoped["review_queue"], [], "--project web should have empty review queue")
        self.assertEqual(scoped["decision_queue"], [], "--project web should have empty decision queue")
        self.assertEqual(scoped["in_flight"], [], "--project web should have empty in_flight")
        self.assertEqual(scoped["project"], "web", "--project not reflected in json")


if __name__ == "__main__":
    unittest.main()
