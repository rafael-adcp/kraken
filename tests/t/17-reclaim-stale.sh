#!/usr/bin/env bash
# The reaper anchors staleness to the worker's last machine line
# (^claimed-by:/^heartbeat:), not the issue's updatedAt. An operator comment on
# a dead worker's issue must NOT keep the claim alive: it is the worker's silence
# that gets reclaimed, and a human's attention should only shorten triage time.
#
# To prove the shipped logic — not a copy of it — this extracts the reaper's own
# `run:` script out of reclaim-stale.yml and runs it verbatim against the gh-stub.
. "$ROOT/tests/lib.sh"

REAPER="$ROOT/skills/unleash/reclaim-stale.yml"

# Pull the `run: |` block body out of the workflow, de-indented. Capture every
# line after `run: |` that is blank or indented deeper than the `run:` key, then
# strip that block's common leading indentation.
extract_run() {
  awk '
    /^[[:space:]]*run: \|[[:space:]]*$/ {
      match($0, /^[[:space:]]*/); key = RLENGTH; grab = 1; next
    }
    grab {
      if ($0 ~ /[^[:space:]]/) {
        match($0, /^[[:space:]]*/)
        if (RLENGTH <= key) { grab = 0; next }
      }
      print
    }
  ' "$1" | sed 's/^          //' | tr -d '\r'
}

RUN="$STATE/reaper.sh"
extract_run "$REAPER" > "$RUN"
[ -s "$RUN" ] || fail "could not extract the reaper run block from $REAPER"

# The extracted script uses GITHUB_REPOSITORY and MAX_HOURS (the workflow env);
# gh is the stub on PATH. now=$(date -u +%s) is real time; the .at sidecars are
# anchored relative to it below.
export GITHUB_REPOSITORY="OWNER/tasks"
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
# the WHOLE thread, not the first page. `gh issue list --json comments` caps its
# nested export at 100 and does not paginate, so the fresh heartbeat below —
# sitting past comment 100 — would fall outside a capped read and this live
# worker would look silent for MAX_HOURS and be wrongly reclaimed. Reading the
# full history via the paginated REST comments endpoint keeps it in the window.
mk_issue 4 "live worker heartbeating past comment 100" kraken-task "project:app" in-progress
mk_comment 4 '<!-- kraken {"type":"claim","worker":"marathon-worker"} -->' "$(ago_iso 9)"
# comments #2-#101: 100 comments of operator chatter pad the thread past the
# 100-comment boundary before the fresh heartbeat lands.
for i in $(seq 1 100); do
  mk_comment 4 "noise $i" "$(ago_iso 1)"
done
# comment #102 (past the boundary): a fresh heartbeat 30m ago (0h floor).
mk_comment 4 '<!-- kraken {"type":"heartbeat","worker":"marathon-worker"} -->' "$(ago_iso 0)"

# #5 — STALE PAST THE 100-COMMENT BOUNDARY: a genuinely dead worker whose only
# liveness marker (an 8h-old claim) sits on comment #1, then 100+ operator
# comments with no liveness payload. Even reading the full thread, the newest
# marker is 8h old, so it MUST still be reclaimed — the fix must not spare a
# dead worker just because the thread is long.
mk_issue 5 "dead worker, long noisy thread" kraken-task "project:app" in-progress
mk_comment 5 '<!-- kraken {"type":"claim","worker":"lost-worker"} -->' "$(ago_iso 8)"
for i in $(seq 1 105); do
  mk_comment 5 "someone keeps commenting $i — the operator" "$(ago_iso 0)"
done

bash "$RUN"
assert_rc $? 0 "reaper run"

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
# reaper reads the FULL paginated history — a capped 100-comment read would
# miss it and wrongly reclaim this live worker.
has_label 4 in-progress || fail "#4 (live past boundary) reclaimed — reaper missed the heartbeat past comment 100"
has_label 4 needs-decision && fail "#4 (live past boundary) wrongly moved to needs-decision"
last_comment 4 | grep -qF '<!-- kraken {"type":"stale-claim"' && fail "#4 (live past boundary) got a stale-claim comment it should not have"

# #5 reclaimed: a long noisy thread must not spare a dead worker — the newest
# liveness marker is still the 8h-old claim, past MAX_HOURS.
has_label 5 needs-decision || fail "#5 (dead past boundary) not reclaimed despite an 8h-old anchor"
has_label 5 in-progress && fail "#5 (dead past boundary) still in-progress after reaping"
last_comment 5 | grep -qF '<!-- kraken {"type":"stale-claim"' || fail "#5 missing stale-claim marker"

exit 0
