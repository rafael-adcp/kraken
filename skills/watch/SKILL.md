---
name: watch
description: Arm an event-driven watcher on the task queue in my coordination repo — a background shell poll (zero tokens while idle) that wakes this worker only when a startable task appears, then drains it with the unleash protocol. The always-on driver that replaces /loop timer polling.
---

# Kraken — the lurking tentacle

You are a tentacle in ambush: instead of draining the queue once (`unleash`) or
being relaunched blind on a timer (`/loop`, one full LLM turn per fire even when
the queue is empty), you arm a **background shell watcher** that polls the queue
for free and wakes you **only when there is something to do**. Idle = zero
tokens; a hit = one ordinary drain.

## Invocation

```
/kraken:watch OWNER/tasks --worker-name <alias> --project <name>
```

**All three arguments are REQUIRED** — the same three as `/kraken:unleash`,
because every wake-up runs that protocol with exactly these arguments. If any
is missing, do not start — ask for it. If the `OWNER/tasks` slug matches `^OWNER/`
or contains `<`/`>`, refuse: it looks like the template placeholder — substitute
your real `owner/repo` and re-run.

## Protocol

1. **Arm the watcher** with the **Monitor tool** — `persistent: true`, running
   this skill's bundled `watch-queue.sh` (it lives in the same folder as this
   SKILL.md; if the Monitor tool is not in your tool list, load it first —
   some harnesses defer tool schemas):

   ```
   Monitor(
     command:     bash "<this skill's folder>/watch-queue.sh" OWNER/tasks <name>
     description: kraken queue: project:<name> for <alias>
     persistent:  true
   )
   ```

   Pass `<name>` bare — the script prepends the `project:` prefix itself. The
   script polls every 60s with a free `gh` call and prints one `kraken-queue:`
   line only when the queue snapshot changes and at least one task is startable
   (same label filter as `unleash` uses to list candidates). Do not inline a
   rewritten script — the bundled one is versioned with the plugin. If you
   cannot arm it (no Monitor tool, script missing), say so and offer
   `/loop /kraken:unleash ...` as the fallback; do not improvise a watcher.

2. **Go quiet.** Confirm what was armed (repo, project, worker name, poll
   cadence) and end your turn. Do not run a drain now and do not poll the
   queue yourself — tasks already waiting will trigger the first event within
   one poll (≤60s). From here on, the watcher does the watching.

3. **On each `kraken-queue:` event**, run the full `unleash` protocol with the
   same OWNER/tasks, `--worker-name`, `--project`: invoke the `kraken:unleash`
   skill (its protocol already in context from a previous wake? follow it
   directly instead of re-invoking). Drain until no startable task remains,
   then end the turn — the queue went quiet, so do you.

4. **A wake can be a false alarm.** The shell filter sees labels, not
   blocked-by relationships, so a task whose blockers are still open looks
   startable to the script; the `unleash` dependency check will skip it.
   Report briefly ("woke for #N, still blocked by #M") and end the turn. The
   watcher will not spam you — it re-emits an unchanged-but-startable queue
   only every 30 minutes, as a safety net for blockers it cannot see closing.

5. **Stopping.** I say stop → stop the monitor (TaskStop) and confirm. Either
   way the watcher dies with the session — it never outlives this terminal.

## Authorization boundaries

- Arming the watcher carries the same durable authorization as `unleash` —
  each wake-up IS an `unleash` run, bound by all of that skill's boundaries
  (deliver on work branches + draft PRs; never merge, never push to default).
- The watcher script itself is read-only over the queue: one `gh issue list`
  per minute, no writes, no state outside its own shell loop.

Coordination repo / flags / extra context: $ARGUMENTS
