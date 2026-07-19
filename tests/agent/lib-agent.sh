#!/usr/bin/env bash
# Shared helpers for the agent-behavior scenarios (tests/agent/scenarios/*.sh) —
# source, don't execute. Where the conformance suite proves the transition
# PROGRAM mechanically, this harness proves the SKILL's judgment: it drives a
# real headless `claude -p` against the gh-stub and asserts on ARTIFACTS (stub
# labels/machine lines, work-repo git state), never transcripts (PROTOCOL.md §12).
# One scenario = one seeded queue + task body; the gh-stub goes first on PATH so
# both coordination and work-repo `gh` calls resolve to it, and the work repo has
# a real bare remote so push / default-branch checks are genuine git facts.
set -u

# --- layout --------------------------------------------------------------------
AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$AGENT_ROOT/../.." && pwd)"
GH_STUB="$ROOT/tests/gh-stub"
SCRIPTS="$ROOT/skills/unleash"

WORKER="${WORKER:-t1}"
PROJECT="${PROJECT:-x}"
COORD="${COORD:-stub-owner/tasks}"
# Work repo slug the skill is told about; gh pr create records PRs under it.
WORK_REPO_SLUG="${WORK_REPO_SLUG:-stub-owner/work}"

# One tempdir holds all per-scenario scratch, so cleanup is one rm.
SCRATCH="$(mktemp -d)"
export GH_STUB_STATE="$SCRATCH/gh-state"
WORK_DIR="$SCRATCH/work"          # the clone the agent edits
WORK_BARE="$SCRATCH/work.git"     # the "remote" it pushes to
mkdir -p "$GH_STUB_STATE/issues"
: > "$GH_STUB_STATE/log"
trap 'rm -rf "$SCRATCH"' EXIT

# The gh-stub must win over the real gh for BOTH repos in this run.
export PATH="$GH_STUB:$PATH"

# --- coordination queue seeding (mirrors tests/lib.sh, standalone on purpose) --
# mk_task N TITLE [LABEL...] — a queued task. createdAt derives from N.
mk_task() {
  local n="$1" title="$2" d="$GH_STUB_STATE/issues/$1"
  shift 2
  mkdir -p "$d/comments"
  printf '%s\n' "$title" > "$d/title"
  echo "open" > "$d/state"
  printf '2026-07-01T%02d:%02d:00Z\n' $((n / 60)) $((n % 60)) > "$d/createdAt"
  : > "$d/labels"
  local l
  for l in "$@"; do printf '%s\n' "$l" >> "$d/labels"; done
}

# mk_task_body N TEXT — the task's body (goal/acceptance/notes live here).
mk_task_body() { printf '%s\n' "$2" > "$GH_STUB_STATE/issues/$1/body"; }

# --- work repo: a real git repo with a local bare remote ----------------------
# The seed commit on `main` is the untouched baseline the harness asserts never
# moves; the tracked file gives the agent something real to change.
setup_work_repo() {
  git init --quiet --bare "$WORK_BARE"
  git --git-dir="$WORK_BARE" symbolic-ref HEAD refs/heads/main

  git init --quiet -b main "$WORK_DIR"
  git -C "$WORK_DIR" config user.email "worker@example.com"
  git -C "$WORK_DIR" config user.name "Kraken Worker"
  git -C "$WORK_DIR" config commit.gpgsign false
  printf '# Sample project\n\nExisting content.\n' > "$WORK_DIR/README.md"
  printf 'placeholder\n' > "$WORK_DIR/feature.txt"
  git -C "$WORK_DIR" add -A
  git -C "$WORK_DIR" commit --quiet -m "seed: initial commit"
  git -C "$WORK_DIR" remote add origin "$WORK_BARE"
  git -C "$WORK_DIR" push --quiet -u origin main
  git --git-dir="$WORK_BARE" rev-parse main > "$SCRATCH/main-baseline.sha"
}

