"""The zero-token ambush: poll the queue, wake on an edge.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Callable

from .contract import EXIT_TRANSPORT, EXIT_UNKNOWN_PROJECT, Epoch
from .transport import Api
from .lease import wake_retry_mtime
from .queue import Queue

# --- subcommand: watch -------------------------------------------------------

def snapshot_state(api: Api, project: str) -> str | None:
    """The queue snapshot list-startable emits in --snapshot mode, via the same
    Queue.candidates. Returns the snapshot text, or None on transport failure."""
    rows = Queue(api).candidates(project)
    if rows is None:
        return None
    return "\n".join(
        f"{c.number}:{c.state}" for c in sorted(rows, key=lambda c: c.number)
    )


def wake_retry_due(flag_mtime: float | None, last_emit: float,
                   retry_seconds: int, now: Epoch) -> bool:
    """Whether the watcher owes a lost-wake retry: the StopFailure hook stamped
    the wake-retry flag AFTER this watcher's last emission (that wake's turn died
    on a usage limit) and the retry spacing has elapsed. A flag older than the
    last emission is stale; no flag means no failed turn on record."""
    if flag_mtime is None:
        return False
    return flag_mtime > last_emit and now - last_emit >= retry_seconds


# How many consecutive failed queue reads between stderr lines (after the first),
# and how many before the watcher gives up. At the default 60s poll that is: a
# line at once, another every ~10 min while the outage lasts, and death after
# ~1h. Only the ceiling is an operator knob (KRAKEN_WATCH_MAX_FAILURES; 0 keeps
# the watcher alive through any outage, warnings included).
WATCH_WARN_EVERY = 10
WATCH_MAX_FAILURES = 60


def watch_failure_action(
    failures: int, warn_every: int, max_failures: int,
) -> str | None:
    """What a run of `failures` consecutive failed queue reads owes the operator:
    "die" once the ceiling is reached, "warn" on the first failure and every
    `warn_every` after it, None in between.

    A watcher whose reads fail is indistinguishable from an idle one — same
    silence, same live process — so a transport fault has to be said out loud,
    but spaced, or an outage would fill the log with one line per poll. Dying
    outranks warning: past the ceiling the watcher is not listening, and a dead
    process in the monitor is honest where a live deaf one is not."""
    if max_failures > 0 and failures >= max_failures:
        return "die"
    if failures <= 0:
        return None
    if failures == 1 or (warn_every > 0 and failures % warn_every == 0):
        return "warn"
    return None


def cmd_watch(args: argparse.Namespace) -> int:
    return watch(args.api, args.project)


def watch(api: Api, project: str, *,
          snapshot_reader: Callable[..., Any] | None = None) -> int:
    """The ambush loop. `snapshot_reader` defaults to the real queue snapshot and
    is injectable so the loop's own behaviour — the edge-triggered emit gate, the
    consecutive-failure warn/die ladder, the lost-wake retry — can be scripted
    without a queue behind it. `cmd_watch` is the argv-shaped entry point."""
    snapshot_reader = snapshot_reader or snapshot_state
    poll_seconds = int(os.environ.get("KRAKEN_WATCH_POLL_SECONDS", "60"))
    retry_seconds = int(os.environ.get("KRAKEN_WATCH_RETRY_SECONDS", "300"))
    max_failures = int(
        os.environ.get("KRAKEN_WATCH_MAX_FAILURES", str(WATCH_MAX_FAILURES))
    )

    # Same preflight as the drain, once, before the first poll: a watcher armed
    # on a project label the repo does not carry can never emit a wake, so it is
    # a silent no-op dressed as an ambush. A label read that merely FAILED is not
    # a refusal — the poll loop already rides out transport faults, so warn and
    # arm rather than killing the watcher over a startup blip.
    ok, message = Queue(api).verify_project(project)
    if ok is False:
        print(message, file=sys.stderr)
        return EXIT_UNKNOWN_PROJECT
    if ok is None:
        print(message + " — arming anyway", file=sys.stderr)

    prev = None
    # Start at "now": retries are owed only for wakes THIS watcher emitted, so
    # a stale flag from an earlier session never triggers one.
    last_emit = time.time()
    failures = 0
    while True:
        snapshot = snapshot_reader(api, project)
        if snapshot is None:
            # Fail loud. The read never landed, so `prev` stays untouched (the
            # edge-triggered gate is not corrupted by an outage) — but silence
            # here is exactly what an idle queue looks like, so say it.
            failures += 1
            action = watch_failure_action(failures, WATCH_WARN_EVERY, max_failures)
            if action == "die":
                print(
                    f"kraken-watch: giving up after {failures} consecutive "
                    f"failures reading the queue in {api.repo} — this watcher is "
                    f"not listening; check the token and the network",
                    file=sys.stderr, flush=True,
                )
                return EXIT_TRANSPORT
            if action == "warn":
                print(
                    f"kraken-watch: {failures} consecutive failure(s) reading "
                    f"the queue in {api.repo} — this worker may be offline or "
                    f"unauthenticated, and wakes no one while it is",
                    file=sys.stderr, flush=True,
                )
        else:
            failures = 0
            startable = [
                line for line in snapshot.split("\n") if line.endswith(":startable")
            ]
            count = len(startable)
            # Emit gate: a startable task exists AND either the queue changed or
            # a lost-wake retry is due. No blind re-emission timer.
            due = wake_retry_due(
                wake_retry_mtime(), last_emit, retry_seconds, time.time()
            )
            if count > 0 and (snapshot != prev or due):
                numbers = " ".join(
                    "#" + line.split(":", 1)[0] for line in startable
                )
                print(
                    f"kraken-queue: {count} startable task(s) "
                    f"in project:{project} ({numbers})",
                    flush=True,
                )
                last_emit = time.time()
            prev = snapshot
        time.sleep(poll_seconds)
