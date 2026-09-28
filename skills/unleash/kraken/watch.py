"""The zero-token ambush: poll the queue, wake on an edge.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable

from .contract import EXIT_TRANSPORT, EXIT_UNKNOWN_PROJECT, Epoch
from .transport import Api, TransportError
from .lease import state_dir, wake_retry_mtime
from .queue import PROJECT_CHECK_FAILED, Queue

# --- subcommand: watch -------------------------------------------------------

def snapshot_state(api: Api, project: str) -> str:
    """The queue snapshot list-startable emits in --snapshot mode, via the same
    Queue.candidates."""
    rows = Queue(api).candidates(project)
    return "\n".join(
        f"{c.number}:{c.state}" for c in sorted(rows, key=lambda c: c.number)
    )


def wake_retry_due(flag_mtime: float | None, last_emit: float,
                   retry_seconds: int, now: Epoch) -> bool:
    """Whether a lost-wake retry is owed: the StopFailure hook stamped its flag
    after our last emission (that turn died) and the spacing has elapsed."""
    if flag_mtime is None:
        return False
    return flag_mtime > last_emit and now - last_emit >= retry_seconds


# Consecutive failed reads between warnings, and before giving up (at a 60s
# poll: ~10 min, ~1 h). KRAKEN_WATCH_MAX_FAILURES=0 never gives up.
WATCH_WARN_EVERY = 10
WATCH_MAX_FAILURES = 60


def watch_failure_action(
    failures: int, warn_every: int, max_failures: int,
) -> str | None:
    """"die" at the ceiling, "warn" on the first failure and every
    `warn_every` after, else None. A failing watcher looks exactly like an idle
    one, so it must say so — spaced out, and a dead process beats a deaf one."""
    if max_failures > 0 and failures >= max_failures:
        return "die"
    if failures <= 0:
        return None
    if failures == 1 or (warn_every > 0 and failures % warn_every == 0):
        return "warn"
    return None


def wake_snapshot_path(worker: str) -> str:
    return os.path.join(state_dir(), f"watch-{worker}.json")


def load_wake_snapshot(worker: str, repo: str, project: str) -> str | None:
    """The snapshot this worker's previous `--exit-on-wake` watcher woke on, or
    None. Best-effort."""
    try:
        with open(wake_snapshot_path(worker), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("repo") != repo or record.get("project") != project:
        return None
    snapshot = record.get("snapshot")
    return snapshot if isinstance(snapshot, str) else None


def save_wake_snapshot(worker: str, repo: str, project: str,
                       snapshot: str) -> None:
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(wake_snapshot_path(worker), "w", encoding="utf-8") as fh:
            json.dump({"repo": repo, "project": project, "snapshot": snapshot}, fh)
            fh.write("\n")
    except OSError:
        pass


def cmd_watch(args: argparse.Namespace) -> int:
    if args.exit_on_wake and not args.worker:
        print("kraken-watch: --exit-on-wake needs --worker <worker-name>",
              file=sys.stderr)
        return 2
    return watch(args.api, args.project,
                 exit_on_wake_as=args.worker if args.exit_on_wake else None)


def watch(api: Api, project: str, *,
          snapshot_reader: Callable[..., Any] | None = None,
          exit_on_wake_as: str | None = None) -> int:
    """The ambush loop. `snapshot_reader` is injectable for tests.

    `exit_on_wake_as` (a worker name) exits after the first wake, for harnesses
    that notify only when a background command exits (Copilot CLI). The wake's
    snapshot is saved so the re-armed watcher keeps the same edge gate."""
    return Watcher(api, project, snapshot_reader=snapshot_reader,
                   exit_on_wake_as=exit_on_wake_as).run()


class Watcher:
    """One ambush: poll the queue, and wake on an edge — a startable task and
    either a changed queue or a lost wake owed a retry."""

    def __init__(self, api: Api, project: str, *,
                 snapshot_reader: Callable[..., Any] | None = None,
                 exit_on_wake_as: str | None = None):
        self.api = api
        self.project = project
        self.read_snapshot = snapshot_reader or snapshot_state
        self.exit_on_wake_as = exit_on_wake_as
        self.poll_seconds = int(os.environ.get("KRAKEN_WATCH_POLL_SECONDS", "60"))
        self.retry_seconds = int(os.environ.get("KRAKEN_WATCH_RETRY_SECONDS", "300"))
        self.max_failures = int(
            os.environ.get("KRAKEN_WATCH_MAX_FAILURES", str(WATCH_MAX_FAILURES)))
        self.prev: str | None = None
        self.failures = 0
        self.last_emit = 0.0

    def run(self) -> int:
        refused = self._preflight()
        if refused is not None:
            return refused
        if self.exit_on_wake_as:
            self.prev = load_wake_snapshot(self.exit_on_wake_as, self.api.repo,
                                           self.project)
        # Retries are owed only for wakes THIS watcher emitted.
        self.last_emit = time.time()
        while True:
            try:
                snapshot = self.read_snapshot(self.api, self.project)
            except TransportError:
                stop = self._read_failed()
            else:
                stop = self._observed(snapshot)
            if stop is not None:
                return stop
            time.sleep(self.poll_seconds)

    def _preflight(self) -> int | None:
        # The drain's project preflight; a failed label read only warns, since
        # the loop rides out transport faults anyway.
        try:
            check = Queue(self.api).check_project(self.project)
        except TransportError:
            print(PROJECT_CHECK_FAILED + " — arming anyway", file=sys.stderr)
            return None
        if not check.carried:
            print(check.refusal, file=sys.stderr)
            return EXIT_UNKNOWN_PROJECT
        return None

    def _read_failed(self) -> int | None:
        # `prev` stays untouched, so an outage cannot fake an edge.
        self.failures += 1
        action = watch_failure_action(self.failures, WATCH_WARN_EVERY,
                                      self.max_failures)
        if action == "die":
            print(f"kraken-watch: giving up after {self.failures} consecutive "
                  f"failures reading the queue in {self.api.repo} — this "
                  f"watcher is not listening; check the token and the network",
                  file=sys.stderr, flush=True)
            return EXIT_TRANSPORT
        if action == "warn":
            print(f"kraken-watch: {self.failures} consecutive failure(s) reading "
                  f"the queue in {self.api.repo} — this worker may be offline or "
                  f"unauthenticated, and wakes no one while it is",
                  file=sys.stderr, flush=True)
        return None

    def _observed(self, snapshot: str) -> int | None:
        self.failures = 0
        startable = [line for line in snapshot.split("\n")
                     if line.endswith(":startable")]
        count, prev = len(startable), self.prev
        due = wake_retry_due(wake_retry_mtime(), self.last_emit,
                             self.retry_seconds, time.time())
        self.prev = snapshot
        if not (count > 0 and (snapshot != prev or due)):
            return None
        numbers = " ".join("#" + line.split(":", 1)[0] for line in startable)
        print(f"kraken-queue: {count} startable task(s) "
              f"in project:{self.project} ({numbers})", flush=True)
        self.last_emit = time.time()
        if self.exit_on_wake_as:
            save_wake_snapshot(self.exit_on_wake_as, self.api.repo,
                               self.project, snapshot)
            return 0
        return None
