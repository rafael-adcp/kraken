# Design review: kraken vs. four from-scratch redesigns (2026-08-01)

An adversarially-verified answer to one question: *if kraken were rebuilt from
scratch today — same functional requirements, same constraints (async
orchestration for one person / a small team, work stays on the operator's
machine where credentials/data/services live) — what would come out different,
and which of those differences are actually better than what exists?*

Nothing in this file is normative. Findings reference the tree at commit
`497d681` with the then-uncommitted working-tree edits to `AGENTS.md`,
`PROTOCOL.md`, `README.md` and `skills/unleash/*`; line numbers may drift.

## Method

A 25-agent workflow in four phases:

1. **Read** — six parallel readers over `PROTOCOL.md`, `HISTORY.md`, the three
   skills, the protocol-core modules, the IO/UX modules, and
   positioning/tests. Their output was compiled into a provider-agnostic
   requirements brief (no mention of GitHub, Issues, labels, or refs).
2. **Design** — four independent architects designed a coordination protocol
   from scratch against that brief only. Each had a distinct lens and **no
   knowledge of kraken's actual architecture**: distributed-systems rigor,
   radical minimalism (UNIX/local-first), operator experience, and
   software-design quality (POODR school).
3. **Compare** — one agent deduplicated the concrete differences between the
   four designs and kraken today into 13 decidable claims.
4. **Verify** — one adversarial reviewer per claim, each required to (a) check
   `HISTORY.md` for whether the idea was already tried and retired, (b) verify
   the claim's description of kraken against the spec and source, and (c)
   judge on the merits *for this product*. Default skeptical:
   `genuinely-better` required a verified, concrete failure mode of the
   current design.

## Headline result: the from-scratch designs reconverged on kraken

All four architects — independently, from provider-agnostic requirements —
arrived at kraken's architecture: a private GitHub repository as the whole
coordination store, Issues as tasks, conflict-failing **git-ref creation as the
single arbitration primitive**, a **generation ladder** where first claim,
renewal and steal are the identical create, reader-applied lease expiry on the
server's clock, a **force-updated state record ref** (deliberately not a CAS,
serialized by the lease proof), **requeue as a pure read-side derivation**
(live comment total vs. frozen anchor), **write-only labels** as the human
projection, reader-side reconciliation, and strict terminal write ordering with
the ref delete last. All four independently listed the same non-goals: no
server, no database, no daemon, no cron, no webhook receiver, no custom
dashboard. The minimalist even reinvented the hidden HTML-comment marker.

Nothing that `HISTORY.md` records as retired (comment-arbitrated claims, the
claim window, the scheduled reconciler, requeue directives, labels as truth)
was re-proposed by any designer and survived verification. **At the protocol
level, kraken is the fixed point of this design space; the nine revisions
already paid for the convergence.** Every difference that survived is at the
implementation or product layer.

## Verified differences

| # | Claim | Verdict |
|---|-------|---------|
| c1 | Bounce detection, prior-PR handover, auth boundary into the `next-action` envelope | **genuinely-better** |
| c13 | Comment-count shrinkage (total < anchor) handled explicitly | **genuinely-better** |
| c4 | Single-source every contract literal; docs derive instead of restating | tradeoff |
| c9 | Parameterize the conformance suite over any transition executable | tradeoff |
| c10 | Escalations @mention a configured operator handle | tradeoff |
| c2 | Extract a pure IO-free derivation core | equivalent (already exists) |
| c3 | Structural auth boundary via fine-grained PAT | equivalent (already prescribed) |
| c7 | Absent vs. Unreadable typed nulls on all surfaces | equivalent (already exists) |
| c12 | Read the requeue anchor back via the writing surface | equivalent (already true) |
| c5 | One fake GitHub — merge FakeApi/FakeQueue into the stub | current-is-better |
| c6 | A Store port with one provider-adapter file | current-is-better |
| c8 | Validation advisory with a CAS debounce ref | current-is-better |
| c11 | ETag conditional requests on the watcher poll | current-is-better |

