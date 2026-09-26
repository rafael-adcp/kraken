#!/usr/bin/env python3
"""`kraken.py watch --exit-on-wake --worker <name>`: the watcher shape for a
harness whose background commands wake the agent only when they exit (GitHub
Copilot CLI). It prints the same `kraken-queue:` line and exits 0 on its first
wake; re-armed on an unchanged queue it stays silent, so an agent that re-arms
after every drain never loops on a task it just could not take."""
import unittest

from harness import KrakenConformanceTest

FAST = {"KRAKEN_WATCH_POLL_SECONDS": "0"}


class WatchExitOnWakeTests(KrakenConformanceTest):
    def test_exits_zero_on_the_first_wake(self):
        self.mk_issue(7, "a task", "kraken-task", "project:app")
        r = self.kraken("watch", "acme/tasks", "app", "--exit-on-wake",
                        "--worker", "w1", env=FAST)
        self.assertEqual(r.rc, 0, "watch --exit-on-wake exit")
        self.assertEqual(r.out, "kraken-queue: 1 startable task(s) in project:app (#7)")

    def test_needs_a_worker_name(self):
        self.mk_issue(7, "a task", "kraken-task", "project:app")
        r = self.kraken("watch", "acme/tasks", "app", "--exit-on-wake", env=FAST)
        self.assertEqual(r.rc, 2, "--exit-on-wake without --worker must be refused")
        self.assertIn("--worker", r.err)


if __name__ == "__main__":
    unittest.main()
