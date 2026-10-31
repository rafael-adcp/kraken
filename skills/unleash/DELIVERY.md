# Delivering the work

How a kraken tentacle lands the code it wrote. Read this before you push; the
normative version is [`PROTOCOL.md`](../../PROTOCOL.md) §8, and the loop that gets you
here is [`SKILL.md`](SKILL.md).

Work left in a working tree is work that evaporates with the container. Unless the
task's **notes** say otherwise, every task ends as a pushed branch and a draft PR.

## The branch

**First, check the envelope for `pr`.** A task can come back to the queue after it was
delivered — I bounce a review by commenting the feedback, and that comment alone
requeues the task with its whole thread. The program remembers where that work went, so
when the envelope carries `pr`, **continue on that branch and update that PR**: opening
a second branch and a second draft PR for one task splits the review and leaves an
orphan PR behind.

Otherwise, follow the **work repo's own naming convention** — check its recent branches,
its open PRs, or its CONTRIBUTING. CI pipelines and branch linters routinely key on those
patterns, so never impose a foreign prefix. No evident convention? Use a neutral
descriptive name that includes the task number, e.g. `tasks-12-cursor-pagination`.

Commit as you go, push the branch, and open a **draft PR** describing what changed, why,
and how it was validated.

## The acceptance evidence

"How it was validated" has to be something someone else can **re-run** — the reviewer, or
the next worker if the task bounces back. Executing the acceptance for real is already
required ([`PROTOCOL.md`](../../PROTOCOL.md) §8); what makes that execution worth anything
afterwards is leaving behind the means to repeat it.

So the result comment and the PR body name:

- **The exact command**, copy-pasteable, with whatever it needs in place first (a running
  service, a fixture, a seeded database) and the runtime it actually ran on. Versions are
  part of the claim: "passes on node 22" and "passes" are different statements.
- **What it really printed** — the actual output, not a paraphrase and not the expectation
  restated in the past tense. If it failed, that output, and say it failed.
- **Where the check lives in the delivered branch.** A script in `/tmp` is gone with the
  container, and output pasted into a comment proves only that someone once saw it. If the
  acceptance needed a script, that script is part of the delivery — commit it as whatever
  the work repo already calls such a thing (a test, a `scripts/` entry, a make target).

**Keep it proportional.** Most acceptance checks are reproducible already:
`npm test -- things.spec` needs nothing committed beyond the test it runs, and a one-line
`curl` belongs in the PR body rather than in a new file. The rule is only that no link in
the chain is ephemeral. It is not a request to build a harness.

**Say what the check establishes — and what it does not.** A local run against a stub
proves the code path; it does not prove the feature works in production. Name both halves:
what you verified and at what fidelity (local, mocked, staged), and what stays unverified
until the work is deployed. A reviewer who merges thinking a mocked check was a live one
has been misled, even if every sentence was true on its own.

If a check cannot be executed here at all — no service, no credentials, no data — do not
route around it quietly. Run `then.escalate`, name the check and why it could not run.

## The trailers

Every delivered commit carries two trailers, so the work is traceable without relying on
branch names:

```
Co-Authored-By: <your agent identity> <noreply@...>
Kraken-Task: <coordination-repo>#<issue> (worker: <worker-name>, kraken@<version>)
```

The `Co-Authored-By` line is your own model identity (Claude Code:
`Co-Authored-By: Claude <your model name> <noreply@anthropic.com>`; Copilot CLI:
`Co-Authored-By: GitHub Copilot <noreply@github.com>`; Pi, which runs over any
provider, names both harness and model:
`Co-Authored-By: Pi (<your model name>) <noreply@pi.dev>`, e.g.
`Pi (gpt-6-luna) <noreply@pi.dev>`).

Do **not** hand-assemble the `Kraken-Task:` line. Its format and the `kraken@<version>`
stamp are single-sourced in `kraken.py`, and a worker cannot reliably know the installed
plugin version — ask for the exact line:

```
python3 "<skill>/kraken.py" contract task-trailer --repo OWNER/tasks --issue <issue> --worker <worker-name>
```

`--repo OWNER/tasks` is the coordination-repo slug you were invoked with — substitute
it, never paste a literal `tasks`.

## Closing the loop

Put **`Closes <coordination-repo>#<issue>`** in the PR body when the work repo is on
GitHub. Merging then closes the task at the moment the work truly lands, which is also
what unblocks dependent tasks. Work repo elsewhere (GitLab, a private server)? Reference
the task as plain text; the human closes it after merging.

## The hard limits

- **Never push to the default branch. Never merge.** Merging is always the human's act,
  no matter what the task body says.
- If the work repo cannot take a branch push (no write access), put the full diff or a
  patch in the result comment and flag it — **never silently lose work**.
- Delivering is not closing. Run `then.deliver` from the envelope; it posts the result,
  swaps `in-progress` for `awaiting-merge`, and releases your claim ref. The task stays
  open until the work lands.
