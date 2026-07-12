#!/usr/bin/env bash
# gh failures in the write transitions: exit 20 at every stage, and the
# comment-first ordering means a half-executed transition is always held
# (never a label state the machine lines can't explain).
. "$ROOT/tests/lib.sh"

# escalate: comment fails -> nothing changed.
mk_issue 7 "blocked task" kraken-task "project:app" in-progress
q="$STATE/q.md"; echo "which way?" > "$q"
out="$(GH_STUB_FAIL='issue comment' bash "$SCRIPTS/escalate.sh" OWNER/tasks 7 w1 "$q")"
assert_rc $? 20 "escalate exit on comment failure"
assert_eq "$out" "escalate: gh-failure issue=7 stage=comment" "escalate failure line"
has_label 7 in-progress || fail "escalate touched labels before the comment landed"
has_label 7 needs-decision && fail "escalate added needs-decision despite comment failure"

# escalate: label swap fails -> comment landed, task still held by in-progress.
out="$(GH_STUB_FAIL='issue edit' bash "$SCRIPTS/escalate.sh" OWNER/tasks 7 w1 "$q")"
assert_rc $? 20 "escalate exit on label failure"
assert_eq "$out" "escalate: gh-failure issue=7 stage=labels" "escalate label-failure line"
has_label 7 in-progress || fail "task lost in-progress on a failed swap"

# deliver: label swap fails -> result recorded, task still held by in-progress.
mk_issue 8 "shipped task" kraken-task "project:app" in-progress
r="$STATE/r.md"; echo "done, validated" > "$r"
out="$(GH_STUB_FAIL='issue edit' bash "$SCRIPTS/deliver.sh" OWNER/tasks 8 w1 "$r" "https://x/pr/1")"
assert_rc $? 20 "deliver exit on label failure"
assert_eq "$out" "deliver: gh-failure issue=8 stage=labels" "deliver label-failure line"
has_label 8 in-progress || fail "task lost in-progress on a failed swap"
has_label 8 awaiting-merge && fail "deliver added awaiting-merge despite swap failure"

# heartbeat: comment fails -> 20, nothing else to roll back.
out="$(GH_STUB_FAIL='issue comment' bash "$SCRIPTS/heartbeat.sh" OWNER/tasks 8 w1 "still here")"
assert_rc $? 20 "heartbeat exit on comment failure"
assert_eq "$out" "heartbeat: gh-failure issue=8" "heartbeat failure line"
exit 0
