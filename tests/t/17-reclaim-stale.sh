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
# read fresh (the operator comment) and spare it; anchored to the last machine
# line it is 8h silent and MUST be reclaimed. The claim body carries the real
# shape (disclaimer + blank line + machine line), proving the `(?m)^` match finds
# the machine line inside a multi-line comment, not just a bare one.
mk_issue 1 "dead worker, operator poked it" kraken-task "project:app" in-progress
mk_comment 1 "$(printf '> disclaimer\n\nclaimed-by: dead-worker\n')" "$(ago_iso 8)"
mk_comment 1 "any update here? — the operator" "$(ago_iso 0)"

# #2 — ALIVE: a fresh heartbeat 30m ago (0h floor) is inside the window and MUST
# be left alone, even though the original claim is old.
mk_issue 2 "live worker heartbeating" kraken-task "project:app" in-progress
mk_comment 2 "claimed-by: live-worker" "$(ago_iso 9)"
mk_comment 2 "heartbeat: live-worker" "$(ago_iso 0)"

# #3 — MALFORMED: in-progress but no worker machine line at all (only an operator
# comment). No anchor exists, so it is infinitely stale and MUST be reclaimed.
mk_issue 3 "in-progress, worker never spoke" kraken-task "project:app" in-progress
mk_comment 3 "someone mislabeled this — the operator" "$(ago_iso 0)"

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

exit 0
