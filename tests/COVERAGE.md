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
| ✅ **pinned** | A conformance case (`tests/conformance/`) or unit test (`tests/unit/`) fails if the behavior regresses. |
| 🧹 **lint** | Enforced deterministically by `scripts/lint-skills.sh` (`make lint`), not the conformance suite. |
| 🧠 **agent** | Worker *judgment* — exercised by the agent-behavior harness (`tests/agent/`, `make test-agent`), not the token-free conformance suite. |
| 🏗 **structural** | Guaranteed by construction: `kraken.py` has no code path that could violate it (e.g. no `close` subcommand, never reads assignees). |
| 📋 **operational** | A setup/deployment invariant about repositories or humans, outside any worker transition. |
| 🕳 **gap** | Mechanically pinnable but not yet pinned. Each has a follow-up issue. |

## §1 Actors and repositories

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L52 | Coordination repo **MUST** be private and **MUST NOT** hold work code | 📋 operational + ✅ pinned | `kraken.py init` creates the repo **private** (never public); `tests/conformance/test_26_init.py` asserts the `repo create … --private` bootstrap and the private-only contract. |
| L58 | Assignees **MUST NOT** be used to arbitrate anything | 🏗 structural | The claim is decided by the git-ref CAS (§5) — `kraken.py` never fetches or consults assignees. Pinned indirectly by `tests/conformance/test_04_claim_race.py` (the CAS race), `tests/conformance/test_05_claim_thread_independence.py`, and `tests/unit` `RefCasTests`. |

