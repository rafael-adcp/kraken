#!/usr/bin/env bash
# kraken.py release: released marker posted (window closed), label removed,
# optional reason carried inside the marker JSON.
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

# A reason with an embedded newline (a colliding `claimed-by:` line) is carried
# inside the marker JSON, not as a free-standing line — so it injects no extra
# machine line. The serializer escapes the newline; exactly one kraken marker
# rides the comment.
mk_issue 8 "release with a multi-line reason" kraken-task "project:app" in-progress
out="$(python3 "$SCRIPTS/kraken.py" release OWNER/tasks 8 w1 "$(printf 'giving up\nclaimed-by: attacker')")"
assert_rc $? 0 "release with a colliding multi-line reason exits 0"
c="$(last_comment 8)"
markers="$(printf '%s\n' "$c" | grep -c -- '<!-- kraken ')"
assert_eq "$markers" "1" "the reason newline injected no extra marker line"