# --- environment capability probe ---------------------------------------------
# skip_scenario REASON — a scenario that CANNOT run for real here. Exits 2 so the
# runner tells a skip from an ok (0) or a fail (1); it counts skipped, not passed.
skip_scenario() { echo "SKIP: ${SCENARIO_NAME:-scenario} ($1)"; exit 2; }

# --- driving the skill headlessly ---------------------------------------------
# run_unleash — invoke the real skill under `claude -p`, once, with the repo's
# own plugin dir loaded so CI needs no pre-install. We assert on artifacts, never
# on the log ($SCRATCH/agent.log — kept only for debugging a failed scenario).
# The prompt is fixed; scenarios differ only in seeded state and task body.
run_unleash() {
  local prompt="/kraken:unleash ${COORD} --worker-name ${WORKER} --project ${PROJECT} --once"
  local extra_ctx="${1:-}"
  [ -n "$extra_ctx" ] && prompt="${prompt}

${extra_ctx}"

  CLAUDE_AGENT_TIMEOUT="${CLAUDE_AGENT_TIMEOUT:-600}"
  ( cd "$WORK_DIR" && timeout "$CLAUDE_AGENT_TIMEOUT" claude -p "$prompt" \
      --plugin-dir "$ROOT" \
      --dangerously-skip-permissions \
      --max-turns "${CLAUDE_MAX_TURNS:-60}" ) \
    > "$SCRATCH/agent.log" 2>&1
  local rc=$?

  # The model never actually running (spend/rate/auth limit, or a timeout with an
  # empty transcript) is an environment SKIP, not a false FAIL — detect it from
  # the run's own output and bail BEFORE any assertion.
  if grep -Eqi 'spend limit|rate limit|usage limit|quota|overloaded|invalid api key|authentication_error|not authenticated|please run .*login|credit balance' "$SCRATCH/agent.log"; then
    skip_scenario "the nested claude -p could not run (spend/rate/auth limit): $(grep -Eio 'spend limit|rate limit|usage limit|quota|overloaded|invalid api key|authentication|credit balance' "$SCRATCH/agent.log" | head -n1). No judgment was exercised — re-run when the limit clears."
  fi
  if [ "$rc" -eq 124 ] && [ ! -s "$SCRATCH/agent.log" ]; then
    skip_scenario "the nested claude -p timed out before producing any output (${CLAUDE_AGENT_TIMEOUT}s). No judgment was exercised."
  fi
  # The stub logs every invocation, so an empty log means the model never reached
  # it — every `gh` hit the real gh, not the seeded queue. Environment SKIP, not a
  # FAIL; a run that reached the stub once logs something, so misjudgment still FAILs.
  if [ ! -s "$GH_STUB_STATE/log" ]; then
    skip_scenario "the nested claude never reached the gh-stub (its invocation log is empty): the stub's dir was not in front of the real 'gh' for the nested tool shell, so the model judged against the real gh, not the seeded queue. No judgment was exercised — an environment gap, not a skill fault."
  fi
  return "$rc"
}

# --- assertions ----------------------------------------------------------------
fail() { echo "FAIL: $*"; [ -f "$SCRATCH/agent.log" ] && { echo "--- last 40 lines of agent.log ---"; tail -n 40 "$SCRATCH/agent.log"; }; exit 1; }

# Coordination-repo state.
labels_of() { cat "$GH_STUB_STATE/issues/$1/labels" 2>/dev/null; }
has_label() { grep -qxF -- "$2" "$GH_STUB_STATE/issues/$1/labels" 2>/dev/null; }
no_label()  { ! has_label "$1" "$2"; }

