#!/usr/bin/env bash
# list-startable.sh OWNER/tasks PROJECT [--snapshot]
#   PROJECT is the bare project name — the script prepends the "project:" prefix.
#
# The single, complete owner of the "startable" filter: open `kraken-task`
# issues scoped to the project, not held by in-progress / needs-decision /
# awaiting-merge, AND not blocked — every blocked-by issue closed (checked
# server-side via `gh api .../dependencies/blocked_by`; a `depends-on: #N`
# line in the issue body is honored as a fallback only when there are no
# native blockers at all). Both consumers resolve it from this file, so the
# filter cannot drift:
#   - unleash protocol step 1 (default mode): startable candidates, oldest first
#   - watch-queue.sh (--snapshot mode): the full queue state it diffs each poll
#
# Output, one line per task, nothing else on stdout:
#   default:     <number><TAB><title>        startable only, oldest first
#   --snapshot:  <number>:startable|held     every open task, sorted by number
#
# A blocked task reports "held" in --snapshot mode — the same token a
# label-held task gets. Nothing downstream needs to tell the two apart:
# watch-queue only counts ":startable$" lines, so a blocked-only queue never
# wakes it.
#
# Exit codes: 0 success (no output = nothing to list); 20 gh/network failure.
set -u

REPO="${1:?usage: list-startable.sh OWNER/tasks PROJECT [--snapshot]}"
PROJECT="${2:?usage: list-startable.sh OWNER/tasks PROJECT [--snapshot]}"
MODE="${3:-}"

case "$MODE" in
  --snapshot | "") ;;
  *)
    echo "list-startable.sh: unknown mode '$MODE'" >&2
    exit 2
    ;;
esac

# The one definition of "label-held": claimed, escalated, or delivered.
HELD='index("in-progress") or index("needs-decision") or index("awaiting-merge")'
# One row per open project task, oldest first, label-held resolved:
#   <number>\t<held 0|1>\t<title>
JQ='sort_by(.createdAt)[] | "\(.number)\t" + (if ([.labels[].name] | ('"$HELD"')) then "1" else "0" end) + "\t\(.title)"'

rows="$(gh -R "$REPO" issue list --state open --limit 100 \
  --label kraken-task --label "project:${PROJECT}" \
  --json number,title,labels,createdAt \
  --jq "$JQ")" || exit 20
rows="${rows//$'\r'/}" # CRLF-emitting gh would silently break $-anchored matches downstream

# is_blocked N -> 0 (blocked, at least one blocker still open) or 1 (clear).
# Native blocked-by relationships take priority; a `depends-on: #N` body line
# is only a fallback, consulted when the candidate has zero native blockers.
# Any gh/network failure returns 2, mapped to exit 20 by the caller.
is_blocked() {
  local n="$1" resp total_count open_count body dep_n dep_state
  resp="$(gh api "repos/${REPO}/issues/${n}/dependencies/blocked_by" \
    --jq '[length, ([.[] | select(.state == "open")] | length)] | @tsv')" || return 2
  total_count="${resp%%$'\t'*}"
  open_count="${resp##*$'\t'}"

  if [ "${total_count:-0}" -gt 0 ]; then
    [ "${open_count:-0}" -gt 0 ] && return 0 # a native blocker is still open
    return 1
  fi

  # No native blockers — fall back to a `depends-on: #N` body line.
  body="$(gh -R "$REPO" issue view "$n" --json body --jq '.body // ""')" || return 2
  dep_n="$(printf '%s\n' "$body" | sed -n 's/^depends-on: *#\([0-9][0-9]*\).*/\1/p' | head -n1)"
  [ -n "$dep_n" ] || return 1 # no fallback declared — clear

  dep_state="$(gh -R "$REPO" issue view "$dep_n" --json state --jq '.state')" || return 2
  case "$dep_state" in
    [Oo][Pp][Ee][Nn]) return 0 ;; # dependency still open — blocked
    *) return 1 ;;
  esac
}

# Single pass, createdAt order (already what default mode wants): label-held
# rows skip the blocked-check entirely; label-clear rows get one, deciding
# startable vs. held. snapshot_out accumulates unsorted (number-sorted at
# print time); startable_out is already in the right order for default mode.
# Real tabs/newlines throughout (never a printf '%b' escape reinterpretation
# of accumulated text) so a title containing a literal backslash sequence
# can't be misread as a format directive.
snapshot_out=""
startable_out=""
nl="
"
tab="$(printf '\t')"
while IFS="$tab" read -r n held title; do
  [ -n "$n" ] || continue
  if [ "$held" -eq 1 ]; then
    snapshot_out="${snapshot_out}${n}:held${nl}"
    continue
  fi
  rc=0
  is_blocked "$n" || rc=$?
  case "$rc" in
    0) snapshot_out="${snapshot_out}${n}:held${nl}" ;;
    1) snapshot_out="${snapshot_out}${n}:startable${nl}"
       startable_out="${startable_out}${n}${tab}${title}${nl}" ;;
    *) exit 20 ;;
  esac
done <<EOF
$rows
EOF

case "$MODE" in
  --snapshot)
    out="$(printf '%s' "$snapshot_out" | sed '/^$/d' | sort -t: -k1,1n)"
    [ -n "$out" ] && printf '%s\n' "$out"
    ;;
  "")
    out="$(printf '%s' "$startable_out" | sed '/^$/d')"
    [ -n "$out" ] && printf '%s\n' "$out"
    ;;
esac
exit 0
