#!/usr/bin/env bash
# Auto-requeue conformance: proves requeue-on-reply.yml's shipped run: block by
# extracting and running it verbatim against the gh-stub (mirrors tests/t/17).
. "$ROOT/tests/lib.sh"

WF="$ROOT/skills/unleash/requeue-on-reply.yml"

extract_run() {
  awk '
    /^[[:space:]]*run: \|[[:space:]]*$/ {
      match($0, /^[[:space:]]*/); key = RLENGTH; grab = 1; next
    }
    grab {
      if ($0 ~ /[^[:space:]]/) {
        match($0, /^[[:space:]]*/)
        if (RLENGTH <= key) { grab = 0; next }
      }
      print
    }
  ' "$1" | sed 's/^          //' | tr -d '\r'
}

RUN="$STATE/requeue.sh"
extract_run "$WF" > "$RUN"
[ -s "$RUN" ] || fail "could not extract the run block from $WF"

export REPO="OWNER/tasks"
# Derive the worker disclaimer from kraken.py (the single source of truth) so this
# test never re-declares the format the requeue filter keys on. See tests/lib.sh.
DISCLAIMER="$(disclaimer_line w1)"

run_case() { export NUM="$1" COMMENT_BODY="$2" COMMENT_AUTHOR_TYPE="$3"; bash "$RUN"; }

mk_issue 1 "decision answered by a bare reply" kraken-task "project:app" needs-decision
run_case 1 "option B, go" "User"
assert_rc $? 0 "#1 run"
has_label 1 needs-decision && fail "#1 needs-decision not removed on a human reply"
last_comment 1 | grep -q '^requeue: ' || fail "#1 missing requeue confirmation comment"

mk_issue 2 "worker comment must not requeue" kraken-task "project:app" needs-decision
run_case 2 "$(printf '%s\n\nneeds-decision: w1\n\nwhich option?' "$DISCLAIMER")" "User"
assert_rc $? 0 "#2 run"
has_label 2 needs-decision || fail "#2 worker comment wrongly requeued (disclaimer ignored)"
assert_eq "$(comment_count 2)" "0" "#2 got a comment it should not have"

# #2b — first-line anchoring: an operator answering a needs-decision by pasting
# the worker's disclaimer line MID-body (quoting the question they answer) must
# still requeue — the disclaimer only classifies a worker when it OPENS the body.
mk_issue 20 "operator reply quoting the disclaimer mid-body still requeues" kraken-task "project:app" needs-decision
run_case 20 "$(printf 'answering your question below:\n\n%s\n\noption B, go' "$DISCLAIMER")" "User"
assert_rc $? 0 "#2b run"
has_label 20 needs-decision && fail "#2b operator reply quoting the disclaimer mid-body was misread as a worker comment"
last_comment 20 | grep -q '^requeue: ' || fail "#2b missing requeue confirmation comment"

mk_issue 3 "no held label" kraken-task "project:app"
run_case 3 "nice work everyone" "User"
assert_rc $? 0 "#3 run"
assert_eq "$(comment_count 3)" "0" "#3 got a comment on an unheld issue"

mk_issue 4 "bot comment must not requeue" kraken-task "project:app" needs-decision
run_case 4 "stale-claim: no worker heartbeat for 8h — the worker likely died." "Bot"
assert_rc $? 0 "#4 run"
has_label 4 needs-decision || fail "#4 bot comment wrongly requeued needs-decision"
assert_eq "$(comment_count 4)" "0" "#4 bot comment produced output"

mk_issue 5 "awaiting-merge, bare comment stays held" kraken-task "project:app" awaiting-merge
run_case 5 "I'll merge this tomorrow, looks good" "User"
assert_rc $? 0 "#5 run"
has_label 5 awaiting-merge || fail "#5 awaiting-merge wrongly requeued on a bare comment"
assert_eq "$(comment_count 5)" "0" "#5 got a comment it should not have"

mk_issue 6 "awaiting-merge, standalone requeue: directive" kraken-task "project:app" awaiting-merge
run_case 6 "$(printf 'requeue:\nplease fix the typo in the README before I merge')" "User"
assert_rc $? 0 "#6 run"
has_label 6 awaiting-merge && fail "#6 awaiting-merge not removed on a standalone requeue: directive"
last_comment 6 | grep -q '^requeue: ' || fail "#6 missing requeue confirmation comment"

# #6b — awaiting-merge bounced back by the structured protocol/3 requeue marker.
mk_issue 60 "awaiting-merge, structured requeue marker" kraken-task "project:app" awaiting-merge
run_case 60 "$(printf 'bounce it back\n\n<!-- kraken {"type":"requeue"} -->')" "User"
assert_rc $? 0 "#6b run"
has_label 60 awaiting-merge && fail "#6b awaiting-merge not removed on a requeue marker"
last_comment 60 | grep -q '^requeue: ' || fail "#6b missing requeue confirmation comment"

# #6c — THE accidental-collision fix: a prose sentence that merely starts a line
# with "requeue:" must NOT bounce delivered work (only a standalone directive or
# the marker does).
mk_issue 61 "awaiting-merge, requeue: buried in prose" kraken-task "project:app" awaiting-merge
run_case 61 "requeue: is something I considered, but let's hold off until Monday" "User"
assert_rc $? 0 "#6c run"
has_label 61 awaiting-merge || fail "#6c a prose 'requeue:' sentence wrongly bounced delivered work"
assert_eq "$(comment_count 61)" "0" "#6c got a comment it should not have"

run_case 1 "and one more thing" "User"
assert_rc $? 0 "#7 run"
assert_eq "$(comment_count 1)" "1" "#7 a second comment requeued/commented again (no debounce)"

exit 0
