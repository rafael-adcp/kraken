#!/usr/bin/env python3
"""scripts/kraken-copilot.sh — the interactive Copilot CLI tentacle launcher.
It opens `copilot -i "/kraken:unleash ..."` in the work repo under exactly the
permission and deny flags the ambush loop's drain passes run with
(lib-copilot-drain.sh), so an interactive worker is neither stalled by a
permission prompt nor running without the authorization floor. With no kraken
plugin installed in Copilot, it loads this checkout as the plugin. Proven
token-free with a fake `copilot` on PATH."""
import os
import stat
import subprocess
import unittest

from harness import KrakenConformanceTest, ROOT

LAUNCHER = os.path.join(ROOT, "scripts", "kraken-copilot.sh")
LIB = os.path.join(ROOT, "scripts", "lib-copilot-drain.sh")


def shared_flags():
    """KRAKEN_COPILOT_FLAGS as bash expands it — the single source."""
    out = subprocess.run(
        ["bash", "-c", '. "$0"; printf "%s\\0" "${KRAKEN_COPILOT_FLAGS[@]}"', LIB],
        capture_output=True, text=True, check=True).stdout
    return out.split("\0")[:-1]


class CopilotLauncherTests(KrakenConformanceTest):
    def setUp(self):
        super().setUp()
        self.cwd_file = os.path.join(self.state, "copilot-cwd")
        self.args_file = os.path.join(self.state, "copilot-args")
        self.plugins_file = os.path.join(self.state, "copilot-plugins")
        self.bin_dir = os.path.join(self.state, "bin")
        os.makedirs(self.bin_dir)
        fake = os.path.join(self.bin_dir, "copilot")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = plugin ]; then cat "%s" 2>/dev/null; exit 0; fi\n'
                    'pwd > "%s"\n'
                    "printf '%%s\\0' \"$@\" > \"%s\"\n"
                    % (self.plugins_file, self.cwd_file, self.args_file))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        self.work = os.path.join(self.state, "work-repo")
        os.makedirs(self.work)

    def installed(self):
        with open(self.plugins_file, "w", encoding="utf-8") as f:
            f.write("Installed plugins:\n  • kraken@kraken (v0.7.1)\n")

    def launch(self, *args, cwd=None):
        env = self.base_env()
        env["PATH"] = self.bin_dir + os.pathsep + env["PATH"]
        for var in ("KRAKEN_TASKS", "KRAKEN_WORKER", "KRAKEN_PROJECT", "KRAKEN_WORK_DIR"):
            env.pop(var, None)
        return subprocess.run(["bash", LAUNCHER, *args], cwd=cwd or self.work,
                              env=env, capture_output=True, text=True)

    def copilot_args(self):
        with open(self.args_file, encoding="utf-8") as f:
            return f.read().split("\0")[:-1]

    def copilot_cwd(self):
        with open(self.cwd_file, encoding="utf-8") as f:
            return f.read().strip()

    def test_opens_unleash_in_the_work_repo_under_the_shared_flags(self):
        self.installed()
        r = self.launch("acme/tasks", "--worker-name", "w1", "--project", "app")
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.copilot_args()
        self.assertEqual(args[:2], ["-i", "/kraken:unleash acme/tasks --worker-name w1 --project app"])
        self.assertEqual(args[2:], shared_flags(),
                         "the launcher drifted from lib-copilot-drain.sh's flags")
        self.assertEqual(os.path.realpath(self.copilot_cwd()), os.path.realpath(self.work))

    def test_deny_rules_are_in_force(self):
        self.installed()
        self.launch("acme/tasks", "--worker-name", "w1", "--project", "app")
        args = self.copilot_args()
        for rule in ("shell(gh pr merge:*)", "shell(gh repo delete:*)",
                     "shell(gh issue close:*)",
                     "github-mcp-server(merge_pull_request)"):
            self.assertIn("--deny-tool=" + rule, args, "missing deny rule %s" % rule)

    def test_once_work_dir_and_passthrough(self):
        self.installed()
        r = self.launch("acme/tasks", "--worker-name", "w1", "--project", "app",
                        "--once", "--work-dir", self.work, "--", "--model", "auto",
                        cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.copilot_args()
        self.assertTrue(args[1].endswith(" --once"), args[1])
        self.assertEqual(args[-2:], ["--model", "auto"])
        self.assertEqual(os.path.realpath(self.copilot_cwd()), os.path.realpath(self.work))

    def test_without_the_plugin_installed_loads_this_checkout(self):
        r = self.launch("acme/tasks", "--worker-name", "w1", "--project", "app")
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.copilot_args()
        self.assertIn("--plugin-dir", args)
        self.assertEqual(os.path.realpath(args[args.index("--plugin-dir") + 1]),
                         os.path.realpath(ROOT))
        self.assertIn("--add-dir", args, "copilot could not run the checkout's kraken.py")

    def test_refuses_missing_arguments_and_the_placeholder(self):
        r = self.launch("acme/tasks", "--worker-name", "w1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--project", r.stderr)
        r = self.launch("OWNER/tasks", "--worker-name", "w1", "--project", "app")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.args_file), "copilot ran on a refused launch")


if __name__ == "__main__":
    unittest.main()
