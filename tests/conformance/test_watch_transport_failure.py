#!/usr/bin/env python3
"""A watcher whose queue reads keep failing must be distinguishable from an idle
queue. `kraken.py watch` swallowed transport faults — no line, no counter, no
exit — so an expired token or a dead network left a watcher spinning forever
while looking exactly like "nothing to do". It must now speak on stderr and,
past a configurable ceiling, die with the transport code (20) so the operator's
monitor shows a dead process instead of a live and deaf one.

(The healthy-transport control — a wake on stdout, silence on stderr, and the
counter reset — is the unit suite's WatchFailureLoopTests: a watcher over a
working transport never returns, so it cannot be run as a subprocess here.)"""
import unittest

from harness import KrakenConformanceTest


class WatchTransportFailureTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.mk_issue(7, "ready task", "kraken-task", "project:app")

    def _watch(self, max_failures):
        # Every queue read fails, exactly as it would with a revoked token. Poll
        # spacing of 0: the cadence is not what is under test, and a real 60s
        # sleep would hang the suite.
        return self.kraken(
            "watch", "acme/tasks", "app",
            env={"KRAKEN_WATCH_POLL_SECONDS": "0",
                 "KRAKEN_WATCH_MAX_FAILURES": str(max_failures)},
            fail="graphql",
        )

    def test_unreadable_queue_speaks_and_dies(self):
        r = self._watch(3)

        self.assertEqual(r.rc, 20,
                         "a watcher that can never read the queue must exit 20, not spin")
        self.assertIn("kraken-watch:", r.err,
                      "the watcher swallowed the transport failure instead of speaking")
        self.assertIn("giving up", r.err, "no give-up line before the exit")
        self.assertEqual(r.out, "",
                         "a diagnostic must never ride stdout — that is the wake channel")

    def test_first_failure_is_audible_immediately(self):
        # Ceiling of 2, so the run is: failure #1 warns, failure #2 gives up. The
        # operator hears about the outage on the first poll, not an hour later.
        r = self._watch(2)
        self.assertIn("kraken-watch: 1 ", r.err,
                      "the first failed queue read produced no stderr line")
        self.assertEqual(r.rc, 20, "the ceiling did not stop the watcher")

    def test_repeated_failures_do_not_flood_stderr(self):
        # 12 failures at the default spacing (warn on #1, then every 10th) is
        # two lines plus the give-up — not one line per poll.
        r = self._watch(12)
        self.assertEqual(r.err.count("kraken-watch:"), 3,
                         "unthrottled failure reporting would flood the log: %r" % r.err)


if __name__ == "__main__":
    unittest.main()
