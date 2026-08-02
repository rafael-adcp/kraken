#!/usr/bin/env python3
"""The lease's read side (PROTOCOL.md §5): expiry is applied by whoever READS
the queue, so a claim ref stops holding a task the moment its TTL is up — no
scheduled job, no repair pass, no operator.

What is pinned here:
  - a lease renewed inside the TTL blocks the claim;
  - a lease past the TTL does not, and the claim STEALS it;
  - a lease whose commit cannot be read is treated as expired (fail open to the
    steal) rather than as holding forever behind a clock nobody can read;
  - the free `list-startable` read applies the same clock the claim does, so an
    idle-queue poll never invites a drain onto a task that is genuinely held —
    nor hides one that is free.
"""
import unittest

from harness import KrakenConformanceTest, lease_ttl


class LeaseExpiryTests(KrakenConformanceTest):
    def test_a_renewed_lease_still_blocks_the_claim(self):
        self.mk_issue(1, "running", "kraken-task", "project:app", "in-progress")
        held = self.mk_claim_ref(1, "w-other", age_seconds=lease_ttl() - 30)

        r = self.kraken("claim-next", "acme/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 3, "a live lease was stolen: %s%s" % (r.out, r.err))
        self.assertEqual(self.claim_ref(1), held, "the live lease was replaced")
        self.assertEqual(self.comment_count(1), 0, "a live lease got a comment")

    def test_a_lease_past_the_ttl_no_longer_blocks(self):
        self.mk_issue(2, "abandoned", "kraken-task", "project:app", "in-progress")
        dead = self.mk_expired_lease(2, "w-dead")

        r = self.kraken("claim-next", "acme/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "an expired lease was not stolen: %s%s"
                         % (r.out, r.err))
        self.assertNotEqual(self.claim_ref(2), dead, "the expired lease survived")
        self.assertTrue(self.has_label(2, "in-progress"), "the projection was lost")
        self.assertIn('"type":"lease-expired"', "\n".join(self.comment_bodies(2)))

    def test_the_ttl_boundary_belongs_to_the_thief(self):
        # `age < ttl` is the live test: exactly at the TTL the lease is over.
        self.mk_issue(3, "on the line", "kraken-task", "project:app", "in-progress")
        dead = self.mk_claim_ref(3, "w-dead", age_seconds=lease_ttl() + 1)
        r = self.kraken("claim-next", "acme/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "a lease past the TTL held: %s%s" % (r.out, r.err))
        self.assertNotEqual(self.claim_ref(3), dead)

    def test_an_unreadable_lease_commit_is_treated_as_expired(self):
        # The ref exists but its commit object does not, so nothing proves the
        # holder alive. Holding the task forever behind an unreadable clock is
        # the one outcome the lease must never produce.
        self.mk_issue(4, "unreadable clock", "kraken-task", "project:app",
                      "in-progress")
        broken = self.mk_claim_ref(4, "w-dead", orphan_commit=True)

        r = self.kraken("claim-next", "acme/tasks", "app", "w-drain")
        self.assertEqual(r.rc, 0, "an unreadable lease held the task: %s%s"
                         % (r.out, r.err))
        self.assertNotEqual(self.claim_ref(4), broken,
                            "the unreadable lease was not replaced")
        self.assertIn('"type":"lease-expired"', "\n".join(self.comment_bodies(4)))

    def test_a_named_claim_applies_the_same_clock(self):
        # `claim <issue>` reads no queue, so it looks the lease up itself — and
        # must reach the same verdict the drain would.
        self.mk_issue(5, "live", "kraken-task", "project:app", "in-progress")
        live = self.mk_claim_ref(5, "w-other", age_seconds=30)
        self.mk_issue(6, "expired", "kraken-task", "project:app", "in-progress")
        dead = self.mk_expired_lease(6, "w-dead")

        r = self.kraken("claim", "acme/tasks", 5, "w-drain")
        # 10, not 11: the LEASE refuses, and a lease says somebody owns the task.
        # (Under protocol/6 the in-progress badge answered first, with the vaguer
        # "not clear" verdict; nothing reads that badge any more.)
        self.assertEqual(r.rc, 10, "a named claim took a live lease")
        self.assertEqual(self.claim_ref(5), live)

        r = self.kraken("claim", "acme/tasks", 6, "w-drain")
        self.assertEqual(r.rc, 0, "a named claim refused an expired lease: %s%s"
                         % (r.out, r.err))
        self.assertNotEqual(self.claim_ref(6), dead)
        self.assertIn("claim: stole issue=6 worker=w-drain", r.out)

    def test_a_protocol5_ref_is_generation_zero(self):
        # No migration: a queue standing on protocol/5 carries bare
        # refs/kraken/claims/<n> refs. They read as the LOWEST generation, so a
        # live one still holds the task and an expired one is stolen by creating
        # generation 1 over it.
        self.mk_issue(9, "legacy live", "kraken-task", "project:app", "in-progress")
        legacy_live = self.mk_claim_ref(9, "w-old", age_seconds=30, gen=0)
        self.mk_issue(10, "legacy dead", "kraken-task", "project:app", "in-progress")
        legacy_dead = self.mk_expired_lease(10, "w-old", gen=0)

        # 10 (lost), the same verdict a live protocol/6 lease gets: the ladder's
        # lowest rung is still a rung, and holding it still means holding it.
        self.assertEqual(self.kraken("claim", "acme/tasks", 9, "w-new").rc, 10,
                         "a live protocol/5 claim was not honoured")
        self.assertEqual(self.claim_ref(9), legacy_live)

        r = self.kraken("claim", "acme/tasks", 10, "w-new")
        self.assertEqual(r.rc, 0, "an expired protocol/5 claim was not stolen: %s%s"
                         % (r.out, r.err))
        self.assertEqual(self.claim_generations(10), [(1, self.claim_ref(10))],
                         "the steal must leave generation 1 alone, gen 0 collected")
        self.assertNotEqual(self.claim_ref(10), legacy_dead)

    def test_the_superseded_generation_is_collected(self):
        self.mk_issue(11, "abandoned", "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(11, "w-dead")   # generation 1

        self.assertEqual(self.kraken("claim-next", "acme/tasks", "app", "w-new").rc, 0)
        gens = self.claim_generations(11)
        self.assertEqual([g for g, _s in gens], [2],
                         "the steal left the generation it climbed past behind")

    def test_the_free_queue_read_applies_the_clock_too(self):
        # list-startable is what the ambush loop polls with; if it disagreed with
        # the claim path it would either burn a drain on a held task or hide a
        # free one forever.
        self.mk_issue(7, "live", "kraken-task", "project:app", "in-progress")
        self.mk_claim_ref(7, "w-other", age_seconds=30)
        self.mk_issue(8, "expired", "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(8, "w-dead")

        out = self.kraken("list-startable", "acme/tasks", "app").out
        self.assertNotIn("7\t", out, "a live lease was offered as startable")
        self.assertIn("8\t", out, "an expired lease was not offered as startable")


if __name__ == "__main__":
    unittest.main()
