"""Configure-watch-inputs: baseline editing through the app (revision 56).

Covers the supported `sched.configure_watch_inputs` op and its
`POST /api/sched/watch-inputs` entry without hardcoding run IDs or
dependency names: successful peer adds preserve the settlement
(settled_at/reason/evidence/trigger/active), disposition, attempts and
other input classes while auditing structured old/new inputs; every
refusal (unknown/mismatched/cancelled/inactive/stale/duplicate/
unsupported/lookup-failed/busy) happens before any write. Fresh
server-side registry reads win over cached scan rows, and explicit
`manager:id` keys never bypass repo membership. Peer releases flow
through the EXISTING watch engine (observe/refresh) unchanged.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from openmainty import deps, repos, runner, sched, serve, store  # noqa: E402
try:
    from tests._home import use_temp_home  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home  # noqa: E402


def make_home(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    use_temp_home(test, str(Path(tmp.name) / "home"))
    return Path(tmp.name)


REPO = "t-repo"
KEY = "dep-alpha-2.0.0"
PRIMARY = "alpha"

ISSUE_INPUTS = {"number": "12", "title": "T", "state": "open",
                "updated_at": "2026-01-01T00:00:00+00:00", "comments": 0}
SDK_INPUTS = {"flutter": "3.44.0"}


def base_inputs():
    return {"registry": {PRIMARY: "1.0.0"},
            "issue": dict(ISSUE_INPUTS), "sdk": dict(SDK_INPUTS)}


def base_rows():
    return [
        {"name": PRIMARY, "kind": "npm", "installed": "1.0.0",
         "latest": "1.0.0", "update_kind": "none", "scope": "dependencies"},
        {"name": "beta", "kind": "npm", "installed": "0.87.0",
         "latest": "0.87.0", "update_kind": "none", "scope": "dependencies"},
        {"name": "gamma", "kind": "pub", "installed": "2.0.0",
         "latest": "2.0.0", "update_kind": "none", "scope": "dependencies"},
        {"name": "myself", "kind": "npm", "installed": "0.0.0-use.local",
         "latest": None, "update_kind": "unmanaged", "scope": "dependencies",
         "note": "self-workspace/local source per lockfile"},
        {"name": "tool", "kind": "sdk", "installed": "3.44.0",
         "latest": None, "update_kind": "sdk", "scope": "dependencies"},
    ]


def settle_active(test, inputs=None, rows=None, dep=PRIMARY,
                  status="waiting-review"):
    """An executed run settled as an active deferred watch + queue item."""
    state = store.create(REPO, "12", "t", "base0", {"worker": "test"})
    state["queue_key"] = KEY
    state["repo"] = REPO
    state["workTreeFingerprint"] = "fp0"
    watch = runner.settle_external_watch(
        state, reason="wait for peer release",
        evidence="pins observed", trigger="peer release then reassess",
        inputs=inputs if inputs is not None else base_inputs(),
        settled_by="test")
    st = sched.load(REPO)
    st["queue"].append({"key": KEY, "status": status, "verified": False,
                        "run_id": state["id"], "kind": "dep-update",
                        "dep": dep, "attempts": 0})
    st["last_rows"] = rows if rows is not None else base_rows()
    sched.save(st)
    run = store.load(state["id"])
    return run, watch["settled_at"], \
        sched.watch_inputs_fingerprint(run["watch"]["inputs"])


def snap_state(run_id):
    return (json.dumps(store.load(run_id), sort_keys=True),
            json.dumps(sched.load(REPO), sort_keys=True))


class ConfigureWatchInputsTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        repos.save({"name": REPO, "github": "o/r", "baseBranch": "main"})
        self.resolver = {}
        self.lookups = []
        orig = deps._latest_fn

        def _fake(kind, ident):
            self.lookups.append((kind, ident))
            if ident in self.resolver:
                return self.resolver[ident]
            raise LookupError(f"not published: {ident}")

        deps.set_latest_fn(_fake)
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def _configure(self, fp, settled, **kw):
        kw.setdefault("actor", "test")
        return sched.configure_watch_inputs(REPO, KEY, expected_fp=fp,
                                            expected_settled_at=settled, **kw)

    def test_fingerprint_stable_and_sensitive(self):
        self.assertEqual(sched.watch_inputs_fingerprint(base_inputs()),
                         sched.watch_inputs_fingerprint(base_inputs()))
        changed = base_inputs()
        changed["registry"] = {PRIMARY: "9.9.9"}
        self.assertNotEqual(sched.watch_inputs_fingerprint(base_inputs()),
                            sched.watch_inputs_fingerprint(changed))

    def test_key_validation(self):
        self.assertEqual(sched._valid_watch_registry_key("alpha"), (None, "alpha"))
        self.assertEqual(sched._valid_watch_registry_key("npm:alpha"),
                         ("npm", "alpha"))
        self.assertEqual(sched._valid_watch_registry_key("@scope/pkg"),
                         (None, "@scope/pkg"))
        for bad in ("", "  ", "https://x/y", "a b", "a;b", "a`b", "a$b",
                    "npmx:alpha", "group:artifact", "-x", 12, None,
                    "x" * 129):
            self.assertIsNone(sched._valid_watch_registry_key(bad), bad)

    def test_success_add_preserves_and_audits(self):
        run, settled, fp = settle_active(self)
        before_interventions = list(run.get("interventions") or [])
        self.resolver = {"beta": "0.88.0"}
        res = self._configure(fp, settled, add=["beta"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["old_keys"], [PRIMARY])
        self.assertEqual(res["new_keys"], [PRIMARY, "beta"])
        # Fresh server pin wins over the cached scan row (0.87.0).
        self.assertEqual(res["observed"], {"beta": "0.88.0"})
        self.assertEqual(self.lookups, [("npm", "beta")])
        now = store.load(run["id"])
        watch = now["watch"]
        self.assertEqual(watch["inputs"]["registry"],
                         {PRIMARY: "1.0.0", "beta": "0.88.0"})
        # Settlement and sibling input classes preserved byte-for-byte.
        for field in ("settled_at", "reason", "evidence", "trigger",
                      "active", "settled_by", "last_check", "check_error",
                      "lifted_at"):
            self.assertEqual(watch.get(field), run["watch"].get(field), field)
        self.assertEqual(watch["inputs"]["issue"], ISSUE_INPUTS)
        self.assertEqual(watch["inputs"]["sdk"], SDK_INPUTS)
        self.assertEqual(now["disposition"], run["disposition"])
        self.assertEqual(now["workCommit"], "base0")
        self.assertEqual(now["workTreeFingerprint"], "fp0")
        self.assertTrue((now.get("waitReason") or "").startswith("watch:"))
        self.assertTrue(watch["next_check"])
        # Structured audit appended after the preserved originals.
        self.assertEqual(now["interventions"][:len(before_interventions)],
                         before_interventions)
        audit = now["interventions"][-1]
        self.assertEqual(audit["kind"], "watch-inputs-configured")
        self.assertEqual(audit["actor"], "test")
        self.assertEqual(audit["run_id"], run["id"])
        self.assertEqual(audit["key"], KEY)
        self.assertEqual(audit["repo"], REPO)
        self.assertEqual(audit["old_inputs"]["registry"], {PRIMARY: "1.0.0"})
        self.assertEqual(audit["new_inputs"]["registry"],
                         {PRIMARY: "1.0.0", "beta": "0.88.0"})
        self.assertEqual(audit["observed"], {"beta": "0.88.0"})
        self.assertEqual(audit["source"],
                         {"workCommit": "base0", "workTreeFingerprint": "fp0"})
        # Queue item untouched apart from the display summary.
        item = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        self.assertEqual(item["status"], "waiting-review")
        self.assertEqual(item["run_id"], run["id"])
        self.assertEqual(item.get("attempts"), 0)
        hist = sched.load(REPO)["history"][-1]
        self.assertEqual(hist["result"], "watch-inputs-configured")
        self.assertEqual(hist["run_id"], run["id"])
        self.assertEqual(hist["old_keys"], [PRIMARY])
        self.assertEqual(res["fingerprint"],
                         sched.watch_inputs_fingerprint(watch["inputs"]))
        self.assertEqual(res["settled_at"], settled)

    def test_cached_row_never_trusted_as_fresh_pin(self):
        rows = base_rows()
        rows[1] = dict(rows[1], latest="0.87.0")
        run, settled, fp = settle_active(self, rows=rows)
        self.resolver = {"beta": "0.88.0"}
        res = self._configure(fp, settled, add=["beta"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(store.load(run["id"])["watch"]["inputs"]["registry"]["beta"],
                         "0.88.0")

    def test_resolver_failure_refuses_despite_cached_latest(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        self.resolver = {}
        res = self._configure(fp, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "lookup")
        self.assertEqual(snap_state(run["id"]), before)

    def test_explicit_unknown_peer_refuses_before_lookup(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["npm:ghost"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_mismatched_manager_refuses(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["pub:beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_unsupported_manager_refuses(self):
        rows = base_rows() + [{"name": "jav", "kind": "maven",
                               "installed": "1.0", "latest": "1.1",
                               "update_kind": "none"}]
        run, settled, fp = settle_active(self, rows=rows)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["maven:jav"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_explicit_valid_peer_succeeds(self):
        run, settled, fp = settle_active(self)
        self.resolver = {"beta": "0.88.0"}
        res = self._configure(fp, settled, add=["npm:beta"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["new_keys"], [PRIMARY, "npm:beta"])
        self.assertEqual(res["observed"], {"npm:beta": "0.88.0"})

    def test_unmanaged_row_refuses_before_lookup(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        for key in ("myself", "npm:myself"):
            res = self._configure(fp, settled, add=[key])
            self.assertFalse(res["ok"], key)
            self.assertEqual(res["status"], "unknown", key)
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_sdk_row_refuses_before_lookup(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["tool"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_duplicate_plain_of_explicit_refuses(self):
        run, settled, fp = settle_active(
            self, inputs={"registry": {"npm:alpha": "1.0.0"}})
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["alpha"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")
        self.assertIn("duplicate", res["reason"])
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_duplicate_explicit_of_plain_refuses(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["npm:alpha"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")
        self.assertIn("duplicate", res["reason"])
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_remove_primary_refused(self):
        run, settled, fp = settle_active(
            self, inputs={"registry": {PRIMARY: "1.0.0", "beta": "0.87.0"}})
        before = snap_state(run["id"])
        res = self._configure(fp, settled, remove=[PRIMARY])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")
        self.assertIn(PRIMARY, res["reason"])
        self.assertEqual(snap_state(run["id"]), before)

    def test_missing_primary_after_op_refused(self):
        run, settled, fp = settle_active(
            self, inputs={"registry": {"beta": "0.87.0"}})
        before = snap_state(run["id"])
        res = self._configure(fp, settled, remove=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")
        self.assertIn("missing primary", res["reason"])
        self.assertEqual(snap_state(run["id"]), before)

    def test_remove_unknown_refused(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, remove=["ghost"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")
        self.assertEqual(snap_state(run["id"]), before)

    def test_remove_success(self):
        run, settled, fp = settle_active(
            self, inputs={"registry": {PRIMARY: "1.0.0", "beta": "0.87.0"}})
        res = self._configure(fp, settled, remove=["beta"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["observed"], {})
        now = store.load(run["id"])
        self.assertEqual(now["watch"]["inputs"]["registry"], {PRIMARY: "1.0.0"})
        self.assertEqual(now["watch"]["settled_at"], settled)

    def test_stale_fingerprint_refuses(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure("0" * 64, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "stale")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_stale_settled_at_refuses(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        res = self._configure(fp, "2000-01-01T00:00:00+00:00", add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "stale")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_concurrent_baseline_change_refuses(self):
        run, settled, fp = settle_active(self)
        other = store.load(run["id"])
        other["watch"]["inputs"] = {"registry": {PRIMARY: "2.0.0"}}
        store.save(other)
        res = self._configure(fp, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "stale")

    def test_cancelled_refuses(self):
        run, settled, fp = settle_active(self, status="cancelled")
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "cancelled")
        self.assertEqual(snap_state(run["id"]), before)

    def test_unknown_key_refuses(self):
        run, settled, fp = settle_active(self)
        res = sched.configure_watch_inputs(
            REPO, "dep-nope-0.0.1", add=["beta"], expected_fp=fp,
            expected_settled_at=settled, actor="test")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "unknown")

    def test_mismatch_refuses(self):
        run, settled, fp = settle_active(self)
        other = store.load(run["id"])
        other["queue_key"] = "dep-other-1.0.0"
        store.save(other)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "mismatch")
        self.assertEqual(snap_state(run["id"]), before)

    def test_inactive_watch_refuses(self):
        run, settled, fp = settle_active(self)
        other = store.load(run["id"])
        other["watch"]["active"] = False
        other["watch"]["lifted_at"] = settled
        other["disposition"] = {"kind": "ACTIONABLE", "detail": "lifted",
                                "ref": KEY, "at": settled}
        other["waitReason"] = None
        store.save(other)
        before = snap_state(run["id"])
        res = self._configure(fp, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "inactive")
        self.assertEqual(snap_state(run["id"]), before)

    def test_busy_refuses_without_writes(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        with mock.patch.object(sched, "acquire_worker_lease", return_value=None):
            res = self._configure(fp, settled, add=["beta"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "busy")
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_malformed_types_refuse(self):
        run, settled, fp = settle_active(self)
        before = snap_state(run["id"])
        for bad in ({"add": "beta"}, {"add": [12]}, {"remove": "beta"},
                    {"remove": [None]}, {"add": [], "remove": []}):
            res = sched.configure_watch_inputs(
                REPO, KEY, expected_fp=fp, expected_settled_at=settled,
                actor="test", **bad)
            self.assertFalse(res["ok"], bad)
            self.assertEqual(res["status"], "invalid", bad)
        self.assertEqual(self.lookups, [])
        self.assertEqual(snap_state(run["id"]), before)

    def test_add_count_bound_refuses(self):
        run, settled, fp = settle_active(self)
        rows = base_rows() + [
            {"name": f"extra{i}", "kind": "npm", "installed": "1.0",
             "latest": "1.0", "update_kind": "none"} for i in range(9)]
        st = sched.load(REPO)
        st["last_rows"] = rows
        sched.save(st)
        many = [f"extra{i}" for i in range(5)]
        res = self._configure(fp, settled, add=many)
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")

    def test_total_cap_refuses(self):
        rows = base_rows() + [
            {"name": f"extra{i}", "kind": "npm", "installed": "1.0",
             "latest": "1.0", "update_kind": "none"} for i in range(9)]
        wide = {"registry": {PRIMARY: "1.0.0",
                             **{f"extra{i}": "1.0" for i in range(7)}}}
        run, settled, fp = settle_active(self, inputs=wide, rows=rows)
        self.resolver = {f"extra{i}": "1.0" for i in range(9)}
        res = self._configure(fp, settled, add=["extra7", "extra8"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "invalid")


class WatchInputsApiTests(unittest.TestCase):
    def setUp(self):
        make_home(self)
        repos.save({"name": REPO, "github": "o/r", "baseBranch": "main"})
        self.resolver = {}
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: self.resolver[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def test_malformed_payloads_400(self):
        for payload in ({"repo": REPO},
                        {"repo": REPO, "key": KEY},
                        {"repo": REPO, "key": KEY, "add": "beta",
                         "expected_fp": "f", "expected_settled_at": "s"},
                        {"repo": REPO, "key": KEY, "add": [1],
                         "expected_fp": "f", "expected_settled_at": "s"},
                        {"repo": REPO, "key": KEY, "add": [],
                         "expected_settled_at": "s"},
                        {"repo": REPO, "key": KEY, "add": [],
                         "expected_fp": "f"}):
            with self.assertRaises(serve.ApiError, msg=payload) as cm:
                serve.api_watch_inputs(payload)
            self.assertEqual(cm.exception.status, 400, payload)

    def test_unknown_repo_404(self):
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_watch_inputs({"repo": "nope", "key": KEY, "add": ["beta"],
                                    "expected_fp": "f",
                                    "expected_settled_at": "s"})
        self.assertEqual(cm.exception.status, 404)

    def test_unknown_key_404(self):
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_watch_inputs({"repo": REPO, "key": "dep-nope-0.0.1",
                                    "add": ["beta"], "expected_fp": "f" * 64,
                                    "expected_settled_at": "s"})
        self.assertEqual(cm.exception.status, 404)

    def test_busy_409_without_writes(self):
        with mock.patch.object(sched, "acquire_worker_lease", return_value=None):
            with self.assertRaises(serve.ApiError) as cm:
                serve.api_watch_inputs({"repo": REPO, "key": KEY,
                                        "add": ["beta"],
                                        "expected_fp": "f" * 64,
                                        "expected_settled_at": "s"})
        self.assertEqual(cm.exception.status, 409)

    def test_stale_409(self):
        run, settled, fp = settle_active(self)
        with self.assertRaises(serve.ApiError) as cm:
            serve.api_watch_inputs({"repo": REPO, "key": KEY, "add": ["beta"],
                                    "expected_fp": "0" * 64,
                                    "expected_settled_at": settled})
        self.assertEqual(cm.exception.status, 409)

    def test_success_round_trip(self):
        run, settled, fp = settle_active(self)
        self.resolver = {"beta": "0.88.0"}
        res = serve.api_watch_inputs({"repo": REPO, "key": KEY,
                                      "add": ["beta"], "expected_fp": fp,
                                      "expected_settled_at": settled})
        self.assertTrue(res["ok"])
        self.assertEqual(res["new_keys"], [PRIMARY, "beta"])
        self.assertEqual(res["observed"], {"beta": "0.88.0"})
        self.assertEqual(res["settled_at"], settled)


class WatchHistoryProjectionTests(unittest.TestCase):
    """`watch_history` is a whitelisted, bounded audit surface."""

    def setUp(self):
        make_home(self)
        repos.save({"name": REPO, "github": "o/r", "baseBranch": "main"})
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: {"beta": "0.88.0"}[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def test_projection_after_save(self):
        run, settled, fp = settle_active(self)
        other = store.load(run["id"])
        other["interventions"].append({"at": "x", "kind": "note",
                                       "detail": "unrelated"})
        store.save(other)
        res = sched.configure_watch_inputs(
            REPO, KEY, add=["beta"], expected_fp=fp,
            expected_settled_at=settled, actor="test")
        self.assertTrue(res["ok"], res)
        out = serve._run_outcome(store.load(run["id"]))
        wh = out["watch_history"]
        self.assertEqual(wh["total"], 1)
        self.assertEqual(len(wh["entries"]), 1)
        entry = wh["entries"][0]
        self.assertEqual(set(entry),
                         {"at", "actor", "repo", "key", "run_id", "added",
                          "removed", "old", "new", "observed", "source"})
        self.assertEqual(entry["actor"], "test")
        self.assertEqual(entry["run_id"], run["id"])
        self.assertEqual(entry["key"], KEY)
        self.assertEqual(entry["repo"], REPO)
        self.assertEqual(entry["added"], ["beta"])
        self.assertEqual(entry["removed"], [])
        self.assertEqual(entry["old"], {PRIMARY: "1.0.0"})
        self.assertEqual(entry["new"], {PRIMARY: "1.0.0", "beta": "0.88.0"})
        self.assertEqual(entry["observed"], {"beta": "0.88.0"})
        self.assertEqual(entry["source"],
                         {"workCommit": "base0", "workTreeFingerprint": "fp0"})

    def test_empty_history_honest(self):
        run, _settled, _fp = settle_active(self)
        out = serve._run_outcome(store.load(run["id"]))
        self.assertEqual(out["watch_history"], {"entries": [], "total": 0})

    def test_bounded_to_newest_five(self):
        run, settled, fp = settle_active(self)
        cur_fp, cur_settled = fp, settled
        # Alternate add/remove so each save appends one audit record.
        have_beta = False
        for _i in range(7):
            if have_beta:
                res = sched.configure_watch_inputs(
                    REPO, KEY, remove=["beta"], expected_fp=cur_fp,
                    expected_settled_at=cur_settled, actor="test")
            else:
                res = sched.configure_watch_inputs(
                    REPO, KEY, add=["beta"], expected_fp=cur_fp,
                    expected_settled_at=cur_settled, actor="test")
            self.assertTrue(res["ok"], res)
            have_beta = not have_beta
            cur_fp = res["fingerprint"]
        out = serve._run_outcome(store.load(run["id"]))
        wh = out["watch_history"]
        self.assertEqual(wh["total"], 7)
        self.assertEqual(len(wh["entries"]), 5)


class WatchResettleLifecycleTests(unittest.TestCase):
    """Configure -> release -> lift -> worker re-defer keeps monitoring.

    All fixtures are synthetic (fake registry, no model, no live runs):
    failures are recorded as test results, never as release claims.
    """

    T0 = "2026-10-19T05:00:00+00:00"
    T1 = "2026-10-19T06:00:00+00:00"

    def setUp(self):
        make_home(self)
        repos.save({"name": REPO, "github": "o/r", "baseBranch": "main"})
        self.resolver = {}
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: self.resolver[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def _configured(self):
        run, settled, fp = settle_active(self)
        self.resolver = {"beta": "0.88.0"}
        res = sched.configure_watch_inputs(
            REPO, KEY, add=["beta"], expected_fp=fp,
            expected_settled_at=settled, actor="test")
        self.assertTrue(res["ok"], res)
        return store.load(run["id"])

    def _lift(self, run, beta, rows_beta=None):
        observed = {"registry": {PRIMARY: "1.0.0", "beta": beta},
                    "issue": dict(ISSUE_INPUTS),
                    "sdk": dict(SDK_INPUTS),
                    "fetched_at": "2026-10-19T04:59:00+00:00"}
        with mock.patch.object(sched, "utcnow", return_value=self.T0):
            res = sched.refresh_external_watch(REPO, KEY, observed)
        self.assertTrue(res["changed"], res)
        return store.load(run["id"])

    def _resettle(self, run):
        with mock.patch.object(runner, "utcnow", return_value=self.T1):
            runner.settle_external_watch(
                run, reason="reassessed still deferred",
                evidence="pins re-observed", trigger="next peer release",
                inputs=None)
        return store.load(run["id"])

    def test_full_two_cycles_with_siblings_and_clocks(self):
        run = self._configured()
        run = self._lift(run, "0.89")
        watch = run["watch"]
        self.assertFalse(watch["active"])
        self.assertEqual(watch["lift_observation"],
                         {"observed_at": self.T0,
                          "inputs": {"registry": {PRIMARY: "1.0.0",
                                                 "beta": "0.89"},
                                     "issue": ISSUE_INPUTS,
                                     "sdk": SDK_INPUTS}})
        item = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["run_id"], run["id"])
        self.assertEqual(item.get("attempts"), 0)

        run = self._resettle(run)
        watch = run["watch"]
        # New pins adopted (no relift of the same release)...
        self.assertEqual(watch["inputs"]["registry"],
                         {PRIMARY: "1.0.0", "beta": "0.89"})
        # ...siblings survive byte-identical, comments 0 included...
        self.assertEqual(watch["inputs"]["issue"], ISSUE_INPUTS)
        self.assertEqual(watch["inputs"]["sdk"], SDK_INPUTS)
        # ...clocks honest: observation T0 kept, decision T1 new...
        self.assertEqual(watch["last_check"], self.T0)
        self.assertEqual(watch["settled_at"], self.T1)
        self.assertEqual(watch["next_check"], self.T1)
        self.assertTrue(watch["active"])
        self.assertIsNone(watch["lifted_at"])
        self.assertIsNone(watch["check_error"])
        self.assertNotIn("lift_observation", watch)
        kinds = [i.get("kind") for i in run.get("interventions", [])]
        self.assertIn("external-watch-settled", kinds)
        self.assertIn("external-watch-lifted", kinds)
        self.assertIn("external-watch-resettled", kinds)
        resettle = next(i for i in run["interventions"]
                        if i.get("kind") == "external-watch-resettled")
        self.assertTrue(resettle["complete"])
        self.assertEqual(resettle["run_id"], run["id"])
        self.assertTrue((run.get("waitReason") or "").startswith("watch:"))

        # Same pins unchanged: no lift, no model, still deferred.
        same = {"registry": {PRIMARY: "1.0.0", "beta": "0.89"},
                "issue": dict(ISSUE_INPUTS), "sdk": dict(SDK_INPUTS),
                "fetched_at": "2026-10-19T07:00:00+00:00"}
        with mock.patch.object(runner, "execute") as ex:
            res = sched.refresh_external_watch(REPO, KEY, same)
        self.assertTrue(res["checked"])
        self.assertFalse(res.get("changed"))
        ex.assert_not_called()
        self.assertTrue(store.load(run["id"])["watch"]["active"])

        # Next release lifts the SAME run again, attempts preserved.
        nxt = dict(same)
        nxt["registry"] = {PRIMARY: "1.0.0", "beta": "0.90"}
        res = sched.refresh_external_watch(REPO, KEY, nxt)
        self.assertTrue(res["changed"])
        now = store.load(run["id"])
        self.assertFalse(now["watch"]["active"])
        item2 = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        self.assertEqual(item2["run_id"], run["id"])
        self.assertEqual(item2.get("attempts"), 0)

    def test_partial_observation_keeps_pins_then_no_wake(self):
        run = self._configured()
        run = self._lift(run, "0.89")
        prev_check = run["watch"]["last_check"]
        tampered = store.load(run["id"])
        tampered["watch"]["lift_observation"]["inputs"]["registry"] = \
            {PRIMARY: "1.0.0"}
        store.save(tampered)
        run = self._resettle(tampered)
        watch = run["watch"]
        self.assertEqual(watch["inputs"]["registry"],
                         {PRIMARY: "1.0.0", "beta": "0.88.0"})
        self.assertTrue(watch["active"])
        self.assertTrue(watch["check_error"])
        self.assertIn("kept prior pins", watch["check_error"])
        self.assertEqual(watch["last_check"], prev_check)
        # A later complete observation of the kept pins stays deferred.
        kept = {"registry": {PRIMARY: "1.0.0", "beta": "0.88.0"},
                "issue": dict(ISSUE_INPUTS), "sdk": dict(SDK_INPUTS)}
        res = sched.refresh_external_watch(REPO, KEY, kept)
        self.assertTrue(res["checked"])
        self.assertFalse(res.get("changed"))
        self.assertTrue(store.load(run["id"])["watch"]["active"])

    def test_extra_obs_keys_never_become_policy(self):
        run = self._configured()
        observed = {"registry": {PRIMARY: "1.0.0", "beta": "0.89",
                                 "gamma": "9.9.9"},
                    "issue": dict(ISSUE_INPUTS), "sdk": dict(SDK_INPUTS)}
        with mock.patch.object(sched, "utcnow", return_value=self.T0):
            res = sched.refresh_external_watch(REPO, KEY, observed)
        self.assertTrue(res["changed"])
        run = self._resettle(store.load(run["id"]))
        self.assertEqual(run["watch"]["inputs"]["registry"],
                         {PRIMARY: "1.0.0", "beta": "0.89"})

    def test_malformed_baseline_refuses_without_writes(self):
        run, _settled, _fp = settle_active(self)
        cases = [
            {"registry": {"alpha": ["not", "a", "string"]}},
            {"registry": {"alpha": "1.0.0"}, "weird": {"x": "1"}},
            {"registry": {"alpha": "1.0.0"},
             "issue": dict(ISSUE_INPUTS, comments=True)},
            {"registry": {"alpha": "1.0.0"},
             "issue": dict(ISSUE_INPUTS, comments=-1)},
            ["not", "a", "record"],
        ]
        for bad in cases:
            state = store.load(run["id"])
            state["watch"]["inputs"] = bad
            store.save(state)
            before = snap_state(run["id"])
            with self.assertRaises(ValueError, msg=bad):
                runner.settle_external_watch(
                    store.load(run["id"]), reason="r", evidence="e",
                    trigger="t", inputs=None)
            self.assertEqual(snap_state(run["id"]), before)

    def test_fresh_none_has_no_resettle_record(self):
        state = store.create(REPO, "12", "t", "base0", {"worker": "test"})
        state["queue_key"] = KEY
        state["repo"] = REPO
        watch = runner.settle_external_watch(
            state, reason="r", evidence="e", trigger="t", inputs=None)
        self.assertEqual(watch["inputs"], {})
        kinds = [i.get("kind") for i in state.get("interventions", [])]
        self.assertEqual(kinds, ["external-watch-settled"])

    def test_explicit_inputs_keep_intentional_semantics(self):
        run, _settled, _fp = settle_active(self)
        with mock.patch.object(runner, "utcnow", return_value=self.T1):
            watch = runner.settle_external_watch(
                store.load(run["id"]), reason="r", evidence="e",
                trigger="t", inputs={"registry": {"solo": "1.0.0"}})
        self.assertEqual(watch["inputs"], {"registry": {"solo": "1.0.0"}})
        self.assertEqual(watch["last_check"], self.T1)
        self.assertIsNone(watch["check_error"])
        kinds = [i.get("kind") for i in
                 store.load(run["id"]).get("interventions", [])]
        self.assertNotIn("external-watch-resettled", kinds)

    def test_resettle_helper_boundaries(self):
        good_obs = {"observed_at": self.T0,
                    "inputs": {"registry": {"alpha": "1.0.0"}}}
        base = {"registry": {"alpha": "1.0.0"}}
        # Missing snapshot / stamp / class / pin: keep prior pins.
        for obs in (None, "x", {"inputs": {"registry": {"alpha": "2.0.0"}}},
                    {"observed_at": self.T0, "inputs": "x"},
                    {"observed_at": self.T0,
                     "inputs": {"registry": {"alpha": ["2.0.0"]}}},
                    {"observed_at": self.T0,
                     "inputs": {"registry": {}}}):
            new, complete, note, _at = runner._resettle_inputs(base, obs)
            self.assertFalse(complete, obs)
            self.assertEqual(new, base, obs)
            self.assertTrue(note, obs)
        # Bool comments is not a valid flat issue record: the whole
        # baseline is refused, never cleared.
        bad_issue = dict(ISSUE_INPUTS, comments=True)
        with self.assertRaises(ValueError):
            runner._resettle_inputs(
                {"registry": {"alpha": "1.0.0"}, "issue": bad_issue},
                {"observed_at": self.T0,
                 "inputs": {"registry": {"alpha": "2.0.0"},
                            "issue": bad_issue}})


class ConfiguredPeerEngineTests(unittest.TestCase):
    """A newly configured peer flows through the EXISTING engine."""

    def setUp(self):
        make_home(self)
        orig = deps._latest_fn
        deps.set_latest_fn(lambda kind, ident: {"alpha": "1.0.0",
                                                "beta": "0.88.0"}[ident])
        self.addCleanup(lambda: deps.set_latest_fn(orig))

    def _watch(self, baseline):
        state = store.create(REPO, "12", "t", "base0", {"worker": "test"})
        state["queue_key"] = KEY
        state["repo"] = REPO
        runner.settle_external_watch(
            state, reason="wait for peer release", evidence="pins observed",
            trigger="peer release then reassess", inputs=baseline,
            settled_by="test")
        st = sched.load(REPO)
        st["queue"].append({"key": KEY, "status": "waiting-review",
                            "verified": False, "run_id": state["id"],
                            "kind": "dep-update", "dep": PRIMARY,
                            "attempts": 0})
        sched.save(st)
        return store.load(state["id"])

    def _rows(self, beta_latest):
        return [{"name": PRIMARY, "kind": "npm", "installed": "1.0.0",
                 "latest": "1.0.0", "update_kind": "none"},
                {"name": "beta", "kind": "npm", "installed": "0.87.0",
                 "latest": beta_latest, "update_kind": "none"}]

    def test_unchanged_peer_no_lift_no_model(self):
        run = self._watch({"registry": {PRIMARY: "1.0.0", "beta": "0.88.0"}})
        item = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        rows = self._rows("0.88.0")
        with mock.patch.object(runner, "execute") as ex:
            observed = sched.observe_watch_inputs(REPO, item, run, rows, None)
            res = sched.refresh_external_watch(REPO, KEY, observed)
        self.assertTrue(res["checked"])
        self.assertFalse(res["changed"])
        ex.assert_not_called()
        now = store.load(run["id"])
        self.assertTrue(now["watch"]["active"])
        item2 = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        self.assertEqual(item2["status"], "waiting-review")
        self.assertEqual(item2.get("attempts"), 0)

    def test_changed_peer_lifts_same_run(self):
        run = self._watch({"registry": {PRIMARY: "1.0.0", "beta": "0.88.0"}})
        item = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        rows = self._rows("0.89.0")
        with mock.patch.object(runner, "execute") as ex:
            observed = sched.observe_watch_inputs(REPO, item, run, rows, None)
            res = sched.refresh_external_watch(REPO, KEY, observed)
        self.assertTrue(res["changed"])
        ex.assert_not_called()
        now = store.load(run["id"])
        self.assertFalse(now["watch"]["active"])
        self.assertEqual(now["disposition"]["kind"], "ACTIONABLE")
        item2 = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        self.assertEqual(item2["status"], "queued")
        self.assertEqual(item2["run_id"], run["id"])
        self.assertEqual(item2.get("attempts"), 0)

    def test_failed_peer_observation_stays_deferred(self):
        run = self._watch({"registry": {PRIMARY: "1.0.0", "beta": "0.88.0"}})
        item = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        rows = [{"name": PRIMARY, "kind": "npm", "installed": "1.0.0",
                 "latest": "1.0.0", "update_kind": "none"}]
        with mock.patch.object(runner, "execute") as ex:
            with mock.patch.object(deps, "_latest_fn",
                                    side_effect=LookupError("down")):
                observed = sched.observe_watch_inputs(REPO, item, run,
                                                      rows, None)
                res = sched.refresh_external_watch(REPO, KEY, observed)
        self.assertIsNone(observed)
        self.assertTrue(res["checked"])
        self.assertFalse(res["changed"])
        ex.assert_not_called()
        now = store.load(run["id"])
        self.assertTrue(now["watch"]["active"])
        self.assertTrue(now["watch"]["check_error"])

    def test_partial_peer_observation_stays_deferred(self):
        run = self._watch({"registry": {PRIMARY: "1.0.0", "beta": "0.88.0"}})
        item = next(i for i in sched.load(REPO)["queue"] if i["key"] == KEY)
        rows = [{"name": PRIMARY, "kind": "npm", "installed": "1.0.0",
                 "latest": "1.0.0", "update_kind": "none"}]
        with mock.patch.object(deps, "_latest_fn",
                                side_effect=LookupError("down")):
            observed = sched.observe_watch_inputs(REPO, item, run, rows, None)
        # Registry-only baseline with one unobservable key: no observation.
        self.assertIsNone(observed)

    def test_observation_metadata_noise_never_wakes(self):
        run = self._watch({"registry": {PRIMARY: "1.0.0", "beta": "0.88.0"}})
        observed = {"registry": {PRIMARY: "1.0.0", "beta": "0.88.0",
                                 "unrelated-new-package": "9.9.9"},
                    "fetched_at": "2026-10-19T04:00:00+00:00"}
        res = sched.refresh_external_watch(REPO, KEY, observed)
        self.assertTrue(res["checked"])
        self.assertFalse(res.get("changed"))
        self.assertTrue(store.load(run["id"])["watch"]["active"])


if __name__ == "__main__":
    unittest.main()
