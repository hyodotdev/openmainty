"""Offline native-containment controls (TEST ONLY).

This Mac refuses nested sandbox-exec init (sandbox_apply EPERM even
for harmless parent->child policy runs), so NO test here execs
sandbox-exec itself: that would nest one wrapper inside another and
prove nothing. Instead each control child is spawned DIRECTLY, and
Root's reviewed harness applies EXACTLY ONE native wrapper per
child: baseline children under Root's baseline policy, product
control children under the exact product policy built below.
Without the harness attestation (NC_NATIVE_WRAPPER_ACTIVE=1) the
live controls FAIL CLOSED — never unfenced, never skipped-as-pass,
never negative-only acceptance.

Per-control env markers consumed ONLY by the reviewed harness:
NC_CONTROL_KIND=baseline|product, NC_PRODUCT_POLICY=<built policy
path>. They are routing labels, not admission: the pass/fail signal
is the kernel errno observed in the child.

Parent baselines (owned test-root I/O, /bin/echo, one owned
loopback fixture) must PASS; the same operations under the product
policy must behave as labeled: owned child writes and reads PASS
(the wrapper actually runs, not all-blocked), while
outside-childroot, fixture-private-home, Data-alias, TCP-connect,
and other-exec attempts fail with kernel EPERM/EACCES. Requires
NC_TEST_ROOT plus HOME/OPENMAINTY_HOME/TMPDIR all inside it,
otherwise fails closed. Nothing here spawns a model, touches
product data, real credentials, external network, actual user-home
contents, or target repos. Artifacts stay under the owned test
root; nothing outside it is cleaned, signalled, or written. The
private-home case is an owned fixture, honestly labeled — not proof
about the actual user home (Root reviews real-home coverage
separately). Inert children run with -I -B -S to avoid site/.pth
startup hooks.
"""
import ctypes
import hashlib
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import ncontain

ALLOWED_ERRNOS = {1, 13}  # EPERM / EACCES: kernel denial.

PROBE_LINES = [
    "import os",
    "import sys",
    "import socket",
    "import subprocess",
    "which = sys.argv[1]",
    "try:",
    "    if which == 'write-allowed':",
    "        open(os.path.join(os.environ['CHILDROOT'],"
    " 'ok.txt'), 'w').write('ok')",
    "        print('ALLOWED-OK')",
    "    elif which == 'read-allowed':",
    "        print('READ-DATA:' + open(os.path.join(",
    "        os.environ['CHILDROOT'], 'ok.txt')).read())",
    "    elif which == 'write-outside':",
    "        open(os.environ['OUTSIDE'], 'w').write('x')",
    "        print('OUTSIDE-WROTE')",
    "    elif which == 'read-outside':",
    "        open(os.environ['OUTSIDE']).read()",
    "        print('READ-OUTSIDE')",
    "    elif which == 'read-outside-fd':",
    "        dfd = os.open(os.environ['CHILDROOT'], os.O_RDONLY)",
    "        name = '../' + os.path.basename(os.environ['OUTSIDE'])",
    "        fd = os.open(name, os.O_RDONLY, dir_fd=dfd)",
    "        print('READ-OUTSIDE-FD:' + os.fdopen(fd).read())",
    "        os.close(dfd)",
    "    elif which == 'stat-parent':",
    "        st = os.stat(os.path.dirname(os.environ['CHILDROOT']))",
    "        print('STAT-OK dev=%s ino=%s' % (st.st_dev,",
    "              st.st_ino))",
    "    elif which == 'listdir-parent':",
    "        print(os.listdir(os.path.dirname(",
    "              os.environ['CHILDROOT'])))",
    "    elif which == 'read-exact':",
    "        print('READ-EXACT:' + open(",
    "              os.environ['READPATH']).read())",
    "    elif which == 'read-allowed-fd':",
    "        dfd = os.open(os.environ['CHILDROOT'], os.O_RDONLY)",
    "        fd = os.open('ok.txt', os.O_RDONLY, dir_fd=dfd)",
    "        print('READ-FD:' + os.fdopen(fd).read())",
    "        os.close(dfd)",
    "    elif which == 'read-privhome':",
    "        open(os.path.join(os.environ['PRIVHOME'],",
    "             'secret.txt')).read()",
    "        print('READ-PRIVHOME')",
    "    elif which == 'tcp':",
    "        socket.create_connection((",
    "            '127.0.0.1', int(os.environ['FIXPORT'])),",
    "            timeout=5)",
    "        print('TCP-OK')",
    "    elif which == 'exec-echo':",
    "        subprocess.run(['/bin/echo', 'hi'], check=True,",
    "                       timeout=10, capture_output=True)",
    "        print('EXEC-OK')",
    "    elif which == 'exec-sandbox':",
    "        os.execv('/usr/bin/sandbox-exec',",
    "                 ['sandbox-exec', '-f', '/dev/null',",
    "                  '/bin/echo', 'hi'])",
    "        print('SANDBOX-OK')",
    "except OSError as e:",
    "    print('DENIED errno=%s' % e.errno)",
]
PROBE = "\n".join(PROBE_LINES)


