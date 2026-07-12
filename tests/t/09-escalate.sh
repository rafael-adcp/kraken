#!/usr/bin/env bash
# escalate.sh: question posted with the needs-decision: line, labels swapped in
# one call — and after the human requeues, ANY worker wins the fresh window.
. "$ROOT/tests/lib.sh"

mk_issue 7 "blocked task" kraken-task "project:app" in-progress
mk_comment 7 "claimed-by: w1"

q="$STATE/question.md"
printf 'Should pagination be cursor- or offset-based?\n\n- A: cursor (recommended)\n- B: offset\n' > "$q"

out="$(bash "$SCRIPTS/escalate.sh" OWNER/tasks 7 w1 "$q")"
assert_rc $? 0 "escalate exit"
assert_eq "$out" "escalate: escalated issue=7 worker=w1" "machine line"

has_label 7 in-progress && fail "in-progress still present after escalate"
has_label 7 needs-decision || fail "needs-decision missing after escalate"
c="$(last_comment 7)"
printf '%s' "$c" | grep -q '^needs-decision: w1$' || fail "needs-decision machine line missing"
printf '%s' "$c" | grep -q '^Should pagination be cursor- or offset-based?$' || fail "question body missing"
printf '%s' "$c" | grep -q '^- A: cursor (recommended)$' || fail "options missing"

# Human answers and requeues (removes the label) — a fresh worker must win.
mk_comment 7 "option A, go"
grep -vxF "needs-decision" "$STATE/issues/7/labels" > "$STATE/issues/7/labels.tmp"
mv "$STATE/issues/7/labels.tmp" "$STATE/issues/7/labels"

out="$(bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w2)"
assert_rc $? 0 "re-claim after decision"
assert_eq "$out" "claim: claimed issue=7 worker=w2" "escalation reset the claim window"

# Bad invocation: missing question file is a 2, not a half-executed transition.
out="$(bash "$SCRIPTS/escalate.sh" OWNER/tasks 7 w1 /nonexistent/q.md 2>/dev/null)"
assert_rc $? 2 "missing question file exit"