### c1 — carry what the program already knows into the envelope (genuinely-better)

All four lenses converged on this one. Kraken's own thesis is *judgment is the
model's, mechanics are the program's* — and three things sit on the wrong side
of that line:

- **Bounce detection.** `skills/unleash/SKILL.md` promises "an execute envelope
  carries everything you need, so you never fetch the task again", then its
  bounce paragraph requires exactly one more fetch (`gh issue view
  --comments`) and a model judgment to notice the task is coming back. The
  program computes the same verdict with exact integer arithmetic
  (`state.py` `requeued()`) and discards it.
- **Prior-delivery handover.** `DELIVERY.md` has the model rediscover the
  earlier branch/PR from `brief.body` and the thread, even though `record.pr`
  is a first-class sticky field since protocol/9. The console got the field;
  the worker never did. A model that misses it opens an orphan second PR.
- **Dropped at the source.** `acquire_next` holds the full record
  (state, pr, comments anchor) at claim time (`claim.py:524-529`) and returns
  only `{issue, title, body}`; the resume path reads the record and discards
  it too (`next_action.py:385, 412-429`).

**Recommendation (no wire-contract change, no protocol bump).** Extend the won
payload with `{"bounced": record.requeued(task.comment_total), "pr":
record.pr}` and have the envelope emit both on `execute` (acquire and resume
paths). Shrink SKILL.md's bounce paragraph to "if `bounced`, read the comments
past brief-time; the newest feedback is the ask; escalate if nothing is
actionable", and replace DELIVERY.md's rediscovery paragraph with "continue on
`envelope.pr` when present". Independent follow-ups: a `boundary` field in
`CONTRACT_FIELDS` printing PROTOCOL.md §11 verbatim (referenced by the
subagent-handover paragraph instead of asking the driver model to paste prose),
and a placeholder-slug refusal (`^OWNER/` or `<>`) in `cli.py main()` so the
guard triplicated across three SKILL.md files becomes a pointer to a program
error.

### c13 — comment-count shrinkage silently buries answered tasks (genuinely-better)

Requeue is strictly `total > record.comments` (`state.py:134`), and the
derivation is memoryless by design. Verified failure sequence: task escalates
with anchor N → operator deletes an old noise comment (total N−1; held —
correct) → operator answers (total N; N > N is false; **held — wrong**) → the
task sits in `needs-decision` indefinitely while the operator believes they
answered. A deletion *after* an answer likewise re-buries a task every reader
had been deriving as queued. This is exactly the failure class protocol/8 was
written to eliminate ("the operator is the one who eats that silence"). The
existing unit test pins only the deletion instant ("nothing was said, nothing
is lifted"), not the stale-anchor aftermath.

Compounding it, three in-tree docstrings claim the low side fails *open* when
it actually fails *closed*: `queue.py` `_comment_total` ("0 makes every held
record look requeued … never one where a delivery is silently buried" — false
for any anchor ≥ 1), `transport.py` `comment_total_of`, and `state.py`
`requeued` repeat the same inverted claim.

**Recommendation.** A fourth idempotent reconciler repair in §6: an open task
whose held record's `comments` anchor exceeds the live total is rewritten with
`comments = total` (re-anchor only — the hold is not lifted, preserving the
pinned semantics), so the *next* comment requeues instead of being absorbed.
One rule in `reconcile.py` keyed on `record.held_state and total <
record.comments`, plus correcting the three inverted docstrings.

### c4 — literal single-sourcing (tradeoff; extract the fixes)

The blanket proposal (skills fetch every literal at runtime, prose holds
judgment only) is not worth it, but its thesis — the linter net has holes and
prose drift ships — was verified twice in the committed tree:

- `README.md:193-196` still says `/kraken:init` "installs the task template and
  its four coordination workflows" — contradicting the same README ten lines
  later ("no workflows") and init's actual behavior (it *deletes* the retired
  workflows; see `test_init_prunes_every_retired_workflow`).
