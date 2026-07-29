#!/usr/bin/env python3
"""The lease clock is the SERVER's, at both ends (PROTOCOL.md §5.1).

A lease timestamp is a commit date GitHub stamped. Subtracting it from the
worker's own wall clock does not measure the lease's age, it measures the age
plus however far the two machines disagree — and the disagreement decides real
outcomes: a worker running fast steals leases that are still alive, one running
slow keeps dead ones alive past their TTL. Neither failure announces itself; the
thief's audit comment reads exactly like a legitimate steal.

The stub's clock is moved away from this machine's (`set_server_clock`), which
is the same experiment as moving the worker's clock away from the server's, and
every seeded age is then measured from the server's now — the anchor the spec
defines. Each case below is written so the two clocks give OPPOSITE verdicts, so
a reader that fell back to `time.time()` fails it rather than passing by
accident.
"""
import json
import unittest

from harness import KrakenConformanceTest, lease_ttl


class LeaseClockSkewTests(KrakenConformanceTest):
    def test_a_worker_running_fast_does_not_steal_a_live_lease(self):
        # The server is two TTLs BEHIND this machine — i.e. the worker's clock
        # runs fast. The lease was renewed a minute ago on the server's clock;
        # subtracting it from local time makes it look 2×TTL stale.
        self.set_server_clock(-2 * lease_ttl())
        self.mk_issue(1, "running", "kraken-task", "project:app", "in-progress")
        held = self.mk_claim_ref(1, "w-other", age_seconds=60)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 3, "a live lease was stolen off a skewed clock: "
                                  "%s%s" % (r.out, r.err))
        self.assertEqual(self.claim_ref(1), held, "the live lease was replaced")
        self.assertEqual(self.comment_count(1), 0, "a live lease got a comment")

    def test_a_worker_running_slow_still_steals_an_expired_lease(self):
        # The mirror image: the server is two TTLs AHEAD, so a lease that is
        # genuinely past its TTL looks freshly renewed — even future-dated — to
        # the local clock, and a reader trusting that clock would leave the task
        # locked to a dead worker forever.
        self.set_server_clock(2 * lease_ttl())
        self.mk_issue(2, "abandoned", "kraken-task", "project:app", "in-progress")
        dead = self.mk_claim_ref(2, "w-dead", age_seconds=lease_ttl() + 60)

        r = self.kraken("claim-next", "OWNER/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "an expired lease held the task off a skewed "
                                  "clock: %s%s" % (r.out, r.err))
        self.assertNotEqual(self.claim_ref(2), dead, "the expired lease survived")
        self.assertIn('"type":"lease-expired"', "\n".join(self.comment_bodies(2)))

    def test_the_named_claim_probe_reads_the_same_server_clock(self):
        # `claim <issue>` reads no queue, so it probes the lease itself — its own
        # call into the clock, and one that must reach the drain's verdict.
        self.set_server_clock(-2 * lease_ttl())
        self.mk_issue(3, "live", "kraken-task", "project:app", "in-progress")
        live = self.mk_claim_ref(3, "w-other", age_seconds=60)

        r = self.kraken("claim", "OWNER/tasks", 3, "w-drain")
        self.assertEqual(r.rc, 10, "the named claim probe took a live lease off "
                                   "a skewed clock: %s%s" % (r.out, r.err))
        self.assertEqual(self.claim_ref(3), live)

    def test_the_console_ages_leases_on_the_server_clock(self):
        # The operator console is a third reader with a clock of its own. It
        # reports the age a human decides on ("is that worker alive?"), so a
        # skewed one is a wrong answer to the only question the row exists for.
        self.set_server_clock(-2 * lease_ttl())
        self.mk_issue(4, "running", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(4, "w-other", age_seconds=60, mtype="heartbeat",
                          msg="still going")

        report = json.loads(self.kraken("status", "OWNER/tasks", "--json").out)
        rows = report["in_flight"]
        self.assertEqual([r["number"] for r in rows], [4], "the in-flight row is gone")
        self.assertFalse(rows[0]["stale"],
                         "a lease renewed a minute ago was reported stale")
        self.assertLess(rows[0]["heartbeat_age_seconds"], lease_ttl(),
                        "the heartbeat age carries the worker's clock skew")

    def test_the_envelope_lease_numbers_use_the_server_clock(self):
        # next-action hands the worker `seconds_remaining` as a NUMBER it acts
        # on, so skew there is a worker that renews far too late — or believes a
        # dead lease has hours left.
        self.set_server_clock(-2 * lease_ttl())
        self.mk_issue(5, "mine", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(5, "w-drain", age_seconds=60)

        r = self.kraken("next-action", "OWNER/tasks", "app", "w-drain")
        env = json.loads(r.out)
        self.assertEqual(env["action"], "execute", "the held task did not resume: "
                                                   "%s%s" % (r.out, r.err))
        self.assertTrue(env["resumed"])
        remaining = env["lease"]["seconds_remaining"]
        self.assertLessEqual(remaining, lease_ttl(),
                             "seconds_remaining exceeds the whole TTL — the "
                             "epoch was aged against the local clock")
        self.assertGreater(remaining, 0, "a live lease was reported already over")


if __name__ == "__main__":
    unittest.main()
