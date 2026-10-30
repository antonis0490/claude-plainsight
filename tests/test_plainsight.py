import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(os.path.dirname(HERE), "plainsight.py")
sys.path.insert(0, os.path.dirname(HERE))
import plainsight  # noqa: E402


class PlainsightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="plainsight-test-")
        self.config = os.path.join(self.tmp, "claude")
        self.vault = os.path.join(self.tmp, "vault")
        self.root = os.path.join(self.tmp, "code")
        self.repo = os.path.join(self.root, "app")
        os.makedirs(os.path.join(self.repo, "web"))
        os.makedirs(self.config)
        with open(os.path.join(self.repo, "CLAUDE.md"), "w") as f:
            f.write("# App rules\n")
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=self.config)
        self.env.pop("CLAUDE_PROJECT_DIR", None)
        self.old_config = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.config
        self.script = os.path.join(self.vault, ".plainsight", "plainsight.py")
        self.proj = os.path.join(self.vault, "projects", "app")

    def tearDown(self):
        if self.old_config is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old_config
        shutil.rmtree(self.tmp)

    # helpers

    def run_cli(self, *args, script=None, stdin=None, cwd=None, env=None):
        result = subprocess.run(
            [sys.executable, script or self.script] + list(args),
            input=stdin if stdin is not None else "",
            capture_output=True, text=True, cwd=cwd or self.tmp, env=env or self.env)
        return result

    def install(self):
        result = self.run_cli("install", "--vault", self.vault, "--root", self.root, script=SOURCE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def sync(self, cwd):
        result = self.run_cli("sync", stdin=json.dumps({"cwd": cwd}))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def overview(self, cwd):
        result = self.run_cli("overview", stdin=json.dumps({"cwd": cwd}))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def settings(self):
        with open(os.path.join(self.config, "settings.json")) as f:
            return json.load(f)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def our_hooks(self):
        hooks = []
        for group in self.settings().get("hooks", {}).get("SessionStart", []):
            hooks += [h for h in group["hooks"] if plainsight.is_our_hook(h)]
        return hooks

    def set_overview(self, body):
        hub = os.path.join(self.proj, "app.md")
        self.write(hub, self.read(hub).replace("## Overview\n", "## Overview\n" + body + "\n", 1))

    # install

    def test_install(self):
        self.install()
        self.assertTrue(os.path.isfile(self.script))
        with open(os.path.join(self.vault, ".plainsight", "config.json")) as f:
            self.assertEqual(json.load(f)["roots"], [os.path.abspath(self.root)])

        hooks = self.our_hooks()
        self.assertEqual(len(hooks), 2)
        sync, overview = hooks
        self.assertEqual(sync["command"], sys.executable)
        self.assertEqual(sync["args"], [self.script, "sync"])
        self.assertTrue(sync["async"])
        self.assertEqual(overview["args"], [self.script, "overview"])
        self.assertNotIn("async", overview)

        claude_md = self.read(os.path.join(self.config, "CLAUDE.md"))
        self.assertIn(plainsight.START, claude_md)
        self.assertIn(plainsight.END, claude_md)
        self.assertIn("Overview", claude_md)

    def test_install_is_idempotent_and_keeps_user_content(self):
        user_hook = {"hooks": [{"type": "command", "command": "echo hi"}]}
        self.write(os.path.join(self.config, "settings.json"),
                   json.dumps({"theme": "dark", "hooks": {"SessionStart": [user_hook]}}))
        self.write(os.path.join(self.config, "CLAUDE.md"), "# My rules\n\nBe brief.\n")
        self.install()
        first_settings = self.settings()
        first_md = self.read(os.path.join(self.config, "CLAUDE.md"))
        self.install()

        self.assertEqual(self.settings(), first_settings)
        self.assertEqual(self.read(os.path.join(self.config, "CLAUDE.md")), first_md)
        self.assertEqual(len(self.our_hooks()), 2)
        self.assertEqual(self.settings()["theme"], "dark")
        self.assertIn(user_hook, self.settings()["hooks"]["SessionStart"])
        self.assertEqual(first_md.count(plainsight.START), 1)
        self.assertTrue(first_md.startswith("# My rules\n\nBe brief.\n"))

    def test_install_rejects_missing_root(self):
        result = self.run_cli("install", "--vault", self.vault, "--root", os.path.join(self.tmp, "nope"),
                              script=SOURCE)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(os.path.exists(os.path.join(self.config, "settings.json")))

    def test_install_refuses_broken_settings(self):
        self.write(os.path.join(self.config, "settings.json"), "{not json")
        result = self.run_cli("install", "--vault", self.vault, "--root", self.root, script=SOURCE)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.read(os.path.join(self.config, "settings.json")), "{not json")

    # sync

    def test_sync_links_memory_and_creates_hub(self):
        self.install()
        self.sync(self.repo)

        memory = plainsight.memory_dir_for(self.repo)
        self.assertTrue(os.path.isdir(memory))
        link = os.path.join(self.proj, "memory")
        self.assertTrue(plainsight.same_path(plainsight.link_target(link), memory))

        # File links can be skipped on Windows without Developer Mode, but are never copies.
        for link, text in [(os.path.join(self.proj, "CLAUDE.md"), "# App rules\n"),
                           (os.path.join(self.vault, "shared", "CLAUDE.md"), None)]:
            if plainsight.link_target(link) is not None:
                if text:
                    self.assertEqual(self.read(link), text)
            else:
                self.assertTrue(os.name == "nt" and not os.path.lexists(link), link)

        hub = self.read(os.path.join(self.proj, "app.md"))
        self.assertIn("## Overview", hub)
        self.assertIn("No problems found", self.read(os.path.join(self.proj, "_health.md")))

    def test_sync_never_overwrites_hub_and_is_idempotent(self):
        self.install()
        self.sync(self.repo)
        self.set_overview("My own words.")
        hub = self.read(os.path.join(self.proj, "app.md"))
        self.sync(self.repo)
        self.sync(self.repo)
        self.assertEqual(self.read(os.path.join(self.proj, "app.md")), hub)
        self.assertEqual(sorted(os.listdir(self.proj)), ["CLAUDE.md", "_health.md", "app.md", "memory"]
                         if os.path.lexists(os.path.join(self.proj, "CLAUDE.md"))
                         else ["_health.md", "app.md", "memory"])

    def assert_memory_link(self, name, memory_of):
        link = os.path.join(self.proj, name)
        self.assertTrue(plainsight.same_path(plainsight.link_target(link), plainsight.memory_dir_for(memory_of)))

    def test_sync_subfolder_of_git_repo_uses_repo_memory(self):
        # Claude Code keys memory on the git root, so a subfolder session shares the repo's memory.
        os.makedirs(os.path.join(self.repo, ".git"))
        self.install()
        self.sync(os.path.join(self.repo, "web"))
        self.assert_memory_link("memory", self.repo)
        self.assertFalse(os.path.lexists(os.path.join(self.proj, "memory-web")))

    def test_sync_nested_repo_in_plain_folder(self):
        web = os.path.join(self.repo, "web")
        os.makedirs(os.path.join(web, ".git"))
        os.makedirs(os.path.join(web, "src"))
        self.install()
        self.sync(os.path.join(web, "src"))
        self.assert_memory_link("memory-web", web)
        self.assertFalse(os.path.lexists(os.path.join(self.proj, "memory")))

    def test_sync_plain_subfolder(self):
        self.install()
        self.sync(os.path.join(self.repo, "web"))
        self.assert_memory_link("memory-web", os.path.join(self.repo, "web"))

    def test_sync_worktree_uses_main_repo_memory(self):
        gitdir = os.path.join(self.repo, ".git", "worktrees", "app-wt")
        self.write(os.path.join(gitdir, "commondir"), "../..\n")
        worktree = os.path.join(self.root, "app-wt")
        self.write(os.path.join(worktree, ".git"), "gitdir: %s\n" % gitdir)
        self.install()
        self.sync(worktree)
        self.proj = os.path.join(self.vault, "projects", "app-wt")
        self.assert_memory_link("memory", self.repo)

    def test_sync_falls_back_to_project_dir_env(self):
        self.install()
        env = dict(self.env, CLAUDE_PROJECT_DIR=self.repo)
        result = self.run_cli("sync", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.lexists(os.path.join(self.proj, "memory")))

    def test_sync_falls_back_to_cwd(self):
        self.install()
        result = self.run_cli("sync", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.lexists(os.path.join(self.proj, "memory")))

    def test_sync_ignores_sessions_outside_roots(self):
        self.install()
        self.sync(self.tmp)
        self.sync(self.root)
        self.assertEqual(os.listdir(os.path.join(self.vault, "projects")), [])

    def test_sync_leaves_real_folder_alone(self):
        self.install()
        self.write(os.path.join(self.proj, "memory", "mine.md"), "mine\n")
        self.sync(self.repo)
        self.assertEqual(self.read(os.path.join(self.proj, "memory", "mine.md")), "mine\n")
        self.assertIn("not a link", self.read(os.path.join(self.proj, "_health.md")))

    # overview

    def test_overview_empty_prints_nothing(self):
        self.install()
        self.assertEqual(self.overview(self.repo), "")  # before the first sync
        self.sync(self.repo)
        self.assertEqual(self.overview(self.repo), "")

    def test_overview_strips_comments(self):
        self.install()
        self.sync(self.repo)
        self.set_overview("A web app.\n<!-- hidden\nnote -->\nShipping v2.\n\n### Detail\nKept.\n\n## Later\nNot shown.")
        out = self.overview(self.repo)
        self.assertIn("A web app.", out)
        self.assertIn("Shipping v2.", out)
        self.assertIn("### Detail", out)
        self.assertNotIn("hidden", out)
        self.assertNotIn("<!--", out)
        self.assertNotIn("Not shown", out)
        self.assertEqual(self.overview(os.path.join(self.repo, "web")), out)

    def test_overview_is_capped(self):
        self.install()
        self.sync(self.repo)
        self.set_overview("x" * 5000)
        out = self.overview(self.repo)
        self.assertLessEqual(len(out.rstrip("\n")), 2000)
        self.assertIn("truncated", out)

    def test_overview_outside_roots_prints_nothing(self):
        self.install()
        self.assertEqual(self.overview(self.tmp), "")

    # lint

    def test_lint_reports_problems(self):
        self.install()
        self.sync(self.repo)
        memory = plainsight.memory_dir_for(self.repo)
        self.write(os.path.join(memory, "MEMORY.md"), "- [Alpha](alpha.md) - first\n")
        self.write(os.path.join(memory, "alpha.md"),
                   "See [[beta]] and [[missing-note]] and [[alpha#Top|me]].\n"
                   "`[[in-code]]`\n"
                   "api_key = sk-ant-abcdefghijklmnopqrstuvwxyz0123456789\n"
                   "token: <your-token-here>\n"
                   "password = <placeholder123>\n")
        self.write(os.path.join(memory, "beta.md"), "Beta.\n")

        result = self.run_cli("lint")
        self.assertEqual(result.returncode, 1)
        out = result.stdout
        self.assertIn("[[missing-note]]", out)
        self.assertNotIn("[[beta]]", out)
        self.assertNotIn("in-code", out)
        self.assertIn("memory/beta.md` is not listed in MEMORY.md", out)
        self.assertNotIn("alpha.md` is not listed", out)
        self.assertIn("alpha.md:3` looks like a credential", out)
        self.assertNotIn("alpha.md:4", out)
        self.assertNotIn("alpha.md:5", out)
        self.assertNotIn("abcdefghijklmnop", out)  # never echo the secret

        self.sync(self.repo)
        self.assertIn("[[missing-note]]", self.read(os.path.join(self.proj, "_health.md")))
        self.assertNotIn("abcdefghijklmnop", self.read(os.path.join(self.proj, "_health.md")))

    def test_lint_is_read_only_and_clean(self):
        self.install()
        self.sync(self.repo)
        memory = plainsight.memory_dir_for(self.repo)
        self.write(os.path.join(memory, "MEMORY.md"), "- [[alpha]]\n")
        self.write(os.path.join(memory, "alpha.md"), "Fine.\n")
        before = {p: os.path.getmtime(p) for p in plainsight.walk_files(self.vault)}
        result = self.run_cli("lint")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual({p: os.path.getmtime(p) for p in plainsight.walk_files(self.vault)}, before)

    # uninstall

    def test_uninstall_keeps_user_notes(self):
        user_hook = {"hooks": [{"type": "command", "command": "echo hi"}]}
        self.write(os.path.join(self.config, "settings.json"), json.dumps({"hooks": {"SessionStart": [user_hook]}}))
        self.write(os.path.join(self.config, "CLAUDE.md"), "# My rules\n")
        self.install()
        self.sync(self.repo)
        self.sync(os.path.join(self.repo, "web"))
        memory = plainsight.memory_dir_for(self.repo)
        self.write(os.path.join(memory, "fact.md"), "A fact.\n")
        self.write(os.path.join(self.proj, "my-note.md"), "Mine.\n")

        result = self.run_cli("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(self.settings(), {"hooks": {"SessionStart": [user_hook]}})
        self.assertEqual(self.read(os.path.join(self.config, "CLAUDE.md")), "# My rules\n")
        self.assertEqual(sorted(os.listdir(self.proj)), ["app.md", "my-note.md"])
        self.assertFalse(os.path.exists(os.path.join(self.vault, "shared")))
        self.assertFalse(os.path.exists(os.path.join(self.vault, ".plainsight")))
        self.assertEqual(self.read(os.path.join(memory, "fact.md")), "A fact.\n")
        self.assertEqual(self.read(os.path.join(self.repo, "CLAUDE.md")), "# App rules\n")

        result = self.run_cli("uninstall", "--vault", self.vault, script=SOURCE)  # second run is harmless
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstall_removes_created_claude_md(self):
        self.install()
        self.run_cli("uninstall")
        self.assertFalse(os.path.exists(os.path.join(self.config, "CLAUDE.md")))
        self.assertNotIn("hooks", self.settings())

    # doctor

    def test_doctor(self):
        self.install()
        self.sync(self.repo)
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("All good", result.stdout)

        settings = self.settings()
        settings["hooks"]["SessionStart"][0]["hooks"].pop()  # drop the overview hook
        self.write(os.path.join(self.config, "settings.json"), json.dumps(settings))
        shutil.rmtree(plainsight.memory_dir_for(self.repo))
        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("overview hook missing", result.stdout)
        self.assertIn("broken link", result.stdout)

    # helpers in isolation

    def test_slug_matches_claude_code(self):
        self.assertEqual(plainsight.project_slug("/Users/me/repo/my_app.v2"), "-Users-me-repo-my-app-v2")
        self.assertEqual(plainsight.project_slug("C:\\code\\app"), "C--code-app")

    def test_block_round_trip(self):
        for text in ["", "# Rules\n", "# Rules", "a\n\nb\n"]:
            added = plainsight.set_block(text, plainsight.START + "\nx\n" + plainsight.END)
            self.assertEqual(plainsight.set_block(added, plainsight.START + "\nx\n" + plainsight.END), added)
            expected = text if text.endswith("\n") or not text else text + "\n"
            self.assertEqual(plainsight.remove_block(added), expected)


if __name__ == "__main__":
    unittest.main()
