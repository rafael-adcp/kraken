#!/usr/bin/env bash
# kraken.py status: the read-only operator console, mechanized. Pins the four
# things the skill used to teach an LLM to orchestrate — the review/decision/
# in-flight queues, the heartbeat-age anchored to the worker's last machine line
# (NOT updatedAt), the merged-PR-but-open-issue orphan heuristic (flag, never
# act), and the launch recon — plus the hard guarantee that the whole thing is
# read-only: not one label change, not one comment, not one write.
. "$ROOT/tests/lib.sh"

nowiso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
ago_iso() { date -u -d "@$(( $(date -u +%s) - $1 * 3600 ))" +%Y-%m-%dT%H:%M:%SZ; }

# --- seed a full queue -------------------------------------------------------

# Review queue: #88 delivered with a MERGED PR (an orphan), #91 delivered with
# an OPEN PR (healthy — waiting on the merge, not flagged).
mk_issue 88 "orphan candidate" kraken-task "project:app" awaiting-merge
mk_comment 88 "$(printf '> d\n\ndelivered: w1\npr: https://github.com/OWNER/work/pull/5\n\nlanded')"
mk_pr 5 MERGED "$(nowiso)"
mk_issue 91 "healthy delivery" kraken-task "project:app" awaiting-merge
mk_comment 91 "$(printf '> d\n\ndelivered: w2\npr: https://github.com/OWNER/work/pull/6')"
mk_pr 6 OPEN

# Decision queue.
mk_issue 97 "needs a human call" kraken-task "project:app" needs-decision

# In flight: #99 DEAD — claimed 8h ago, then an operator commented just now.
# updatedAt would read fresh and hide the death; anchored to the last machine
# line it is 8h silent. #100 ALIVE — a fresh heartbeat 0h ago.
mk_issue 99 "dead worker, operator poked it" kraken-task "project:app" in-progress
mk_comment 99 "$(printf '> d\n\nclaimed-by: dead-worker\n')" "$(ago_iso 8)"
mk_comment 99 "any progress? — the operator" "$(ago_iso 0)"
mk_issue 100 "live worker" kraken-task "project:app" in-progress
mk_comment 100 "$(printf '> d\n\nclaimed-by: live-worker')" "$(ago_iso 9)"
mk_comment 100 "$(printf '> d\n\nheartbeat: live-worker')" "$(ago_iso 0)"

# A startable task in another project, and an empty registered project — both
# must show in the launch recon; only project: labels drive it.
mk_issue 12 "queued elsewhere" kraken-task "project:web"
mk_label "project:idle"

# --- a read-only snapshot for the write-guarantee assertion ------------------
snapshot="$STATE/snapshot"
( cd "$STATE/issues"
  for d in */; do
    printf '%s|labels=%s|comments=%s\n' "$d" \
      "$(tr '\n' ',' < "$d/labels")" "$(ls "$d/comments"/*.md 2>/dev/null | wc -l)"
  done ) | sort > "$snapshot"

# --- 1. human console --------------------------------------------------------
out="$(python3 "$SCRIPTS/kraken.py" status OWNER/tasks)"
assert_rc $? 0 "status human exit"

printf '%s\n' "$out" | grep -qF '#88  orphan candidate' || fail "review item #88 missing"
printf '%s\n' "$out" | grep -qF 'https://github.com/OWNER/work/pull/5' || fail "PR link for #88 missing"
printf '%s\n' "$out" | grep -qF '#97  needs a human call' || fail "decision item #97 missing"

# Heartbeat age anchors to the machine line: #99 reads 8h despite the operator's
# just-now comment; #100's fresh heartbeat keeps it recent (not 8h/9h).
printf '%s\n' "$out" | grep -qE '#99 .*worker dead-worker .*last heartbeat 8h ago' \
  || fail "in-flight #99 heartbeat age not anchored to the last machine line (expected 8h)"
printf '%s\n' "$out" | grep -qE '#100 .*worker live-worker' || fail "in-flight #100 missing worker"
printf '%s\n' "$out" | grep -qE '#100 .*last heartbeat 8h ago' \
  && fail "in-flight #100 read the stale claim, not the fresh heartbeat"

# Orphan heuristic: #88 (merged PR) flagged; #91 (open PR) is NOT.
printf '%s\n' "$out" | grep -qF 'possible orphan(s): #88' || fail "#88 not flagged as an orphan"
printf '%s\n' "$out" | grep -q 'orphan.*#91' && fail "#91 (open PR) wrongly flagged as an orphan"

# Launch recon lists every project: label, incl. the empty one; never a literal.
for p in app idle web; do
  printf '%s\n' "$out" | grep -qF -- "--project $p" || fail "launch recon missing project:$p"
done
printf '%s\n' "$out" | grep -qF 'OWNER/tasks --worker-name <worker-name>' \
  || fail "launch line shape wrong"

# --- 2. THE read-only guarantee: nothing was written -------------------------
after="$STATE/after"
( cd "$STATE/issues"
  for d in */; do
    printf '%s|labels=%s|comments=%s\n' "$d" \
      "$(tr '\n' ',' < "$d/labels")" "$(ls "$d/comments"/*.md 2>/dev/null | wc -l)"
  done ) | sort > "$after"
diff -u "$snapshot" "$after" || fail "status mutated the queue — it must be read-only"
# The orphan was flagged, not acted on: #88 is still open and still awaiting-merge.
has_label 88 awaiting-merge || fail "status changed #88's label (must only flag the orphan)"

# --- 3. --json: the documented stable schema ---------------------------------
js="$(python3 "$SCRIPTS/kraken.py" status OWNER/tasks --json)"
assert_rc $? 0 "status --json exit"
printf '%s' "$js" | jq -e '.review_queue | length == 2' >/dev/null || fail "json review_queue wrong length"
printf '%s' "$js" | jq -e '.decision_queue | map(.number) | index(97) != null' >/dev/null \
  || fail "json decision_queue missing #97"
printf '%s' "$js" | jq -e '.orphans == [88]' >/dev/null || fail "json orphans != [88]"
printf '%s' "$js" | jq -e '(.review_queue[] | select(.number==88) | .orphan) == true' >/dev/null \
  || fail "json #88 orphan flag not true"
printf '%s' "$js" | jq -e '(.review_queue[] | select(.number==91) | .orphan) == false' >/dev/null \
  || fail "json #91 orphan flag not false"
printf '%s' "$js" | jq -e '(.in_flight[] | select(.number==99) | .worker) == "dead-worker"' >/dev/null \
  || fail "json in_flight #99 worker wrong"
printf '%s' "$js" | jq -e '(.in_flight[] | select(.number==99) | .heartbeat_age_seconds) >= 28000' >/dev/null \
  || fail "json in_flight #99 age not anchored (expected ~8h in seconds)"
printf '%s' "$js" | jq -e '.projects == ["app","idle","web"]' >/dev/null \
  || fail "json projects list wrong"

# --- 4. --project scopes every list to that project --------------------------
scoped="$(python3 "$SCRIPTS/kraken.py" status OWNER/tasks --project web --json)"
assert_rc $? 0 "status --project --json exit"
printf '%s' "$scoped" | jq -e '.review_queue == [] and .decision_queue == [] and .in_flight == []' >/dev/null \
  || fail "--project web should have empty held queues"
printf '%s' "$scoped" | jq -e '.project == "web"' >/dev/null || fail "--project not reflected in json"

exit 0
