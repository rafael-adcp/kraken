#!/usr/bin/env bash
# watch-queue.sh OWNER/tasks PROJECT
#   PROJECT is the bare project name — the script prepends the "project:" prefix.
#
# The kraken watcher: polls the coordination repo's queue with free `gh` calls
# and prints one line only when a startable task is waiting. Armed as a
# persistent Monitor by skills/unleash/SKILL.md (protocol step 6), each printed
# line wakes the agent, which runs the unleash drain protocol. Idle queue = no
# output = the model is never invoked.
#
# Emission rules (a state machine, not a naive "print while non-empty" — labels
# cannot see blocked-by relationships, so a dependency-blocked task can sit
# startable-by-label for hours and must not wake the agent every cycle):
#   - emit when the queue snapshot CHANGES and at least one task is startable
#     (new task, requeue via label removal, a potential blocker closing);
#   - while startable tasks sit unclaimed with no snapshot change, re-emit only
#     every REMIND_SECONDS — the safety net for blockers that close outside the
#     filter (other project, non-task issue) and for missed notifications;
#   - a failed poll (network, gh hiccup) skips the cycle without killing the
#     watcher or corrupting the last-seen snapshot.
set -u

REPO="${1:?usage: watch-queue.sh OWNER/tasks PROJECT}"
PROJECT="${2:?usage: watch-queue.sh OWNER/tasks PROJECT}"

POLL_SECONDS="${KRAKEN_WATCH_POLL_SECONDS:-60}"
REMIND_SECONDS="${KRAKEN_WATCH_REMIND_SECONDS:-1800}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

prev=""
last_emit=0

while true; do
  # One line per open kraken-task in the project: "<number>:startable" or
  # "<number>:held". The startable filter itself lives in list-startable.sh —
  # the same file unleash protocol step 1 runs — so the two can never drift.
  if snapshot="$(bash "$SCRIPT_DIR/list-startable.sh" "$REPO" "$PROJECT" --snapshot 2>/dev/null)"; then
    count="$(printf '%s\n' "$snapshot" | grep -c ':startable$')" || true
    now="$(date +%s)"
    if [ "$count" -gt 0 ] && { [ "$snapshot" != "$prev" ] || [ "$((now - last_emit))" -ge "$REMIND_SECONDS" ]; }; then
      numbers="$(printf '%s\n' "$snapshot" | sed -n 's/^\([0-9][0-9]*\):startable$/#\1/p' | paste -sd' ' -)"
      echo "kraken-queue: ${count} startable task(s) in project:${PROJECT} (${numbers})"
      last_emit="$now"
    fi
    prev="$snapshot"
  fi
  sleep "$POLL_SECONDS"
done
