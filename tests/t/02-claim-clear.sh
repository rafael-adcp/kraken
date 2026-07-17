#!/usr/bin/env bash
# kraken.py claim on a clear task: exit 0, in-progress added, disclaimer + the
# protocol/2 claim marker posted (the visible prose is human courtesy only).
. "$ROOT/tests/lib.sh"

mk_issue 7 "a task" kraken-task "project:app"

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1)"
assert_rc $? 0 "clean claim exit"
assert_eq "$out" "claim: claimed issue=7 worker=w1" "machine line"

has_label 7 in-progress || fail "in-progress label missing after claim"
assert_eq "$(comment_count 7)" "1" "exactly one comment posted"
assert_disclaimer 7 w1
assert_marker 7 '{"type":"claim","worker":"w1"}'
# The retired protocol/1 visible line is NOT emitted by a protocol/2 producer.
last_comment 7 | grep -q '^claimed-by:' && fail "protocol/2 producer emitted a legacy claimed-by: line"
exit 0
