"""Slice 299b: app-side git must not run worker-planted code (root493).

Real git in scratch repos under TMPDIR, no mocks for the vectors.
Each vector test plants a script and asserts it never ran and the
read refused (or ignored the plant). The guard test scans src with
ast: any git spawn on a worktree that bypasses the hardened path
fails, with an explicit allowlist for owner-tree call sites.
"""
import ast
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                               "..", "src"))

from openmainty import derived_fix as dfix
from openmainty import ncontain
from openmainty import runner


def _scratch_home(root):
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    empty = os.path.join(home, "empty-gitconfig")
    with open(empty, "w"):
        pass
    return home, empty


def _git_env(home, empty):
    env = dict(os.environ)
    env["HOME"] = home
    env["GIT_CONFIG_GLOBAL"] = empty
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(args, cwd, env):
    proc = subprocess.run(
        ["git"] + list(args), cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=60)
    return proc


class _GitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t299b-")
        self.home, self.empty = _scratch_home(self.tmp)
        self.env = _git_env(self.home, self.empty)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def mk_repo(self, name="repo"):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        proc = _git(["init", "-b", "main"], path, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(path, "a.txt"), "w") as fh:
            fh.write("one\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "add", "a.txt"], path, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "init"], path, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        return path

    def mk_script(self, name="hook.sh"):
        script = os.path.join(self.tmp, name)
        marker = script + ".ran"
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\ntouch %s\n" % marker)
        os.chmod(script, 0o700)
        return script, marker


class GitPointerRecordTest(_GitCase):
    def test_record_and_reprove_linked_worktree(self):
        repo = self.mk_repo()
        work = os.path.join(self.tmp, "work")
        proc = _git(["worktree", "add", "--detach", work, "HEAD"],
                    repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(work)
        self.assertEqual(rec["kind"], "file")
        got = runner.reprove_git_pointer(work, rec)
        self.assertTrue(os.path.isdir(got["gitdir"]))
        self.assertEqual(got["worktree"], os.path.realpath(work))

    def test_pointer_rewrite_refuses_before_any_spawn(self):
        repo = self.mk_repo()
        work = os.path.join(self.tmp, "work")
        proc = _git(["worktree", "add", "--detach", work, "HEAD"],
                    repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(work)
        script, marker = self.mk_script()
        fake = os.path.join(self.tmp, "fake.git")
        os.makedirs(fake, exist_ok=True)
        with open(os.path.join(fake, "config"), "w") as fh:
            fh.write("[core]\n\tfsmonitor = %s\n" % script)
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % fake)
        with self.assertRaises(dfix.DerivedFixError) as ctx:
            dfix._git(work, ["status", "--porcelain=v1", "-z"],
                      git_bound=rec)
        self.assertEqual(ctx.exception.code, "git-pointer-changed")
        self.assertFalse(os.path.exists(marker))

    def test_planted_config_in_git_dir_refuses(self):
        repo = self.mk_repo()
        rec = runner.record_git_pointer(repo)
        script, marker = self.mk_script()
        with open(os.path.join(repo, ".git", "config"), "a") as fh:
            fh.write("\n[core]\n\tfsmonitor = %s\n" % script)
        with self.assertRaises(dfix.DerivedFixError) as ctx:
            dfix._git(repo, ["status", "--porcelain=v1", "-z"],
                      git_bound=rec)
        self.assertEqual(ctx.exception.code, "git-pointer-changed")
        self.assertFalse(os.path.exists(marker))

    def test_attributes_driver_never_executes(self):
        repo = self.mk_repo()
        script, marker = self.mk_script("driver.sh")
        with open(os.path.join(repo, ".git", "config"), "a") as fh:
            fh.write("\n[diff \"evil\"]\n\tcommand = %s\n" % script)
        rec = runner.record_git_pointer(repo)
        with open(os.path.join(repo, ".gitattributes"), "w") as fh:
            fh.write("*.txt diff=evil\n")
        with open(os.path.join(repo, "a.txt"), "a") as fh:
            fh.write("two\n")
        proc = dfix._git(repo, ["diff", "HEAD", "--", "."],
                         git_bound=rec)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertFalse(os.path.exists(marker))

    def test_submodule_pointer_rewrite_refuses_before_spawn(self):
        # A populated submodule carries sub/.git, so the
        # nested walk refuses before any git spawns; no
        # child can run. (--ignore-submodules=all stays as
        # defense in depth, pinned at argv level.)
        sub = self.mk_repo("sub")
        repo = self.mk_repo("super")
        proc = _git(["-c", "protocol.file.allow=always",
                     "-c", "user.name=t", "-c", "user.email=t@t",
                     "submodule", "add", sub, "sub"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "add sub"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(repo)
        script, marker = self.mk_script()
        fake = os.path.join(self.tmp, "subfake.git")
        os.makedirs(fake, exist_ok=True)
        proc = _git(["init", "-b", "main"], fake, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(fake, "config"), "a") as fh:
            fh.write("[core]\n\tfsmonitor = %s\n" % script)
        with open(os.path.join(repo, "sub", ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % fake)
        with self.assertRaises(dfix.DerivedFixError) as ctx:
            dfix._git(repo, ["status", "--porcelain=v1", "-z"],
                      git_bound=rec)
        self.assertEqual(ctx.exception.code,
                         "git-pointer-changed")
        with self.assertRaises(dfix.DerivedFixError) as ctx:
            dfix._git(repo, ["diff", "--raw", "-z", "HEAD",
                             "--", "."],
                      git_bound=rec)
        self.assertEqual(ctx.exception.code,
                         "git-pointer-changed")
        self.assertFalse(os.path.exists(marker))

    def test_global_config_has_no_effect(self):
        repo = self.mk_repo()
        script, marker = self.mk_script()
        with open(os.path.join(self.home, ".gitconfig"), "w") as fh:
            fh.write("[core]\n\tfsmonitor = %s\n" % script)
        rec = runner.record_git_pointer(repo)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        try:
            proc = dfix._git(repo, ["status", "--porcelain=v1", "-z"],
                             git_bound=rec)
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertFalse(os.path.exists(marker))

    def test_forged_history_refused_against_record(self):
        repo = self.mk_repo()
        work = os.path.join(self.tmp, "work")
        proc = _git(["worktree", "add", "--detach", work, "HEAD"],
                    repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(work)
        other = self.mk_repo("other")
        with open(os.path.join(other, "a.txt"), "w") as fh:
            fh.write("forged\n")
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qam", "forged"], other, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        with open(os.path.join(work, ".git"), "w") as fh:
            fh.write("gitdir: %s\n" % os.path.join(other, ".git"))
        head = _git(["rev-parse", "HEAD"], repo, self.env)
        with self.assertRaises(dfix.DerivedFixError) as ctx:
            dfix._worktree_changes(
                work, head.stdout.decode().strip(), git_bound=rec)
        self.assertEqual(ctx.exception.code, "git-pointer-changed")


class NestedGitRefusalTest(_GitCase):
    """A nested .git renamed in after prepare must refuse.

    The kernel deny cannot see a directory renamed in from
    a writable area outside the workdir, so the app walk in
    reprove_git_pointer fails closed on any nested entry
    whose casefolded name is .git.
    """
    def _bound_changes(self, work, head, rec):
        with self.assertRaises(dfix.DerivedFixError) as ctx:
            dfix._worktree_changes(work, head, git_bound=rec)
        self.assertEqual(ctx.exception.code,
                         "git-pointer-changed")

    def test_planted_nested_git_dir_refuses(self):
        repo = self.mk_repo()
        head = _git(["rev-parse", "HEAD"], repo,
                    self.env).stdout.decode().strip()
        rec = runner.record_git_pointer(repo)
        os.makedirs(os.path.join(repo, "planted", ".git"))
        self._bound_changes(repo, head, rec)

    def test_planted_nested_git_casefold_refuses(self):
        repo = self.mk_repo()
        head = _git(["rev-parse", "HEAD"], repo,
                    self.env).stdout.decode().strip()
        rec = runner.record_git_pointer(repo)
        os.makedirs(os.path.join(repo, "planted", ".GIT"))
        self._bound_changes(repo, head, rec)

    def test_planted_nested_git_symlink_refuses(self):
        repo = self.mk_repo()
        head = _git(["rev-parse", "HEAD"], repo,
                    self.env).stdout.decode().strip()
        rec = runner.record_git_pointer(repo)
        os.makedirs(os.path.join(repo, "planted"))
        os.symlink("nowhere",
                   os.path.join(repo, "planted", ".git"))
        self._bound_changes(repo, head, rec)

    def test_worktree_add_leaves_no_nested_git(self):
        # No false positive: git worktree add does not
        # populate submodules, so a fresh worktree of a
        # repo with a submodule has no nested .git and the
        # bound read passes.
        sub = self.mk_repo("subsrc")
        repo = self.mk_repo("super")
        proc = _git(["-c", "protocol.file.allow=always",
                     "-c", "user.name=t", "-c", "user.email=t@t",
                     "submodule", "add", sub, "sub"], repo,
                    self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        proc = _git(["-c", "user.name=t", "-c", "user.email=t@t",
                     "commit", "-qm", "add sub"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        work = os.path.join(self.tmp, "work")
        proc = _git(["worktree", "add", "--detach", work,
                     "HEAD"], repo, self.env)
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        rec = runner.record_git_pointer(work)
        head = _git(["rev-parse", "HEAD"], repo,
                    self.env).stdout.decode().strip()
        dfix._worktree_changes(work, head, git_bound=rec)


class HardenedArgvTest(unittest.TestCase):
    def test_diff_gets_submodule_ignore_and_config(self):
        argv = runner.hardened_git_argv(
            ["diff", "--raw", "-z", "HEAD", "--", "."],
            gitdir="/g/g.git", worktree="/w/work")
        self.assertEqual(argv[0], "git")
        self.assertIn("--ignore-submodules=all", argv)
        for key in ("core.fsmonitor=false",
                    "core.hooksPath=/dev/null",
                    "core.attributesFile=/dev/null"):
            self.assertIn(key, argv)
        # Flags sit right after the verb: past "--" git would
        # read them as paths.
        verb = argv.index("diff")
        sep = argv.index("--")
        for flag in ("--ignore-submodules=all", "--no-ext-diff",
                     "--no-textconv"):
            self.assertIn(flag, argv)
            self.assertLess(verb, argv.index(flag))
            self.assertLess(argv.index(flag), sep)

    def test_rev_parse_has_no_submodule_flag(self):
        argv = runner.hardened_git_argv(["rev-parse", "HEAD"])
        self.assertNotIn("--ignore-submodules=all", argv)
        self.assertIn("core.fsmonitor=false", argv)

    def test_unbound_diff_keeps_owner_behavior(self):
        argv = runner.hardened_git_argv(["diff", "--stat"])
        self.assertNotIn("--ignore-submodules=all", argv)
        self.assertNotIn("--git-dir", argv)

    def test_bound_argv_pins_gitdir_before_verb(self):
        argv = runner.hardened_git_argv(
            ["status", "--porcelain=v1"], gitdir="/g/g.git",
            worktree="/w/work")
        verb = argv.index("status")
        self.assertLess(argv.index("--git-dir"), verb)
        self.assertLess(argv.index("--work-tree"), verb)
        self.assertEqual(argv[argv.index("--git-dir") + 1], "/g/g.git")
        self.assertEqual(argv[argv.index("--work-tree") + 1],
                         "/w/work")
        self.assertNotIn("-C", argv)

    def test_env_disables_system_and_global(self):
        env = runner.hardened_git_env(
            {"HOME": "/h", "PATH": "/p", "GIT_SSH": "/s",
             "GIT_CONFIG_GLOBAL": "/evil"})
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["HOME"], "/h")
        self.assertEqual(env["PATH"], "/p")
        self.assertNotIn("GIT_SSH", env)
        for key in env:
            self.assertFalse(
                key.startswith("GIT_")
                and key not in ("GIT_CONFIG_NOSYSTEM",
                                "GIT_CONFIG_GLOBAL",
                                "GIT_OPTIONAL_LOCKS"),
                key)


class PolicyGitDenyTest(unittest.TestCase):
    def _text(self, tmp):
        tmp = os.path.realpath(tmp)
        root = os.path.join(tmp, "root")
        os.makedirs(root, exist_ok=True)
        work = os.path.join(tmp, "ws")
        os.makedirs(work, exist_ok=True)
        text = ncontain.build_repair_broker_policy_text(
            root, "/bin/true", "/Users/qa-owner", 15441, work)
        return text, work

    def test_deny_line_present_after_allows(self):
        tmp = tempfile.mkdtemp(prefix="t299b-pol-")
        self.addCleanup(shutil.rmtree, tmp, True)
        text, work = self._text(tmp)
        lines = text.splitlines()
        deny = next(i for i, ln in enumerate(lines)
                    if ln.startswith("(deny file-write*")
                    and '(regex #"' in ln)
        allows = [i for i, ln in enumerate(lines)
                  if ln.startswith("(allow file-write*")
                  and work in ln]
        self.assertTrue(allows, text[-600:])
        for pos in allows:
            self.assertLess(pos, deny)

    def test_deny_pattern_matches_git_paths_only(self):
        import re as _re
        tmp = tempfile.mkdtemp(prefix="t299b-pol-")
        self.addCleanup(shutil.rmtree, tmp, True)
        text, work = self._text(tmp)
        line = next(ln for ln in text.splitlines()
                    if ln.startswith("(deny file-write*")
                    and '(regex #"' in ln)
        _sb, _lp, pat = line.partition('#"')
        pat = pat.rsplit('"', 1)[0]
        # No unescaping: the #"..." regex literal is read
        # raw by the kernel, so the text must match as-is.
        # (An earlier .replace("\\\\", "\\") here hid a
        # rule that denied nothing; the kernel test below
        # is the real proof.)
        rx = _re.compile(pat)
        for rel in (".git", ".git/config", ".git/hooks/x",
                    "sub/.git", "sub/.git/config",
                    ".GIT", ".GIT/config", "sub/.Git/x"):
            self.assertTrue(
                rx.match(os.path.join(work, rel)), rel)
        for rel in (".gitignore", "sub.git/x", "a.txt",
                    "sub/.github/x"):
            self.assertFalse(
                rx.match(os.path.join(work, rel)), rel)
        self.assertFalse(rx.match("/elsewhere/.git"))

    def test_proof_rejects_missing_deny(self):
        tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="t299b-pol-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "root")
        os.makedirs(root, exist_ok=True)
        work = os.path.join(tmp, "ws")
        os.makedirs(work, exist_ok=True)
        base = ncontain.build_subscription_broker_policy_text(
            root, "/bin/true", "/Users/qa-owner", 15441)
        text = ncontain.build_repair_broker_policy_text(
            root, "/bin/true", "/Users/qa-owner", 15441, work)
        extra = ncontain._repair_extra_lines(work, root)
        stripped = "\n".join(
            ln for ln in text.splitlines()
            if not ln.startswith("(deny file-write*")) + "\n"
        self.assertTrue(any(ln.startswith("(deny file-write*")
                            for ln in extra))
        with self.assertRaises(ncontain.ContainmentError):
            ncontain._prove_repair_plus(stripped, base, extra)


class KernelGitDenyTest(unittest.TestCase):
    """The deny line must actually deny under sandbox-exec.

    Uses the product's _repair_git_deny_line, not a copy.
    /bin/sh only, no model binary, no network. One fresh
    workdir per case. A rule that fails to deny FAILS; skip
    only when sandbox-exec is missing or the no-rule
    control cannot run.
    """
    SH = "/bin/sh"
    SBX = "/usr/bin/sandbox-exec"
    TIMEOUT = 60

    def _fresh_work(self):
        tmp = os.path.realpath(
            tempfile.mkdtemp(prefix="t299b-k-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        work = os.path.join(tmp, "work")
        os.mkdir(work, 0o700)
        return work

    def _profile(self, work, with_deny):
        text = "(version 1)(allow default)"
        if with_deny:
            text += ncontain._repair_git_deny_line(work)
        return text

    def _sh(self, profile, script, args, cwd):
        try:
            proc = subprocess.run(
                [self.SBX, "-p", profile, self.SH,
                 "-c", script] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, cwd=cwd,
                timeout=self.TIMEOUT)
        except OSError as exc:
            self.skipTest("sandbox-exec unavailable: %s"
                          % exc)
        except subprocess.TimeoutExpired:
            self.fail("sandboxed sh timed out")
        return proc.returncode

    def _write_file(self, path, data):
        with open(path, "wb") as fh:
            fh.write(data)

    def test_kernel_deny_blocks_git_writes(self):
        if not os.path.isfile(self.SBX) \
                or not os.path.isfile(self.SH):
            self.skipTest("sandbox-exec or sh missing")
        # No-rule control must succeed, else nothing here
        # can tell allowed from denied.
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        note = os.path.join(work, ".git", "note")
        rc = self._sh(self._profile(work, False),
                      'printf x > "$0"', [note], work)
        if rc != 0 or not os.path.isfile(note):
            self.skipTest("no-rule control cannot run: "
                          "rc=%r" % (rc,))
        deny = [
            ("new-file-in-git", self._case_new_file),
            ("overwrite-config", self._case_overwrite),
            ("truncate-config", self._case_truncate),
            ("mkdir-nested", self._case_mkdir_nested),
            ("pointer-overwrite",
             self._case_pointer_overwrite),
            ("rename-over-pointer",
             self._case_rename_over),
            ("rename-git-away", self._case_rename_away),
            ("symlink-named-git",
             self._case_symlink_named),
            ("write-through-symlink",
             self._case_through_symlink),
            ("hardlink-then-write",
             self._case_hardlink),
            ("upper-git", self._case_upper),
        ]
        for name, case in deny:
            with self.subTest(name=name):
                case()
        allow = [
            ("plain", "plain.txt"),
            ("gitignore", ".gitignore"),
        ]
        for name, rel in allow:
            with self.subTest(name=name):
                work = self._fresh_work()
                target = os.path.join(work, rel)
                rc = self._sh(
                    self._profile(work, True),
                    'printf x > "$0"', [target], work)
                self.assertEqual(rc, 0)
                self.assertTrue(os.path.isfile(target))
        with self.subTest(name="sub.git"):
            work = self._fresh_work()
            prof = self._profile(work, True)
            sub = os.path.join(work, "sub.git")
            rc = self._sh(prof, 'mkdir -p "$0"',
                           [sub], work)
            self.assertEqual(rc, 0)
            target = os.path.join(sub, "x")
            rc = self._sh(prof, 'printf x > "$0"',
                           [target], work)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(target))

    def _case_new_file(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        note = os.path.join(work, ".git", "note")
        rc = self._sh(self._profile(work, True),
                      'printf x > "$0"', [note], work)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(note))

    def _case_overwrite(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        cfg = os.path.join(work, ".git", "config")
        self._write_file(cfg, b"orig\n")
        rc = self._sh(self._profile(work, True),
                      'printf new > "$0"', [cfg], work)
        self.assertNotEqual(rc, 0)
        with open(cfg, "rb") as fh:
            self.assertEqual(fh.read(), b"orig\n")

    def _case_truncate(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        cfg = os.path.join(work, ".git", "config")
        self._write_file(cfg, b"orig\n")
        rc = self._sh(self._profile(work, True),
                      ': > "$0"', [cfg], work)
        self.assertNotEqual(rc, 0)
        with open(cfg, "rb") as fh:
            self.assertEqual(fh.read(), b"orig\n")

    def _case_mkdir_nested(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, "sub"), 0o700)
        target = os.path.join(work, "sub", ".git")
        rc = self._sh(self._profile(work, True),
                      'mkdir -p "$0"', [target], work)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(target))

    def _case_pointer_overwrite(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, "sub2"), 0o700)
        ptr = os.path.join(work, "sub2", ".git")
        self._write_file(ptr, b"gitdir: /x\n")
        rc = self._sh(self._profile(work, True),
                      'printf new > "$0"', [ptr], work)
        self.assertNotEqual(rc, 0)
        with open(ptr, "rb") as fh:
            self.assertEqual(fh.read(), b"gitdir: /x\n")

    def _case_rename_over(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, "sub2"), 0o700)
        ptr = os.path.join(work, "sub2", ".git")
        self._write_file(ptr, b"gitdir: /x\n")
        other = os.path.join(work, "other")
        self._write_file(other, b"y")
        rc = self._sh(self._profile(work, True),
                      'mv "$0" "$1"', [other, ptr], work)
        self.assertNotEqual(rc, 0)
        with open(ptr, "rb") as fh:
            self.assertEqual(fh.read(), b"gitdir: /x\n")
        self.assertTrue(os.path.isfile(other))

    def _case_rename_away(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        moved = os.path.join(work, "moved")
        rc = self._sh(self._profile(work, True),
                      'mv "$0" "$1"',
                      [os.path.join(work, ".git"), moved],
                      work)
        self.assertNotEqual(rc, 0)
        self.assertTrue(os.path.isdir(
            os.path.join(work, ".git")))
        self.assertFalse(os.path.lexists(moved))

    def _case_symlink_named(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, "sub3"), 0o700)
        link = os.path.join(work, "sub3", ".git")
        rc = self._sh(self._profile(work, True),
                      'ln -s "$1" "$0"',
                      [link, "/tmp/x"], work)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(link))

    def _case_through_symlink(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        os.symlink(".git", os.path.join(work, "redir"))
        note = os.path.join(work, "redir", "note")
        rc = self._sh(self._profile(work, True),
                      'printf x > "$0"', [note], work)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(
            os.path.join(work, ".git", "note")))

    def _case_hardlink(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".git"), 0o700)
        cfg = os.path.join(work, ".git", "config")
        self._write_file(cfg, b"orig\n")
        # The link target sits OUTSIDE .git, so the deny cannot
        # come from the target name alone: the attempt that
        # matters is aliasing a .git file from a writable path.
        # The kernel denies it at ln.
        hard = os.path.join(work, "hl")
        rc = self._sh(self._profile(work, True),
                      'ln "$0" "$1"', [cfg, hard], work)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(hard))
        with open(cfg, "rb") as fh:
            self.assertEqual(fh.read(), b"orig\n")

    def _case_upper(self):
        work = self._fresh_work()
        os.mkdir(os.path.join(work, ".GIT"), 0o700)
        note = os.path.join(work, ".GIT", "note")
        rc = self._sh(self._profile(work, True),
                      'printf x > "$0"', [note], work)
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.lexists(note))


_SPAWNERS = ("run", "Popen", "call", "check_output", "check_call",
             "run_cmd", "_run")
_BUILDERS = ("hardened_git_argv",)

# (file, qualname) allowed to spawn git outside the hardened path.
# Owner checkouts and driver flows only; every worker-touched tree
# (derived repair worktree, M2 run tree, prepared op tree) must
# route via hardened_git_argv, which the scanner treats as clean.
# 301 hardened every remaining model-tree site, so the FOLLOWUP
# entries are gone: membership now means owner-only (or
# driver-owned), and any bare spawn outside it fails the gate.
_GIT_SPAWN_ALLOW = frozenset({
    ("batches.py", "_git_head_diag"),
    ("batches.py", "_run_git"),
    ("batches.py", "_qa_git"),
    ("batches.py", "_qa_archive"),
    ("branches.py", "local_branches"),
    ("branches.py", "probe_next"),
    ("branches.py", "remote_next_exists"),
    ("branches.py", "_remote_ref_exists"),
    ("branches.py", "ensure_next"),
    ("branches.py", "_next_checked_out"),
    ("branches.py", "maybe_fast_forward_next"),
    ("chat.py", "delete"),
    ("chat.py", "ensure_workdir"),
    ("gc.py", "_git_rev_parse"),
    ("graph.py", "_local_ref_branches"),
    ("issue_recovery.py", "_git"),
    ("manifests.py", "lock_provenance"),
    ("repos.py", "_pub_lock_state._git_ok"),
    ("runner.py", "ensure_branch_workdir"),
    ("runner.py", "git_rev"),
    ("runner.py", "git_rev_strict"),
    ("runner.py", "attr_source_supported"),
    ("runner.py", "_git_config_origins"),
    ("sched.py", "_strict_rev_parse"),
    ("sched.py", "_fetch_remote_refs"),
    ("sched.py", "_resolve_main_base"),
    ("sched.py", "_fresh_next_base"),
    ("sched.py", "_next_divergence_note"),
    ("sched.py", "_dep_diff_evidence"),
    ("serve.py", "_git"),
    ("serve.py", "_clone_repo"),
    ("serve.py", "_git_run"),
    ("util.py", "_private_index_copy"),
    # 301 hardened (bound, pinned, scrubbed) — intentionally NOT
    # allowlisted, so the gate fails if a bare spawn returns:
    # batches._git_text, batches._git_lines,
    # fastcheck._git_tracked_files, fastcheck._podfile_pins_by_file,
    # fastcheck._git_dirs, prs.work_diffstat, serve._git_text,
    # runner.work_commit, runner._candidate_path_set,
    # runner.maybe_commit_worktree, runner._head_file_hashes,
    # runner._candidate_already_committed, runner.snapshot_diff,
    # runner._worktree_changed, runner.review_scope_artifact,
    # runner._draft_context.
    # 302 adds runner.attr_source_supported: a --version-only
    # capability probe (no driver-capable verb); allowlisted so
    # the gate stays green.
})


def _scan_git_spawns(src_dir):
    """{records, nonconst}: git spawns per function in src_dir."""
    import glob as _glob
    records, nonconst = set(), set()
    for path in sorted(_glob.glob(os.path.join(src_dir, "*.py"))):
        name = os.path.basename(path)
        try:
            with open(path) as handle:
                tree = ast.parse(handle.read(), path)
        except (OSError, SyntaxError):
            nonconst.add((name, "<unparseable>"))
            continue
        mod_tainted = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and any(
                            isinstance(c, ast.Constant)
                            and c.value == "git"
                            for c in ast.walk(node.value)):
                        mod_tainted.add(tgt.id)

        def _own_calls(fn):
            nested = set()
            for child in ast.walk(fn):
                if child is not fn and isinstance(
                        child, (ast.FunctionDef,
                                ast.AsyncFunctionDef,
                                ast.ClassDef)):
                    nested.add(child)
            skip = set()
            for sub in nested:
                skip.update(ast.walk(sub))
            return [c for c in ast.walk(fn)
                    if isinstance(c, ast.Call) and c not in skip]

        def _own_assigns(fn):
            nested = set()
            for child in ast.walk(fn):
                if child is not fn and isinstance(
                        child, (ast.FunctionDef,
                                ast.AsyncFunctionDef,
                                ast.ClassDef)):
                    nested.add(child)
            skip = set()
            for sub in nested:
                skip.update(ast.walk(sub))
            return [c for c in ast.walk(fn)
                    if isinstance(c, (ast.Assign, ast.AnnAssign))
                    and c not in skip]

        stack = []

        class _V(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                stack.append(node.name)
                _check(node, ".".join(stack))
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node):
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

        def _check(fn, qual):
            clean, tainted = set(), set(mod_tainted)
            for asn in _own_assigns(fn):
                tgts = asn.targets if isinstance(
                    asn, ast.Assign) else [asn.target]
                for tgt in tgts:
                    if not isinstance(tgt, ast.Name):
                        continue
                    val = asn.value
                    if val is None:
                        continue
                    bname = None
                    if isinstance(val, ast.Call):
                        if isinstance(val.func, ast.Name):
                            bname = val.func.id
                        elif isinstance(val.func, ast.Attribute):
                            bname = val.func.attr
                    if bname in _BUILDERS:
                        clean.add(tgt.id)
                    elif any(isinstance(c, ast.Constant)
                             and c.value == "git"
                             for c in ast.walk(val)):
                        tainted.add(tgt.id)
            for call in _own_calls(fn):
                func = call.func
                fname = func.attr if isinstance(
                    func, ast.Attribute) else (
                        func.id if isinstance(func, ast.Name)
                        else "?")
                if fname not in _SPAWNERS:
                    continue
                consts = [c.value for c in ast.walk(call)
                          if isinstance(c, ast.Constant)
                          and isinstance(c.value, str)]
                used = [x.id for x in ast.walk(call)
                        if isinstance(x, ast.Name)]
                if "git" in consts or \
                        any(u in tainted for u in used):
                    records.add((name, qual))
                elif not consts and "git" in qual.lower() and \
                        not any(u in clean for u in used):
                    nonconst.add((name, qual))
        for node in tree.body:
            _V().visit(node)
    return records, nonconst


class GitSpawnGuardTest(unittest.TestCase):
    def test_no_bypass_outside_allowlist(self):
        import openmainty as _om
        src = os.path.dirname(os.path.abspath(_om.__file__))
        records, nonconst = _scan_git_spawns(src)
        self.assertEqual(nonconst, set())
        extra = records - _GIT_SPAWN_ALLOW
        self.assertEqual(extra, set())

    def test_scanner_flags_raw_and_nonconst(self):
        tmp = tempfile.mkdtemp(prefix="t299b-scan-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a.py"), "w") as fh:
            fh.write("import subprocess\n"
                     "def raw():\n"
                     "    return subprocess.run([\"git\", \"status\"])\n"
                     "def git_passthrough(argv):\n"
                     "    return subprocess.run(argv)\n")
        records, nonconst = _scan_git_spawns(tmp)
        self.assertIn(("a.py", "raw"), records)
        self.assertIn(("a.py", "git_passthrough"), nonconst)

    def test_scanner_accepts_builder_path(self):
        tmp = tempfile.mkdtemp(prefix="t299b-scan-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "b.py"), "w") as fh:
            fh.write("import subprocess\n"
                     "def ok():\n"
                     "    argv = hardened_git_argv([\"status\"])\n"
                     "    return subprocess.run(argv)\n")
        records, nonconst = _scan_git_spawns(tmp)
        self.assertEqual(records, set())
        self.assertEqual(nonconst, set())
