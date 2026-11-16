"""Generic toolkit tests for pp: run against a fresh `pp init` instance, no owner data.

Run: skills/policies-preferences/test/run-tests.sh
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLKIT = HERE.parent
sys.path.insert(0, str(TOOLKIT / "lib"))
import pp  # noqa: E402
import yaml  # noqa: E402

OWNER, NAME, BOTS = "jdoe", "Jane Doe", ["alpha-bot", "beta-bot"]
PP_ENV = ("PP_HOME", "PP_SOURCE_DIR", "PP_TOOLS_DIR", "PP_OWNER", "PP_OWNER_NAME", "PP_CONFIG",
          "PP_BOT", "PP_SOURCE_REPO")


def run(*a, **k):
    return subprocess.run([str(x) for x in a], check=k.pop("check", True), capture_output=True, text=True, **k)


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in PP_ENV}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    env.update(extra)
    return env


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in PP_ENV if k in os.environ}
        os.environ["PP_NOW"] = "2026-01-05T10:00:00+00:00"
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "acme-prefs"
        self.repo.mkdir()
        run("git", "-C", self.repo, "init", "-q", "-b", "main")
        run("git", "-C", self.repo, "remote", "add", "origin", "https://github.com/acme/acme-prefs.git")
        self.lines = pp.init_instance(self.repo, OWNER, NAME, None, "guidance", BOTS)
        self.home = self.repo / "guidance"
        self.commit("init")

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.update(self._saved)

    def commit(self, msg):
        run("git", "-C", self.repo, "add", "-A")
        run("git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)

    def store(self):
        return pp.Store(cfg=pp.resolve_config(source_dir=self.repo))

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = pp.main(["--source-dir", str(self.repo), *args])
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()


class TestInit(Base):
    def test_scaffold_is_valid_and_complete(self):
        for f in ("policies.yaml", "preferences.yaml", "CHANGELOG.md", "README.md", "decisions/.gitkeep"):
            self.assertTrue((self.home / f).exists(), f)
        self.assertTrue((self.repo / ".github/workflows/preferences-validate.yml").exists())
        self.assertTrue((self.repo / "skills/policies-preferences/bin/pp").exists())  # vendored
        self.assertFalse(list((self.repo / "skills").rglob("__pycache__")))
        cfg = yaml.safe_load((self.repo / "pp.config.yaml").read_text())
        self.assertEqual(cfg, {"source_repo": "acme/acme-prefs", "home": "guidance", "owner": OWNER, "owner_name": NAME})
        s = self.store()
        self.assertEqual(pp.validate(s), [])
        self.assertEqual(set(s.scopes), {"all", "coding", "review", "comms", "research", *BOTS})
        self.assertIn("(acme/acme-prefs@", s.citation())

    def test_examples_marked_and_never_loaded(self):
        s = self.store()
        ex = [e for e in s.policies + s.preferences]
        self.assertTrue(ex and all(e["id"].startswith("EXAMPLE-") and e.get("example") for e in ex))
        pols, prefs, _ = pp.applicable(s, "alpha-bot", ["comms", "coding"])
        self.assertEqual(pols + prefs, [])

    def test_workflow_paths_substituted(self):
        wf = (self.repo / ".github/workflows/preferences-validate.yml").read_text()
        self.assertNotIn("__HOME__", wf); self.assertNotIn("__TOOLKIT__", wf)
        self.assertIn('"guidance/**"', wf)
        self.assertIn("skills/policies-preferences/bin/pp check-diff", wf)
        self.assertIn("^guidance/decisions/", wf)

    def test_refuses_overwrite_and_bad_input(self):
        with self.assertRaises(pp.PPError):
            pp.init_instance(self.repo, OWNER, home_dir="guidance")
        with self.assertRaises(pp.PPError):
            pp.init_instance(self.tmp / "x", "Bad Owner")
        with self.assertRaises(pp.PPError):
            pp.init_instance(self.tmp / "x", OWNER, bots=["all"])
        with self.assertRaises(pp.PPError):
            pp.init_instance(self.tmp / "x", OWNER, home_dir="../escape")

    def test_cli_init(self):
        other = self.tmp / "other"
        other.mkdir()
        rc, out, err = self.cli("--source-dir", str(other), "init", "--owner", "kim", "--no-ci")
        self.assertEqual(rc, 0, err)
        self.assertIn("OK: valid instance", out)
        self.assertFalse((other / ".github").exists())
        self.assertTrue((other / "preferences/policies.yaml").exists())


class TestOwnerNotHardcoded(Base):
    def test_full_cycle_with_configured_owner(self):
        pid = pp.propose(self.store(), "alpha-bot", "Short PR titles", "Keep PR titles under 60 chars.",
                         ["coding"], f"{NAME} shortened two titles.")
        self.assertEqual(pp.check_diff(self.home, "HEAD", pp.resolve_config(source_dir=self.repo)), [])
        _, tmpl = pp.render_review(self.store())
        self.assertEqual(tmpl["reviewer"], OWNER)
        with self.assertRaises(pp.PPError) as cm:
            pp.apply_decisions(self.store(), {"reviewer": "alpha-bot", "decisions": [{"id": pid, "action": "approve"}]})
        self.assertIn(f"reviewer: {OWNER}", str(cm.exception))
        pp.apply_decisions(self.store(), {"reviewer": OWNER, "decisions": [{"id": pid, "action": "approve"}]})
        s = self.store()
        self.assertEqual((s.find(pid)[1]["status"], s.find(pid)[1]["reviewed"]["by"]), ("active", OWNER))
        _, prefs, _ = pp.applicable(s, "alpha-bot", ["coding"])
        self.assertEqual([p["id"] for p in prefs], [pid])
        self.assertIn(NAME, (self.home / "policies.yaml").read_text())

    def test_policy_set_by_must_be_owner(self):
        s = self.store()
        s.find("EXAMPLE-POL-001")[1]["set_by"] = "alpha-bot"
        self.assertTrue(any("set_by must be 'jdoe'" in e for e in pp.validate(s)))

    def test_config_owner_mismatch_is_invalid(self):
        os.environ["PP_OWNER"] = "mallory"
        self.assertTrue(any("owner must be 'mallory'" in e for e in pp.validate(self.store())))

    def test_messages_use_owner_name(self):
        rc, out, _ = self.cli("load", "--bot", "alpha-bot")
        self.assertIn(f"how {NAME} wants", out)
        _, _, warn = pp.applicable(self.store(), "ghost-bot", [])
        self.assertIn(f"Ask {NAME}", warn[0])


class TestConfig(Base):
    def test_precedence_flag_env_file_default(self):
        c = pp.resolve_config(source_dir=self.repo, env={})
        self.assertEqual((c.owner, c.owner_name, c.source_repo, c.home), (OWNER, NAME, "acme/acme-prefs", self.home))
        env = {"PP_OWNER": "envowner", "PP_SOURCE_REPO": "env/repo", "PP_BOT": "beta-bot",
               "PP_HOME": str(self.tmp / "h")}
        c = pp.resolve_config(source_dir=self.repo, env=env)
        self.assertEqual((c.owner, c.source_repo, c.bot, c.home), ("envowner", "env/repo", "beta-bot", self.tmp / "h"))
        c = pp.resolve_config(source_dir=self.repo, owner="flagowner", repo="flag/repo", env=env)
        self.assertEqual((c.owner, c.source_repo), ("flagowner", "flag/repo"))
        (self.repo / "pp.config.yaml").unlink()
        run("git", "-C", self.repo, "remote", "remove", "origin")
        c = pp.resolve_config(source_dir=self.repo, env={})
        self.assertEqual((c.owner, c.source_repo, c.home.name), (None, "local", "preferences"))

    def test_owner_defaults_to_instance_field(self):
        cfgf = self.repo / "pp.config.yaml"
        d = yaml.safe_load(cfgf.read_text()); d.pop("owner"); d.pop("owner_name")
        cfgf.write_text(yaml.safe_dump(d))
        s = self.store()
        self.assertEqual((s.owner, s.owner_name), (OWNER, OWNER))

    def _standalone_toolkit(self):
        """A toolkit copy that is NOT inside an instance repo, so env vars pick the source."""
        tk = self.tmp / "standalone" / "policies-preferences"
        shutil.copytree(TOOLKIT, tk, ignore=shutil.ignore_patterns("__pycache__"))
        return tk / "bin/pp"

    def test_pp_tools_dir_legacy_alias_and_source_dir(self):
        ppbin = self._standalone_toolkit()
        r = run(ppbin, "config", "--get", "home", env=clean_env(PP_TOOLS_DIR=str(self.repo)))
        self.assertEqual(r.stdout.strip(), str(self.home))
        decoy = self.tmp / "decoy"
        pp.init_instance(decoy, "zed", ci=False)
        r = run(ppbin, "config", "--get", "owner",
                env=clean_env(PP_TOOLS_DIR=str(self.repo), PP_SOURCE_DIR=str(decoy)))
        self.assertEqual(r.stdout.strip(), "zed")  # PP_SOURCE_DIR beats the legacy alias

    def test_colocated_repo_beats_env(self):
        """A worktree's own toolkit must never write into the shared clone named by env."""
        decoy = self.tmp / "decoy"
        pp.init_instance(decoy, "zed", ci=False)
        vend = self.repo / "skills/policies-preferences/bin/pp"
        r = run(vend, "config", "--get", "owner", env=clean_env(PP_SOURCE_DIR=str(decoy), PP_TOOLS_DIR=str(decoy)))
        self.assertEqual(r.stdout.strip(), OWNER)

    def test_pp_bot_defaults_by(self):
        vend = self.repo / "skills/policies-preferences/bin/pp"
        r = run(vend, "propose", "--title", "t", "--rule", "Use short titles.", "--evidence", "said so",
                env=clean_env(PP_BOT="beta-bot"))
        self.assertIn("PREF-001 proposed", r.stdout)
        self.assertEqual(self.store().find("PREF-001")[1]["source"]["by"], "beta-bot")
        r = run(vend, "propose", "--title", "t", "--rule", "Other.", "--evidence", "e", env=clean_env(), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PP_BOT", r.stderr)


class TestSubmitGeneric(Base):
    def test_submit_from_vendored_toolkit_custom_home(self):
        remote = self.tmp / "remote.git"
        run("git", "init", "-q", "--bare", "-b", "main", remote)
        run("git", "-C", self.repo, "remote", "set-url", "origin", remote)
        run("git", "-C", self.repo, "push", "-q", "origin", "main")
        fake = self.tmp / "fakebin"; fake.mkdir()
        (fake / "gh").write_text('#!/usr/bin/env bash\necho "$@" > "$(dirname "$0")/gh-args"\necho https://example.invalid/pull/7\n')
        (fake / "gh").chmod(0o755)
        env = clean_env(PATH=f"{fake}:{os.environ['PATH']}", PP_BOT="alpha-bot")
        tk = self.repo / "skills/policies-preferences/bin"
        run(tk / "pp", "propose", "--applies-to", "coding", "--title", "t", "--rule", "Short titles.",
            "--evidence", "said so", env=env)
        out = run(tk / "pp-submit", "--title", "preferences: propose PREF-001", env=env).stdout
        self.assertIn("/pull/7", out)
        self.assertIn("pp/alpha-bot-", run("git", "-C", remote, "branch", "--list").stdout)
        self.assertEqual(len(run("git", "-C", remote, "log", "--oneline", "main").stdout.splitlines()), 1)
        self.assertIn(f"for {NAME}'s approval", (fake / "gh-args").read_text())


class TestNoOwnerNames(unittest.TestCase):
    """The toolkit must stay owner-agnostic: no instance names baked in."""
    # Built from pieces so this file doesn't trip its own check.
    BANNED = re.compile("|".join(["sha" + "wn", "sswa" + "ner", "ir" + "is-" + "rami" + "kad"]), re.I)
    # The published public toolkit repo may appear in clone URLs / PP_TOOLKIT_REPO examples only.
    PUBLISHED_REPO = "sswa" + "ner/grok-policies-preferences"

    def test_toolkit_has_no_owner_names(self):
        hits = []
        for f in sorted(TOOLKIT.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if self.BANNED.search(line.replace(self.PUBLISHED_REPO, "")):
                        hits.append(f"{f.relative_to(TOOLKIT)}:{i}: {line.strip()[:80]}")
                if self.BANNED.search(f.name.replace(self.PUBLISHED_REPO.split("/")[-1], "")):
                    hits.append(str(f.relative_to(TOOLKIT)))
        self.assertEqual(hits, [], "owner-specific names in the generic toolkit:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main(verbosity=2)
