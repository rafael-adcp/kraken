#!/usr/bin/env bash
# kraken.py heartbeat: progress comment posted, no label changes — and the heartbeat:
# line must NOT reset the claim window (a worker heartbeating must never make
# its own claim re-claimable).
. "$ROOT/tests/lib.sh"

mk_issue 7 "long task" kraken-task "project:app" in-progress
mk_comment 7 "claimed-by: w1"

out="$(python3 "$SCRIPTS/kraken.py" heartbeat OWNER/tasks 7 w1 "tests green, writing docs")"
assert_rc $? 0 "heartbeat exit"
assert_eq "$out" "heartbeat: posted issue=7 worker=w1" "machine line"

has_label 7 in-progress || fail "heartbeat touched the labels"
c="$(last_comment 7)"
assert_disclaimer 7 w1
printf '%s' "$c" | grep -q '^heartbeat: w1$' || fail "heartbeat machine line missing"
printf '%s' "$c" | grep -q '^tests green, writing docs$' || fail "progress message missing"
grep -q 'issue edit' "$STATE/log" && fail "heartbeat ran an issue edit"

# Window invariant: w1's claim still wins after its own heartbeat.
echo "kraken-task" > "$STATE/issues/7/labels"; echo "project:app" >> "$STATE/issues/7/labels"
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w2)"
assert_rc $? 10 "claim against heartbeated window"
assert_eq "$out" "claim: lost-tiebreaker issue=7 winner=w1" "heartbeat did not reset the window"
