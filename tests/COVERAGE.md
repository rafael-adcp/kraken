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
| L43 | Coordination repo **MUST** be private and **MUST NOT** hold work code | 📋 operational + ✅ pinned | `kraken.py init` creates the repo **private** (never public); `tests/conformance/test_26_init.py` asserts the `repo create … --private` bootstrap and the private-only contract. |
| L49 | Assignees **MUST NOT** be used to arbitrate anything | 🏗 structural | Arbitration (`kraken.py` `arbitrate_winner`) reads only claim markers in the claim window; assignees are never fetched or consulted. Pinned indirectly by `tests/conformance/test_04_claim_race.py`, `tests/conformance/test_05_claim_window.py`, `tests/unit` (arbitration ignores everything but claim markers/lines). |

## §2 Task shape

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L65 | **Goal** field **MUST** be present | 🧠 agent / 🧹 lint | Task template field presence: `scripts/lint-skills.sh`; worker restates goal as assumptions: `tests/agent/`. |
| L66 | **Acceptance MUST** be run for real; a task whose acceptance was not executed **MUST NOT** move to `awaiting-merge` | 🧠 agent | Worker judgment — `tests/agent/`. The conformance suite cannot observe whether real acceptance ran. |
| L69 | Every task **MUST** carry exactly one `project:<name>` label; a task without one is invisible to every worker | ✅ pinned (invisible) / 🧹 lint ("exactly one") | Missing-label invisibility: `tests/conformance/test_01_list_startable.py` (issue 8, no project label, absent from both list and snapshot). Cross-project routing: `tests/conformance/test_01_list_startable.py` (issue 3, `project:other`, excluded). "Exactly one" is a label-hygiene rule, not enforced by `kraken.py`. |
| §2.1 validator | Queue-entry gate flags a missing `project:<name>` label or an empty/absent Goal or Acceptance section with one actionable comment; compliant task and non-`kraken-task` issue are no-ops; idempotent (no duplicate on re-flag) | ✅ pinned | `tests/conformance/test_25_validate_task.py` drives `kraken.py validate` — the subcommand `validate-task.yml` now execs (issue #37) — against the stub (missing label, missing Acceptance, heading-less body, compliant, non-task, debounce, edit-after-fix); `tests/unit/test_workflow_commands.py` unit-tests the section parse + debounce. |

## §3 Labels: the state machine

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L90 | Colors and label descriptions are **SHOULD** | 🧹 lint | Label set drift: `scripts/lint-skills.sh`. |
| L91 | The label *names* are **MUST** | 🧹 lint + ✅ pinned | Name drift across files: `scripts/lint-skills.sh`. The exact names are also exercised throughout `tests/conformance/` (every `mk_issue`/`has_label` uses them). |
| L95 | Startable definition; a task **MUST** carry at most one held label | ✅ pinned | Startable/held classification: `tests/conformance/test_01_list_startable.py`, `tests/conformance/test_12_list_startable_blocked.py` (blocked-by), `tests/conformance/test_13_watch_queue_blocked.py` (watch gate). At-most-one-held is enforced by the claim guard: `tests/conformance/test_03_claim_held.py`. |
| L114 | Workers **MUST NOT** close task issues | 🏗 structural | `kraken.py` exposes no close/cancel path; closing is out of the transition surface (§11). See also §11 authorization. |

## §4 Comments: the machine marker and attribution

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L135–136 | The marker JSON **MUST** be a single line carrying a string `type`, encoded with a real JSON serializer | ✅ pinned + 🧹 lint | `tests/unit` `MarkerTests` (`test_make_marker_is_compact_ascii_json`, `test_make_marker_round_trips_through_parse`); marker byte-form pinned in conformance by `assert_marker` in `tests/conformance/test_02_claim_clear.py`, `tests/conformance/test_24_protocol3_marker_only.py`. Serializer-not-interpolation cross-checked by `scripts/lint-skills.sh`. |
| L140–142 | Consumers **MUST** scan every comment in server order; a malformed marker (undecodable JSON, no string `type`) **MUST** be ignored, never guessed | ✅ pinned | `tests/unit` `MarkerTests` (`test_parse_marker_rejects_undecodable_json`, `test_parse_marker_rejects_a_payload_without_a_string_type`, `test_malformed_marker_never_arbitrates`, `test_parse_marker_tolerates_surrounding_prose`); per-line scan `MarkerEdgeCaseTests`. |
| L147 | `heartbeat` marker **MUST NOT** reset the claim window | ✅ pinned | `tests/conformance/test_08_heartbeat.py`; `tests/unit` `ArbitrationTests.test_heartbeat_does_not_reset`. |
| L186–193 | A protocol/3 consumer reads the hidden marker and **NOTHING else**: the retired protocol/1 line grammar is not parsed, so free text can never occupy a machine-line position | ✅ pinned + 🧹 lint | `tests/unit` `MarkerOnlyReadingTests` (former claim/reset lines inert, result-file `released:` resets nothing, heartbeat message with `claimed-by:` forges no machine line, release reason newline injects nothing); `tests/conformance/test_24_protocol3_marker_only.py` (end-to-end marker-only + produced-comment shape). Marker vocabulary agreement across spec/emitter cross-checked by `scripts/lint-skills.sh`. |
| L164 | Every worker-posted comment **MUST** open with the attribution disclaimer | ✅ pinned + 🧹 lint | Disclaimer asserted on claim `tests/conformance/test_02_claim_clear.py`, release `tests/conformance/test_06_release.py`, heartbeat `tests/conformance/test_08_heartbeat.py`, and `tests/unit` `MarkerReaderTests.test_composed_comment_carries_disclaimer_prose_and_marker`; disclaimer shape cross-checked by `scripts/lint-skills.sh`. |

## §5 The claim algorithm

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L183 | Label filtering **SHOULD** be client-side for determinism | ✅ pinned | `tests/conformance/test_01_list_startable.py` asserts exact client-side-filtered output; `tests/conformance/test_19_list_startable_call_count.py` pins the O(1) call count that client-side filtering enables. |
| L187 | Guard: a held task **MUST** be skipped without writing anything | ✅ pinned | `tests/conformance/test_03_claim_held.py` (exit 11, zero writes). |
| L194 | A loser **MUST** back off removing nothing | ✅ pinned | `tests/conformance/test_04_claim_race.py` (the claim race: loser exits 10, removes nothing); `tests/conformance/test_22_claim_next.py` + `tests/unit` `ClaimNextIterationTests` (`claim-next` skips a lost/held candidate forward — never retries it — and two concurrent `claim-next` workers claim two different tasks). |
| L201 | Claim markers before the window start **MUST** be ignored | ✅ pinned | `tests/conformance/test_05_claim_window.py`; `tests/unit` `ArbitrationTests` (`test_released_resets_window`, `test_stale_claim_resets_window`, `test_needs_decision_resets_window`, `test_reset_after_claim_leaves_no_winner`, `test_delivered_is_a_review_bounce_reset`). |
| L204 | A liveness signal **MUST NOT** reset the window | ✅ pinned | (same as §4 L147) `tests/conformance/test_08_heartbeat.py`, `tests/unit` `ArbitrationTests.test_heartbeat_does_not_reset`. |
| L211 | A worker **MUST** work one task at a time; **MUST NOT** claim a second while holding a claim | ✅ pinned | `tests/conformance/test_28_claim_one_at_a_time.py`: `kraken.py claim` refuses (exit 11, writing nothing — no label, no comment) a claim on a *different* issue while a `claim-<worker>.json` state file marks an open claim, and `claim-next` refuses on any open claim; re-claiming the *same* issue is permitted (the §5 network-failure caveat), and resolving the claim (deliver / escalate / release) clears the guard. State-file lifecycle: `tests/conformance/test_15_claim_state_file.py`. |

## §6 Heartbeats and the reaper

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L217 | A worker holding `in-progress` **SHOULD** heartbeat at least every 2h | 🧠 agent | Cadence is worker behavior. The reaper *mechanism* that consumes heartbeats is pinned below. |
| §6 reaper | Staleness anchored to the worker's last liveness marker (`claim`/`heartbeat`), 6h `MAX_HOURS`; operator comments do not reset the clock | ✅ pinned | `tests/conformance/test_17_reclaim_stale.py` drives `kraken.py reap` — the subcommand `reclaim-stale.yml` now execs (issue #37) — against the stub; `tests/unit/test_workflow_commands.py` unit-tests the staleness anchoring. |
| §6 requeue | requeue-on-reply asymmetry (bare comment requeues `needs-decision`, not `awaiting-merge`); no-op on worker/bot/unheld | ✅ pinned | `tests/conformance/test_18_requeue_on_reply.py` drives `kraken.py requeue-check` — the subcommand `requeue-on-reply.yml` now execs (issue #37) — against the stub; `tests/unit/test_workflow_commands.py` unit-tests the human-vs-worker discrimination + requeue-directive detection. |

## §7 Escalation

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L258 | The worker **MUST** escalate rather than guess | 🧠 agent | Deciding *when* to escalate is judgment — `tests/agent/`. |
| L260 | The comment **MUST** land before the label swap | ✅ pinned | `tests/conformance/test_09_escalate.py`; `tests/conformance/test_11_write_transition_failures.py` (comment-fails → nothing changed, task stays held). |

## §8 Delivery

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| §8 delivery | Post `delivered:` + `pr:`, then swap `in-progress`→`awaiting-merge`; comment first | ✅ pinned | `tests/conformance/test_10_deliver.py`; `tests/conformance/test_11_write_transition_failures.py` (gh failure ordering); review-bounce window reset: `tests/conformance/test_10_deliver.py` and `tests/unit` `test_delivered_is_a_review_bounce_reset`. |
| L277 | Every delivered commit **MUST** carry the `Co-Authored-By` and `Kraken-Task:` trailers | ✅ pinned | The `Kraken-Task:` trailer's format and its `kraken@<version>` stamp are single-sourced in `kraken.py` (`contract task-trailer`, `task_trailer`/`plugin_version`) and unit-pinned (`tests/unit` `ContractCommandTests`, `PluginVersionTests`). That delivered commits actually carry **both** trailers on real git commits is pinned by `tests/conformance/test_29_deliver_commit_trailers.py`: a throwaway work repo builds a multi-commit delivery whose `Kraken-Task:` line is taken verbatim from `kraken.py contract task-trailer`, then asserts every `base..HEAD` commit carries both trailers — well-formed, read through git's own `%(trailers:...)` parser — and that the check fails on a trailer-less and a malformed commit. |
| L284 | The PR body **SHOULD** carry `Closes …` when the work repo is on GitHub | 🧠 agent | PR authorship is agent behavior — `tests/agent/`. |
| L294 | Work **MUST NOT** be silently lost (fall back to the diff in a comment) | 🧠 agent | Fallback behavior is judgment — `tests/agent/`. |

## §9 Release

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L299–301 | A worker abandoning a claim **MUST** release honestly: post the `released` marker (window closes), **then** remove `in-progress` — comment first | ✅ pinned | `tests/conformance/test_06_release.py`; `tests/conformance/test_11_write_transition_failures.py` (ordering under gh failure); `tests/conformance/test_16_session_end_release.py` (SessionEnd auto-release runs `kraken.py release`); `tests/conformance/test_27_stop_failure_release.py` (StopFailure usage-limit auto-release runs `kraken.py release`). |

## §10 Close and cleanup

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L309 | The coordination repo **SHOULD** run the cleanup workflow; on close every label except `kraken-task` and `project:<name>` is stripped | ✅ pinned | `cleanup-closed.yml` is a thin exec of `kraken.py cleanup` (issues #37/#39); `tests/conformance/test_21_cleanup_closed.py` drives that subcommand against the gh-stub, and `tests/unit/test_workflow_commands.py` (`IdentityLabelTests`, `CleanupCommandTests`) pins the keep/strip rule and the no-op/transport paths. |

## §11 Authorization boundaries

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L325 | A conforming worker **MUST NOT** merge, push to default/protected branches, close task issues, deploy, delete, or publish | 🏗 structural + 🧠 agent | `kraken.py`'s subcommand surface contains none of these operations; refusing task-body instructions to do them is judgment — `tests/agent/`, `SECURITY.md`. |

## §12 Conformance

| Clause (line) | Normative text | Status | Pinned by |
| --- | --- | --- | --- |
| L370 | Matching the exit-code contract (`0`/`10`/`11`/`20`) is **RECOMMENDED**; the wire contract is what conformance means | ✅ pinned | `0` success: `tests/conformance/test_02_claim_clear.py`; `10` lost tiebreaker: `tests/conformance/test_04_claim_race.py`; `11` no longer clear: `tests/conformance/test_03_claim_held.py`; `20` transport failure: `tests/conformance/test_07_gh_failure.py`, `tests/conformance/test_11_write_transition_failures.py`. |

## Open gaps (follow-up issues)

No open gaps: every mechanically pinnable clause is pinned.

- (**G2 — §8 L229, commit attribution trailers** — now pinned by `tests/conformance/test_29_deliver_commit_trailers.py`;
  see the §8 L277 row above.)
- (**G1 — §5 L211, one-task-at-a-time** — now pinned by `tests/conformance/test_28_claim_one_at_a_time.py`; see the §5
  row above.)
