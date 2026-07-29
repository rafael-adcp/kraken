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
    lease_renew_seconds, lease_ttl_seconds, open_claim_record, write_claim_state
)
from .refs import Refs
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


class NextActionEnvelope:
    """Builds next-action's answers for ONE worker draining ONE repo.

    The repo, the worker and the script path are invariant for the whole call, so
    they live in the constructor rather than trailing every answer. And `answer`
    DERIVES the exit code from the action through `NEXT_ACTION_EXIT` instead of
    being told it: a hand-written code beside each envelope is a copy of a row of
    that table, which drifts rather than differs.

    `action` is the verdict, `reason` a stable machine slug for it, and `detail`
    the human sentence — so a consumer branches on the slug and a human reads
    the sentence, never the other way round."""

    def __init__(self, repo: Repo, worker: Worker, script: str | None = None):
        self.repo = repo
        self.worker = worker
        self.script = script

    def answer(self, action: str, **fields) -> tuple[int, Envelope]:
        """The envelope AND the exit code that carries the same verdict to a
        shell — one call, because they are one decision."""
        return (NEXT_ACTION_EXIT[action], self.build(action, **fields))

    def build(self, action: str, *,
              issue: Issue | None = None,
              resumed: bool | None = None,
              brief: Json | None = None,
              lease: Json | None = None,
              reason: str | None = None,
              detail: str | None = None,
              holding: Json | None = None) -> Envelope:
        """The one JSON shape next-action emits.

        `holding` is `{"repo", "issue"}` naming a claim this worker must resolve,
        and it exists because a `blocked` claim may live in a **different repo**
        than the one being drained: pairing the top-level `repo` with that
        claim's issue number would name a task that does not exist. So `blocked`
        reports the claim under `holding` and never as a bare top-level `issue`.

        `then` is attached wherever a write is actually legal: to `execute` for
        the task in hand, and to `blocked` for the claim that has to be resolved
        first — built against **that** claim's repo and issue. Emitting `blocked`
        without it would tell a worker to run a transition it has no way to
        construct."""
        env = {"action": action, "repo": self.repo, "worker": self.worker}
        if issue is not None:
            env["issue"] = int(issue)
        if resumed is not None:
            env["resumed"] = resumed
        if reason:
            env["reason"] = reason
        if detail:
            env["detail"] = detail
        if holding is not None:
            env["holding"] = {"repo": holding["repo"],
                              "issue": int(holding["issue"])}
        if brief is not None:
            env["brief"] = brief
        if lease is not None:
            env["lease"] = lease
        if action == "execute" and issue is not None:
            env["then"] = self._then(self.repo, issue)
        elif action == "blocked" and holding is not None:
            env["then"] = self._then(holding["repo"], int(holding["issue"]))
        return env

    def _then(self, repo: Repo, issue: Issue) -> dict[str, str]:
        return then_commands(repo, issue, self.worker, script=self.script)


def next_action_envelope(action: str, repo: Repo, worker: Worker, *,
                         script: str | None = None, **fields) -> Envelope:
    """The envelope as a single call, for a caller that has no context object in
    hand."""
    return NextActionEnvelope(repo, worker, script).build(action, **fields)


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


