"""The driver loop as one call: what a worker should do next, as an envelope.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import argparse
import json

from .contract import (
    ClaimRecord, CommentRecord, ENTRYPOINT, EXIT_LOST, EXIT_NONE,
    EXIT_NOT_CLEAR, EXIT_OK, EXIT_TRANSPORT, EXIT_UNKNOWN_PROJECT, Envelope,
    Epoch, Gen, Issue, Json, Repo, Worker, diag, diagnostics_on_stderr
)
from .comments import parse_marker
from .transport import Api, comment_total_of
from .lease import (
    Lease, UNREADABLE_LEASE, clear_claim_state, format_iso,
    lease_renew_seconds, lease_ttl_seconds, open_claim_record, write_claim_state
)
from .refs import Refs
from .state import NO_RECORD, States, TaskState
from .queue import section_text
from .render import render_next_action
from .claim import AlreadyHolding, Claimed, acquire_next, claim_is_moot

# --- subcommand: next-action -------------------------------------------------
# The driver loop as one call (§12): the bookkeeping a worker would otherwise
# have to remember, done by the program. It writes nothing the claim path does
# not, and its stdout is the JSON envelope alone.

# Single-sourced: `contract next-actions` prints it and the lint checks SKILL.md.
NEXT_ACTIONS = ("execute", "idle", "abandon", "blocked", "stop", "retry")

# action -> the exit code carrying the same verdict, reusing claim-next's codes.
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
    """The command lines this worker may run next, fully interpolated so the
    agent only writes the placeholders. The path is quoted: a plugin folder may
    contain spaces."""
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
    """The renewal contract as numbers. `source` is "claim-ref" (the server's
    commit date, §5.1) or "estimated" for a claim won moments ago, which skips
    a re-read — renewal every TTL/3 leaves ample slack for drift."""
    expires = epoch + ttl
    return {
        "generation": generation,
        "renew_every_seconds": lease_renew_seconds(ttl),
        "expires_at": format_iso(expires),
        "seconds_remaining": int(expires - now),
        "renew_now": expires <= now,
        "source": source,
    }


def feedback_since(api: Api, issue: Issue, anchor: int,
                   ) -> list[CommentRecord] | None:
    """The human comments past `anchor` — what a bounce is about — or None on
    transport failure.

    Cut first, filter second: the anchor is a position in the whole thread, so
    removing worker comments (they carry a marker, §4) before the cut would
    slide it. A thread that shrank below its anchor yields [], never a slice
    from the end."""
    records = api.comment_records(issue)
    if records is None:
        return None
    return [c for c in records[max(0, int(anchor)):]
            if parse_marker(c.get("body") or "") is None]


def task_brief(title: str, body: str) -> Json:
    """The issue-form sections split out, plus the raw body for a hand-written
    issue with no headings."""
    return {
        "title": title,
        "goal": section_text(body, "Goal"),
        "acceptance": section_text(body, "Acceptance"),
        "notes": section_text(body, "Notes"),
        "body": body,
    }


class NextActionEnvelope:
    """Builds next-action's answers for one worker draining one repo; the exit
    code is derived from the action, never written beside it."""

    def __init__(self, repo: Repo, worker: Worker, script: str | None = None):
        self.repo = repo
        self.worker = worker
        self.script = script

    def answer(self, action: str, **fields) -> tuple[int, Envelope]:
        """(exit code, envelope): one decision."""
        return (NEXT_ACTION_EXIT[action], self.build(action, **fields))

    def build(self, action: str, *,
              issue: Issue | None = None,
              resumed: bool | None = None,
              bounced: bool | None = None,
              pr: str | None = None,
              feedback: list[CommentRecord] | None = None,
              brief: Json | None = None,
              lease: Json | None = None,
              reason: str | None = None,
              detail: str | None = None,
              holding: Json | None = None) -> Envelope:
        """The one JSON shape next-action emits (fields: `contract.Envelope`).

        Absences carry meaning: `bounced` rides every execute, false included;
        `feedback` is `[]` when the read found nothing and absent when the read
        failed. `holding` exists because a blocking claim may be in another
        repo, so its `then` is built against that repo."""
        env = {"action": action, "repo": self.repo, "worker": self.worker}
        if issue is not None:
            env["issue"] = int(issue)
        if resumed is not None:
            env["resumed"] = resumed
        if bounced is not None:
            env["bounced"] = bool(bounced)
        if pr:
            env["pr"] = pr
        if feedback is not None:
            env["feedback"] = list(feedback)
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
    """The envelope as a single call."""
    return NextActionEnvelope(repo, worker, script).build(action, **fields)


def issue_is_finished(issue_obj: Json | None, record: TaskState = NO_RECORD,
                      ) -> bool:
    """Closed, or in a held state per its record (§3.1) — not per its badge,
    which still says `awaiting-merge` on a task an operator just requeued."""
    if str((issue_obj or {}).get("state", "")).upper() == "CLOSED":
        return True
    names = {lbl.get("name", "") for lbl in (issue_obj or {}).get("labels", [])}
    return claim_is_moot(record, comment_total_of(issue_obj or {}), names)


def resume_verdict(
    record: ClaimRecord, repo: Repo, worker: Worker, head: Lease,
    issue_obj: Json | None, state: TaskState = NO_RECORD,
) -> tuple[str, Json]:
    """What a recorded open claim is worth, as a pure function of what was
    observed: `(verdict, detail)`, verdict being "blocked" (claim in another
    repo), "retry" (a read did not land), "resolved" (the task already left),
    "abandon" (the lease is not ours: write nothing, §5.3) or "execute".

    No clock reaches this: an expired lease that is still ours resumes (§5.3),
    so the question is who holds it, never how old it is."""
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
        if issue_is_finished(issue_obj, state):
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
    if issue_is_finished(issue_obj, state):
        # Our lease, but the task ended under us (an operator closed or answered
        # it). Nothing to resume; the lease frees itself within one TTL.
        return ("resolved", {"reason": "task-finished"})

    # `held_by` above proves the ladder is there, so these need no guard.
    return ("execute", {"generation": head.gen, "epoch": head.epoch})


class NextAction:
    """One `next-action` call: resume the claim this worker holds, or acquire
    the next task, and answer as an envelope."""

    def __init__(self, api: Api, project: str, worker: Worker, *,
                 ttl: int | None = None, now: Epoch | None = None,
                 script: str | None = None):
        self.api = api
        self.project = project
        self.worker = worker
        self.ttl = lease_ttl_seconds(ttl)
        self._now = now
        self.envelope = NextActionEnvelope(api.repo, worker, script)

    @property
    def now(self) -> Epoch:
        """The server's clock (§5.1), resolved at use: it is only known once a
        response has been seen."""
        return self.api.server_now() if self._now is None else self._now

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
        verdict, detail, issue_obj, state = self._observe(record)
        issue = record["issue"]

        if verdict == "resolved":
            # The claim is over: drop the stale scratch file and drain on.
            clear_claim_state(self.worker)
            diag(f"next-action: claim resolved issue={issue} ({detail['reason']}) "
                 "— continuing the drain")
            return (None, None)

        if verdict == "abandon":
            # Provably not ours (an ambiguous read is "retry", never this).
            clear_claim_state(self.worker)

        action = verdict  # the remaining verdicts are the action names themselves
        if action != "execute":
            return self._refuse(action, record, detail)
        return self._resumed(issue, detail, issue_obj, state)

    def _observe(self, record: ClaimRecord):
        """The verdict plus what it was decided from — the issue and the state
        record, which the resumed envelope reuses for `bounced` and `pr`."""
        if record["repo"] and record["repo"] != self.api.repo:
            verdict, detail = resume_verdict(
                record, self.api.repo, self.worker, UNREADABLE_LEASE, None)
            return (verdict, detail, None, NO_RECORD)
        issue = record["issue"]
        head = Refs(self.api).head(issue)
        # One GET serves the finished-task check, the comment anchor and the brief.
        issue_obj = None if head.unknown else self.api.issue_detail(issue)
        state = NO_RECORD if issue_obj is None else States(self.api).of(issue)
        if state.unknown:
            issue_obj = None  # ambiguous is never a decision: retry
        verdict, detail = resume_verdict(
            record, self.api.repo, self.worker, head, issue_obj, state)
        return (verdict, detail, issue_obj, state)

    def _refuse(self, action: str, record: ClaimRecord,
                detail: Json) -> tuple[int, Envelope]:
        """Every non-execute resume verdict."""
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

    def _resumed(self, issue: Issue, detail: Json, issue_obj: Json,
                 state: TaskState) -> tuple[int, Envelope]:
        # Re-stamp the scratch file the hooks read, in case it was lost.
        write_claim_state(self.api.repo, issue, self.worker)
        epoch = detail["epoch"]
        # An unreadable clock reads as expired (§5.1): renew now.
        lease = lease_block(
            epoch if epoch is not None else self.now - self.ttl,
            self.now, self.ttl, generation=detail["generation"])
        diag(f"next-action: resumed issue={issue} worker={self.worker}")
        # A claim writes no record (§3.1), so a resume reports the same bounce
        # and the same feedback its acquisition did.
        bounced = state.requeued(comment_total_of(issue_obj))
        return self.envelope.answer(
            "execute", issue=issue, resumed=True,
            bounced=bounced, pr=state.pr,
            feedback=self._feedback(issue, bounced, state.comments),
            brief=task_brief(issue_obj.get("title") or "",
                             issue_obj.get("body") or ""),
            lease=lease)

    def _feedback(self, issue: Issue, bounced: bool,
                  anchor: int) -> list[CommentRecord] | None:
        """The comments a bounce is about; only read when `bounced`, so a fresh
        task pays nothing."""
        if not bounced:
            return None
        return feedback_since(self.api, issue, anchor)

    # --- acquiring the next task ---------------------------------------------

    def acquire(self) -> tuple[int, Envelope]:
        """Take the next startable task, and translate the claim loop's result
        into the verdict a driver reads."""
        got = acquire_next(self.api, self.project, self.worker, ttl=self.ttl)
        if isinstance(got, Claimed):
            return self.envelope.answer(
                "execute", issue=got.issue, resumed=False,
                bounced=got.bounced, pr=got.pr,
                feedback=self._feedback(got.issue, got.bounced, got.anchor),
                brief=task_brief(got.title, got.body),
                lease=lease_block(self.now, self.now, self.ttl,
                                  source="estimated"))
        if isinstance(got, AlreadyHolding):
            return self._resume_discovered(got.issue)
        if got.exit_code == EXIT_NONE:
            return self.envelope.answer(
                "idle", reason="queue-empty",
                detail=f"nothing startable in project:{self.project}")
        if got.exit_code == EXIT_UNKNOWN_PROJECT:
            return self.envelope.answer(
                "stop", reason="unknown-project",
                detail=f"the repo carries no project:{self.project} label — this "
                       "worker would filter every task out; fix the label, never "
                       "drain unscoped")
        return self.envelope.answer(
            "retry", reason="transport",
            detail="the queue read or a claim write did not land — re-check the "
                   "task's real state before retrying")

    def _resume_discovered(self, held: Issue) -> tuple[int, Envelope]:
        """The §5 guard found a claim ref of ours the local scratch file did not
        name (lost with its machine, or landed after the resume check). The
        ladder is the truth, so resume that task; `resume` re-proves the lease
        before anything is written."""
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
    """`(exit_code, envelope)` for one next-action call."""
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
