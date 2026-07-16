#!/usr/bin/env bash
# gh/network failures surface as exit 20 at every stage — never a silent
# success, never a zero.
. "$ROOT/tests/lib.sh"

mk_issue 7 "a task" kraken-task "project:app"

# list-startable: the batched listing/native-blocked-by gh graphql call fails.
out="$(GH_STUB_FAIL='graphql' python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app)"
assert_rc $? 20 "list-startable exit on gh graphql failure"

# list-startable: the depends-on fallback's own batched gh graphql call fails
# — a candidate needing the fallback must still surface 20, not silently list
# or drop. 'issue(number:' only appears in that second (singular-issue-alias)
# call, never in the first (plural `issues(states: ...`) listing call.
mk_issue 70 "dep target"         kraken-task "project:app"
mk_issue 71 "fallback candidate" kraken-task "project:app"
mk_body 71 "depends-on: #70"
out="$(GH_STUB_FAIL='issue\(number:' python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app)"
assert_rc $? 20 "list-startable exit on depends-on fallback gh graphql failure"

# claim: failure at the guard read — nothing written.
out="$(GH_STUB_FAIL='issue view' python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1)"
assert_rc $? 20 "claim exit on guard failure"
assert_eq "$out" "claim: gh-failure issue=7 stage=guard" "guard failure machine line"
assert_eq "$(comment_count 7)" "0" "no comment after guard failure"

# claim: failure at the comment — label landed, state honestly ambiguous (20).
out="$(GH_STUB_FAIL='issue comment' python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1)"
assert_rc $? 20 "claim exit on comment failure"
assert_eq "$out" "claim: gh-failure issue=7 stage=comment" "comment failure machine line"

# release: failure posting released: — the label must NOT have been removed
# (comment-first ordering is what keeps the claim window sound).
mk_issue 8 "held task" kraken-task "project:app" in-progress
out="$(GH_STUB_FAIL='issue comment' python3 "$SCRIPTS/kraken.py" release OWNER/tasks 8 w1)"
assert_rc $? 20 "release exit on comment failure"
has_label 8 in-progress || fail "release removed the label before the released: comment landed"
