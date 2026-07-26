"""The driver loop as one call: what a worker should do next, as an envelope.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import json
import os
import time

from .contract import (
    ClaimRecord, ENTRYPOINT, EXIT_LOST, EXIT_NONE, EXIT_NOT_CLEAR, EXIT_OK,
    EXIT_TRANSPORT, EXIT_UNKNOWN_PROJECT, Envelope, Epoch, Gen, Issue, Json,
    Repo, Worker, diag, diagnostics_on_stderr
)
from .transport import Api
from .lease import (
    Lease, UNREADABLE_LEASE, clear_claim_state, format_iso,
    lease_renew_seconds, lease_ttl_seconds, open_claim_record
)
from .refs import claim_ref_head
from .queue import is_empty_section, section_body
from .render import render_next_action
from .claim import acquire_next

# --- subcommand: next-action -------------------------------------------------
#
# The driver loop, as one call. Everything a worker otherwise has to REMEMBER
# between transitions — whether it already holds a task, whether that lease is
# still its own, when the lease must be renewed, which writes are legal next and
# with which arguments — is bookkeeping, and bookkeeping belongs in the program.
# What is left for the agent is the part only it can do: read the goal, write the
# code, write the question, write the result.
#
# This is a worker-side ergonomic (PROTOCOL.md §12), exactly like claim-next and
# status: no new wire semantics and no new write. The only writes it performs are
# the claim path's, reached through acquire_next.
#
# stdout is the JSON envelope and NOTHING else, so the whole computation runs
# with `diag` routed to stderr (see diagnostics_on_stderr).

# The action vocabulary, single-sourced here: `kraken.py contract next-actions`
# prints it and the skill lint checks the SKILL.md documents every one, the same
# execute-don't-diff rule the marker types follow.
NEXT_ACTIONS = ("execute", "idle", "abandon", "blocked", "stop", "retry")

# action -> the exit code that carries the same verdict to a shell. They reuse
# the established contract (§12) rather than inventing a second numbering: a
# caller that already branches on claim-next's codes needs to learn nothing.
NEXT_ACTION_EXIT = {
    "execute": EXIT_OK,
    "idle": EXIT_NONE,
    "abandon": EXIT_LOST,
    "blocked": EXIT_NOT_CLEAR,
    "stop": EXIT_UNKNOWN_PROJECT,
    "retry": EXIT_TRANSPORT,
}


def then_commands(repo: Repo, issue: Issue, worker: Worker,
                  script: str | None = None) -> dict[str, str]:
    """The exact command lines this worker may run next, fully interpolated —
    the entry point's absolute path, the repo, the issue and the worker name
    already filled in, with only the parts that are the agent's to write left as
    placeholders.

    This is the point of the envelope: assembling an argv is bookkeeping, and an
    agent that assembles one gets the path wrong, or the argument order, or the
    worker name. The path is quoted because a plugin folder may contain spaces.

    The default is `ENTRYPOINT`, not this file: the implementation lives in the
    package, but the thing a worker RUNS is still skills/unleash/kraken.py, and
    an envelope naming a module inside the package would not be executable."""
    script = script or ENTRYPOINT
    base = f'python3 "{script}"'
    return {
        "renew": f"{base} heartbeat {repo} {issue} {worker} '<one line of progress>'",
        "note": f"{base} note {repo} {issue} {worker} <body-file>",
        "escalate": f"{base} escalate {repo} {issue} {worker} <question-file>",
        "deliver": f"{base} deliver {repo} {issue} {worker} <result-file> <pr-url>",
        "release": f"{base} release {repo} {issue} {worker} '<reason>'",
    }


def lease_block(
    epoch: Epoch | None, now: Epoch, ttl: int,
    generation: Gen | None = None, source: str = "claim-ref",
) -> Json:
    """The renewal contract as numbers instead of as an instruction to remember:
    when this lease dies, how long is left, and how often to renew.

    `source` says where the clock came from. "claim-ref" is the authoritative
    one — the ref's server-stamped commit date, the same anchor every reader
    applies the TTL to (§5.1). "estimated" is the freshly-won claim, whose commit
    was created moments ago and is timed off the local clock rather than paying a
    re-read for it: renewal is TTL/3, so a local clock has three renewals of
    slack before drift could matter, and the authoritative clock still decides
    every actual expiry."""
    expires = epoch + ttl
    return {
        "generation": generation,
        "renew_every_seconds": lease_renew_seconds(ttl),
        "expires_at": format_iso(expires),
        "seconds_remaining": int(expires - now),
        "renew_now": expires <= now,
        "source": source,
    }


def task_brief(title: str, body: str) -> Json:
    """The task as the agent needs it: the issue-form sections split out, plus
    the raw body for a hand-written issue that carries no headings. An empty or
    `_No response_` section reads as "" rather than as the placeholder text."""
    def section(name):
        content = section_body(body, name)
        return "" if is_empty_section(content) else content.strip()

    return {
        "title": title,
        "goal": section("Goal"),
        "acceptance": section("Acceptance"),
        "notes": section("Notes"),
        "body": body,
    }


def next_action_envelope(action: str, repo: Repo, worker: Worker, *,
                         issue: Issue | None = None,
                         resumed: bool | None = None,
                         brief: Json | None = None,
                         lease: Json | None = None,
                         reason: str | None = None,
                         detail: str | None = None,
                         holding: Json | None = None,
                         script: str | None = None) -> Envelope:
    """The one JSON shape next-action emits. `action` is the verdict, `reason` a
    stable machine slug for it, and `detail` the human sentence — so a consumer
    branches on the slug and a human reads the sentence, never the other way
    round.

    `holding` is `{"repo", "issue"}` naming a claim this worker must resolve, and
    it exists because a `blocked` claim may live in a **different repo** than the
    one being drained: pairing the top-level `repo` with that claim's issue
    number would name a task that does not exist. So `blocked` reports the claim
    under `holding` and never as a bare top-level `issue`.

    `then` is attached wherever a write is actually legal: to `execute` for the
    task in hand, and to `blocked` for the claim that has to be resolved first —
    built against **that** claim's repo and issue. Emitting `blocked` without it
    would tell a worker to run a transition it has no way to construct."""
    env = {"action": action, "repo": repo, "worker": worker}
    if issue is not None:
        env["issue"] = int(issue)
    if resumed is not None:
        env["resumed"] = resumed
    if reason:
        env["reason"] = reason
    if detail:
        env["detail"] = detail
    if holding is not None:
        env["holding"] = {"repo": holding["repo"], "issue": int(holding["issue"])}
    if brief is not None:
        env["brief"] = brief
    if lease is not None:
        env["lease"] = lease
    if action == "execute" and issue is not None:
        env["then"] = then_commands(repo, issue, worker, script=script)
    elif action == "blocked" and holding is not None:
        env["then"] = then_commands(holding["repo"], int(holding["issue"]),
                                    worker, script=script)
    return env


def issue_is_finished(issue_obj: Json | None) -> bool:
    """Whether an issue has left the state where "keep executing" makes sense:
    closed, or carrying a terminal held label. A worker whose state file still
    names such a task already finished with it — the transition landed and only
    the local scratch file lagged."""
    if str((issue_obj or {}).get("state", "")).upper() == "CLOSED":
        return True
    names = {lbl.get("name", "") for lbl in (issue_obj or {}).get("labels", [])}
    return bool(names & {"needs-decision", "awaiting-merge"})


def resume_verdict(
    record: ClaimRecord, repo: Repo, worker: Worker, head: Lease,
    issue_obj: Json | None,
) -> tuple[str, Json]:
    """Decide what a recorded open claim is worth, as a pure function of what was
    observed. Returns `(verdict, detail)` where verdict is one of:

      - `"blocked"`  — the record names a claim in a DIFFERENT repo. Decided
        before any read, so the caller passes `UNREADABLE_LEASE` and no issue —
        nothing was observed; one task at a time is repo-independent (§5).
      - `"retry"`    — a read did not land. Ambiguous is never a decision:
        write nothing, change nothing, re-check.
      - `"resolved"` — this worker no longer holds the task AND the task has
        already left the queue (closed, or `needs-decision`/`awaiting-merge`).
        The transition landed and the state file lagged, so the drain simply
        continues to the next task.
      - `"abandon"`  — this worker no longer holds the lease on a task that is
        still open and unfinished: it was stolen, or the ref is gone. Write
        NOTHING (§5.3) and say so.
      - `"execute"`  — the lease is still ours. Resume.

    Ownership is checked before the issue's own state, because the lease is the
    lock and the labels are projection. A lease that is ours but PAST its TTL
    still resumes: §5.3 is explicit that an expired-but-unstolen lease may
    complete its transition — the caller is told to renew first. That is why no
    clock reaches this function: the verdict is about WHO holds the lease, never
    how old it is, so there is no `now`/`ttl` here to get backwards."""
    if record["repo"] and record["repo"] != repo:
        return ("blocked", {"reason": "claim-elsewhere",
                            "detail": f"this worker holds {record['repo']}"
                                      f"#{record['issue']}; resolve it (deliver / "
                                      f"escalate / release) before draining {repo}"})
    if head.unknown:
        return ("retry", {"reason": "lease-unreadable",
                          "detail": "the claim ref did not read — re-check "
                                    "before writing anything"})
    if issue_obj is None:
        return ("retry", {"reason": "issue-unreadable",
                          "detail": "the task issue did not read — re-check "
                                    "before writing anything"})

    if not head.held_by(worker):
        if issue_is_finished(issue_obj):
            # Whether this worker's own terminal transition deleted the ref or
            # the reconciler reclaimed it is not answerable from here — and the
            # right move is the same either way: the task is not ours and not in
            # the queue, so keep draining rather than report a loss.
            return ("resolved", {"reason": "already-resolved"})
        if not head.present:
            return ("abandon", {"reason": "lease-gone",
                                "detail": "the claim ref is gone — re-claim the "
                                          "task before writing to it"})
        holder = head.worker or "another worker"
        return ("abandon", {"reason": "lease-stolen",
                            "detail": f"the lease is held by {holder} now — the "
                                      f"branch and PR you pushed stay, and "
                                      f"whoever holds the task inherits them"})
    if issue_is_finished(issue_obj):
        # Our lease, but the task ended under us (an operator closed or answered
        # it). Nothing to resume; the lease frees itself within one TTL.
        return ("resolved", {"reason": "task-finished"})

    # `held_by` above proves the ladder is there, so these need no guard.
    return ("execute", {"generation": head.gen, "epoch": head.epoch})


def _resume(
    record: ClaimRecord, api: Api, worker: Worker, ttl: int, now: Epoch,
    script: str | None,
) -> tuple[int | None, Envelope | None]:
    """Fetch what `resume_verdict` needs and turn its verdict into an envelope.
    Returns `(exit_code, envelope)`, or `(None, None)` when the recorded claim is
    resolved and the caller should go acquire a new task."""
    issue = record["issue"]
    issue_obj = None

    if record["repo"] and record["repo"] != api.repo:
        # Nothing was read, and nothing needs to be: the repo mismatch decides it.
        verdict, detail = resume_verdict(record, api.repo, worker,
                                         UNREADABLE_LEASE, None)
    else:
        head = claim_ref_head(api, issue)
        # One GET serves both the finished-task check and the brief.
        issue_obj = None if head.unknown else api.issue_detail(issue)
        verdict, detail = resume_verdict(record, api.repo, worker, head, issue_obj)

    if verdict == "resolved":
        # The claim is over. Drop the stale scratch file so the one-task-at-a-time
        # guard stops refusing on it, and let the caller drain on. This is the
        # self-heal that used to need an operator running `release` by hand.
        clear_claim_state(worker)
        diag(f"next-action: claim resolved issue={issue} ({detail['reason']}) "
             "— continuing the drain")
        return (None, None)

    if verdict == "abandon":
        # Provably not ours: clearing local state is safe and is what unjams this
        # worker. An AMBIGUOUS read never reaches here (it is "retry"), so no
        # transport fault can make us drop a claim we might still hold.
        clear_claim_state(worker)

    action = verdict  # the remaining verdicts are the action names themselves
    if action != "execute":
        diag(f"next-action: {action} issue={issue} ({detail['reason']})")
        # `blocked` names the claim under `holding` (it may be in another repo,
        # so a bare top-level issue would read against the wrong one) and carries
        # the commands that resolve it. The other verdicts are about the task in
        # this repo, and none of them makes a write legal.
        if action == "blocked":
            return (NEXT_ACTION_EXIT[action], next_action_envelope(
                action, api.repo, worker, reason=detail["reason"],
                detail=detail.get("detail"), script=script,
                holding={"repo": record["repo"] or api.repo, "issue": issue}))
        return (NEXT_ACTION_EXIT[action], next_action_envelope(
            action, api.repo, worker, issue=issue, reason=detail["reason"],
            detail=detail.get("detail"), script=script))

    epoch = detail["epoch"]
    # An unreadable clock is an expired lease (§5.1) — fail open, toward renewing
    # immediately, never toward assuming time is left.
    lease = lease_block(epoch if epoch is not None else now - ttl, now, ttl,
                        generation=detail["generation"])
    diag(f"next-action: resumed issue={issue} worker={worker}")
    return (EXIT_OK, next_action_envelope(
        "execute", api.repo, worker, issue=issue, resumed=True,
        brief=task_brief(issue_obj.get("title") or "", issue_obj.get("body") or ""),
        lease=lease, script=script))


def next_action(api: Api, project: str, worker: Worker,
                ttl: int | None = None, now: Epoch | None = None,
                script: str | None = None) -> tuple[int, Envelope]:
    """The driver loop's single call: resume the task this worker already holds,
    or acquire the next one. Returns `(exit_code, envelope)`."""
    ttl = lease_ttl_seconds(ttl)
    now = time.time() if now is None else now

    record = open_claim_record(worker)
    if record is not None:
        rc, env = _resume(record, api, worker, ttl, now, script)
        if env is not None:
            return (rc, env)
        # Resolved: fall through and take the next task.

    rc, won = acquire_next(api, project, worker, ttl=ttl)
    if rc == EXIT_OK:
        return (EXIT_OK, next_action_envelope(
            "execute", api.repo, worker, issue=won["issue"], resumed=False,
            brief=task_brief(won["title"], won["body"]),
            lease=lease_block(now, now, ttl, source="estimated"),
            script=script))
    if rc == EXIT_NONE:
        return (EXIT_NONE, next_action_envelope(
            "idle", api.repo, worker, reason="queue-empty",
            detail=f"nothing startable in project:{project}",
            script=script))
    if rc == EXIT_UNKNOWN_PROJECT:
        return (EXIT_UNKNOWN_PROJECT, next_action_envelope(
            "stop", api.repo, worker, reason="unknown-project",
            detail=f"the repo carries no project:{project} label — this worker "
                   "would filter every task out; fix the label, never drain "
                   "unscoped",
            script=script))
    if rc == EXIT_NOT_CLEAR:
        # A claim appeared between the resume check and the acquisition. Re-read
        # the record so this envelope, like the other `blocked`, names the claim
        # to resolve and carries the commands that resolve it.
        late = open_claim_record(worker)
        return (EXIT_NOT_CLEAR, next_action_envelope(
            "blocked", api.repo, worker, reason="claim-open",
            detail="this worker already holds an open claim — resolve it "
                   "(deliver / escalate / release) first",
            holding=({"repo": late["repo"] or api.repo, "issue": late["issue"]}
                     if late else None),
            script=script))
    return (EXIT_TRANSPORT, next_action_envelope(
        "retry", api.repo, worker, reason="transport",
        detail="the queue read or a claim write did not land — re-check the "
               "task's real state before retrying",
        script=script))


def cmd_next_action(args: argparse.Namespace) -> int:
    with diagnostics_on_stderr():
        rc, env = next_action(args.api, args.project, args.worker)
    if args.text:
        render_next_action(env)
    else:
        print(json.dumps(env, indent=2))
    return rc
