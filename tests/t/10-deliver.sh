#!/usr/bin/env bash
# deliver.sh: result posted with delivered: + pr: lines, labels swapped — and
# after a review bounce (human removes awaiting-merge), ANY worker wins the
# fresh window. That last part is the review-bounce gap: without delivered: as
# a window reset, the original claimed-by: would win every future arbitration
# and the task could never be re-claimed by anyone else.
. "$ROOT/tests/lib.sh"

mk_issue 7 "shipped task" kraken-task "project:app" in-progress
mk_comment 7 "claimed-by: w1"

r="$STATE/result.md"
printf 'Added cursor pagination. Acceptance run: 12/12 green.\n' > "$r"

out="$(python3 "$SCRIPTS/kraken.py" deliver OWNER/tasks 7 w1 "$r" "https://github.com/owner/app/pull/9")"
assert_rc $? 0 "deliver exit"
assert_eq "$out" "deliver: delivered issue=7 worker=w1 pr=https://github.com/owner/app/pull/9" "machine line"

has_label 7 in-progress && fail "in-progress still present after deliver"
has_label 7 awaiting-merge || fail "awaiting-merge missing after deliver"
c="$(last_comment 7)"
printf '%s' "$c" | grep -q '^delivered: w1$' || fail "delivered machine line missing"
printf '%s' "$c" | grep -q '^pr: https://github.com/owner/app/pull/9$' || fail "pr line missing"
printf '%s' "$c" | grep -q '^Added cursor pagination' || fail "result body missing"

# Review bounce: feedback comment, awaiting-merge removed — w2 must win.
mk_comment 7 "please rename the flag before merge"
grep -vxF "awaiting-merge" "$STATE/issues/7/labels" > "$STATE/issues/7/labels.tmp"
mv "$STATE/issues/7/labels.tmp" "$STATE/issues/7/labels"

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w2)"
assert_rc $? 0 "re-claim after review bounce"
assert_eq "$out" "claim: claimed issue=7 worker=w2" "delivery reset the claim window"

# No PR URL (diff-in-comment path): no pr: line, everything else identical.
mk_issue 8 "patch task" kraken-task "project:app" in-progress
out="$(python3 "$SCRIPTS/kraken.py" deliver OWNER/tasks 8 w1 "$r")"
assert_rc $? 0 "deliver without pr exit"
assert_eq "$out" "deliver: delivered issue=8 worker=w1" "machine line without pr"
last_comment 8 | grep -q '^pr: ' && fail "pr line present without a PR URL"
has_label 8 awaiting-merge || fail "awaiting-merge missing on no-pr deliver"
exit 0
