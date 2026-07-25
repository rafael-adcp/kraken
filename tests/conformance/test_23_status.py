#!/usr/bin/env python3
"""kraken.py status: the read-only operator console, mechanized. Pins the
review/decision/in-flight queues, the heartbeat-age anchored to the claim ref's
commit date, the silent-claim flag, the merged-PR-but-open-issue orphan heuristic
(flag, never act), the queue-hygiene report, the launch recon — plus the hard
guarantee that the whole thing is read-only."""
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

        # In flight: age comes from the claim ref's commit date. #99 DEAD (claim
        # ref 8h old, operator poked the thread — must NOT reset the clock),
        # #100 ALIVE (heartbeated 0h ago). The operator comment on #99 proves the
        # anchor ignores the timeline.
        self.mk_issue(99, "dead worker, operator poked it", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(99, "dead-worker", age_hours=8)
        self.mk_comment(99, "any progress? — the operator", ago_iso(0))
        self.mk_issue(100, "live worker", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(100, "live-worker", age_hours=0, mtype="heartbeat", msg="writing tests")

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
        self.assertRegex(out, r"#99 .*worker dead-worker .*lease renewed 8h ago",
                         "in-flight #99 lease age not anchored (expected 8h)")
        self.assertRegex(out, r"#100 .*worker live-worker", "in-flight #100 missing worker")
        self.assertFalse(re.search(r"#100 .*lease renewed 8h ago", out),
                         "in-flight #100 read the stale lease, not the fresh renewal")

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
        self.assertEqual(
            [d["pr_source"] for d in js["review_queue"]], ["marker", "marker"],
            "json review_queue must say the PR came from the delivered marker")
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

    def test_pr_link_comes_from_the_delivered_marker(self):
        """#71 — the delivery URL is the `delivered` marker's `pr` field
        (PROTOCOL.md §8), never something grepped out of the prose. The regex
        survives only for threads delivered before the field existed, and what it
        finds is reported as legacy."""
        # #1: a human quotes an unrelated PR of another repo BEFORE the delivery.
        self.mk_issue(1, "marker wins", "kraken-task", "project:app", "awaiting-merge")
        self.mk_comment(1, "cf. https://github.com/OWNER/other/pull/999 — unrelated")
        self.mk_comment(1, '> d\n\n<!-- kraken {"type":"delivered","worker":"w1","pr":"https://github.com/OWNER/work/pull/5"} -->\n\nlanded')
        self.mk_pr(5, "OPEN")
        self.mk_pr(999, "MERGED", now_iso())
        # #2: a legacy thread — prose only, no marker carrying `pr`.
        self.mk_issue(2, "legacy delivery", "kraken-task", "project:app", "awaiting-merge")
        self.mk_comment(2, "delivered in https://github.com/OWNER/work/pull/6")
        self.mk_pr(6, "OPEN")
        # #3: delivered with no PR recorded at all — must not break the read.
        self.mk_issue(3, "no pr", "kraken-task", "project:app", "awaiting-merge")
        self.mk_comment(3, '> d\n\n<!-- kraken {"type":"delivered","worker":"w1"} -->\n\npatch in this comment')

        r = self.kraken("status", "OWNER/tasks", "--project", "app")
        self.assertEqual(r.rc, 0, "status exit: %s" % r.err)
        self.assertRegex(r.out, r"#1 .*https://github\.com/OWNER/work/pull/5",
                         "#1 must report the marker's PR")
        self.assertNotIn("other/pull/999", r.out,
                         "#1 reported a PR a human merely mentioned")
        self.assertNotIn("possible orphan(s): #1", r.out,
                         "#1 was flagged an orphan off someone else's merged PR")
        self.assertRegex(r.out, r"#2 .*pull/6.*legacy",
                         "#2's free-text link must be marked legacy")
        self.assertRegex(r.out, r"#3 .*no PR link recorded")

        js = json.loads(self.kraken("status", "OWNER/tasks", "--project", "app",
                                    "--json").out)
        sources = {d["number"]: (d["pr_url"], d["pr_source"]) for d in js["review_queue"]}
        self.assertEqual(sources, {
            1: ("https://github.com/OWNER/work/pull/5", "marker"),
            2: ("https://github.com/OWNER/work/pull/6", "legacy-free-text"),
            3: (None, None),
        }, "json pr_url/pr_source wrong: %s" % sources)

    def test_silent_claim_is_flagged(self):
        """A lease about to expire reads exactly like one being renewed, and
        only the operator knows which of their workers is alive. The console has
        to say which leases no longer hold."""
        self.mk_issue(1, "silent", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(1, "dead-worker", age_hours=8)
        self.mk_issue(2, "alive", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(2, "live-worker", age_hours=0, mtype="heartbeat", msg="going")

        r = self.kraken("status", "OWNER/tasks", "--project", "app")
        self.assertEqual(r.rc, 0, "status exit: %s" % r.err)
        self.assertRegex(r.out, r"#1 .*lease expired — the next drain steals it")
        self.assertNotRegex(r.out, r"#2 .*lease expired",
                            "a live worker's lease was flagged expired")

        js = json.loads(self.kraken("status", "OWNER/tasks", "--project", "app",
                                    "--json").out)
        flags = {d["number"]: d["stale"] for d in js["in_flight"]}
        self.assertEqual(flags, {1: True, 2: False})

    def test_queue_hygiene_reports_what_a_worker_cannot_start(self):
        """The read-side twin of the arrival-time validator: same three checks,
        computed off the walk status already performed."""
        good = "### Goal\n\nShip it.\n\n### Acceptance\n\n`npm test` passes.\n"
        self.mk_issue(1, "fine", "kraken-task", "project:app")
        self.mk_body(1, good)
        self.mk_issue(2, "no project label", "kraken-task")
        self.mk_body(2, good)
        self.mk_issue(3, "no sections", "kraken-task", "project:app")
        self.mk_body(3, "just some prose")

        r = self.kraken("status", "OWNER/tasks", "--project", "app")
        self.assertEqual(r.rc, 0, "status exit: %s" % r.err)
        self.assertIn("Queue hygiene", r.out)

        js = json.loads(self.kraken("status", "OWNER/tasks", "--project", "app",
                                    "--json").out)
        reported = {d["number"]: d["missing"] for d in js["queue_hygiene"]}
        self.assertEqual(reported, {2: ["project label"], 3: ["Goal", "Acceptance"]},
                         "hygiene report wrong: %s" % reported)

        # A compliant queue says nothing at all — no noise on the happy path.
        js = json.loads(self.kraken("status", "OWNER/tasks", "--json").out)
        self.mk_body(2, good)
        self.mk_body(3, good)
        self.set_labels(2, ["kraken-task", "project:app"])
        js = json.loads(self.kraken("status", "OWNER/tasks", "--json").out)
        self.assertEqual(js["queue_hygiene"], [])
        self.assertNotIn("Queue hygiene",
                         self.kraken("status", "OWNER/tasks").out)


if __name__ == "__main__":
    unittest.main()