class NextAction:
    """One `next-action` call: resume the claim this worker already holds, or
    acquire the next task, and answer as an envelope.

    Resuming and acquiring share the same context — api, project, worker, ttl,
    now, script — and the same envelope factory built from it. That context is
    what this object is: it belongs to one call, not to each half of it."""

    def __init__(self, api: Api, project: str, worker: Worker, *,
                 ttl: int | None = None, now: Epoch | None = None,
                 script: str | None = None):
        self.api = api
        self.project = project
        self.worker = worker
        self.ttl = lease_ttl_seconds(ttl)
        self.now = time.time() if now is None else now
        self.envelope = NextActionEnvelope(api.repo, worker, script)

    def run(self) -> tuple[int, Envelope]:
        """Resume what is held, else acquire. Returns `(exit_code, envelope)`."""
        record = open_claim_record(self.worker)
        if record is not None:
            rc, env = self.resume(record)
            if env is not None:
                return (rc, env)
            # Resolved: fall through and take the next task.
        return self.acquire()

    # --- resuming a claim this worker already holds ---------------------------

    def resume(self, record: ClaimRecord) -> tuple[int | None, Envelope | None]:
        """Fetch what `resume_verdict` needs and turn its verdict into an
        envelope. Returns `(exit_code, envelope)`, or `(None, None)` when the
        recorded claim is resolved and the caller should acquire a new task."""
        verdict, detail, issue_obj = self._observe(record)
        issue = record["issue"]

        if verdict == "resolved":
            # The claim is over. Drop the stale scratch file so the lifecycle
            # hooks stop hinting at a claim that no longer exists, and let the
            # caller drain on — the self-heal that saves the operator a hand
            # `release`.
            clear_claim_state(self.worker)
            diag(f"next-action: claim resolved issue={issue} ({detail['reason']}) "
                 "— continuing the drain")
            return (None, None)

        if verdict == "abandon":
            # Provably not ours: clearing local state is safe and is what unjams
            # this worker. An AMBIGUOUS read never reaches here (it is "retry"),
            # so no transport fault can make us drop a claim we might still hold.
            clear_claim_state(self.worker)

        action = verdict  # the remaining verdicts are the action names themselves
        if action != "execute":
            return self._refuse(action, record, detail)
        return self._resumed(issue, detail, issue_obj)

    def _observe(self, record: ClaimRecord):
        """What the verdict is decided from: the lease and the issue, or neither
        when the record names a claim in a different repo."""
        if record["repo"] and record["repo"] != self.api.repo:
            # Nothing was read, and nothing needs to be: the repo mismatch
            # decides it.
            verdict, detail = resume_verdict(
                record, self.api.repo, self.worker, UNREADABLE_LEASE, None)
            return (verdict, detail, None)
        issue = record["issue"]
        head = Refs(self.api).head(issue)
        # One GET serves both the finished-task check and the brief.
        issue_obj = None if head.unknown else self.api.issue_detail(issue)
        verdict, detail = resume_verdict(
            record, self.api.repo, self.worker, head, issue_obj)
        return (verdict, detail, issue_obj)

    def _refuse(self, action: str, record: ClaimRecord,
                detail: Json) -> tuple[int, Envelope]:
        """Every non-execute resume verdict. `blocked` names the claim under
        `holding` (it may be in another repo, so a bare top-level issue would
        read against the wrong one) and carries the commands that resolve it. The
        other verdicts are about the task in this repo, and none of them makes a
        write legal."""
        issue = record["issue"]
        diag(f"next-action: {action} issue={issue} ({detail['reason']})")
        if action == "blocked":
            return self.envelope.answer(
                action, reason=detail["reason"], detail=detail.get("detail"),
                holding={"repo": record["repo"] or self.api.repo,
                         "issue": issue})
        return self.envelope.answer(
            action, issue=issue, reason=detail["reason"],
            detail=detail.get("detail"))

    def _resumed(self, issue: Issue, detail: Json,
                 issue_obj: Json) -> tuple[int, Envelope]:
        # Re-stamp the scratch file: a resume proves the claim is open, and the
        # lifecycle hooks read the file, so a hint lost with its machine is
        # restored the moment the claim is picked back up.
        write_claim_state(self.api.repo, issue, self.worker)
        epoch = detail["epoch"]
        # An unreadable clock is an expired lease (§5.1) — fail open, toward
        # renewing immediately, never toward assuming time is left.
        lease = lease_block(
            epoch if epoch is not None else self.now - self.ttl,
            self.now, self.ttl, generation=detail["generation"])
        diag(f"next-action: resumed issue={issue} worker={self.worker}")
        return self.envelope.answer(
            "execute", issue=issue, resumed=True,
            brief=task_brief(issue_obj.get("title") or "",
                             issue_obj.get("body") or ""),
            lease=lease)

    # --- acquiring the next task ---------------------------------------------

    def acquire(self) -> tuple[int, Envelope]:
        """Take the next startable task, and translate the claim loop's exit code
        into the verdict a driver reads."""
        rc, won = acquire_next(self.api, self.project, self.worker, ttl=self.ttl)
        if rc == EXIT_OK:
            return self.envelope.answer(
                "execute", issue=won["issue"], resumed=False,
                brief=task_brief(won["title"], won["body"]),
                lease=lease_block(self.now, self.now, self.ttl,
                                  source="estimated"))
        if rc == EXIT_NONE:
            return self.envelope.answer(
                "idle", reason="queue-empty",
                detail=f"nothing startable in project:{self.project}")
        if rc == EXIT_UNKNOWN_PROJECT:
            return self.envelope.answer(
                "stop", reason="unknown-project",
                detail=f"the repo carries no project:{self.project} label — this "
                       "worker would filter every task out; fix the label, never "
                       "drain unscoped")
        if rc == EXIT_NOT_CLEAR:
            return self._resume_discovered(won)
        return self.envelope.answer(
            "retry", reason="transport",
            detail="the queue read or a claim write did not land — re-check the "
                   "task's real state before retrying")

    def _resume_discovered(self, payload: Json | None) -> tuple[int, Envelope]:
        """The acquisition's §5 guard found a claim ref of ours the local record
        did not name — a scratch file lost with its machine, or a claim that
        landed between the resume check and the read. The ladder is the truth,
        so resume THAT task rather than report a dead end; `resume` re-proves
        the lease before anything is written."""
        held = (payload or {}).get("issue")
        if held is None:
            return self.envelope.answer(
                "retry", reason="claim-open",
                detail="the queue read says this worker holds a claim it could "
                       "not name — re-check before retrying")
        rc, env = self.resume({"repo": self.api.repo, "issue": str(held),
                               "worker": self.worker})
        if env is not None:
            return (rc, env)
        # Resolved between the guard and this read — the drain continues on the
        # next invocation rather than looping inside this one.
        return self.envelope.answer(
            "retry", reason="claim-just-resolved",
            detail=f"the claim on #{held} resolved between the guard and the "
                   "re-read — invoke next-action again")


def next_action(api: Api, project: str, worker: Worker,
                ttl: int | None = None, now: Epoch | None = None,
                script: str | None = None) -> tuple[int, Envelope]:
    """The driver loop's single call, as a call. Returns `(exit_code, envelope)`."""
    return NextAction(api, project, worker, ttl=ttl, now=now,
                      script=script).run()


def cmd_next_action(args: argparse.Namespace) -> int:
    with diagnostics_on_stderr():
        rc, env = next_action(args.api, args.project, args.worker)
    if args.text:
        render_next_action(env)
    else:
        print(json.dumps(env, indent=2))
    return rc
