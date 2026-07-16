#!/usr/bin/env bash
# Claim-window arbitration must see the WHOLE comment thread, not just the
# first page. `gh issue view --json comments` is known to cap the nested
# GraphQL connection at 100 in some gh versions — kraken.py's comment_bodies
# sidesteps that entirely by reading the paginated REST comments endpoint
# instead (tests/unit/test_kraken.py's CommentPaginationTests pins the
# call shape and the algorithm against a mocked transport). This is the
# end-to-end companion: it drives the real `kraken.py claim` subcommand
# through the gh-stub with a >100-comment thread, so the actual gh-output ->
# jq -> line-scan pipeline is exercised, not just the arbitration function in
# isolation.
. "$ROOT/tests/lib.sh"

mk_issue 50 "long-lived task" kraken-task "project:app"

# comment #1: a worker claims it, then goes dark.
mk_comment 50 "claimed-by: dead-worker"

# comments #2-#100: 99 comments of unrelated chatter — pads the thread past
# the 100-comment boundary before the reset and the real winner land.
for i in $(seq 1 99); do
  mk_comment 50 "noise $i"
done

# comment #101 (past the boundary): the reaper clears the dead claim.
mk_comment 50 "stale-claim: no activity for 7h"
# comment #102 (past the boundary): a second worker wins the fresh window.
mk_comment 50 "claimed-by: heir"

# A third worker now tries to claim it. If arbitration only saw the first 100
# comments, it would see #1's claimed-by: dead-worker with no reset in view
# (both the reset and heir's claim live past #100) and wrongly report
# dead-worker as the still-live winner. Seeing the full 102-comment thread,
# the reset clears dead-worker and #102's claimed-by: heir is the true winner.
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 50 challenger)"
assert_rc $? 10 "challenger loses to the winner past the 100-comment boundary"
assert_eq "$out" "claim: lost-tiebreaker issue=50 winner=heir" \
  "arbitration reads past comment 100 — heir wins, not the falsely-still-claimed dead-worker"