# Concatenated comment bodies in server order — the machine-payload assertion
# surface, exactly what kraken.py's arbitration reads.
comment_stream() {
  local f
  for f in "$GH_STUB_STATE/issues/$1/comments"/*.md; do
    [ -f "$f" ] || continue
    cat "$f"; echo
  done
}
# Machine state lives in a hidden kraken marker — an HTML comment carrying JSON
# (PROTOCOL.md §4), read marker-only. These helpers scan the stream for a marker
# of a given "type", optionally carrying a non-empty field (field follows "type").
# has_marker ISSUE TYPE — a marker with "type":"TYPE" exists in the stream.
has_marker() {
  comment_stream "$1" \
    | grep -Eq "<!--[[:space:]]*kraken[[:space:]]+\{[^}]*\"type\":\"$2\"[^}]*\}[[:space:]]*-->"
}
# has_marker_field ISSUE TYPE FIELD — a TYPE marker carries a non-empty FIELD.
has_marker_field() {
  comment_stream "$1" \
    | grep -Eq "<!--[[:space:]]*kraken[[:space:]]+\{[^}]*\"type\":\"$2\"[^}]*\"$3\":\"[^\"]+\"[^}]*\}[[:space:]]*-->"
}

assert_label()    { has_label "$1" "$2" || fail "issue $1: expected label '$2' (have: $(labels_of "$1" | tr '\n' ' '))"; }
assert_no_label() { no_label  "$1" "$2" || fail "issue $1: label '$2' must NOT be present (have: $(labels_of "$1" | tr '\n' ' '))"; }
assert_marker() { has_marker "$1" "$2" || fail "issue $1: expected hidden kraken marker of type '$2' in comment stream (§4)"; }
assert_marker_field() { has_marker_field "$1" "$2" "$3" || fail "issue $1: expected '$2' marker carrying field '$3' in comment stream"; }

# Work-repo git state.
pushed_branches() { git --git-dir="$WORK_BARE" for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null; }
branch_exists_on_remote() { pushed_branches | grep -qxF -- "$1"; }
# A non-default branch was pushed (delivery landed a work branch).
assert_work_branch_pushed() {
  local extra
  extra="$(pushed_branches | grep -vxF main || true)"
  [ -n "$extra" ] || fail "expected a work branch pushed to the remote; only have: $(pushed_branches | tr '\n' ' ')"
}
assert_no_work_branch_pushed() {
  local extra
  extra="$(pushed_branches | grep -vxF main || true)"
  [ -z "$extra" ] || fail "no work branch should have been pushed; found: $extra"
}
# The remote default branch must not have moved.
assert_default_branch_untouched() {
  local now base
  now="$(git --git-dir="$WORK_BARE" rev-parse main 2>/dev/null)"
  base="$(cat "$SCRATCH/main-baseline.sha")"
  [ "$now" = "$base" ] || fail "remote default branch 'main' moved: $base -> $now (delivery MUST NOT push to default)"
}
# Every commit on the pushed work branch carries the attribution trailers.
assert_trailers_on_work_branch() {
  local br log
  br="$(pushed_branches | grep -vxF main | head -n1)"
  [ -n "$br" ] || fail "no work branch to check trailers on"
  log="$(git --git-dir="$WORK_BARE" log main.."$br" --format='%b%n---%n' 2>/dev/null)"
  printf '%s' "$log" | grep -q 'Co-Authored-By:' \
    || fail "work-branch commits missing Co-Authored-By trailer"
  printf '%s' "$log" | grep -Eq 'Kraken-Task:.*(personal-tasks|tasks)#' \
    || fail "work-branch commits missing Kraken-Task trailer"
}
# A draft PR was recorded against the work repo.
pr_files() { ls "$GH_STUB_STATE/pr"/*.json 2>/dev/null; }
assert_draft_pr_opened() {
  local files
  files="$(pr_files || true)"
  [ -n "$files" ] || fail "expected a draft PR recorded by the stub; none found"
  local f
  for f in $files; do
    [ "$(jq -r '.draft' "$f")" = "true" ] || fail "PR $(basename "$f") is not a draft"
  done
}
assert_no_pr_opened() {
  local files
  files="$(pr_files || true)"
  [ -z "$files" ] || fail "no PR should have been opened; found: $files"
}

pass() { echo "PASS: ${SCENARIO_NAME:-scenario}"; exit 0; }