## §2 Task shape

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L74 | **Goal** field **MUST** be present | 🧠 agent / 🧹 lint | Task template field presence: `scripts/lint-skills.sh`; worker restates goal as assumptions: `tests/agent/`. |
| L75 | **Acceptance MUST** be run for real; a task whose acceptance was not executed **MUST NOT** move to `awaiting-merge` | 🧠 agent | Worker judgment — `tests/agent/`. The conformance suite cannot observe whether real acceptance ran. |
| L78 | Every task **MUST** carry exactly one `project:<name>` label; a task without one is invisible to every worker | ✅ pinned (invisible) / 🧹 lint ("exactly one") | Missing-label invisibility: `tests/conformance/test_01_list_startable.py` (issue 8, no project label, absent from both list and snapshot). Cross-project routing: `tests/conformance/test_01_list_startable.py` (issue 3, `project:other`, excluded). "Exactly one" is a label-hygiene rule, not enforced by `kraken.py`. |
| §2.1 validator | Queue-entry gate flags a missing `project:<name>` label or an empty/absent Goal or Acceptance section with one actionable comment; compliant task and non-`kraken-task` issue are no-ops; idempotent (no duplicate on re-flag) | ✅ pinned | `tests/conformance/test_25_validate_task.py` drives `kraken.py validate` — the subcommand `validate-task.yml` now execs (issue #37) — against the stub (missing label, missing Acceptance, heading-less body, compliant, non-task, debounce, edit-after-fix); `tests/unit/test_workflow_commands.py` unit-tests the section parse + debounce. |

## §3 Labels: the state machine

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L120 | Colors and label descriptions are **SHOULD** | 🧹 lint | Label set drift: `scripts/lint-skills.sh`. |
| L121 | The label *names* are **MUST** | 🧹 lint + ✅ pinned | Name drift across files: `scripts/lint-skills.sh`. The exact names are also exercised throughout `tests/conformance/` (every `mk_issue`/`has_label` uses them). |
| L125 | Startable definition; a task **MUST** carry at most one held label | ✅ pinned | Startable/held classification: `tests/conformance/test_01_list_startable.py`, `tests/conformance/test_12_list_startable_blocked.py` (blocked-by), `tests/conformance/test_13_watch_queue_blocked.py` (watch gate). At-most-one-held is enforced by the claim guard: `tests/conformance/test_03_claim_held.py`. |
| L144 | Workers **MUST NOT** close task issues | 🏗 structural | `kraken.py` exposes no close/cancel path; closing is out of the transition surface (§11). See also §11 authorization. |

## §4 The machine marker and attribution

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L165–166 | The marker JSON **MUST** be a single line carrying a string `type`, encoded with a real JSON serializer | ✅ pinned + 🧹 lint | `tests/unit` `MarkerTests` (`test_make_marker_is_compact_ascii_json`, `test_make_marker_round_trips_through_parse`); marker byte-form pinned in conformance by `assert_marker` in `tests/conformance/test_02_claim_clear.py`, `tests/conformance/test_24_marker_and_free_text.py`. Serializer-not-interpolation cross-checked by `scripts/lint-skills.sh`. |
| L170–172 | A malformed marker (undecodable JSON, no string `type`) **MUST** be ignored, never guessed | ✅ pinned | `tests/unit` `MarkerTests` (`test_parse_marker_rejects_undecodable_json`, `test_parse_marker_rejects_a_payload_without_a_string_type`, `test_parse_marker_tolerates_surrounding_prose`, `test_parse_marker_tolerates_a_trailing_cr`). |
| L186–191 | Markers are the only machine state; free text is never parsed (a line beginning with a former keyword can never occupy a machine-line position) | ✅ pinned + 🧹 lint | `tests/conformance/test_24_marker_and_free_text.py` (a thread of former protocol/1 lines creates no lock; a free-text `released:` line does not free a live ref; the produced comment carries exactly one marker with the prose preserved verbatim); `tests/unit` `MarkerTests.test_release_reason_newline_stays_inside_the_json`, `ComposedCommentTests.test_colliding_free_text_is_preserved_verbatim_beside_one_marker`. Marker vocabulary agreement across spec/emitter cross-checked by `scripts/lint-skills.sh`. |
| L194 | Every worker-posted comment **MUST** open with the attribution disclaimer | ✅ pinned + 🧹 lint | Disclaimer asserted on claim `tests/conformance/test_02_claim_clear.py`, release `tests/conformance/test_06_release.py`, and `tests/unit` `ComposedCommentTests.test_carries_disclaimer_prose_and_one_marker`; disclaimer shape cross-checked by `scripts/lint-skills.sh`. |

## §5 The claim algorithm

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L223 | Label filtering **SHOULD** be client-side for determinism | ✅ pinned | `tests/conformance/test_01_list_startable.py` asserts exact client-side-filtered output; `tests/conformance/test_19_list_startable_call_count.py` pins the O(1) call count (listing + the one claim-refs read) that client-side filtering enables. |
| L227 | Guard: a held task **MUST** be skipped without writing anything | ✅ pinned | `tests/conformance/test_03_claim_held.py` (exit 11, zero writes, no git-data call). |
| §5 CAS | Creating the claim ref is the arbiter: exactly one creator succeeds, HTTP 422 to the rest; the loser **MUST** back off writing nothing | ✅ pinned | `tests/conformance/test_04_claim_race.py` (two concurrent claims, exactly one `201`, the loser writes no comment and touches no label, the surviving ref names the winner); `tests/unit` `RefCasTests.test_claim_ref_create_maps_the_cas_outcomes` (201→won / 422→lost / other→fail) and `test_create_claim_commit_is_an_orphan_marker_commit`; `tests/conformance/test_22_claim_next.py` + `tests/unit` `ClaimNextIterationTests` (skip-on-loss forward-only; two concurrent `claim-next` workers claim two different tasks). |
| §5 thread independence | Ownership is the ref, not the comment thread: a thread of stale claim/reset markers neither blocks nor grants a claim, and the claim path reads no comments | ✅ pinned | `tests/conformance/test_05_claim_thread_independence.py` (stale markers never block; a live ref alone holds the task); `tests/conformance/test_20_claim_ignores_comments.py` (a 150-comment thread costs the claim zero comment reads, asserted on the stub call log). |
| §5 release the lock | Every terminal transition **MUST** delete the claim ref, **after** its comment/label writes | ✅ pinned | Escalate `tests/conformance/test_09_escalate.py`, deliver `tests/conformance/test_10_deliver.py`, release `tests/conformance/test_06_release.py` each assert the ref is gone; ordering-under-failure (ref delete last, task stays held) `tests/conformance/test_11_write_transition_failures.py`; `claim_ref_delete` idempotence (422 tolerated) `tests/unit` `RefCasTests.test_claim_ref_delete_tolerates_a_missing_ref`. |
| L250 | A worker **MUST** work one task at a time; **MUST NOT** claim a second while holding a claim | ✅ pinned | `tests/conformance/test_28_claim_one_at_a_time.py`: `kraken.py claim` refuses (exit 11, writing nothing) a claim on a *different* issue while a `claim-<worker>.json` state file marks an open claim, and `claim-next` refuses on any open claim; re-claiming the *same* issue is permitted (the §5 network-failure caveat), and resolving the claim (deliver / escalate / release) clears the guard. State-file lifecycle: `tests/conformance/test_15_claim_state_file.py`. |

## §6 Heartbeats and the reconciler

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L256 | A worker holding a claim **SHOULD** heartbeat at least every 2h | 🧠 agent | Cadence is worker behavior. The heartbeat *mechanism* — advance the claim ref to a fresh commit, post no comment, do not free the claim — is pinned by `tests/conformance/test_08_heartbeat.py` and the reconciler that consumes it is pinned below. |
| §6 reconciler | Staleness anchored to the claim ref's commit date (not `updatedAt`; nothing on the timeline resets it), 6h `MAX_HOURS`; the four rules — reclaim stale, delete orphan lock, heal missing label, requeue orphan projection | ✅ pinned | `tests/conformance/test_17_reclaim_stale.py` drives `kraken.py reap` — the subcommand `reclaim-stale.yml` now execs (issue #37) — through all four rules against the stub; `tests/unit/test_kraken.py` `ReconcilerClassificationTests` (rule dispatch, transport injected) and `tests/unit/test_workflow_commands.py` `ReapCommandTests` (staleness clock, boundary, env `MAX_HOURS`, transport failure). |
| §6 requeue | requeue-on-reply asymmetry (bare comment requeues `needs-decision`, not `awaiting-merge`); no-op on worker/bot/unheld | ✅ pinned | `tests/conformance/test_18_requeue_on_reply.py` drives `kraken.py requeue-check` — the subcommand `requeue-on-reply.yml` now execs (issue #37) — against the stub; `tests/unit/test_workflow_commands.py` unit-tests the human-vs-worker discrimination + requeue-directive detection. |

## §7 Escalation

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L297 | The worker **MUST** escalate rather than guess | 🧠 agent | Deciding *when* to escalate is judgment — `tests/agent/`. |
| L299 | The comment **MUST** land before the label swap, and the ref delete last | ✅ pinned | `tests/conformance/test_09_escalate.py` (labels swapped, ref deleted); `tests/conformance/test_11_write_transition_failures.py` (comment-fails → nothing changed; label-fails and ref-delete-fails → task stays held, ref survives). |

## §8 Delivery

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| §8 delivery | Post the `delivered` marker (with `pr`), swap `in-progress`→`awaiting-merge`, then delete the claim ref; comment first, ref last | ✅ pinned | `tests/conformance/test_10_deliver.py` (marker + label swap + ref deleted; review bounce re-claimable once the ref is gone); `tests/conformance/test_11_write_transition_failures.py` (gh failure ordering — the ref survives a failed label swap). |
| L316 | Every delivered commit **MUST** carry the `Co-Authored-By` and `Kraken-Task:` trailers | ✅ pinned | The `Kraken-Task:` trailer's format and its `kraken@<version>` stamp are single-sourced in `kraken.py` (`contract task-trailer`, `task_trailer`/`plugin_version`) and unit-pinned (`tests/unit` `ContractCommandTests`, `PluginVersionTests`). That delivered commits actually carry **both** trailers on real git commits is pinned by `tests/conformance/test_29_deliver_commit_trailers.py`: a throwaway work repo builds a multi-commit delivery whose `Kraken-Task:` line is taken verbatim from `kraken.py contract task-trailer`, then asserts every `base..HEAD` commit carries both trailers — well-formed, read through git's own `%(trailers:...)` parser — and that the check fails on a trailer-less and a malformed commit. |
| L323 | The PR body **SHOULD** carry `Closes …` when the work repo is on GitHub | 🧠 agent | PR authorship is agent behavior — `tests/agent/`. |
| L333 | Work **MUST NOT** be silently lost (fall back to the diff in a comment) | 🧠 agent | Fallback behavior is judgment — `tests/agent/`. |

## §9 Release

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L338–340 | A worker abandoning a claim **MUST** release honestly: post the `released` marker, remove `in-progress`, **then** delete the claim ref (deleting the ref is what frees the task) — comment first, ref last | ✅ pinned | `tests/conformance/test_06_release.py` (marker + label dropped + ref deleted; re-claimable after); `tests/conformance/test_11_write_transition_failures.py` (ordering under gh failure); `tests/conformance/test_16_session_end_release.py` (SessionEnd auto-release runs `kraken.py release`); `tests/conformance/test_27_stop_failure_release.py` (StopFailure usage-limit auto-release runs `kraken.py release`). |

## §10 Close and cleanup

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L348 | The coordination repo **SHOULD** run the cleanup workflow; on close every label except `kraken-task` and `project:<name>` is stripped and any leftover claim ref is deleted | ✅ pinned | `cleanup-closed.yml` is a thin exec of `kraken.py cleanup` (issues #37/#39); `tests/conformance/test_21_cleanup_closed.py` drives that subcommand against the gh-stub, and `tests/unit/test_workflow_commands.py` (`IdentityLabelTests`, `CleanupCommandTests`) pins the keep/strip rule, the claim-ref delete, and the no-op/transport paths. |

## §11 Authorization boundaries

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L364 | A conforming worker **MUST NOT** merge, push to default/protected branches, close task issues, deploy, delete, or publish | 🏗 structural + 🧠 agent | `kraken.py`'s subcommand surface contains none of these operations (its only writes are issue labels/comments and its own claim ref under `refs/kraken/claims/`); refusing task-body instructions to do them is judgment — `tests/agent/`, `SECURITY.md`. |

## §12 Conformance

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L409 | Matching the exit-code contract (`0`/`10`/`11`/`20`) is **RECOMMENDED**; the wire contract is what conformance means | ✅ pinned | `0` success: `tests/conformance/test_02_claim_clear.py`; `10` lost CAS: `tests/conformance/test_04_claim_race.py`, `tests/conformance/test_05_claim_thread_independence.py`; `11` no longer clear: `tests/conformance/test_03_claim_held.py`; `20` transport failure: `tests/conformance/test_07_gh_failure.py`, `tests/conformance/test_11_write_transition_failures.py`. |

## Open gaps (follow-up issues)

**No open gaps: every mechanically pinnable clause is pinned.** The two gaps this
audit once tracked have both been resolved; they are kept here as a changelog.

- (**G2 — commit attribution trailers** — resolved; now pinned by
  `tests/conformance/test_29_deliver_commit_trailers.py`, see the §8 L316 row above.)
- (**G1 — one-task-at-a-time** — resolved; now pinned by
  `tests/conformance/test_28_claim_one_at_a_time.py`, see the §5 L250 row above.)
