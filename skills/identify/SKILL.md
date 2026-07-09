---
name: identify
description: Given a coordination repo, list its `project:` labels and print ready-to-paste `/kraken:unleash` invocations — one per project — so I don't have to remember the slug or which projects live in that queue.
---

# Kraken — find targets

You are the recon step for `/kraken:unleash`: given a coordination repo, discover
which projects live in its queue and print the copy-paste commands to launch a
worker against each one, with the coordination slug already baked in.

## Invocation

```
/kraken:identify OWNER/tasks
```

The argument is REQUIRED — the coordination repo whose queue you will enumerate.
Missing? Do not guess. Ask for it and stop. If the slug matches `^OWNER/` or contains
`<`/`>`, refuse: it looks like the template placeholder — substitute your real
`owner/repo` and re-run.

## Protocol

1. **List the project labels.** Filter client-side — `gh label list --search` is a
   substring match, and we want an exact prefix:

   ```
   gh -R OWNER/tasks label list --limit 200 --json name \
     --jq '.[].name | select(startswith("project:"))'
   ```

2. **Zero results?** Say so and stop — do not invent projects. Point at the
   Quickstart in `README.md`: labels are created with
   `gh -R OWNER/tasks label create "project:<name>"`.

3. **Print the launch block.** Sort the project names, strip the `project:`
   prefix, and emit exactly one fenced block with one line per project. Substitute
   the real `OWNER/tasks` — never leave a literal `OWNER/tasks` in the output.
   Keep `<worker-name>` as a placeholder — that stays my call at launch time
   (`unleash`: capacity = how many workers I start):

   ```
   /kraken:unleash OWNER/tasks --worker-name <worker-name> --project <name-1>
   /kraken:unleash OWNER/tasks --worker-name <worker-name> --project <name-2>
   ```

4. **Nothing else.** No issues are read or written; no labels are created. This
   skill is read-only recon over the label list.

## Authorization boundaries

- Read-only. This skill runs one `gh label list` call and prints the result.
- It does NOT read issues, create labels, or invoke `/kraken:unleash` on my
  behalf. The output is copy-paste — I launch workers deliberately.

Coordination repo: $ARGUMENTS
