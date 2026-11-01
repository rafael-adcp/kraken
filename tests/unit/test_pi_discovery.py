#!/usr/bin/env python3
"""Unit tests for the Pi package-discovery entrypoint, extensions/pi/index.ts.

Pi finds a package's extensions by scanning `extensions/`: loose `.ts`/`.js`
files at the top of it are taken directly, but a *subdirectory* is entered only
through a `package.json` carrying a `pi.extensions` manifest, an `index.ts`, or
an `index.js`. `extensions/pi/` matches none of those on its own, so before
`index.ts` existed `pi install git:github.com/rafael-adcp/kraken@v0.9.0`
resolved the three skills and `extensions: []` — an install that looks
successful while the deny check and the session-end release are both absent
(#188).

These tests pin the shape that makes discovery work, not Pi's behavior:
`make test` is stdlib-only, so the loader itself is covered by the e2e suite
(tests/e2e/test_pi_e2e.py loads the checkout as a directory extension, the
package-install path, and checks the deny check and the release fire). What a
stdlib test *can* guarantee is that nobody renames, empties or re-points the
entrypoint without deciding to.
"""
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PI_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "extensions", "pi"))
INDEX = os.path.join(PI_DIR, "index.ts")
IMPL = os.path.join(PI_DIR, "kraken.ts")
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


class PiDiscoveryEntrypointTests(unittest.TestCase):
    def test_index_ts_exists(self):
        """The one filename Pi's subdirectory discovery accepts (besides a
        package.json manifest, which we deliberately do not ship)."""
        self.assertTrue(
            os.path.isfile(INDEX),
            "extensions/pi/index.ts is how a Pi package install finds the "
            "extension at all; without it the install silently carries only "
            "the skills (#188)",
        )

    def test_index_re_exports_the_implementation(self):
        """Pi requires the module's *default* export to be a function, and
        rejects the extension outright when it is not."""
        source = read(INDEX)
        self.assertRegex(
            source,
            r"export\s*\{\s*default\s*\}\s*from\s*[\"']\./kraken\.ts[\"']",
            "index.ts must re-export kraken.ts's default export; Pi drops an "
            "extension whose default export is not a function",
        )

    def test_the_implementation_it_points_at_exists(self):
        """kraken.ts stays the path the loop's `-e` and the docs name, so the
        shim and the documented path must not drift apart."""
        self.assertTrue(os.path.isfile(IMPL), "extensions/pi/kraken.ts is missing")

    def test_the_documented_path_is_still_the_loop_argv(self):
        """If the loop ever moves to index.ts, this test and the comment in
        index.ts about double registration both need rewriting — so fail loudly
        rather than let the two descriptions drift."""
        loop = read(os.path.join(REPO_ROOT, "scripts", "kraken-loop.sh"))
        self.assertIn(
            "extensions/pi/kraken.ts",
            loop,
            "scripts/kraken-loop.sh no longer passes -e .../extensions/pi/"
            "kraken.ts; update index.ts's header and this test together",
        )

    def test_no_root_package_json(self):
        """Pi runs `npm install` inside any git-installed package whose root has
        a package.json and deletes the checkout when that fails, which would put
        npm on the critical path of installing a bash-and-python3 repo. A `pi`
        manifest also replaces conventional discovery instead of adding to it,
        so one that forgot `skills` would ship no skills at all."""
        self.assertFalse(
            os.path.exists(os.path.join(REPO_ROOT, "package.json")),
            "a root package.json makes every `pi install git:` run npm, and a "
            "`pi` manifest in it would replace skill discovery rather than add "
            "to it — see extensions/pi/index.ts for why the shim is used instead",
        )

    def test_the_extensions_dir_groups_by_harness(self):
        """A loose `extensions/*.ts` would also be discovered, so the shim is
        not the only shape that works — it is the one that keeps the directory
        grouped per harness, the way hooks/ and lib-copilot-drain.sh are."""
        entries = {e for e in os.listdir(os.path.join(REPO_ROOT, "extensions"))}
        stray = {e for e in entries if e.endswith((".ts", ".js"))}
        self.assertEqual(
            set(),
            stray,
            "extensions/ holds one directory per harness; a loose "
            f"{sorted(stray)} would be discovered as a second, unrelated "
            "extension",
        )


class PiDiscoveryDocumentationTests(unittest.TestCase):
    def test_readme_documents_the_package_install(self):
        """The install an operator reaches for first has to appear in the Pi
        section, or the settings pointer reads as the only way in."""
        readme = read(os.path.join(REPO_ROOT, "README.md"))
        self.assertRegex(
            readme,
            r"pi install\s+git:",
            "README.md's Pi section must name the `pi install git:` path now "
            "that it carries the extension (#188)",
        )


if __name__ == "__main__":
    unittest.main()
