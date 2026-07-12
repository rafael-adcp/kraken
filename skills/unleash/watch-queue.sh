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
# Emission rule: emit when the queue snapshot CHANGES and at least one task is
# startable (new task, requeue via label removal, a blocker closing).
# list-startable.sh is the single, complete owner of "startable" — including
# the blocked-by check — so a dependency-blocked task is never counted here;
# there is nothing left for this loop to second-guess.
#
# A failed poll (network, gh hiccup) skips the cycle without killing the
# watcher or corrupting the last-seen snapshot.
set -u

REPO="${1:?usage: watch-queue.sh OWNER/tasks PROJECT}"
PROJECT="${2:?usage: watch-queue.sh OWNER/tasks PROJECT}"

POLL_SECONDS="${KRAKEN_WATCH_POLL_SECONDS:-60}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

prev=""

while true; do
  # One line per open kraken-task in the project: "<number>:startable" or
  # "<number>:held". The startable filter itself lives in list-startable.sh —
  # the same file unleash protocol step 1 runs — so the two can never drift.
  if snapshot="$(bash "$SCRIPT_DIR/list-startable.sh" "$REPO" "$PROJECT" --snapshot 2>/dev/null)"; then
    count="$(printf '%s\n' "$snapshot" | grep -c ':startable$')" || true
    if [ "$count" -gt 0 ] && [ "$snapshot" != "$prev" ]; then
      numbers="$(printf '%s\n' "$snapshot" | sed -n 's/^\([0-9][0-9]*\):startable$/#\1/p' | paste -sd' ' -)"
      echo "kraken-queue: ${count} startable task(s) in project:${PROJECT} (${numbers})"
    fi
    prev="$snapshot"
  fi
  sleep "$POLL_SECONDS"
done
