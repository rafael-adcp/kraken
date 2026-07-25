#!/usr/bin/env python3
"""THE steal race (PROTOCOL.md §5): N workers find the SAME expired lease at the
same instant and all try to take it. The steal is `delete` + `create`, and the
`create` is still the compare-and-swap — so the server admits exactly one
creator and answers 422 to the rest, exactly as it does for a first claim.

The stub's barrier holds every racer at the label-guard read until all of them
have arrived, so they leave for the steal together — the worst-case
interleaving, deterministically.

The verdict is read from EXIT CODES, never from stderr text (issue #53): the
winner exits 0, every loser exits 10 having written nothing.
"""
import json
import os
import re
import unittest

from harness import KrakenConformanceTest


class LeaseStealRaceTests(KrakenConformanceTest):
    THIEVES = ("w-a", "w-b", "w-c")

    def test_exactly_one_thief_wins_an_expired_lease(self):
        self.mk_issue(7, "abandoned", "kraken-task", "project:app", "in-progress")
        dead = self.mk_expired_lease(7, "w-dead")

        results = self.run_concurrent(
            [("claim", "OWNER/tasks", 7, w) for w in self.THIEVES],
            env={"GH_STUB_BARRIER": str(len(self.THIEVES))},
        )
        rcs = [r.rc for r in results]

        # Exactly one winner, the rest lose — never zero, never two.
        self.assertEqual(sorted(rcs), [0, 10, 10],
                         "steal-race exit codes (expected one 0 and two 10s), got %s:\n%s"
                         % (rcs, "\n".join(r.out + r.err for r in results)))
        winner = self.THIEVES[rcs.index(0)]

        # The surviving lease is the winner's, and it is NOT the dead one.
        sha = self.claim_ref(7)
        self.assertIsNotNone(sha, "no lease survived the race")
        self.assertNotEqual(sha, dead, "the expired lease survived the steal")
        with open(os.path.join(self.state, "objects", sha + ".json"),
                  encoding="utf-8") as f:
            commit = json.load(f)
        self.assertIn('"worker":"%s"' % winner, commit["message"],
                      "the surviving lease must carry the winner's claim commit")

        # The losers wrote NOTHING: exactly one steal was recorded and exactly
        # one claim comment posted, both the winner's.
        bodies = self.comment_bodies(7)
        expiries = [b for b in bodies if '"type":"lease-expired"' in b]
        claims = [b for b in bodies if '"type":"claim"' in b]
        self.assertEqual(len(expiries), 1,
                         "a loser posted a steal note too: %s" % bodies)
        self.assertEqual(len(claims), 1,
                         "a loser posted a claim comment too: %s" % bodies)
        self.assertIn('"worker":"%s"' % winner, claims[0])
        self.assertFalse(
            any(re.search(r"DELETE \S*/labels/", l) for l in self.log_lines()),
            "a racer removed a label while backing off")

    def test_a_renewal_and_a_steal_cannot_both_win(self):
        """The race the generation ladder exists for. A worker at the very edge
        of its TTL renews at the same instant another worker decides the lease is
        expired and takes it — and because a renewal is ALSO a create of the next
        generation, the two collide on one ref name instead of one deleting the
        other's work. Exactly one wins, and the loser is told immediately."""
        self.mk_issue(9, "on the edge", "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(9, "w-slow")

        renew, steal = self.run_concurrent(
            [("heartbeat", "OWNER/tasks", 9, "w-slow", "still here"),
             ("claim", "OWNER/tasks", 9, "w-thief")],
            env={"GH_STUB_CAS_BARRIER": "2"},
        )
        self.assertEqual(sorted([renew.rc, steal.rc]), [0, 10],
                         "renewal/steal must produce one winner and one loser, got "
                         "%s / %s:\n%s%s" % (renew.rc, steal.rc,
                                             renew.out + renew.err,
                                             steal.out + steal.err))

        # The surviving lease belongs to whoever won, and nobody else.
        winner = "w-slow" if renew.rc == 0 else "w-thief"
        sha = self.claim_ref(9)
        with open(os.path.join(self.state, "objects", sha + ".json"),
                  encoding="utf-8") as f:
            commit = json.load(f)
        self.assertIn('"worker":"%s"' % winner, commit["message"],
                      "the surviving lease does not belong to the winner")

        if renew.rc == 0:
            # The holder kept it: the thief must have written nothing at all.
            self.assertEqual(self.comment_count(9), 0,
                             "the losing thief wrote to the thread")
        else:
            # The thief took it: the renewal must report the loss, not succeed.
            self.assertIn("lost-lease issue=9", renew.out)

    def test_a_loser_reports_defeat_by_exit_code_alone(self):
        # The verdict must be readable without matching stderr prose: a caller
        # branches on 10, and the machine line stays on stdout.
        self.mk_issue(8, "abandoned", "kraken-task", "project:app", "in-progress")
        self.mk_expired_lease(8, "w-dead")

        results = self.run_concurrent(
            [("claim", "OWNER/tasks", 8, w) for w in self.THIEVES],
            env={"GH_STUB_BARRIER": str(len(self.THIEVES))},
        )
        for r in results:
            self.assertIn(r.rc, (0, 10), "a racer neither won nor lost cleanly")
            if r.rc == 10:
                self.assertEqual(r.err, "", "a loss was reported on stderr")
                self.assertRegex(r.out, r"^claim: (lost-cas|lost-steal) issue=8")


if __name__ == "__main__":
    unittest.main()
