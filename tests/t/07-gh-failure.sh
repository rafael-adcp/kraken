#!/usr/bin/env bash
# gh/network failures surface as exit 20 at every stage — never a silent
# success, never a zero.
. "$ROOT/tests/lib.sh"

mk_issue 7 "a task" kraken-task "project:app"

# list-startable: the gh call fails.
out="$(GH_STUB_FAIL='issue list' bash "$SCRIPTS/list-startable.sh" OWNER/tasks app)"
assert_rc $? 20 "list-startable exit on gh failure"

# claim: failure at the guard read — nothing written.
out="$(GH_STUB_FAIL='issue view' bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w1)"
assert_rc $? 20 "claim exit on guard failure"
assert_eq "$out" "claim: gh-failure issue=7 stage=guard" "guard failure machine line"
assert_eq "$(comment_count 7)" "0" "no comment after guard failure"

# claim: failure at the comment — label landed, state honestly ambiguous (20).
out="$(GH_STUB_FAIL='issue comment' bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w1)"
assert_rc $? 20 "claim exit on comment failure"
assert_eq "$out" "claim: gh-failure issue=7 stage=comment" "comment failure machine line"

# release: failure posting released: — the label must NOT have been removed
# (comment-first ordering is what keeps the claim window sound).
mk_issue 8 "held task" kraken-task "project:app" in-progress
out="$(GH_STUB_FAIL='issue comment' bash "$SCRIPTS/release.sh" OWNER/tasks 8 w1)"
assert_rc $? 20 "release exit on comment failure"
has_label 8 in-progress || fail "release removed the label before the released: comment landed"