- `skills/status/SKILL.md` documents a `legacy:` tag no code emits since
  protocol/9 (example line, its explanation, and a comment-pagination sentence
  contradicted later in the same file).

Also worth one notch of the existing "executed, not diffed" lint pattern: check
that the status skill's restated JSON schema keys match what `kraken.py status
--json` actually emits, so that restatement cannot drift again. (Note:
`contract` already prints the disclaimer, trailers, marker types, TTL and
renew cadence — but not the exit codes, which live only in `contract.py`
constants and §12 prose.)

### c9 — conformance suite portability (tradeoff; make §12's promise true)

PROTOCOL.md §12 promises "a third-party implementation MAY validate itself by
pointing that suite at its own transition executables" — and the harness
cannot do that: `harness.py:35` hardcodes the path, `:174` invokes `python3
KRAKEN`, and `:46-75` imports the kraken module to read the TTL and build
markers. Meanwhile the wire seam already half-exists: the harness reads the
disclaimer over the wire via `contract disclaimer` (`harness.py:457-460`).

**Recommendation, and stop there until a second implementation exists:** take
the program under test from an env var (`KRAKEN_UNDER_TEST` as an argv prefix,
defaulting to the reference), read the TTL via the existing `contract
lease-ttl` seam, build/parse markers from the §4 grammar the spec already
fixes, and tag the ~14 ergonomics-only test files (next-action, status, init,
watch, hooks…) with a skip-unless-reference marker. Do not restructure the
`mk_*`/`assert_*` helpers — the `KrakenConformanceTest` god node (128 edges) is
the shared harness working as intended.

### c10 — operator @mention on escalation (tradeoff; niche)

Protocol-inert (visible prose is machine-inert per §4) and genuinely untried.
But in the default deployment every worker authenticates as the operator's own
account, and GitHub sends no notifications for one's own comments — the
mention would be a silent no-op exactly where it claims to help. If added:
an optional `--operator <handle>` appending a plain `cc @<handle>` line,
documented as effective only when workers use a different account.

### c11 — ETags on the watcher poll (current-is-better)

The proposal cannot be bolted on: the hot read is GraphQL (issue walk, batched
commit meta, depends-on), and GitHub's conditional-request/304 exemption is
REST-only. Moving the walk to REST would abandon what protocol/9 built
(comments.totalCount, native blockedBy and state records riding one batched
walk). If rate-limit robustness is wanted: have the watcher's failure path
recognize 403/429 with `x-ratelimit-remaining: 0` and sleep until
`x-ratelimit-reset` instead of burning blind 60s-spaced failures
(`watch.py`/`snapshot_state`; transport stays retry-free), and/or document
raising `KRAKEN_WATCH_POLL_SECONDS` when several workers share one PAT.

### Claims where kraken already does what the redesign proposed

The verifiers killed these by reading the source — worth recording because
they are the parts of the from-scratch designs the current code already earns:

- **c2, pure derivation core**: `reconcile_plan` is literally the proposed
  repair-ops-as-data — "a PURE function of one queue read — no network"
  (`reconcile.py:61-103`) — shared by `reap` and `claim-next` so they cannot
  drift. Residual: add one equivalence test asserting `project_reconcile`'s
  in-memory fold matches a fresh read after `apply_reconcile` (today the
  lockstep is enforced by comments only), and align or document the reclaim
  comment-count skew (`reconcile.py:186-193` vs `:235-241`).
- **c3, structural authz**: program-owned writes are the shipped architecture,
  and the fine-grained PAT is already prescribed by SECURITY.md's checklist.
  Residual idea: a `doctor`/init preflight that probes for a classic token
  (`x-oauth-scopes` header) and for missing required-review protection on the
  work repo's default branch, warning loudly.
- **c7, typed nulls**: `NO_RECORD` / `UNREADABLE_RECORD` are distinct frozen
  objects with per-null fail directions (`state.py:179-186`), mirroring the
  lease's `present`/`known` — the pattern did not stop at the lease.
- **c8, validation advisory**: §2.1 + `cmd_validate` already are the proposed
  read-side advisory. Residual gap, see below.
- **c12, anchor read-back surface**: the transition's post and its read-back
  are both REST already; the GraphQL totalCount lives only on the queue walk.

### Claims where the current design beat the redesign

- **c5, one fake GitHub**: `FakeApi` is 99 lines and *subclasses the real
  `Api`*, raising on any method name the real class lacks — it structurally
  cannot drift and carries no model of GitHub. Merging the unit seam into the
  third-party-facing conformance stub would couple the two audiences. The one
  verified cost stands alone: `tests/unit/test_kraken.py` is a ~2,300-line
  monolith — split it by module (`test_refs.py`, `test_lease.py`, …).
- **c6, Store port**: the protocol is *normatively* GitHub-shaped — §5 defines
  the claim as the one GitHub write that fails on conflict with HTTP 422. A
  port would abstract the spec itself, cost without a second backend. The one
  verified kernel: GraphQL built by f-string interpolation
  (`queue.py:380-389`, `refs.py:178-181`, `transport.py:263-268`) — move
  owner/name/cursor into GraphQL `$variables` (~10-20 lines; SHA aliases stay
  inline but can be hex-validated).

## Spec-vs-implementation gap found on the way

`cmd_validate` (`workflow.py:200-252`) does not honor the PROTOCOL.md §3.1
SHOULD its own comment triggers: after the validation comment lands, it should
read the issue's comment total back and refresh the state record's `comments`
field when a record exists — otherwise its own comment reads as an operator
reply. Per the spec this is "noisy, never corrupt" (the claiming worker finds
nothing actionable and escalates), but the fix is small and local.

## Reader-noted smells (not adversarially verified)

Surfaced by the read phase; unverified beyond what the claims above absorbed.
Kept for the record: complexity concentrates in `claim.py` (~600 LOC, four
related "is it held / is it mine" guards) and `queue.py` (~600 LOC); the
overlapping done/held predicates chain (`issue_is_finished` → `claim_is_moot`
→ `holding_state`) is correct but fragile against a fifth caller; the
`ClaimRecord` scratch file stores the issue as a string while everything else
uses int; `watch.py` parses env knobs with bare `int()` (a malformed value is
a traceback, not a diagnostic); `put_content`'s annotation says `str` while
callers pass bytes; `reconcile.py`'s failure path bypasses the `diag()`
routing; the `hooks/lib-release-claims.sh` grep/sed JSON fallback is coupled
to kraken writing the file itself; mixed-version fleets on one queue are
flagged in three consecutive HISTORY entries but nothing mechanical detects
skew; and the TTL is a silent *work-loss* bound (a mid-task expiry discards
the working tree — deliberate, but worth remembering when sizing it).

## Recommended priority

1. **c1** — `bounced` + `pr` in the execute envelope; shrink the two skill
   paragraphs; then the `boundary` contract field and the slug guard.
2. **c13** — the re-anchor reconciler rule + the three inverted docstrings.
3. **Validation anchor refresh** — make `cmd_validate` honor §3.1's SHOULD.
4. **c4 extracts** — the README init line and the status-skill `legacy:`
   remnants; optionally the status-schema lint check.
5. **c9** — `KRAKEN_UNDER_TEST` + wire-seam constants in the harness.
6. **c6 extract** — GraphQL `$variables` (small hardening).
7. Optional: c5's unit-test split, c11's rate-limit-aware watcher backoff,
   c3's `doctor` preflight, c10's `--operator` mention.

Items 1 and 2 are kraken applying its own doctrine — mechanics belong to the
program, and silence never lands on the operator — to the two places that
lagged behind.
