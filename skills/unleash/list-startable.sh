#!/usr/bin/env bash
# list-startable.sh OWNER/tasks PROJECT [--snapshot]
#   PROJECT is the bare project name — the script prepends the "project:" prefix.
#
# The single source of the "startable" filter: open `kraken-task` issues scoped
# to the project, not held by in-progress / needs-decision / awaiting-merge.
# Both consumers resolve it from this file, so the filter cannot drift:
#   - unleash protocol step 1 (default mode): startable candidates, oldest first
#   - watch-queue.sh (--snapshot mode): the full queue state it diffs each poll
#
# Output, one line per task, nothing else on stdout:
#   default:     <number><TAB><title>        startable only, oldest first
#   --snapshot:  <number>:startable|held     every open task, sorted by number
#
# Exit codes: 0 success (no output = nothing to list); 20 gh/network failure.
# Labels cannot see blocked-by relationships — a listed task may still be
# dependency-blocked; the unleash dependency check (protocol step 2) is the
# caller's job.
set -u

REPO="${1:?usage: list-startable.sh OWNER/tasks PROJECT [--snapshot]}"
PROJECT="${2:?usage: list-startable.sh OWNER/tasks PROJECT [--snapshot]}"
MODE="${3:-}"

# The one definition of "held": claimed, escalated, or delivered.
HELD='index("in-progress") or index("needs-decision") or index("awaiting-merge")'

case "$MODE" in
  --snapshot)
    JQ='sort_by(.number)[] | "\(.number):" + (if ([.labels[].name] | ('"$HELD"')) then "held" else "startable" end)'
    ;;
  "")
    JQ='[.[] | select([.labels[].name] | ('"$HELD"') | not)] | sort_by(.createdAt)[] | "\(.number)\t\(.title)"'
    ;;
  *)
    echo "list-startable.sh: unknown mode '$MODE'" >&2
    exit 2
    ;;
esac

out="$(gh -R "$REPO" issue list --state open --limit 100 \
  --label kraken-task --label "project:${PROJECT}" \
  --json number,title,labels,createdAt \
  --jq "$JQ")" || exit 20

out="${out//$'\r'/}" # CRLF-emitting gh would silently break $-anchored matches downstream
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
