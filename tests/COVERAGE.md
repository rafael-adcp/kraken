# Conformance coverage of `PROTOCOL.md`

This is the clause-by-clause audit the project's spec-first process
([CONTRIBUTING.md](../CONTRIBUTING.md)) requires: every normative statement
(**MUST** / **MUST NOT** / **SHOULD** / **SHOULD NOT** / **RECOMMENDED**) in
[`PROTOCOL.md`](../PROTOCOL.md) is mapped to the test(s) that pin it, or marked
as a gap with a disposition. "The spec says it but no test pins it" is a
finding: the reference implementation passing is *not* evidence a third-party
implementation would.

Regenerate the clause list with:

```bash
grep -nE 'MUST|SHOULD|RECOMMENDED' PROTOCOL.md
```

## Legend

| Status | Meaning |
| --- | --- |
| ✅ **pinned** | A conformance case (`tests/t/`) or unit test (`tests/unit/`) fails if the behavior regresses. |
| 🧹 **lint** | Enforced deterministically by `scripts/lint-skills.sh` (`make lint`), not the conformance suite. |
| 🧠 **agent** | Worker *judgment* — exercised by the agent-behavior harness (`tests/agent/`, `make test-agent`), not the token-free conformance suite. |
| 🏗 **structural** | Guaranteed by construction: `kraken.py` has no code path that could violate it (e.g. no `close` subcommand, never reads assignees). |
| 📋 **operational** | A setup/deployment invariant about repositories or humans, outside any worker transition. |
| 🕳 **gap** | Mechanically pinnable but not yet pinned. Each has a follow-up issue. |

## §1 Actors and repositories

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L29 | Coordination repo **MUST** be private and **MUST NOT** hold work code | 📋 operational | Enforced at queue setup (`skills/init`); not a worker transition. |
| L35 | Assignees **MUST NOT** be used to arbitrate anything | 🏗 structural | Arbitration (`kraken.py` `arbitrate_winner`) reads only `claimed-by:` machine lines in the claim window; assignees are never fetched or consulted. Pinned indirectly by `tests/t/04`, `tests/t/05`, `tests/unit` (arbitration ignores everything but machine lines). |

## §2 Task shape

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L50 | **Goal** field **MUST** be present | 🧠 agent / 🧹 lint | Task template field presence: `scripts/lint-skills.sh`; worker restates goal as assumptions: `tests/agent/`. |
| L51 | **Acceptance MUST** be run for real; a task whose acceptance was not executed **MUST NOT** move to `awaiting-merge` | 🧠 agent | Worker judgment — `tests/agent/`. The conformance suite cannot observe whether real acceptance ran. |
| L54 | Every task **MUST** carry exactly one `project:<name>` label; a task without one is invisible to every worker | ✅ pinned (invisible) / 🧹 lint ("exactly one") | Missing-label invisibility: `tests/t/01` (issue 8, no project label, absent from both list and snapshot). Cross-project routing: `tests/t/01` (issue 3, `project:other`, excluded). "Exactly one" is a label-hygiene rule, not enforced by `kraken.py`. |

## §3 Labels: the state machine

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L75 | Colors and label descriptions are **SHOULD** | 🧹 lint | Label set drift: `scripts/lint-skills.sh`. |
| L76 | The label *names* are **MUST** | 🧹 lint + ✅ pinned | Name drift across files: `scripts/lint-skills.sh`. The exact names are also exercised throughout `tests/t/` (every `mk_issue`/`has_label` uses them). |
| L80–83 | Startable definition; a task **MUST** carry at most one held label | ✅ pinned | Startable/held classification: `tests/t/01`, `tests/t/12` (blocked-by), `tests/t/13` (watch gate). At-most-one-held is enforced by the claim guard: `tests/t/03`. |
| L99 | Workers **MUST NOT** close task issues | 🏗 structural | `kraken.py` exposes no close/cancel path; closing is out of the transition surface (§11). See also §11 authorization. |

## §4 Comments: machine lines and attribution

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L106–107 | Consumers **MUST** scan all comment lines in server order; producers **MUST** put each machine line on its own line | ✅ pinned | `tests/unit` `MachineLineParsingTests` (`test_multiline_comment_bodies_scan_per_line`, `test_prose_mentioning_claimed_by_midline_is_ignored`). |
| L113 | `heartbeat:` **MUST NOT** reset the claim window | ✅ pinned | `tests/t/08`; `tests/unit` `test_heartbeat_does_not_reset`. |
| L121 | Every worker-posted comment **MUST** open with the attribution disclaimer | ✅ pinned + 🧹 lint | Disclaimer asserted on claim `tests/t/02`, release `tests/t/06`, heartbeat `tests/t/08`; disclaimer shape cross-checked by `scripts/lint-skills.sh`. |