def _req(name):
    value = os.environ.get(name)
    if not value or not os.path.isabs(value):
        raise AssertionError(
            "fenced env %s must be absolute; refusing to run "
            "unfenced" % name)
    return value


class OfflineContainmentCase(unittest.TestCase):
    baseline = {}

    @classmethod
    def setUpClass(cls):
        if sys.platform != "darwin":
            raise unittest.SkipTest("macOS sandbox-exec only")
        cls.root = os.path.realpath(_req("NC_TEST_ROOT"))
        if os.path.islink(os.environ["NC_TEST_ROOT"]):
            raise AssertionError("test root must not symlink")
        cls.home = os.path.realpath(_req("HOME"))
        cls.omhome = os.path.realpath(_req("OPENMAINTY_HOME"))
        cls.tmp = os.path.realpath(_req("TMPDIR"))
        for path in (cls.home, cls.omhome, cls.tmp):
            if path != cls.root and not path.startswith(
                    cls.root + os.sep):
                raise AssertionError(
                    "fenced env escapes test root")
        # Test-only routing flag: the reviewed harness sets it when
        # it applies exactly one native wrapper per product child.
        # It is NOT admission; without it all controls fail closed.
        cls.harness = (os.environ.get(
            "NC_NATIVE_WRAPPER_ACTIVE") == "1")
        cls.childroot = os.path.join(cls.root, "childroot")
        os.makedirs(cls.childroot, mode=0o700, exist_ok=True)
        cls.outside = os.path.join(cls.root, "outside-canary.txt")
        cls.privhome = os.path.join(cls.root, "privhome")
        os.makedirs(cls.privhome, mode=0o700, exist_ok=True)
        # Actual OS interpreter image of THIS process via the public
        # libproc API (own PID only) — never silently rebound, never
        # a guess. sys.executable is reported separately and is NOT
        # the pin source. Never called a Muse worker.
        try:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib",
                               use_errno=True)
            libc.proc_pidpath.argtypes = [ctypes.c_int,
                                          ctypes.c_char_p,
                                          ctypes.c_uint32]
            libc.proc_pidpath.restype = ctypes.c_int
            image_buf = ctypes.create_string_buffer(1024)
            got = libc.proc_pidpath(os.getpid(), image_buf, 1024)
        except OSError as exc:
            raise AssertionError(
                "proc_pidpath unavailable: %s" % exc)
        if got <= 0:
            raise AssertionError(
                "proc_pidpath refused; failing closed, no guessed image")
        cls.reported_executable = sys.executable
        cls.os_image = image_buf.value.decode()
        exe = os.path.realpath(cls.os_image)
        if not exe or not os.path.isabs(exe):
            raise AssertionError("need absolute test python image")
        with open(exe, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        cls.python = ncontain.pin_executable(exe, digest)
        # Explicit runtime tree of THIS interpreter install: must
        # contain the actual image, be a real system location, never
        # the policy root, the account home, or an arbitrary fixture
        # path; a user-local interpreter fails loudly by design.
        cls.runtime_roots = [os.path.realpath(sys.base_prefix)]
        if (exe != cls.runtime_roots[0]
                and not exe.startswith(
                    cls.runtime_roots[0] + os.sep)):
            raise AssertionError(
                "OS image outside its base prefix: %r vs %r"
                % (exe, cls.runtime_roots[0]))
        cls.policy = ncontain.create_offline_policy(
            cls.childroot, cls.python,
            runtime_read_roots=cls.runtime_roots)
        # Spec of the exact native wrapper; the reviewed
        # harness instantiates it once per product child.
        cls.native_argv = ncontain.wrapper_argv(
            cls.policy, cls.python["path"])

    @classmethod
    def tearDownClass(cls):
        sys.stdout.write(json.dumps(
            {"scope": "offline containment controls",
             "baselines": dict(cls.baseline),
             "policy": cls.policy.get("policy_sha256")
             if hasattr(cls, "policy") else None,
             "os_image": getattr(cls, "os_image", None),
             "reported_sys_executable": getattr(
                 cls, "reported_executable", None),
             "harness_wrapped": getattr(cls, "harness",
                                        False)}) + "\n")

    def _require_harness(self):
        if not self.harness:
            self.fail(
                "native wrapper attestation missing "
                "(NC_NATIVE_WRAPPER_ACTIVE!=1): refusing to run "
                "controls unfenced; not a pass, not a skip")

    def _observe(self, label, kind, proc):
        obs = {"control": label, "kind": kind,
               "rc": proc.returncode,
               "stdout": proc.stdout.strip()[-500:],
               "stderr": proc.stderr.strip()[-500:]}
        sys.stdout.write(json.dumps(obs, sort_keys=True) + "\n")
        return obs

    # ---- parent baselines (must PASS) ----
    def test_baseline_outside_canary_rw(self):
        with open(self.outside, "w") as fh:
            fh.write("parent")
        with open(self.outside) as fh:
            self.assertEqual(fh.read(), "parent")
        type(self).baseline["outside_canary_rw"] = "parent-ok"

    def test_baseline_tcp_loopback(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,
                           1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            cli = socket.create_connection(("127.0.0.1", port),
                                           timeout=10)
            cli.close()
        finally:
            srv.close()
        type(self).baseline["tcp_loopback"] = "parent-ok"

    def test_baseline_bin_echo(self):
        proc = subprocess.run(["/bin/echo", "hi"],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30,
                              text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "hi")
        type(self).baseline["bin_echo"] = "parent-ok"

    # ---- product-wrapped child controls (harness-wrapped) ----
    def _child(self, op, fixport=None, outside=None, exact=None):
        self._require_harness()
        env = {"PATH": "/usr/bin:/bin",
               "HOME": self.home, "TMPDIR": self.tmp,
               "OPENMAINTY_HOME": self.omhome,
               "CHILDROOT": self.childroot,
               "OUTSIDE": outside or self.outside,
               "PRIVHOME": self.privhome,
               "READPATH": exact or "",
               "NC_CONTROL_KIND": "product",
               "NC_PRODUCT_POLICY":
                   self.policy["policy_path"]}
        if fixport is not None:
            env["FIXPORT"] = str(fixport)
        self.assertNotIn("META_API_KEY", env)
        self.assertFalse([k for k in env
                          if k.startswith(("LD_", "DYLD_"))])
        # Direct spawn (-I -B -S: no site/.pth hooks): the
        # reviewed harness applies exactly one native wrapper
        # (this product policy) to this child. This file never
        # execs sandbox-exec itself (nesting is refused by
        # the OS).
        proc = subprocess.run(
            [self.python["path"], "-I", "-B", "-S", "-c",
             PROBE, op],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, text=True,
            cwd=self.childroot, env=env)
        return self._observe(op, "product", proc), proc

    def test_child_write_and_read_pass(self):
        obs, proc = self._child("write-allowed")
        self.assertEqual(proc.returncode, 0, obs)
        self.assertIn("ALLOWED-OK", proc.stdout)
        with open(os.path.join(self.childroot, "ok.txt")) as fh:
            self.assertEqual(fh.read(), "ok")
        obs, proc = self._child("read-allowed")
        self.assertEqual(proc.returncode, 0, obs)
        self.assertIn("READ-DATA:ok", proc.stdout)
        obs, proc = self._child("read-allowed-fd")
        self.assertEqual(proc.returncode, 0, obs)
        self.assertIn("READ-FD:ok", proc.stdout)

    def _denied(self, op, fixport=None, outside=None):
        obs, proc = self._child(op, fixport=fixport,
                                outside=outside)
        self.assertEqual(proc.returncode, 0, obs)
        line = proc.stdout.strip()
        match = re.match(r"^DENIED errno=(\d+)$", line)
        self.assertIsNotNone(match, obs)
        self.assertIn(int(match.group(1)), ALLOWED_ERRNOS, obs)

    def test_child_outside_write_denied_by_kernel(self):
        if os.path.lexists(self.outside):
            os.unlink(self.outside)
        self._denied("write-outside")
        self.assertFalse(os.path.lexists(self.outside))

    def test_child_outside_read_denied_by_kernel(self):
        with open(self.outside, "w") as fh:
            fh.write("parent")
        self._denied("read-outside")

    def test_child_private_home_fixture_denied(self):
        # Owned private-home fixture (NOT the actual user home):
        # parent baseline reads it, product child must be denied.
        # This proves fixture isolation only; real-home coverage is
        # Root's separate review, never claimed from this fixture.
        secret = os.path.join(self.privhome, "secret.txt")
        with open(secret, "w") as fh:
            fh.write("fixture-secret")
        with open(secret) as fh:
            self.assertEqual(fh.read(), "fixture-secret")
        type(self).baseline["private_home_fixture"] = "parent-ok"
        self._denied("read-privhome")
        self.assertTrue(os.path.lexists(secret))

    def test_child_data_alias_outside_read_denied(self):
        # Same owned canary reached through the /System/Volumes/Data
        # alias: Data is excluded from the generic /System scope, so
        # this alias path must be denied too.
        with open(self.outside, "w") as fh:
            fh.write("parent")
        if not self.outside.startswith("/Volumes/"):
            self.skipTest("fixture not on /Volumes; no Data alias")
        alias = "/System/Volumes/Data" + self.outside
        if not os.path.lexists(alias):
            self.skipTest("no Data-volume alias on this host")
        alias_st = os.stat(alias)
        plain_st = os.stat(self.outside)
        self.assertEqual((alias_st.st_dev, alias_st.st_ino),
                         (plain_st.st_dev, plain_st.st_ino),
                         "alias and canonical path must be the same file")
        self._denied("read-outside", outside=alias)

    def test_child_outside_fd_read_denied_by_kernel(self):
        # A childroot-fd-relative open of ../outside-canary.txt must
        # not bypass the outside denial.
        with open(self.outside, "w") as fh:
            fh.write("parent")
        self._denied("read-outside-fd")

    def test_child_parent_metadata_stat_ok_listdir_denied(self):
        # Exact-parent metadata is allowed (realpath resolution);
        # listing the same parent (data) is denied. Pending
        # independent kernel execution; this names the rule.
        parent_st = os.stat(self.root)
        obs, proc = self._child("stat-parent")
        self.assertEqual(proc.returncode, 0, obs)
        match = re.match(r"^STAT-OK dev=(\d+) ino=(\d+)$",
                         proc.stdout.strip())
        self.assertIsNotNone(match, obs)
        self.assertEqual((int(match.group(1)),
                          int(match.group(2))),
                         (parent_st.st_dev, parent_st.st_ino))
        self._denied("listdir-parent")

    def test_child_owned_inside_via_data_alias_allowed(self):
        # Owned childroot canary read through its Data-volume alias:
        # explicit owned scope must not be accidentally denied by the
        # generic System exclusion. Same inode only, exact content.
        if not self.childroot.startswith("/Volumes/"):
            self.skipTest("fixture not on /Volumes; no Data alias")
        alias_root = "/System/Volumes/Data" + self.childroot
        if not os.path.isdir(alias_root):
            self.skipTest("no Data-volume alias on this host")
        canary = os.path.join(self.childroot, "alias-ok.txt")
        with open(canary, "w") as fh:
            fh.write("alias-ok")
        alias_canary = os.path.join(alias_root, "alias-ok.txt")
        alias_st = os.stat(alias_canary)
        plain_st = os.stat(canary)
        self.assertEqual((alias_st.st_dev, alias_st.st_ino),
                         (plain_st.st_dev, plain_st.st_ino),
                         "alias and canonical path must be the same file")
        obs, proc = self._child("read-exact", exact=alias_canary)
        self.assertEqual(proc.returncode, 0, obs)
        self.assertEqual(proc.stdout.strip(), "READ-EXACT:alias-ok")

    def test_child_tcp_denied_by_kernel_not_refused(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,
                           1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            self._denied("tcp",
                         fixport=srv.getsockname()[1])
        finally:
            srv.close()

    def test_child_other_exec_denied_by_kernel(self):
        self._denied("exec-echo")

    def test_child_cannot_exec_sandbox(self):
        self._denied("exec-sandbox")

    def test_pin_rejects_symlink_drift_and_wrong_sha(self):
        link = os.path.join(self.root, "exe-link")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(self.python["path"], link)
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.pin_executable(link,
                                    self.python["sha256"])
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.pin_executable(self.python["path"],
                                    "0" * 64)
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.pin_executable(self.childroot,
                                    "0" * 64)
        plain = os.path.join(self.root, "not-executable.txt")
        with open(plain, "w") as fh:
            fh.write("x")
        os.chmod(plain, 0o600)
        with self.assertRaises(ncontain.ContainmentError):
            import hashlib as _hl
            with open(plain, "rb") as fh:
                digest = _hl.sha256(fh.read()).hexdigest()
            ncontain.pin_executable(plain, digest)

    def test_policy_quotes_and_content(self):
        quoted = ncontain._sb_literal('a"b\\c')
        self.assertEqual(quoted, '"a\\"b\\\\c"')
        with self.assertRaises(ncontain.ContainmentError):
            ncontain._sb_literal("a\x00b")
        with open(self.policy["policy_path"]) as fh:
            text = fh.read()
        for marker in ("(allow default)",
                       "(deny network*)",
                       "(deny mach-lookup",
                       "(deny file-read*",
                       "(deny process-exec",
                       "(require-any ",
                       '(literal "/")',
                       "(require-all "
                       '(subpath "/System")',
                       "(require-not "
                       '(subpath "/System/Volumes/Data"))',
                       "(allow file-read-metadata ",
                       self.python["path"]):
            self.assertIn(marker, text)
        self.assertNotIn("verified", text)
        # Data is excluded only from the generic /System scope, not
        # via a blanket deny that would also refuse validated
        # owned/runtime inputs reached through an alias.
        self.assertNotIn(
            "(deny file-read* "
            '(subpath "/System/Volumes/Data"))', text)
        # No broad user-home allowlist scope; the exact pinned
        # binary literal is the only user-tree exception allowed.
        self.assertNotIn('(subpath "/Users', text)
        # The OS root entry is an exact literal, never a subtree.
        self.assertNotIn('(subpath "/"', text)
        # Device nodes are read literals, never path subtrees.
        for dev in ("/dev/null", "/dev/random", "/dev/urandom"):
            self.assertIn('(literal "%s")' % dev, text)
        self.assertNotIn('(subpath "/dev', text)

    def test_ancestor_literals_are_exact_parents_only(self):
        got = ncontain._ancestor_literals(
            ["/Volumes/SSD/Temp/Codex/x/childroot"])
        self.assertEqual(
            got, ["/Volumes/SSD/Temp/Codex/x",
                  "/Volumes/SSD/Temp/Codex",
                  "/Volumes/SSD/Temp", "/Volumes/SSD",
                  "/Volumes", "/"])

    def test_runtime_roots_reject_broad_and_fixture_scopes(self):
        # Whole buckets, ancestors of the fixtures, the account
        # home, and fixture paths themselves are all refused as
        # runtime roots — never an implicit user-data scope.
        home = os.path.realpath(
            pwd.getpwuid(os.getuid()).pw_dir)
        for candidate in ("/", "/private", "/Users",
                          "/Library", "/Applications",
                          "/opt/homebrew", home,
                          self.root, self.childroot):
            with self.assertRaises(
                    ncontain.ContainmentError,
                    msg="runtime root accepted: %s" % candidate):
                ncontain._check_runtime_roots(
                    [candidate], self.childroot)
        # The actual interpreter runtime is accepted.
        real = os.path.realpath(sys.base_prefix)
        self.assertEqual(
            ncontain._check_runtime_roots([real],
                                          self.childroot),
            [real])

    def _make_mini_policy(self, name):
        proot = os.path.join(self.root, name)
        os.makedirs(proot, mode=0o700, exist_ok=True)
        mini = os.path.join(proot, "mini-exe")
        with open(mini, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(mini, 0o755)
        import hashlib as _hl
        with open(mini, "rb") as fh:
            digest = _hl.sha256(fh.read()).hexdigest()
        pin = ncontain.pin_executable(mini, digest)
        pol = ncontain.create_offline_policy(
            proot, pin, runtime_read_roots=self.runtime_roots)
        argv = ncontain.wrapper_argv(pol, mini)
        self.assertEqual(argv[:3],
                         ["/usr/bin/sandbox-exec", "-f",
                          pol["policy_path"]])
        return proot, mini, pol

    def test_wrapper_argv_refuses_exe_drift(self):
        _proot, mini, pol = self._make_mini_policy("reval-exe")
        with open(mini, "a") as fh:
            fh.write("# drift\n")
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.wrapper_argv(pol, mini)

    def test_wrapper_argv_refuses_policy_drift(self):
        _proot, mini, pol = self._make_mini_policy(
            "reval-policy")
        with open(pol["policy_path"], "a") as fh:
            fh.write("# drift\n")
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.wrapper_argv(pol, mini)

    def test_wrapper_argv_refuses_root_mismatch(self):
        _proot, mini, pol = self._make_mini_policy(
            "reval-root")
        tampered = dict(pol)
        tampered["root_dev"] = -1
        with self.assertRaises(ncontain.ContainmentError):
            ncontain.wrapper_argv(tampered, mini)

    def test_no_worker_admission_claim(self):
        self.assertFalse(self.policy["admits_worker"])
        self.assertTrue(self.policy["offline_only"])
        self.assertEqual(self.native_argv[0],
                         "/usr/bin/sandbox-exec")
        self.assertEqual(self.native_argv[1:3],
                         ["-f", self.policy["policy_path"]])
        self.assertEqual(self.native_argv[3],
                         self.python["path"])
