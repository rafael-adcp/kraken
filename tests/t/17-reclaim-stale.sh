#!/usr/bin/env bash
# The reaper anchors staleness to the worker's last liveness marker (the newest
# claim/heartbeat marker), not the issue's updatedAt. An operator comment on a
# dead worker's issue must NOT keep the claim alive: it is the worker's silence
# that gets reclaimed, and a human's attention should only shorten triage time.
#
# reclaim-stale.yml is now a thin exec of `kraken.py reap` (issue #37), so this
# drives the shipped subcommand directly against the gh-stub — the same way the
# suite exercises every other transition — preserving the scenarios the old
# extract-and-run test pinned.
. "$ROOT/tests/lib.sh"

export REPO="OWNER/tasks"
export MAX_HOURS=6

ago_iso() { date -u -d "@$(( $(date -u +%s) - $1 * 3600 ))" +%Y-%m-%dT%H:%M:%SZ; }

# #1 — DEAD: claimed 8h ago, then an operator commented 10m ago. updatedAt would
# read fresh (the operator comment) and spare it; anchored to the last liveness
# marker it is 8h silent and MUST be reclaimed. The claim body carries the real
# shape (disclaimer + blank line + marker), proving the reaper's marker match
# finds the marker inside a multi-line comment, not just a bare one.
mk_issue 1 "dead worker, operator poked it" kraken-task "project:app" in-progress
mk_comment 1 "$(printf '> disclaimer\n\n<!-- kraken {"type":"claim","worker":"dead-worker"} -->\n')" "$(ago_iso 8)"
mk_comment 1 "any update here? — the operator" "$(ago_iso 0)"

# #2 — ALIVE: a fresh heartbeat 30m ago (0h floor) is inside the window and MUST
# be left alone, even though the original claim is old.
mk_issue 2 "live worker heartbeating" kraken-task "project:app" in-progress
mk_comment 2 '<!-- kraken {"type":"claim","worker":"live-worker"} -->' "$(ago_iso 9)"
mk_comment 2 '<!-- kraken {"type":"heartbeat","worker":"live-worker"} -->' "$(ago_iso 0)"

# #3 — MALFORMED: in-progress but no worker liveness marker at all (only an
# operator comment). No anchor exists, so it is infinitely stale and MUST be reclaimed.
mk_issue 3 "in-progress, worker never spoke" kraken-task "project:app" in-progress
mk_comment 3 "someone mislabeled this — the operator" "$(ago_iso 0)"

# #4 — ALIVE PAST THE 100-COMMENT BOUNDARY: the reaper must anchor staleness to
# the WHOLE thread, not the first page. A capped 100-comment read would miss the
# fresh heartbeat sitting past comment 100 and wrongly reclaim this live worker;
# kraken.py's paginated comment read keeps it in the window.
mk_issue 4 "live worker heartbeating past comment 100" kraken-task "project:app" in-progress
mk_comment 4 '<!-- kraken {"type":"claim","worker":"marathon-worker"} -->' "$(ago_iso 9)"
for i in $(seq 1 100); do
  mk_comment 4 "noise $i" "$(ago_iso 1)"
done
mk_comment 4 '<!-- kraken {"type":"heartbeat","worker":"marathon-worker"} -->' "$(ago_iso 0)"

# #5 — STALE PAST THE 100-COMMENT BOUNDARY: a genuinely dead worker whose only
# liveness marker (an 8h-old claim) sits on comment #1, then 100+ operator
# comments with no liveness payload. Even reading the full thread, the newest
# marker is 8h old, so it MUST still be reclaimed.
mk_issue 5 "dead worker, long noisy thread" kraken-task "project:app" in-progress
mk_comment 5 '<!-- kraken {"type":"claim","worker":"lost-worker"} -->' "$(ago_iso 8)"
for i in $(seq 1 105); do
  mk_comment 5 "someone keeps commenting $i — the operator" "$(ago_iso 0)"
done

python3 "$SCRIPTS/kraken.py" reap "$REPO"
assert_rc $? 0 "reap run"

# #1 reclaimed to needs-decision, in-progress dropped, stale-claim: posted.
has_label 1 needs-decision || fail "#1 (dead) not moved to needs-decision"
has_label 1 in-progress && fail "#1 (dead) still in-progress after reaping"
last_comment 1 | grep -qF '<!-- kraken {"type":"stale-claim"' || fail "#1 missing stale-claim marker"

# #2 untouched: the fresh heartbeat kept it inside the window.
has_label 2 in-progress || fail "#2 (live) was reclaimed despite a fresh heartbeat"
has_label 2 needs-decision && fail "#2 (live) wrongly moved to needs-decision"
assert_eq "$(comment_count 2)" "2" "#2 got a stale-claim comment it should not have"

# #3 reclaimed: no worker machine line means no liveness proof.
has_label 3 needs-decision || fail "#3 (no machine line) not reclaimed"
has_label 3 in-progress && fail "#3 (no machine line) still in-progress"
last_comment 3 | grep -qF '<!-- kraken {"type":"stale-claim"' || fail "#3 missing stale-claim marker"

# #4 untouched: the fresh heartbeat past comment 100 is only visible when the
# reaper reads the FULL paginated history.
has_label 4 in-progress || fail "#4 (live past boundary) reclaimed — reaper missed the heartbeat past comment 100"
has_label 4 needs-decision && fail "#4 (live past boundary) wrongly moved to needs-decision"
last_comment 4 | grep -qF '<!-- kraken {"type":"stale-claim"' && fail "#4 (live past boundary) got a stale-claim comment it should not have"

# #5 reclaimed: a long noisy thread must not spare a dead worker.
has_label 5 needs-decision || fail "#5 (dead past boundary) not reclaimed despite an 8h-old anchor"
has_label 5 in-progress && fail "#5 (dead past boundary) still in-progress after reaping"
last_comment 5 | grep -qF '<!-- kraken {"type":"stale-claim"' || fail "#5 missing stale-claim marker"

exit 0
