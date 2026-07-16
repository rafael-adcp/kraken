#!/usr/bin/env bash
# claim.sh guard: a held task is skipped with exit 11 and ZERO writes —
# stacking in-progress on awaiting-merge is the corruption class the guard exists for.
. "$ROOT/tests/lib.sh"

n=10
for held in in-progress needs-decision awaiting-merge; do
  n=$((n + 1))
  mk_issue "$n" "held by $held" kraken-task "project:app" "$held"

  out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks "$n" w1)"
  rc=$?
  assert_rc "$rc" 11 "claim on $held exit"
  assert_eq "$out" "claim: held issue=$n label=$held" "machine line for $held"
  assert_eq "$(comment_count "$n")" "0" "no comment written on $held"
  grep -Eq "issue (edit|comment) $n " "$STATE/log" && fail "guard wrote to issue $n despite $held"
done
exit 0
