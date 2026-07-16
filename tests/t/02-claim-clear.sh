#!/usr/bin/env bash
# claim.sh on a clear task: exit 0, in-progress added, disclaimer + claimed-by posted.
. "$ROOT/tests/lib.sh"

mk_issue 7 "a task" kraken-task "project:app"

out="$(bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w1)"
assert_rc $? 0 "clean claim exit"
assert_eq "$out" "claim: claimed issue=7 worker=w1" "machine line"

has_label 7 in-progress || fail "in-progress label missing after claim"
assert_eq "$(comment_count 7)" "1" "exactly one comment posted"
c="$(last_comment 7)"
assert_disclaimer 7 w1
printf '%s' "$c" | grep -q '^claimed-by: w1$' || fail "claimed-by machine line missing"
