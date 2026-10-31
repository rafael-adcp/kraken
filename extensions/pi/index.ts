/**
 * index.ts — the entrypoint Pi's package discovery looks for. One line, so
 * that `pi install git:github.com/rafael-adcp/kraken@<tag>` carries the
 * extension and not only the skills (#188).
 *
 * Why the file has to exist: a package's `extensions/` directory is scanned by
 * `collectAutoExtensionEntries`, which takes loose `.ts`/`.js` files at the top
 * of it, but descends into a subdirectory only through a `package.json` with a
 * `pi.extensions` manifest, an `index.ts`, or an `index.js`. `extensions/pi/`
 * matches none of those on its own, so a package install resolved
 * `skills: init, status, unleash` and `extensions: []` — a successful-looking
 * install with no deny check and no session-end release.
 *
 * Why not a `pi` manifest in a root `package.json` instead: Pi runs
 * `npm install` inside any git-installed package whose root has a
 * `package.json`, and deletes the checkout if that fails — which would put npm
 * on the critical path of installing a repo that needs only bash and python3.
 * A `pi` manifest also *replaces* conventional discovery rather than adding to
 * it, so one that forgot `skills` would silently ship no skills at all.
 *
 * kraken.ts stays the path the loop's `-e` and the docs name. Pi dedupes by
 * resolved path, so an operator who both package-installs kraken and runs the
 * loop loads this file *and* kraken.ts, registering both handlers twice. That
 * is wasteful, not harmful: the release is idempotent by design (a released
 * claim leaves no state file) and a refused command is refused either way.
 */
export { default } from "./kraken.ts";