## §5 The claim algorithm

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L140 | Label filtering **SHOULD** be client-side for determinism | ✅ pinned | `tests/t/01` asserts exact client-side-filtered output; `tests/t/19` pins the O(1) call count that client-side filtering enables. |
| L144 | Guard: a held task **MUST** be skipped without writing anything | ✅ pinned | `tests/t/03` (exit 11, zero writes). |
| L150 | A loser **MUST** back off removing nothing | ✅ pinned | `tests/t/04` (the claim race: loser exits 10, removes nothing). |
| L157 | `claimed-by:` lines before the window start **MUST** be ignored | ✅ pinned | `tests/t/05`; `tests/unit` `test_released_resets_window`, `test_stale_claim_resets_window`, `test_needs_decision_resets_window`, `test_reset_after_claim_leaves_no_winner`. |
| L160 | `heartbeat:` **MUST NOT** reset the window | ✅ pinned | (same as §4 L113) `tests/t/08`, `tests/unit` `test_heartbeat_does_not_reset`. |
| L167 | A worker **MUST** work one task at a time; **MUST NOT** claim a second while holding a claim | 🕳 gap | `kraken.py claim` does not refuse a second claim while a `claim-<worker>.json` state file exists. State-file lifecycle is pinned (`tests/t/15`) but the one-at-a-time guard is not. Follow-up: rafael-adcp/personal-tasks#35 (gap **G1** below). |

## §6 Heartbeats and the reaper

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L173 | A worker holding `in-progress` **SHOULD** heartbeat at least every 2h | 🧠 agent | Cadence is worker behavior. The reaper *mechanism* that consumes heartbeats is pinned below. |
| §6 reaper | Staleness anchored to the worker's last machine line, 6h `MAX_HOURS`; operator comments do not reset the clock | ✅ pinned | `tests/t/17` extracts and runs `reclaim-stale.yml`'s shipped `run:` block verbatim. |
| §6 requeue | requeue-on-reply asymmetry (bare comment requeues `needs-decision`, not `awaiting-merge`); no-op on worker/bot/unheld | ✅ pinned | `tests/t/18` extracts and runs `requeue-on-reply.yml`'s shipped `run:` block verbatim. |

## §7 Escalation

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L210 | The worker **MUST** escalate rather than guess | 🧠 agent | Deciding *when* to escalate is judgment — `tests/agent/`. |
| L212 | The comment **MUST** land before the label swap | ✅ pinned | `tests/t/09`; `tests/t/11` (comment-fails → nothing changed, task stays held). |

## §8 Delivery

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| §8 delivery | Post `delivered:` + `pr:`, then swap `in-progress`→`awaiting-merge`; comment first | ✅ pinned | `tests/t/10`; `tests/t/11` (gh failure ordering); review-bounce window reset: `tests/t/10` and `tests/unit` `test_delivered_is_a_review_bounce_reset`. |
| L229 | Every delivered commit **MUST** carry the `Co-Authored-By` and `Kraken-Task:` trailers | 🕳 gap | No test verifies delivered commit trailers (the conformance stub has no git). Follow-up: rafael-adcp/personal-tasks#36 (gap **G2** below). |
| L236 | The PR body **SHOULD** carry `Closes …` when the work repo is on GitHub | 🧠 agent | PR authorship is agent behavior — `tests/agent/`. |
| L246 | Work **MUST NOT** be silently lost (fall back to the diff in a comment) | 🧠 agent | Fallback behavior is judgment — `tests/agent/`. |

## §9 Release

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L251–253 | A worker abandoning a claim **MUST** release honestly: post `released:` (window closes), **then** remove `in-progress` — comment first | ✅ pinned | `tests/t/06`; `tests/t/11` (ordering under gh failure); `tests/t/16` (SessionEnd auto-release runs `kraken.py release`). |

## §10 Close and cleanup

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L261 | The coordination repo **SHOULD** run the cleanup workflow; on close every label except `kraken-task` and `project:<name>` is stripped | ✅ pinned | `tests/t/21` extracts and runs `cleanup-closed.yml`'s shipped `run:` block verbatim. |

## §11 Authorization boundaries

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L277 | A conforming worker **MUST NOT** merge, push to default/protected branches, close task issues, deploy, delete, or publish | 🏗 structural + 🧠 agent | `kraken.py`'s subcommand surface contains none of these operations; refusing task-body instructions to do them is judgment — `tests/agent/`, `SECURITY.md`. |

## §12 Conformance

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L301 | Matching the exit-code contract (`0`/`10`/`11`/`20`) is **RECOMMENDED**; the wire contract is what conformance means | ✅ pinned | `0` success: `tests/t/02`; `10` lost tiebreaker: `tests/t/04`; `11` no longer clear: `tests/t/03`; `20` transport failure: `tests/t/07`, `tests/t/11`. |

## Open gaps (follow-up issues)

Two clauses are mechanically pinnable but need a harness larger than this PR
should introduce; each should be filed as its own task in the coordination
queue (`rafael-adcp/personal-tasks`, labels `kraken-task` + `project:kraken`)
and the follow-up number recorded here:

- **G1 — §5 L167, one-task-at-a-time** ([personal-tasks#35](https://github.com/rafael-adcp/personal-tasks/issues/35)). `kraken.py claim` should refuse (or
  warn and exit non-zero) when a `claim-<worker>.json` state file already marks
  an open claim. Needs a claim-guard test extending the `tests/t/15` state-file
  fixtures. Mind the §5 network-failure caveat ("or while a claim of its own is
  in an unknown state after a network failure — re-check first").
- **G2 — §8 L229, commit attribution trailers** ([personal-tasks#36](https://github.com/rafael-adcp/personal-tasks/issues/36)). No test asserts that delivered
  commits carry the `Co-Authored-By` and `Kraken-Task:` trailers. Needs a
  git-integration harness (a throwaway work repo) the conformance stub does not
  currently model.
