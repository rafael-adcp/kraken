#!/usr/bin/env bash
# kraken.py release: released: machine line posted (window closed), label removed,
# optional reason line carried.
. "$ROOT/tests/lib.sh"

mk_issue 7 "abandoned task" kraken-task "project:app" in-progress

out="$(python3 "$SCRIPTS/kraken.py" release OWNER/tasks 7 w1 "environment cannot host the task")"
assert_rc $? 0 "release exit"
assert_eq "$out" "release: released issue=7 worker=w1" "machine line"

has_label 7 in-progress && fail "in-progress label still present after release"
assert_disclaimer 7 w1
assert_marker 7 '{"type":"released","worker":"w1","reason":"environment cannot host the task"}'

# The released issue is claimable again — end to end with kraken.py claim.
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w2)"
assert_rc $? 0 "re-claim after release"
