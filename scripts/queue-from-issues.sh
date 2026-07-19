#!/usr/bin/env bash
#
# queue-from-issues.sh — feed the kraken queue from plain issues.
#
# Translates open issues you authored in a SOURCE repo into kraken tasks in your
# coordination (DEST) repo: one issue per task, shaped like the task template and
# labeled kraken-task + project:<name>. With a worker looping on the queue, the
# things you jot down through the day get picked up on their own.
#
# Idempotent: each translated source issue is tagged with a marker label (default
# "queued") and skipped on later runs, so re-running never duplicates a task.
#
# Requires: gh (authenticated). No external jq — gh's built-in --jq is used.

set -euo pipefail

usage() {
  cat <<'EOF'
Feed the kraken queue from plain issues.

Usage:
  queue-from-issues.sh --dest OWNER/tasks [options]

Options:
  --source OWNER/REPO   Repo to read issues from (default: the current repo)
  --dest   OWNER/REPO   Coordination repo to create tasks in
                        (default: $KRAKEN_TASKS_REPO)
  --project NAME        project:<name> label put on each task (default: kraken)
  --author LOGIN        Only translate issues by this author; repeatable and/or
                        comma-separated to include several accounts (default: you)
  --marker LABEL        Dedup label set on translated source issues (default: queued)
  --dry-run             Print what would be queued; create nothing
  -h, --help            Show this help
EOF
}

die() { echo "error: $*" >&2; exit 1; }

SOURCE=""
DEST="${KRAKEN_TASKS_REPO:-}"
PROJECT="kraken"
AUTHORS=()
MARKER="queued"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --source)  SOURCE="${2:-}"; shift 2 ;;
    --dest)    DEST="${2:-}"; shift 2 ;;
    --project) PROJECT="${2:-}"; shift 2 ;;
    --author)  # repeatable and/or comma-separated
               IFS=',' read -ra _more <<< "${2:-}"
               for _a in "${_more[@]}"; do
                 _a="${_a// /}"; [ -n "$_a" ] && AUTHORS+=("$_a")
               done
               shift 2 ;;
    --marker)  MARKER="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

command -v gh >/dev/null 2>&1 || die "gh not found on PATH"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (run: gh auth login)"

# Defaults that need gh to resolve.
if [ -z "$SOURCE" ]; then
  SOURCE=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) \
    || die "could not detect the source repo — pass --source OWNER/REPO"
fi
[ -n "$DEST" ] || die "no destination repo — pass --dest OWNER/REPO or set KRAKEN_TASKS_REPO"
if [ "${#AUTHORS[@]}" -eq 0 ]; then
  me=$(gh api user --jq .login) || die "could not resolve your gh login"
  AUTHORS=("$me")
fi

authors_csv=$(IFS=,; echo "${AUTHORS[*]}")
echo "source: $SOURCE (authors: $authors_csv, skip label: $MARKER)"
echo "dest:   $DEST (labels: kraken-task, project:$PROJECT)"
[ "$DRY_RUN" -eq 1 ] && echo "mode:   DRY RUN — nothing will be created"
echo

# Candidate issues: open, authored by any of AUTHORS, not already marked.
# gh issue list takes one --author, so query per author and union the numbers.
RAW=()
for a in "${AUTHORS[@]}"; do
  while IFS= read -r n; do
    [ -n "$n" ] && RAW+=("$n")
  done < <(
    gh issue list -R "$SOURCE" --state open --author "$a" --limit 200 \
      --json number,labels \
      --jq '[.[] | select(([.labels[].name] | index("'"$MARKER"'")) | not) | .number] | .[]'
  )
done

NUMBERS=()
if [ "${#RAW[@]}" -gt 0 ]; then
  while IFS= read -r n; do
    NUMBERS+=("$n")
  done < <(printf '%s\n' "${RAW[@]}" | sort -un)
fi

if [ "${#NUMBERS[@]}" -eq 0 ]; then
  echo "nothing to queue."
  exit 0
fi

# Best-effort: make sure the labels we apply exist (no-op if they already do).
if [ "$DRY_RUN" -eq 0 ]; then
  gh label create "$MARKER"          -R "$SOURCE" --color BFDADC --description "Copied into the kraken queue" 2>/dev/null || true
  gh label create "kraken-task"      -R "$DEST"   --color 5319E7 --description "A unit of work for a kraken worker" 2>/dev/null || true
  gh label create "project:$PROJECT" -R "$DEST"   --color 0E8A16 --description "Scopes a task to the $PROJECT project" 2>/dev/null || true
fi

count=0
for n in "${NUMBERS[@]}"; do
  # title+url are single-line (lines 1-2), body is line 3 on — keeps us jq-free.
  data=$(gh issue view "$n" -R "$SOURCE" --json title,url,body --jq '.title, .url, .body')
  title=$(printf '%s\n' "$data" | sed -n '1p')
  url=$(printf '%s\n' "$data" | sed -n '2p')
  body=$(printf '%s\n' "$data" | sed -n '3,$p')

  goal="${body:-$title}"
  task_body=$(cat <<EOF
### Goal

$goal

### Acceptance

_(fill in / derive from the goal)_

### Notes

Translated from $SOURCE#$n — $url
EOF
)

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "would queue: $SOURCE#$n  \"$title\"  ->  $DEST (project:$PROJECT)"
    count=$((count + 1))
    continue
  fi

  new_url=$(gh issue create -R "$DEST" \
    --title "$title" \
    --body "$task_body" \
    --label "kraken-task" \
    --label "project:$PROJECT")

  gh issue edit "$n" -R "$SOURCE" --add-label "$MARKER" >/dev/null

  echo "queued: $SOURCE#$n  ->  $new_url"
  count=$((count + 1))
done

echo
if [ "$DRY_RUN" -eq 1 ]; then
  echo "done: $count issue(s) would be queued."
else
  echo "done: $count issue(s) queued."
fi
