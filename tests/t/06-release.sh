#!/usr/bin/env bash
# release.sh: released: machine line posted (window closed), label removed,
# optional reason line carried.
. "$ROOT/tests/lib.sh"

mk_issue 7 "abandoned task" kraken-task "project:app" in-progress

out="$(bash "$SCRIPTS/release.sh" OWNER/tasks 7 w1 "environment cannot host the task")"
assert_rc $? 0 "release exit"
assert_eq "$out" "release: released issue=7 worker=w1" "machine line"

has_label 7 in-progress && fail "in-progress label still present after release"
c="$(last_comment 7)"
assert_disclaimer 7 w1
printf '%s' "$c" | grep -q '^released: w1$' || fail "released: machine line missing"
printf '%s' "$c" | grep -q '^reason: environment cannot host the task$' || fail "reason line missing"

# The released issue is claimable again — end to end with claim.sh.
out="$(bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w2)"
assert_rc $? 0 "re-claim after release"
