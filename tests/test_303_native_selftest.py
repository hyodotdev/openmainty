"""Slice 303: native self-test before the repair spawn.

exec_source_repair must run controls under the attempt's own wrapper
argv and policy file -- before the model child starts -- and refuse
125/native-selftest-failed when any control gives the wrong answer.
No model request can happen during the self-test because the only
processes it spawns are the wrapper argv itself (plus the one
unwrapped /usr/bin/true baseline), and the model child is spawned
after the self-test returns passed.

Unit tests use fixture wrapper/exe scripts (a fake sandbox-exec that
pins the exe like the real one: exit 71 for anything else). The
real-kernel test at the bottom runs the product's own self-test path
under real sandbox-exec on a linked worktree fixture; it skips with
an explicit pin-drift reason when resolve_pinned_muse() refuses, and
never bypasses the pin.

Child envs are minimal dicts (PATH/HOME/TMPDIR), never dict(os.environ),
so a failure can never print inherited secrets. No test prints an env
mapping: assertions use assertTrue/assertEqual with a message.
"""

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openmainty import mspbroker as mspbroker_mod  # noqa: E402
from openmainty import muse as muse_mod  # noqa: E402
from openmainty import preflight as pf  # noqa: E402

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _minimal_env(home):
    return {
        "PATH": os.pathsep.join(("/usr/bin", "/bin", "/usr/sbin", "/sbin")),
        "HOME": home,
        "TMPDIR": home,
    }


_SANDBOX_FAKE = """#!/usr/bin/python3
import json, os, sys
MODE_PATH = {mode_path!r}
LOG_PATH = {log_path!r}
EXPECTED_EXE = {expected_exe!r}
argv = sys.argv[1:]
mode = open(MODE_PATH).read().strip() if os.path.exists(MODE_PATH) else "ok"
with open(LOG_PATH, "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
exe = argv[2] if len(argv) > 2 else ""
rest = argv[3:]
if exe == "/usr/bin/true":
    if mode != "true-allow":
        sys.exit(71)
elif exe != EXPECTED_EXE:
    if mode != "copy-allow":
        sys.exit(71)
os.execvp(exe, [exe] + rest)
"""

_EXE_FAKE = """#!/usr/bin/python3
import os, sys, time
MODE_PATH = {mode_path!r}
a = sys.argv[1:]
mode = open(MODE_PATH).read().strip() if os.path.exists(MODE_PATH) else "ok"


def _write_tree(out):
    os.makedirs(out, exist_ok=True)
    for name in ("a.json", "b.json"):
        with open(os.path.join(out, name), "w") as fh:
            fh.write("{{}}\\n")


if a == ["--help"]:
    if mode == "help-fail":
        sys.exit(2)
    print("offline help")
    sys.exit(0)
if (len(a) >= 4 and a[0] == "schema"
        and a[1] == "generate-json-schema" and a[2] == "--out"):
    out = a[3]
    is_git = "/.git/" in out
    is_outside = "selftest-outside-" in out
    is_5a = "/.om-selftest-" in out and out.endswith("/ctl")
    if mode == "hang-export":
        time.sleep(30)
    if mode == "git-clean-zero" and is_git:
        sys.exit(0)
    if mode == "symlink-git" and is_git:
        d = os.path.dirname(os.path.dirname(out))
        os.makedirs(d, exist_ok=True)
        try:
            os.symlink("ctl", os.path.join(d, "other"))
        except FileExistsError:
            pass
        sys.exit(1)
    if mode == "git-sneak" and is_git:
        _write_tree(out)
        sys.exit(1)
    if mode == "readonly-d" and is_git:
        d = os.path.dirname(os.path.dirname(out))
        os.chmod(os.path.join(d, "ctl"), 0o555)
        os.chmod(d, 0o555)
    if is_git and mode != "git-allow":
        sys.exit(1)
    if mode == "outside-sneak" and is_outside:
        _write_tree(out)
        sys.exit(1)
    if is_outside and mode != "outside-allow":
        sys.exit(1)
    if mode == "export-fail":
        sys.exit(1)
    if mode == "fail-5a-only" and is_5a:
        sys.exit(1)
    if mode == "fail-5a-empty" and is_5a:
        sys.exit(0)
    if mode == "inside-empty" and "native-selftest-exports" in out:
        sys.exit(0)
    _write_tree(out)
    if mode == "litter" and is_5a:
        workdir = os.path.dirname(os.path.dirname(out))
        victim = os.path.join(workdir, "litter")
        os.makedirs(victim, exist_ok=True)
        with open(os.path.join(victim, "x"), "w") as fh:
            fh.write("litter\\n")
    sys.exit(0)
sys.exit(0)
"""


class _EmulatedProc(object):
    """Emulate a correctly-behaving kernel+policy for the real argv.

    Unwrapped /usr/bin/true exits 0; anything else named true
    through a wrapper is denied 71 (the policy pins its own
    executable); --help exits 0; schema exports create their
    target files except under .git/ or selftest-outside-, which
    are denied barren. No real spawn happens.
    """

    def __init__(self, code):
        self._code = code
        self.pid = 4194311
        self.stdin = None
        self.returncode = code

    def communicate(self, timeout=None):
        _ = timeout
        return (b"", b"")

    def kill(self):
        return None

    def wait(self, timeout=None):
        _ = timeout
        return self._code

    def poll(self):
        return self._code


def _emulated_spawn(argv, env, cwd):
    _ = (env, cwd)
    tail = argv[-1] if argv else ""
    if tail == "/usr/bin/true" and len(argv) == 1:
        return _EmulatedProc(0)
    if tail == "/usr/bin/true":
        return _EmulatedProc(71)
    if tail == "--help":
        if len(argv) >= 2 and ".om-selftest-" in argv[-2]:
            # A copied binary is not the pinned path: denied.
            return _EmulatedProc(71)
        return _EmulatedProc(0)
    if len(argv) >= 2 and argv[-2] == "--out":
        out = tail
        if "/.git/" in out or "selftest-outside-" in out:
            return _EmulatedProc(1)
        os.makedirs(out, exist_ok=True)
        for name in ("a.json", "b.json"):
            with open(os.path.join(out, name), "w") as fh:
                fh.write("{}\n")
        return _EmulatedProc(0)
    return _EmulatedProc(0)


class _FakeBroker(object):
    instances = []

    def __init__(self, *args, **kwargs):
        self.scripted_log = []
        self.stopped = False
        self._allow = (("api.meta.ai", 443),)
        type(self).instances.append(self)

    def start(self):
        return 19999

    def stop(self):
        self.stopped = True
        return {"stopped": True, "reason": "test-stop"}

    def log(self):
        return [dict(e) for e in self.scripted_log]

    def is_alive(self):
        return not self.stopped


def _control_shaped(argv):
    """True when a logged wrapper invocation is a self-test control."""
    if not argv:
        return False
    tail = argv[-1]
    if tail == "/usr/bin/true":
        return True
    if tail == "--help":
        return True
    if len(argv) >= 2 and argv[-2] == "--out":
        return ("native-selftest-exports" in tail
                or "selftest-outside-" in tail
                or ".om-selftest-" in tail)
    return False


class SelftestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="muse303-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = _minimal_env(self.home)
        popped = {}
        for key in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE",
                    "OPENMAINTY_MUSE_BINARY"):
            if key in os.environ:
                popped[key] = os.environ.pop(key)
        self.addCleanup(os.environ.update, popped)
        _FakeBroker.instances = []
        self._patchers = []

        mode_path = os.path.join(self.tmp, "mode")
        log_path = os.path.join(self.tmp, "spawn-log.jsonl")
        exe_fake = os.path.join(self.tmp, "exe-fake")
        with open(mode_path, "w") as fh:
            fh.write("ok")
        self.mode_path = mode_path
        self.log_path = log_path
        with open(exe_fake, "w") as fh:
            fh.write(_EXE_FAKE.format(mode_path=mode_path))
        os.chmod(exe_fake, 0o755)
        self.exe_fake = exe_fake
        sandbox_fake = os.path.join(self.tmp, "sandbox-fake")
        with open(sandbox_fake, "w") as fh:
            fh.write(_SANDBOX_FAKE.format(
                mode_path=mode_path, log_path=log_path,
                expected_exe=exe_fake))
        os.chmod(sandbox_fake, 0o755)
        self.sandbox_fake = sandbox_fake

        # A fake derived worktree: regular file plus a .git DIRECTORY,
        # the writable counterpart of the production linked shape.
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(self.work, ".git"))
        with open(os.path.join(self.work, "f1.txt"), "w") as fh:
            fh.write("work\n")
        with open(os.path.join(self.work, ".git", "HEAD"), "w") as fh:
            fh.write("ref: refs/heads/x\n")

        self.policy_path = os.path.join(self.tmp, "policy.sb")
        with open(self.policy_path, "w") as fh:
            fh.write("(version 1)\n")
        with open(self.policy_path, "rb") as fh:
            policy_bytes = fh.read()
        policy_stat = os.stat(self.policy_path)
        digest = hashlib.sha256(policy_bytes).hexdigest()
        self.policy = {"path": self.policy_path,
                       "sha256": digest,
                       "size": policy_stat.st_size,
                       "dev": policy_stat.st_dev,
                       "ino": policy_stat.st_ino,
                       "sandbox_exec": self.sandbox_fake}
        self.wrapper_argv = [sandbox_fake, "-f", self.policy_path,
                             exe_fake]

    def tearDown(self):
        for patcher in reversed(self._patchers):
            patcher.stop()

    def _patch_object(self, obj, name, new=None, **kwargs):
        if new is None and "new" not in kwargs:
            patcher = mock.patch.object(obj, name, **kwargs)
        else:
            patcher = mock.patch.object(obj, name, new, **kwargs)
        self._patchers.append(patcher)
        return patcher.start()

    def _set_mode(self, mode):
        with open(self.mode_path, "w") as fh:
            fh.write(mode)

    def _admit(self, scripted_log=()):
        def _start_with_log(self):
            self.scripted_log = [dict(e) for e in scripted_log]
            return 19999

        self._patch_object(pf, "check_target_authority",
                           return_value={"authorized": True,
                                         "kind": "target"})
        self._patch_object(pf, "native_containment",
                           return_value=None)
        # A real snapshot of the fixture executable: the product
        # re-snapshots before spawn and refuses on drift, and the
        # pre-spawn revalidation compares against this same snap.
        snap = muse_mod._snapshot_binary(self.exe_fake)
        self.snap = snap
        self._patch_object(muse_mod, "resolve_pinned_muse",
                           return_value=snap)
        policy = self.policy
        wrapper_argv = self.wrapper_argv

        def _wrapper(*args, **kwargs):
            _ = (args, kwargs)
            return {"argv": list(wrapper_argv),
                    "policy": dict(policy)}

        self._patch_object(muse_mod, "_msp_build_wrapper",
                           side_effect=_wrapper)
        self._patch_object(muse_mod, "_BROKER_CLASS", _FakeBroker)
        self._patch_object(_FakeBroker, "start", _start_with_log)
        self._patch_object(pf, "_read_worker_credential_trace",
                           return_value={"ok": True, "trace": {}})
        self._patch_object(pf, "_read_worker_transport_trace",
                           return_value={"ok": True, "trace": {}})

    def _run(self, mode="ok", scripted_log=()):
        self._set_mode(mode)
        self._admit(scripted_log=scripted_log)
        with mock.patch.dict(os.environ, self.env, clear=True):
            return muse_mod.exec_source_repair(
                "selftest probe", self.work, timeout=120,
                admission_kind="target",
                preflight_home=self.home)

    def _spawn_argvs(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _record(self, usage):
        record = usage.get("native_selftest")
        self.assertTrue(isinstance(record, dict),
                        "usage must carry the native_selftest record")
        return record

    def _by_name(self, record):
        return {c["name"]: c for c in record["controls"]}

    # -- C0: unwrapped /usr/bin/true baseline -----------------------------

    def test_baseline_unwrapped_true_must_exit_zero(self):
        twin = os.path.join(self.tmp, "true-twin")
        with open(twin, "w") as fh:
            fh.write("#!/usr/bin/python3\nimport sys\nsys.exit(3)\n")
        os.chmod(twin, 0o755)
        # create=True: pre-fix the product has no _SELFTEST_TRUE yet;
        # the test must FAIL behaviourally (no 125), not error.
        with mock.patch.object(muse_mod, "_SELFTEST_TRUE", twin,
                               create=True):
            exit_code, _out, usage = self._run()
        self.assertEqual(exit_code, 125,
                         "a nonzero unwrapped baseline must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"],
                         "baseline_unwrapped_true")
        self.assertTrue(_FakeBroker.instances[0].stopped,
                        "broker must be stopped on refusal")

    # -- C1: non-pinned exec through the wrapper must exit 71 -------------

    def test_non_pinned_exec_through_wrapper_must_exit_71(self):
        exit_code, _out, usage = self._run(mode="true-allow")
        self.assertEqual(exit_code, 125,
                         "exec of a non-pinned binary must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "non_pinned_exec")
        self.assertEqual(
            self._by_name(record)["non_pinned_exec"]["exit"], 0)
        for argv in self._spawn_argvs():
            self.assertTrue(_control_shaped(argv),
                            "model child must never start on refusal")

    # -- C2: pinned binary runs offline -----------------------------------

    def test_pinned_binary_runs_offline(self):
        exit_code, _out, usage = self._run(mode="help-fail")
        self.assertEqual(exit_code, 125,
                         "a failing pinned run must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "pinned_runs")
        self.assertEqual(self._by_name(record)["pinned_runs"]["exit"], 2)

    # -- C3: write inside the writable set succeeds ------------------------

    def test_write_inside_writable_set_succeeds(self):
        for mode in ("export-fail", "inside-empty"):
            with self.subTest(mode=mode):
                exit_code, _out, usage = self._run(mode=mode)
                self.assertEqual(exit_code, 125,
                                 "a failed inside write must refuse")
                self.assertEqual(usage.get("code"),
                                 "native-selftest-failed")
                record = self._record(usage)
                self.assertEqual(record["failed_control"],
                                 "write_inside")

    # -- C4: write outside fails and creates nothing -----------------------

    def test_write_outside_fails_and_creates_nothing(self):
        for mode in ("outside-allow", "outside-sneak"):
            with self.subTest(mode=mode):
                exit_code, _out, usage = self._run(mode=mode)
                self.assertEqual(exit_code, 125,
                                 "a bad outside write must refuse")
                self.assertEqual(usage.get("code"),
                                 "native-selftest-failed")
                record = self._record(usage)
                self.assertEqual(record["failed_control"],
                                 "write_outside")
                ctl = self._by_name(record)["write_outside"]
                self.assertTrue(ctl["created"])

    # -- C5: paired write / .git deny --------------------------------------

    def test_paired_write_fails_when_5a_fails(self):
        for mode in ("fail-5a-only", "fail-5a-empty"):
            with self.subTest(mode=mode):
                exit_code, _out, usage = self._run(mode=mode)
                self.assertEqual(exit_code, 125,
                                 "a failed 5a export must refuse")
                self.assertEqual(usage.get("code"),
                                 "native-selftest-failed")
                record = self._record(usage)
                self.assertEqual(record["failed_control"],
                                 "paired_write")

    def test_git_deny_fails_when_5b_exits_zero(self):
        for mode, want_created in (("git-allow", True),
                                   ("git-clean-zero", False)):
            with self.subTest(mode=mode):
                exit_code, _out, usage = self._run(mode=mode)
                self.assertEqual(exit_code, 125,
                                 "a 5b exit 0 must refuse")
                self.assertEqual(usage.get("code"),
                                 "native-selftest-failed")
                record = self._record(usage)
                self.assertEqual(record["failed_control"], "git_deny")
                ctl = self._by_name(record)["git_deny"]
                self.assertEqual(ctl["exit"], 0)
                self.assertEqual(ctl["created"], want_created)

    def test_git_deny_fails_when_git_created_despite_nonzero(self):
        exit_code, _out, usage = self._run(mode="git-sneak")
        self.assertEqual(exit_code, 125,
                         "a created D/.git must refuse even at exit 1")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "git_deny")
        self.assertTrue(self._by_name(record)["git_deny"]["created"])

    def test_git_deny_refuses_symlink_under_d(self):
        exit_code, _out, usage = self._run(mode="symlink-git")
        self.assertEqual(exit_code, 125,
                         "a symlink under D must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "git_deny")

    def test_git_deny_fails_when_removal_fails(self):
        exit_code, _out, usage = self._run(mode="readonly-d")
        self.assertEqual(exit_code, 125,
                         "a failed D removal must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "git_deny")
        self.assertFalse(self._by_name(record)["git_deny"]["removed"])

    def test_leftover_dir_fails_identity(self):
        exit_code, _out, usage = self._run(mode="litter")
        self.assertEqual(exit_code, 125,
                         "a leftover dir must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "worktree_identity")
        self.assertFalse(
            self._by_name(record)["worktree_identity"]["match"])

    # -- C6: broker log must be EMPTY ---------------------------------------

    def test_broker_tripwire_fails_on_any_entry(self):
        for method in ("CONNECT", "DENIED"):
            with self.subTest(method=method):
                _FakeBroker.instances = []
                entry = {"ts": "t", "method": method, "host": "h",
                         "at_close": None, "bytes": 0}
                exit_code, _out, usage = self._run(
                    scripted_log=[entry])
                self.assertEqual(
                    exit_code, 125,
                    "any broker entry must refuse")
                self.assertEqual(usage.get("code"),
                                 "native-selftest-failed")
                record = self._record(usage)
                self.assertEqual(record["failed_control"],
                                 "broker_tripwire")
                self.assertEqual(
                    self._by_name(record)["broker_tripwire"]
                    ["entries"], 1)

    # -- all right: spawn proceeds, record in usage -------------------------

    def test_all_right_proceeds_with_record(self):
        exit_code, _out, usage = self._run()
        record = usage.get("native_selftest")
        self.assertTrue(isinstance(record, dict),
                        "a passing self-test must record in usage")
        self.assertTrue(record.get("passed"))
        self.assertTrue(record.get("failed_control") is None)
        argvs = self._spawn_argvs()
        shaped = [a for a in argvs if _control_shaped(a)]
        self.assertEqual(len(shaped), 7,
                         "seven wrapped control spawns must run")
        self.assertEqual(len(argvs), 8,
                         "exactly one extra spawn (the model) may run")
        by_name = self._by_name(record)
        self.assertEqual(by_name["copy_exec"]["exit"], 71)
        self.assertTrue(by_name["copy_exec"]["created"])
        self.assertNotEqual(usage.get("code"), "native-selftest-failed")
        self.assertTrue(_FakeBroker.instances[0].stopped,
                        "broker must be stopped after the run")

    # -- timeout and missing sandbox-exec -----------------------------------

    def test_export_timeout_fails(self):
        started = time.monotonic()
        exit_code, _out, usage = self._run(mode="hang-export")
        elapsed = time.monotonic() - started
        self.assertEqual(exit_code, 125,
                         "a hung control must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "write_inside")
        ctl = self._by_name(record)["write_inside"]
        self.assertTrue(ctl["timeout"],
                        "the hung control must report a timeout")
        self.assertTrue(elapsed < 60,
                        "controls must be bounded (elapsed %.1fs)"
                        % elapsed)

    def test_spawn_timeout_kills_and_reaps_child(self):
        seen = {}
        orig = muse_mod._msp_spawn_process

        def _recording(argv, env, cwd):
            proc = orig(argv, env, cwd)
            seen["proc"] = proc
            return proc

        with mock.patch.object(muse_mod, "_msp_spawn_process",
                               side_effect=_recording):
            rec = muse_mod._native_selftest_spawn(
                ["/bin/sleep", "30"], dict(self.env), self.tmp, 0.2)
        self.assertTrue(rec["timeout"],
                        "a hung spawn must report a timeout")
        self.assertTrue(seen["proc"].poll() is not None,
                        "a timed-out control must leave no live child")

    def test_spawn_timeout_kills_process_group(self):
        import signal as _signal
        import time as _time
        gpid_file = os.path.join(self.tmp, "grandchild.pid")
        script = ("sleep 30 & echo $! > %s; wait" % gpid_file)
        seen = {}
        orig = muse_mod._msp_spawn_process

        def _recording(argv, env, cwd):
            proc = orig(argv, env, cwd)
            seen["proc"] = proc
            return proc

        try:
            with mock.patch.object(
                    muse_mod, "_msp_spawn_process",
                    side_effect=_recording):
                rec = muse_mod._native_selftest_spawn(
                    ["/bin/sh", "-c", script], dict(self.env),
                    self.tmp, 0.3)
            self.assertTrue(rec["timeout"],
                            "a hung group must report a timeout")
            self.assertTrue(seen["proc"].poll() is not None,
                            "the direct child must be reaped")
            deadline = _time.monotonic() + 2.0
            gone = False
            while _time.monotonic() < deadline:
                try:
                    with open(gpid_file) as fh:
                        gpid = int(fh.read().strip())
                except (OSError, ValueError):
                    gpid = None
                if gpid is not None:
                    try:
                        os.kill(gpid, 0)
                    except ProcessLookupError:
                        gone = True
                        break
                    except OSError:
                        pass
                _time.sleep(0.05)
            self.assertTrue(gone,
                            "the grandchild must be gone with the group")
        finally:
            try:
                with open(gpid_file) as fh:
                    gpid = int(fh.read().strip())
                os.kill(gpid, _signal.SIGKILL)
            except Exception:
                pass

    def test_spawn_unexpected_error_kills_and_reaps_child(self):
        real_popen = muse_mod.subprocess.Popen
        holder = {}

        class _ExplodingProc(object):
            def __init__(self, proc):
                self._proc = proc
                self.stdin = proc.stdin

            def communicate(self, *args, **kwargs):
                raise RuntimeError("mid-capture boom")

            # No pid attribute on purpose: reading proc.pid (or the
            # group signal) can itself raise, and that must never
            # skip the single-kill fallback. The real child must
            # still die and be reaped.
            def kill(self):
                return self._proc.kill()

            def wait(self, timeout=None):
                return self._proc.wait(timeout=timeout)

            def poll(self):
                return self._proc.poll()

            @property
            def returncode(self):
                return self._proc.returncode

        def _explode(argv, env, cwd):
            proc = real_popen(
                argv, env=env, cwd=cwd,
                stdin=muse_mod.subprocess.PIPE,
                stdout=muse_mod.subprocess.PIPE,
                stderr=muse_mod.subprocess.PIPE,
                start_new_session=True)
            holder["proc"] = proc
            return _ExplodingProc(proc)

        with mock.patch.object(muse_mod, "_msp_spawn_process",
                               side_effect=_explode):
            rec = muse_mod._native_selftest_spawn(
                ["/bin/sleep", "5"], dict(self.env), self.tmp, 10)
        self.assertTrue(rec["spawn_error"])
        self.assertTrue("boom" in rec["spawn_error"])
        self.assertTrue(holder["proc"].poll() is not None,
                        "a failed control must leave no live child")

    def test_missing_sandbox_exec_fails(self):
        self.policy["sandbox_exec"] = os.path.join(self.tmp, "no-sb")
        self.wrapper_argv[0] = self.policy["sandbox_exec"]
        exit_code, _out, usage = self._run()
        self.assertEqual(exit_code, 125,
                         "a missing sandbox-exec must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "sandbox_exec")
        self.assertEqual(self._spawn_argvs(), [],
                         "no control may spawn without sandbox-exec")

    def test_overall_limit_fails_without_spawns(self):
        broker = _FakeBroker()
        record = muse_mod._native_selftest(
            wrapper_argv=list(self.wrapper_argv),
            policy=dict(self.policy), pinned_sha256="b" * 64,
            canon=self.tmp, workdir=self.work, broker=broker,
            base_env=dict(self.env), overall_timeout_s=0)
        self.assertTrue(not record.get("passed"))
        self.assertEqual(record.get("failed_control"),
                         "baseline_unwrapped_true",
                         "an expired limit fails before any control")
        self.assertEqual(self._spawn_argvs(), [],
                         "an expired overall limit spawns nothing")

    # -- pin drift: the self-test is never reached ---------------------------

    def _real_wrapped(self):
        """REAL _msp_build_wrapper over a fixture binary (no mocks).

        Product profile: needs no broker port, and the return shape
        is shared by every profile.
        """
        root = os.path.join(os.path.realpath(self.tmp), "realroot")
        os.makedirs(root, 0o700)
        exe = os.path.join(self.tmp, "real-exe")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        snap = muse_mod._snapshot_binary(exe)
        return muse_mod._msp_build_wrapper(root, snap)

    def test_real_builder_policy_feeds_selftest(self):
        wrapped = self._real_wrapped()
        # The real pin for the same fixture binary: the copy
        # control requires the staged bytes to equal the pinned
        # snapshot, so a placeholder pin can no longer feed it.
        snap = muse_mod._snapshot_binary(
            os.path.join(self.tmp, "real-exe"))
        canon = os.path.join(self.tmp, "canon")
        os.makedirs(canon)
        work = os.path.join(self.tmp, "work2")
        os.makedirs(os.path.join(work, ".git"))
        with open(os.path.join(work, "f1.txt"), "w") as fh:
            fh.write("work\n")
        calls = []

        def _emu(argv, env, cwd):
            calls.append(list(argv))
            return _emulated_spawn(argv, env, cwd)

        with mock.patch.object(muse_mod, "_msp_spawn_process",
                               side_effect=_emu):
            record = muse_mod._native_selftest(
                wrapper_argv=wrapped["argv"],
                policy=wrapped["policy"],
                pinned_sha256=snap["sha256"], canon=canon,
                workdir=work, broker=_FakeBroker(),
                base_env=dict(self.env))
        self.assertTrue(record.get("passed"),
                        "the real builder's policy must feed the "
                        "self-test: %r" % (record,))
        self.assertTrue(calls,
                        "the self-test must spawn controls")

    def test_fixture_wrapper_matches_real_builder_shape(self):
        wrapped = self._real_wrapped()
        fake = {"argv": list(self.wrapper_argv),
                "policy": dict(self.policy)}
        self.assertEqual(set(fake), set(wrapped),
                         "top-level wrapper keys must match the "
                         "real builder")
        self.assertEqual(set(fake["policy"]),
                         set(wrapped["policy"]),
                         "policy keys must match the real builder")

    def test_sandbox_exec_mismatch_fails(self):
        policy = dict(self.policy)
        policy["sandbox_exec"] = "/usr/bin/true"

        def _boom(argv, env, cwd):
            raise AssertionError("no spawn past a sandbox refusal")

        with mock.patch.object(muse_mod, "_msp_spawn_process",
                               side_effect=_boom):
            record = muse_mod._native_selftest(
                wrapper_argv=list(self.wrapper_argv),
                policy=policy, pinned_sha256="b" * 64,
                canon=self.tmp, workdir=self.work,
                broker=_FakeBroker(), base_env=dict(self.env))
        self.assertEqual(record.get("failed_control"), "sandbox_exec",
                         "a policy/argv sandbox mismatch must refuse")

    def test_copy_exec_fails_when_copy_runs(self):
        exit_code, _out, usage = self._run(mode="copy-allow")
        self.assertEqual(exit_code, 125,
                         "an executable copy must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "copy_exec")
        ctl = self._by_name(record)["copy_exec"]
        self.assertEqual(ctl["exit"], 0)
        self.assertTrue(ctl["created"])

    def test_revalidate_catches_binary_drift(self):
        # Direct seam test: the binary changes after the
        # snapshot was taken. Revalidation must refuse before
        # the model spawn, naming the binary drift. The
        # end-to-end swap test now trips the earlier copy
        # integrity gate; this pins the revalidation gate
        # itself for swaps landing after the copy.
        snap = muse_mod._snapshot_binary(self.exe_fake)
        wrapped = {"argv": [self.sandbox_fake, "-f",
                            self.policy_path, self.exe_fake],
                   "policy": dict(self.policy)}
        with open(self.exe_fake, "ab") as fh:
            fh.write(b"# drift\n")
        ok, why = muse_mod._revalidate_pre_spawn(wrapped, snap)
        self.assertFalse(ok,
                         "a drifted binary must not revalidate")
        self.assertIn("binary", why,
                      "the refusal must name the binary: %r"
                      % (why,))

    def test_policy_swap_fails_pre_spawn(self):
        swapped = {}
        orig = muse_mod._msp_spawn_process

        def _swapping(argv, env, cwd):
            if "done" not in swapped:
                with open(self.policy_path, "ab") as fh:
                    fh.write(b"# drift\n")
                swapped["done"] = True
            return orig(argv, env, cwd)

        with mock.patch.object(muse_mod, "_msp_spawn_process",
                               side_effect=_swapping):
            exit_code, _out, usage = self._run()
        self.assertEqual(exit_code, 125,
                         "a swapped policy file must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-stale")
        self.assertTrue(_FakeBroker.instances[0].stopped,
                        "broker must be stopped on stale refusal")

    def test_binary_swap_fails_pre_spawn(self):
        # The swap lands on the first spawn, before the copy
        # control stages bytes, so the copy control's required
        # pinned-equality check fires first (still a 125
        # refusal): the staged bytes cannot equal the pinned
        # snapshot once the binary has drifted.
        swapped = {}
        orig = muse_mod._msp_spawn_process

        def _swapping(argv, env, cwd):
            if "done" not in swapped:
                with open(self.exe_fake, "ab") as fh:
                    fh.write(b"# drift\n")
                swapped["done"] = True
            return orig(argv, env, cwd)

        with mock.patch.object(muse_mod, "_msp_spawn_process",
                               side_effect=_swapping):
            exit_code, _out, usage = self._run()
        self.assertEqual(exit_code, 125,
                         "a swapped binary must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "copy_exec")
        ctl = self._by_name(record)["copy_exec"]
        self.assertIn("mismatch", ctl.get("error", ""),
                      "a drifted binary must fail the copy "
                      "integrity check: %r" % (ctl,))

    def test_tampered_record_missing_control_refuses(self):
        names = ["baseline_unwrapped_true", "non_pinned_exec",
                 "pinned_runs", "write_inside", "write_outside",
                 "paired_write", "copy_exec", "git_deny",
                 "worktree_identity", "broker_tripwire"]
        self._admit()
        record = {"passed": True, "failed_control": None,
                  "controls": [{"name": n, "status": "passed"}
                               for n in names if n != "git_deny"],
                  "policy_sha256": self.policy["sha256"],
                  "policy_size": self.policy["size"],
                  "policy_dev": self.policy["dev"],
                  "policy_ino": self.policy["ino"],
                  "sandbox_exec": self.policy["sandbox_exec"],
                  "pinned_sha256": self.snap["sha256"]}
        with mock.patch.object(muse_mod, "_native_selftest",
                               return_value=record):
            with mock.patch.dict(os.environ, self.env, clear=True):
                exit_code, _out, usage = muse_mod.exec_source_repair(
                    "selftest probe", self.work, timeout=120,
                    admission_kind="target",
                    preflight_home=self.home)
        self.assertEqual(exit_code, 125,
                         "a downgraded record must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")

    def test_policy_field_tamper_refuses(self):
        # The record is built from the same dict, so the validator
        # sees a consistent pair; the revalidation against the
        # policy FILE is what refuses (stale, not failed).
        for field, bad in (("sha256", "0" * 64),
                           ("size", 999999),
                           ("dev", 999999),
                           ("ino", 999999)):
            with self.subTest(field=field):
                _FakeBroker.instances = []
                self.policy[field] = bad
                exit_code, _out, usage = self._run()
                self.assertEqual(
                    exit_code, 125,
                    "a tampered policy %s must refuse" % field)
                self.assertEqual(usage.get("code"),
                                 "native-selftest-stale")

    def test_repair_cmd_disables_tools(self):
        cmd = muse_mod.build_source_repair_cmd("probe", self.tmp)
        self.assertTrue("--disable-shell" in cmd,
                        "repair argv must disable shell tools")
        self.assertTrue("--disable-web-tools" in cmd,
                        "repair argv must disable web tools")
        self.assertTrue("--trust-workspace" not in cmd,
                        "repair argv must not trust the workspace")
        self.assertTrue("--sandbox-network" not in cmd,
                        "repair argv must not open network")

    def test_repair_policy_text_proof_rejects_tampered_exceptions(
            self):
        from openmainty import ncontain as ncontain_mod
        root = os.path.join(os.path.realpath(self.tmp), "rproot")
        os.makedirs(root, 0o700)
        exe = os.path.join(self.tmp, "repair-exe")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        snap = muse_mod._snapshot_binary(exe)
        exe_pin = {"path": snap["real_path"],
                   "real_path": snap["real_path"],
                   "sha256": snap["sha256"], "size": snap["size"],
                   "dev": snap["dev"], "ino": snap["ino"],
                   "mode": oct(stat.S_IMODE(os.lstat(exe).st_mode)),
                   "offline_only": True, "admits_worker": False}
        home = os.path.join(os.path.realpath(self.tmp), "rphome")
        os.makedirs(home, 0o700)
        work = os.path.join(os.path.realpath(self.tmp), "rpwork")
        os.makedirs(work)
        base = list(ncontain_mod.SUBSCRIPTION_WORKER_READ_EXCEPTIONS)
        dropped = tuple(base[:-1])
        added = tuple(base + [("subpath", "/tmp", "tampered")])
        for label, tampered in (("drop", dropped), ("add", added)):
            with self.subTest(label=label):
                with mock.patch.object(
                        ncontain_mod,
                        "SUBSCRIPTION_WORKER_READ_EXCEPTIONS",
                        tampered):
                    with self.assertRaisesRegex(
                            ncontain_mod.ContainmentError,
                            "differ from the reviewed set",
                            msg="tampered exception set must refuse"):
                        ncontain_mod.create_repair_broker_policy(
                            root, exe_pin, home, 19999, work)

    def test_repair_policy_text_proof_carries_reviewed_exceptions(
            self):
        # Positive control: an untampered build succeeds and the
        # returned text carries every live read exception as its
        # exact allow clause, exactly once, in the single
        # read-deny line. Text-only proof, not live enforcement.
        from openmainty import ncontain as ncontain_mod
        root = os.path.join(os.path.realpath(self.tmp), "okroot")
        os.makedirs(root, 0o700)
        exe = os.path.join(self.tmp, "ok-repair-exe")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        snap = muse_mod._snapshot_binary(exe)
        exe_pin = {"path": snap["real_path"],
                   "real_path": snap["real_path"],
                   "sha256": snap["sha256"], "size": snap["size"],
                   "dev": snap["dev"], "ino": snap["ino"],
                   "mode": oct(stat.S_IMODE(os.lstat(exe).st_mode)),
                   "offline_only": True, "admits_worker": False}
        home = os.path.join(os.path.realpath(self.tmp), "okhome")
        os.makedirs(home, 0o700)
        work = os.path.join(os.path.realpath(self.tmp), "okwork")
        os.makedirs(work)
        policy = ncontain_mod.create_repair_broker_policy(
            root, exe_pin, home, 19998, work)
        with open(policy["policy_path"]) as fh:
            text = fh.read()
        readlines = [ln for ln in text.splitlines()
                     if ln.startswith("(deny file-read*")]
        self.assertEqual(len(readlines), 1,
                         "repair text must keep one read-deny line")
        live = ncontain_mod.subscription_worker_exceptions(home)
        self.assertEqual(len(live), 8,
                         "reviewed exception set has 8 entries")
        for entry in live:
            clause = "(%s %s)" % (
                entry["kind"],
                ncontain_mod._sb_literal(entry["path"]))
            self.assertEqual(readlines[0].count(clause), 1,
                             "clause must appear exactly once: "
                             "%s %s" % (entry["kind"],
                                        entry["path"]))

    def _build_repair_text(self):
        # Untouched repair text plus its home, for tests that
        # tamper the TEXT while the exception tuple stays
        # intact: the text half of the proof must refuse.
        from openmainty import ncontain as ncontain_mod
        base = os.path.realpath(self.tmp)
        root = os.path.join(base, "txtroot")
        os.makedirs(root, 0o700, exist_ok=True)
        exe = os.path.join(base, "txt-repair-exe")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        snap = muse_mod._snapshot_binary(exe)
        exe_pin = {"path": snap["real_path"],
                   "real_path": snap["real_path"],
                   "sha256": snap["sha256"], "size": snap["size"],
                   "dev": snap["dev"], "ino": snap["ino"],
                   "mode": oct(stat.S_IMODE(os.lstat(exe).st_mode)),
                   "offline_only": True, "admits_worker": False}
        home = os.path.join(base, "txthome")
        os.makedirs(home, 0o700, exist_ok=True)
        work = os.path.join(base, "txtwork")
        os.makedirs(work, exist_ok=True)
        policy = ncontain_mod.create_repair_broker_policy(
            root, exe_pin, home, 19996, work)
        with open(policy["policy_path"]) as fh:
            return fh.read(), home, root, work, exe

    def test_proof_text_missing_clause_refuses(self):
        from openmainty import ncontain as ncontain_mod
        text, home, _root, _work, _exe = self._build_repair_text()
        live = ncontain_mod.subscription_worker_exceptions(home)
        victim = live[0]
        clause = "(%s %s)" % (
            victim["kind"],
            ncontain_mod._sb_literal(victim["path"]))
        self.assertEqual(text.count(clause), 1,
                         "victim clause must occur once overall")
        tampered = text.replace(clause, "", 1)
        with self.assertRaisesRegex(
                ncontain_mod.ContainmentError, "missing from",
                msg="a text missing a reviewed clause must refuse"):
            ncontain_mod._prove_repair_read_exceptions(
                live, os.path.realpath(home), tampered)

    def test_proof_text_duplicated_clause_refuses(self):
        from openmainty import ncontain as ncontain_mod
        text, home, _root, _work, _exe = self._build_repair_text()
        live = ncontain_mod.subscription_worker_exceptions(home)
        victim = live[0]
        clause = "(%s %s)" % (
            victim["kind"],
            ncontain_mod._sb_literal(victim["path"]))
        self.assertEqual(text.count(clause), 1,
                         "victim clause must occur once overall")
        tampered = text.replace(clause, clause + clause, 1)
        with self.assertRaisesRegex(
                ncontain_mod.ContainmentError, "duplicated in",
                msg="a text duplicating a reviewed clause must "
                    "refuse"):
            ncontain_mod._prove_repair_read_exceptions(
                live, os.path.realpath(home), tampered)

    def test_proof_text_two_read_lines_refuses(self):
        from openmainty import ncontain as ncontain_mod
        text, home, _root, _work, _exe = self._build_repair_text()
        live = ncontain_mod.subscription_worker_exceptions(home)
        readlines = [ln for ln in text.splitlines()
                     if ln.startswith("(deny file-read*")]
        self.assertEqual(len(readlines), 1,
                         "untouched text keeps one read-deny line")
        tampered = text + readlines[0] + "\n"
        with self.assertRaisesRegex(
                ncontain_mod.ContainmentError, "not unique",
                msg="a text with two read-deny lines must refuse"):
            ncontain_mod._prove_repair_read_exceptions(
                live, os.path.realpath(home), tampered)

    def test_copy_exec_short_write_writes_full_copy(self):
        # os.write short-writes the staged copy once: the control
        # must still stage byte-identical bytes (looped write plus
        # re-read verify), so the self-test passes and the record
        # carries the verified copy identity.
        orig_open, orig_write = os.open, os.write
        orig_close = os.close
        copy_fds = set()
        state = {"cut": False}

        def _open(path, flags, *args, **kwargs):
            fd = orig_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and path.endswith("/copy"):
                copy_fds.add(fd)
            return fd

        def _close(fd):
            copy_fds.discard(fd)
            return orig_close(fd)

        def _short_write(fd, data):
            if fd in copy_fds and not state["cut"] \
                    and len(data) > 1:
                state["cut"] = True
                return orig_write(fd, data[:len(data) // 2])
            return orig_write(fd, data)

        with mock.patch.object(os, "open", side_effect=_open), \
                mock.patch.object(os, "close", side_effect=_close), \
                mock.patch.object(os, "write",
                                  side_effect=_short_write):
            exit_code, _out, usage = self._run()
        self.assertTrue(state["cut"],
                        "the short-write stub must have fired")
        record = self._record(usage)
        self.assertTrue(record.get("passed"),
                        "a short write must not fail the "
                        "self-test: %r" % (record,))
        ctl = self._by_name(record)["copy_exec"]
        self.assertEqual(ctl.get("copy_sha256"),
                         self.snap["sha256"],
                         "the staged copy must match the pinned "
                         "snapshot")
        self.assertEqual(ctl.get("copy_size"), self.snap["size"],
                         "the staged copy size must match the "
                         "pinned snapshot")

    def test_copy_exec_corrupt_copy_refuses(self):
        # The staged bytes differ from the pinned snapshot (one
        # flipped bit, same length): the control must refuse with
        # a mismatch reason instead of counting the nonzero exit
        # as a denial.
        orig_open, orig_write = os.open, os.write
        orig_close = os.close
        copy_fds = set()

        def _open(path, flags, *args, **kwargs):
            fd = orig_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and path.endswith("/copy"):
                copy_fds.add(fd)
            return fd

        def _close(fd):
            copy_fds.discard(fd)
            return orig_close(fd)

        def _corrupt_write(fd, data):
            if fd in copy_fds and len(data) > 0:
                raw = bytes(data)
                return orig_write(fd, bytes([raw[0] ^ 1])
                                  + raw[1:])
            return orig_write(fd, data)

        with mock.patch.object(os, "open", side_effect=_open), \
                mock.patch.object(os, "close", side_effect=_close), \
                mock.patch.object(os, "write",
                                  side_effect=_corrupt_write):
            exit_code, _out, usage = self._run()
        self.assertEqual(exit_code, 125,
                         "a corrupt copy must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "copy_exec")
        ctl = self._by_name(record)["copy_exec"]
        self.assertIn("mismatch", ctl.get("error", ""),
                      "a corrupt copy must report a mismatch, not "
                      "a denial: %r" % (ctl,))

    def test_copy_exec_zero_write_ends_in_refusal(self):
        # os.write on the staged copy returns 0 for a while, then
        # completes: the zero-progress guard must refuse at the
        # first 0 (125, copy_exec, no model spawn) instead of
        # looping. The stub bounds itself to 5 zeroes so a
        # missing guard fails fast instead of hanging.
        orig_open, orig_write, orig_close = os.open, os.write, os.close
        copy_fds = set()
        state = {"zeros": 0}

        def _open(path, flags, *args, **kwargs):
            fd = orig_open(path, flags, *args, **kwargs)
            if isinstance(path, str) and path.endswith("/copy"):
                copy_fds.add(fd)
            return fd

        def _close(fd):
            copy_fds.discard(fd)
            return orig_close(fd)

        def _zero_write(fd, data):
            if fd in copy_fds and len(data) > 0:
                if state["zeros"] < 5:
                    state["zeros"] += 1
                    return 0
            return orig_write(fd, data)

        with mock.patch.object(os, "open", side_effect=_open), \
                mock.patch.object(os, "close", side_effect=_close), \
                mock.patch.object(os, "write",
                                  side_effect=_zero_write):
            exit_code, _out, usage = self._run()
        self.assertTrue(state["zeros"] > 0,
                        "the zero-write stub must have fired")
        self.assertEqual(exit_code, 125,
                         "a zero-progress copy write must refuse")
        self.assertEqual(usage.get("code"), "native-selftest-failed")
        record = self._record(usage)
        self.assertEqual(record["failed_control"], "copy_exec")
        ctl = self._by_name(record)["copy_exec"]
        self.assertIn("no progress", ctl.get("error", ""),
                      "a zero-progress write must say so: %r"
                      % (ctl,))
        for argv in self._spawn_argvs():
            self.assertTrue(_control_shaped(argv),
                            "model child must never start on refusal")

    def test_allow_extra_write_rule_refuses(self):
        from openmainty import ncontain as ncontain_mod
        prove = getattr(ncontain_mod,
                        "_prove_no_extra_allow_rules", None)
        self.assertTrue(callable(prove),
                        "the policy must prove no extra allow "
                        "rules")
        text, home, root, work, exe = self._build_repair_text()
        live = ncontain_mod.subscription_worker_exceptions(home)
        tampered = (text + "(allow file-write* "
                    "(subpath /tmp))\n")
        with self.assertRaisesRegex(
                ncontain_mod.ContainmentError, "allow rule",
                msg="an extra allow rule must refuse"):
            prove(tampered, root, exe, home, work, live)

    def test_allow_proof_wired_into_build(self):
        # Wiring: the builder must invoke the allow-rule proof
        # and propagate its refusal. A raising stub fails the
        # build; if the call were removed, the build would
        # succeed and this test would fail.
        from openmainty import ncontain as ncontain_mod
        root = os.path.join(os.path.realpath(self.tmp), "wroot")
        os.makedirs(root, 0o700)
        exe = os.path.join(self.tmp, "w-repair-exe")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(exe, 0o755)
        snap = muse_mod._snapshot_binary(exe)
        exe_pin = {"path": snap["real_path"],
                   "real_path": snap["real_path"],
                   "sha256": snap["sha256"], "size": snap["size"],
                   "dev": snap["dev"], "ino": snap["ino"],
                   "mode": oct(stat.S_IMODE(os.lstat(exe).st_mode)),
                   "offline_only": True, "admits_worker": False}
        home = os.path.join(os.path.realpath(self.tmp), "whome")
        os.makedirs(home, 0o700)
        work = os.path.join(os.path.realpath(self.tmp), "wwork")
        os.makedirs(work)

        def _wired(*args):
            raise ncontain_mod.ContainmentError("wired")

        with mock.patch.object(ncontain_mod,
                               "_prove_no_extra_allow_rules",
                               side_effect=_wired):
            with self.assertRaisesRegex(
                    ncontain_mod.ContainmentError, "wired",
                    msg="the build must invoke the allow proof"):
                ncontain_mod.create_repair_broker_policy(
                    root, exe_pin, home, 19994, work)

    def test_allow_removed_rule_refuses(self):
        from openmainty import ncontain as ncontain_mod
        prove = getattr(ncontain_mod,
                        "_prove_no_extra_allow_rules", None)
        self.assertTrue(callable(prove),
                        "the policy must prove no extra allow "
                        "rules")
        text, home, root, work, exe = self._build_repair_text()
        live = ncontain_mod.subscription_worker_exceptions(home)
        d2 = [ln for ln in text.splitlines()
              if ln.startswith("(allow file-write-data")]
        self.assertEqual(len(d2), 1,
                         "untouched text keeps one D2 write line")
        tampered = text.replace(d2[0] + "\n", "")
        with self.assertRaisesRegex(
                ncontain_mod.ContainmentError, "allow rule",
                msg="a removed allow rule must refuse"):
            prove(tampered, root, exe, home, work, live)

    def _valid_record(self):
        names = ["baseline_unwrapped_true", "non_pinned_exec",
                 "pinned_runs", "write_inside", "write_outside",
                 "paired_write", "copy_exec", "git_deny",
                 "worktree_identity", "broker_tripwire"]
        snap = muse_mod._snapshot_binary(self.exe_fake)
        record = {"passed": True, "failed_control": None,
                  "controls": [{"name": n, "status": "passed"}
                               for n in names],
                  "policy_sha256": self.policy["sha256"],
                  "policy_size": self.policy["size"],
                  "policy_dev": self.policy["dev"],
                  "policy_ino": self.policy["ino"],
                  "sandbox_exec": self.policy["sandbox_exec"],
                  "pinned_sha256": snap["sha256"]}
        return record, snap

    def test_record_validator_rejects_bad_records(self):
        record, snap = self._valid_record()
        ok, _why = muse_mod._validate_selftest_record(
            list(self.wrapper_argv), dict(self.policy),
            snap, record)
        self.assertTrue(ok, "a complete record must validate")
        bad_unpassed = dict(record, passed=False)
        ok, _why = muse_mod._validate_selftest_record(
            list(self.wrapper_argv), dict(self.policy),
            snap, bad_unpassed)
        self.assertTrue(not ok, "an unpassed record must not count")
        bad_failed = dict(record, failed_control="git_deny")
        ok, _why = muse_mod._validate_selftest_record(
            list(self.wrapper_argv), dict(self.policy),
            snap, bad_failed)
        self.assertTrue(not ok, "failed_control set must not count")
        bad_order = dict(
            record,
            controls=list(reversed(record["controls"])))
        ok, _why = muse_mod._validate_selftest_record(
            list(self.wrapper_argv), dict(self.policy),
            snap, bad_order)
        self.assertTrue(not ok, "reordered controls must not count")
        bad_status = dict(record, controls=[
            dict(c, status="failed") if c["name"] == "git_deny"
            else dict(c) for c in record["controls"]])
        ok, _why = muse_mod._validate_selftest_record(
            list(self.wrapper_argv), dict(self.policy),
            snap, bad_status)
        self.assertTrue(not ok, "a failed control must not count")
        bad_pinned = dict(record, pinned_sha256="0" * 64)
        ok, _why = muse_mod._validate_selftest_record(
            list(self.wrapper_argv), dict(self.policy),
            snap, bad_pinned)
        self.assertTrue(not ok, "a wrong pin must not count")
        for field, bad in (("policy_sha256", "0" * 64),
                           ("policy_size", 999999),
                           ("policy_dev", 999999),
                           ("policy_ino", 999999)):
            bad_pol = dict(record)
            bad_pol[field] = bad
            ok, _why = muse_mod._validate_selftest_record(
                list(self.wrapper_argv), dict(self.policy),
                snap, bad_pol)
            self.assertTrue(not ok,
                            "a wrong %s must not count" % field)

    def test_record_validator_sandbox_head(self):
        # sandbox_exec must equal the spawned wrapper head.
        record, snap = self._valid_record()
        other_argv = ["/usr/bin/true", "-f", "x"]
        ok, _why = muse_mod._validate_selftest_record(
            other_argv, dict(self.policy), snap, record)
        self.assertTrue(
            not ok, "sandbox_exec must match the wrapper head")

    def test_record_validator_sandbox_policy(self):
        # sandbox_exec must equal the policy entry (argv chosen so
        # the head binding holds and only the policy comparison
        # can refuse).
        record, snap = self._valid_record()
        other_argv = ["/usr/bin/true", "-f", "x"]
        rec2 = dict(record, sandbox_exec="/usr/bin/true")
        ok, _why = muse_mod._validate_selftest_record(
            other_argv, dict(self.policy), snap, rec2)
        self.assertTrue(
            not ok, "sandbox_exec must match the policy entry")

    def test_pin_drift_never_reaches_selftest(self):
        self._set_mode("ok")
        self._patch_object(pf, "check_target_authority",
                           return_value={"authorized": True,
                                         "kind": "target"})
        self._patch_object(pf, "native_containment",
                           return_value=None)
        self._patch_object(
            muse_mod, "resolve_pinned_muse",
            side_effect=OSError("pinned muse content drift: "
                                "sha256 76665eda != pinned 1c961144; "
                                "re-pin only with a reviewed binary"))
        self._patch_object(muse_mod, "_BROKER_CLASS", _FakeBroker)
        with mock.patch.dict(os.environ, self.env, clear=True):
            exit_code, _out, usage = muse_mod.exec_source_repair(
                "selftest probe", self.work, timeout=120,
                admission_kind="target",
                preflight_home=self.home)
        self.assertEqual(exit_code, 127,
                         "pin drift must refuse before the self-test")
        self.assertTrue("code" not in usage,
                        "drift refusal carries no code key")
        self.assertTrue("native_selftest" not in usage,
                        "no self-test record on drift refusal")
        self.assertEqual(_FakeBroker.instances, [],
                         "broker must never start on drift refusal")
        self.assertEqual(self._spawn_argvs(), [],
                         "nothing may spawn when resolution refuses")


class RealKernelSelftestCase(unittest.TestCase):
    """Run the product self-test under real sandbox-exec.

    Linked worktree fixture, empty-allowlist broker, real wrapper and
    policy. Skips with an explicit pin-drift reason when
    resolve_pinned_muse() refuses; it never bypasses the pin.
    """

    def test_real_kernel_selftest(self):
        import subprocess
        if sys.platform != "darwin":
            self.skipTest("needs macOS sandbox-exec")
        proc = subprocess.run(["/usr/bin/true"],
                              capture_output=True, timeout=10)
        if proc.returncode != 0:
            self.skipTest("outer sandbox denies no-rule exec "
                          "(unwrapped true -> %d)" % proc.returncode)
        try:
            resolved = muse_mod.resolve_pinned_muse()
        except OSError as exc:
            self.skipTest(
                "pin drift: resolve_pinned_muse() refused (%s); "
                "real-kernel evidence needs a reviewed re-pin; "
                "this test never bypasses the pin" % (exc,))

        tmp = tempfile.mkdtemp(prefix="muse303-real-")
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        env = _minimal_env(home)
        popped = {}
        for key in ("META_API_KEY", "MUSE_CONTRIBUTOR_MODE"):
            if key in os.environ:
                popped[key] = os.environ.pop(key)
        self.addCleanup(os.environ.update, popped)

        origin = os.path.join(tmp, "origin")
        os.makedirs(origin)
        subprocess.run(["git", "init", "-q", origin], check=True,
                       env=env, timeout=30)
        subprocess.run(["git", "-C", origin, "config", "user.email",
                        "t@t"], check=True, env=env, timeout=30)
        subprocess.run(["git", "-C", origin, "config", "user.name",
                        "t"], check=True, env=env, timeout=30)
        subprocess.run(["git", "-C", origin, "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True,
                       env=env, timeout=30)
        work = os.path.join(tmp, "linked")
        subprocess.run(["git", "-C", origin, "worktree", "add", work],
                       check=True, env=env, timeout=30,
                       capture_output=True)
        with open(os.path.join(work, "f1.txt"), "w") as fh:
            fh.write("work\n")

        canon = os.path.join(tmp, "canon")
        os.makedirs(canon)
        broker = mspbroker_mod.ConnectBroker(allow=())
        assert getattr(broker, "_allow", None) == ()
        broker_port = broker.start()
        try:
            owned = muse_mod._msp_host_env(canon)
            owned["HOME"] = home
            wrapped = muse_mod._msp_build_wrapper(
                canon, resolved, profile="repair-broker",
                home=home, broker_port=broker_port,
                workdir=work)
            record = muse_mod._native_selftest(
                wrapper_argv=wrapped["argv"],
                policy=wrapped["policy"],
                pinned_sha256=resolved["sha256"],
                canon=canon, workdir=work, broker=broker,
                base_env=owned)
        finally:
            broker.stop()
        self.assertTrue(record.get("passed"),
                        "real-kernel self-test must pass: %r"
                        % (record,))
        by_name = {c["name"]: c for c in record["controls"]}
        self.assertEqual(by_name["non_pinned_exec"]["exit"], 71)
        self.assertEqual(by_name["pinned_runs"]["exit"], 0)
        self.assertTrue(by_name["write_inside"]["created"])
        self.assertTrue(by_name["paired_write"]["created"])
        self.assertFalse(by_name["write_outside"]["created"])
        self.assertFalse(by_name["git_deny"]["created"])
        self.assertTrue(by_name["worktree_identity"]["match"])
        self.assertEqual(by_name["broker_tripwire"]["entries"], 0)


if __name__ == "__main__":
    unittest.main()
