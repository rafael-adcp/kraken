# Delivering the work

How a kraken tentacle lands the code it wrote. Read this before you push; the
normative version is [`PROTOCOL.md`](../../PROTOCOL.md) §8, and the loop that gets you
here is [`SKILL.md`](SKILL.md).

Work left in a working tree is work that evaporates with the container. Unless the
task's **notes** say otherwise, every task ends as a pushed branch and a draft PR.

## The branch

Follow the **work repo's own naming convention** — check its recent branches, its open
PRs, or its CONTRIBUTING. CI pipelines and branch linters routinely key on those
patterns, so never impose a foreign prefix. No evident convention? Use a neutral
descriptive name that includes the task number, e.g. `tasks-12-cursor-pagination`.

Commit as you go, push the branch, and open a **draft PR** describing what changed, why,
and how it was validated.

## The trailers

Every delivered commit carries two trailers, so the work is traceable without relying on
branch names:

```
Co-Authored-By: <your agent identity> <noreply@...>
Kraken-Task: <coordination-repo>#<issue> (worker: <worker-name>, kraken@<version>)
```

The `Co-Authored-By` line is your own model identity (Claude Code:
`Co-Authored-By: Claude <your model name> <noreply@anthropic.com>`; Copilot CLI:
`Co-Authored-By: GitHub Copilot <noreply@github.com>`).

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
