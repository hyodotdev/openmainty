"""Durable coverage: review-batch APIs served by the dashboard.

The single HTTP import path (/api/batch/import) is a thin operator-
trusted wrapper around batches.import_batch: these tests pin the read
surface (list/detail/evidence), traversal rejection, and that import
accepts valid manifests while refusing invalid ones with the
validator's own verdicts.
Run with: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openmainty import batches, runner, sched, store  # noqa: E402
from test_serve import ServeMixin  # noqa: E402
try:
    from tests._home import use_temp_home, offline_qa_registry  # noqa: E402
except ImportError:  # discover top-level mode: tests/ is on sys.path
    from _home import use_temp_home, offline_qa_registry  # noqa: E402


def _bind_accept(run_id, head, fp):
    from openmainty import store
    run = store.load(run_id)
    for step in store.VERIFY_STEPS:
        run["steps"][step]["status"] = "done"
    run["workCommit"] = head
    run["workTreeFingerprint"] = fp
    run["verifiedCommits"] = {"review": head, "e2e": head}
    run["verifiedFingerprints"] = {"review": fp, "e2e": fp}
    store.save(run)


def _base_head(wt):
    import subprocess
    return subprocess.run(
        ["git", "rev-parse", "main"], cwd=str(wt), check=True,
        text=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL).stdout.strip()


class BatchSeedMixin:
    """Helper-only seed mixin (no TestCase: never collected or duplicated)."""

    def _seed(self, extras=()):
        from openmainty import batches
        origin = Path(self.tmp.name) / "origin"
        origin.mkdir()
        for args in (["init", "-b", "main"],):
            self._git(origin, *args)
        (origin / "f.txt").write_text("v1\n")
        self._git(origin, "add", ".")
        self._git(origin, "commit", "-m", "base")
        self._git(origin, "checkout", "-b", "codex/next/demo-1")
        (origin / "f.txt").write_text("v2\n")
        self._git(origin, "commit", "-am", "cand")
        proof = Path(self.tmp.name) / "proof.json"
        proof.write_bytes(b'{"ok": true}')
        run = store.create("fakerepo", "jest@30.5.2", "t", None, {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k", "repo": "fakerepo", "dep": "jest",
             "target": "30.5.2", "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/next/demo-1"})
        sched.save(st)
        snap = runner.source_snapshot(origin)
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        payload = {
            "id": "api-batch-1", "repo": "fakerepo",
            "target_branch": "next", "purpose": "stable",
            "origin": {"checkout": str(origin), "base_branch": "main",
                       "base_head": _base_head(origin)},
            "primaries": [{"dep": "jest", "target": "30.5.2", "lane": "next",
                           "run_id": run["id"],
                           "source": {"worktree": str(origin),
                                      "branch": "codex/next/demo-1",
                                      "head": snap["head"],
                                      "fingerprint": snap["fingerprint"]},
                           "review": {"verdict": "accept"}}],
            "integrations": [{"lane": "next", "worktree": str(origin),
                              "status": "testing",
                              "gates": [{"key": "unit", "status": "pass",
                                         "inputs": {"head": snap["head"],
                                                    "fingerprint": snap["fingerprint"]},
                                         "evidence": [
                                             {"label": "log", "kind": "file",
                                              "origin": str(proof)}] + [
                                             {"label": p.name, "kind": "file",
                                              "origin": str(p)}
                                             for p in extras]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }
        self._last_payload = payload
        return batches.import_batch(payload)

    def _git(self, cwd, *args):
        import subprocess
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        *args], cwd=str(cwd), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class BatchApiTests(ServeMixin, BatchSeedMixin):
    def test_batches_list_and_detail(self):
        b = self._seed()
        code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        self.assertIn("batches", body)
        self.assertIn("summary", body)
        self.assertEqual(body["batches"][0]["id"], "api-batch-1")
        code, detail = self.get("/api/batch?id=api-batch-1")
        self.assertEqual(code, 200)
        self.assertEqual(detail["batch"]["id"], "api-batch-1")
        self.assertIn("evaluation", detail)
        self.assertIn("unregistered", detail)
        self.assertTrue(detail["evaluation"]["results"][0]["ready"])
        _ = b

    def test_batch_unknown_is_404(self):
        code, body = self.get("/api/batch?id=nope")
        self.assertEqual(code, 404)

    def _code(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(self.url(path),
                                        timeout=30) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_evidence_served_and_traversal_rejected(self):
        self._seed()
        from openmainty import batches
        b = batches.load_batch("api-batch-1")
        name = Path(b["integrations"][0]["gates"][0]["evidence"][0]
                    ["stored_path"]).name
        self.assertEqual(
            self._code(f"/api/batch/evidence?batch=api-batch-1&name={name}"),
            200)
        for bad in ("../x.json", "/etc/passwd", ".hidden"):
            self.assertEqual(
                self._code(
                    "/api/batch/evidence?batch=api-batch-1&name=" + bad),
                404)
        self.assertEqual(
            self._code("/api/batch/evidence?batch=nope&name=x"), 404)

    def _head(self, path):
        import urllib.request
        with urllib.request.urlopen(self.url(path),
                                     timeout=30) as resp:
            return resp.status, resp.headers.get_content_type(), \
                resp.headers.get("X-Content-Type-Options")

    def test_evidence_safe_mime_over_http(self):
        """Raster PNG renders inline; SVG/JSON stay inert plaintext
        with nosniff — no scriptable type on the app origin."""
        from openmainty import batches
        png = Path(self.tmp.name) / "shot.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        svg = Path(self.tmp.name) / "evil.svg"
        svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"'
                        b' onload="alert(1)"></svg>')
        doc = Path(self.tmp.name) / "junit.xml"
        doc.write_bytes(b"<xml/>")
        self._seed(extras=(png, svg, doc))
        b = batches.load_batch("api-batch-1")
        gate = b["integrations"][0]["gates"][0]
        names = [Path(e["stored_path"]).name for e in gate["evidence"]]
        got = {}
        for name in names:
            code, ctype, nosniff = self._head(
                f"/api/batch/evidence?batch=api-batch-1&name={name}")
            self.assertEqual(code, 200)
            self.assertEqual(nosniff, "nosniff")
            got[name.rsplit("-", 1)[-1]] = ctype
        self.assertEqual(got["shot.png"], "image/png")
        self.assertEqual(got["evil.svg"], "text/plain")
        self.assertEqual(got["junit.xml"], "text/plain")

    def test_inherited_ready_over_http_with_distinct_paths(self):
        """Public detail route resolves A+B readiness through the shared
        closure when A and B own genuinely different checkouts; a real
        move of A's source blocks B with an inherited reason."""
        from openmainty import batches
        proof = Path(self.tmp.name) / "proof.json"
        proof.write_bytes(b'{"ok": true}')

        def _origin(name, branch, dep, target, key):
            wt = Path(self.tmp.name) / name
            wt.mkdir()
            self._git(wt, "init", "-b", "main")
            (wt / "f.txt").write_text("v1\n")
            self._git(wt, "add", ".")
            self._git(wt, "commit", "-m", "base")
            self._git(wt, "checkout", "-b", branch)
            (wt / "f.txt").write_text("v2\n")
            self._git(wt, "commit", "-am", "cand")
            run = store.create("fakerepo", dep + "@" + target, "t", None,
                               {"by": "t"})
            st = sched.load("fakerepo")
            st.setdefault("queue", []).append(
                {"key": key, "repo": "fakerepo", "dep": dep,
                 "target": target, "lane": "next", "status": "waiting-review",
                 "run_id": run["id"], "branch": branch})
            sched.save(st)
            snap = runner.source_snapshot(wt)
            _bind_accept(run["id"], snap["head"], snap["fingerprint"])
            return {
                "repo": "fakerepo", "target_branch": "next",
                "purpose": "stable",
                "origin": {"checkout": str(wt), "base_branch": "main",
                           "base_head": _base_head(wt)},
                "primaries": [
                    {"dep": dep, "target": target, "lane": "next",
                     "run_id": run["id"],
                     "source": {"worktree": str(wt), "branch": branch,
                                "head": snap["head"],
                                "fingerprint": snap["fingerprint"]},
                     "review": {"verdict": "accept"}}],
                "integrations": [
                    {"lane": "next", "worktree": str(wt),
                     "status": "testing",
                     "gates": [{"key": "unit", "status": "pass",
                                "inputs": {"head": snap["head"],
                                           "fingerprint": snap["fingerprint"]},
                                "evidence": [{"label": "log", "kind": "file",
                                              "origin": str(proof)}]}]}],
                "policy": {"required_gates": ["unit"],
                           "gates": {"unit": {}}},
            }

        base_payload = _origin("origin-b", "codex/next/demo-2",
                               "eslint", "10.0.0", "k-b")
        base_payload["id"] = "api-base"
        batches.import_batch(base_payload)
        top_payload = _origin("origin-a", "codex/next/demo-1",
                              "jest", "30.5.2", "k-a")
        top_payload["id"] = "api-top"
        batches.import_batch(top_payload)
        # Honest reimport path adds the inheritance link (validated).
        top_payload["inherited"] = [{"group": "stable", "count": 1,
                                     "batch": "api-base"}]
        batches.import_batch(top_payload)
        code, detail = self.get("/api/batch?id=api-top")
        self.assertEqual(code, 200)
        self.assertTrue(detail["evaluation"]["results"][0]["ready"],
                        detail["evaluation"]["results"][0]["reasons"])
        origin_b = Path(self.tmp.name) / "origin-b"
        (origin_b / "f.txt").write_text("v3\n")
        self._git(origin_b, "commit", "-am", "drift-b")
        code, detail = self.get("/api/batch?id=api-top")
        self.assertEqual(code, 200)
        self.assertFalse(detail["evaluation"]["results"][0]["ready"])
        self.assertTrue(any(
            "inherit" in r.lower()
            for r in detail["evaluation"]["results"][0]["reasons"]))

    def test_first_ever_review_visible_without_batches(self):
        """Repo-scoped unregistered projection needs no batch: a natural
        waiting-review queue item is visible via /api/batches."""
        from openmainty import batches, repos
        repos.save({"name": "fakerepo", "github": "example/fakerepo",
                    "baseBranch": "main", "knowledge": [], "checks": [],
                    "e2e": [], "release": {}})
        run = store.create("fakerepo", "jest@30.5.2", "t", None, {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-first", "repo": "fakerepo", "dep": "jest",
             "target": "30.5.2", "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/next/demo-1"})
        sched.save(st)
        self.assertEqual(batches.list_batches(), [])
        code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        un = body.get("unregistered", {}).get("fakerepo", [])
        self.assertTrue(any(u.get("dep") == "jest" for u in un), un)
        _ = batches

    def test_batch_changes_scoped_to_recorded_range(self):
        self._seed()
        code, body = self.get("/api/batch/changes?batch=api-batch-1"
                              "&lane=next")
        self.assertEqual(code, 200)
        self.assertTrue(any(c.get("subject") == "cand"
                            for c in body["commits"]), body["commits"])
        self.assertIn("f.txt", body["files"])
        code, diff = self.get("/api/batch/changes?batch=api-batch-1"
                              "&lane=next&path=f.txt")
        self.assertEqual(code, 200)
        self.assertIn("v2", diff["diff"])
        for bad, want in (("batch=nope&lane=next", 404),
                          ("batch=api-batch-1&lane=main", 404),
                          ("batch=api-batch-1&lane=next&path=../x", 400)):
            c, _ = self.get("/api/batch/changes?" + bad)
            self.assertEqual(c, want, bad)

    def test_list_cards_carry_content_revision_identities(self):
        """Poll fp inputs: recorded + live heads/fingerprints, gate input
        bindings and artifact hashes — no clock."""
        self._seed()
        code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        card = [x for x in body["batches"] if x["id"] == "api-batch-1"][0]
        integ = card["integrations"][0]
        self.assertTrue(integ["fingerprint"])
        self.assertTrue(integ["live_head"])
        self.assertIn(integ["live_clean"], (True, False))
        sig = integ["gates"][0]
        self.assertTrue(sig["inputs_head"])
        self.assertTrue(sig["ev"])
        self.assertNotIn("evaluated_at", json.dumps(sig))

    def test_list_cards_carry_primary_items(self):
        """List titles: typed dep/target/lane/run_id projection only.

        Same shape the cached snapshot and the cold fallback both emit:
        exact key set, real values from the seeded primary, no source or
        evidence expansion."""
        self._seed()
        code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        card = [x for x in body["batches"] if x["id"] == "api-batch-1"][0]
        items = card.get("primary_items")
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0].keys()),
                         {"dep", "target", "lane", "run_id"})
        self.assertEqual(items[0]["dep"], "jest")
        self.assertEqual(items[0]["target"], "30.5.2")
        self.assertEqual(items[0]["lane"], "next")
        self.assertTrue(items[0]["run_id"])
        self.assertEqual(card.get("primaries"), 1)

    def test_list_cards_carry_typed_issue_primary(self):
        """Typed issue primary: exact issue key set, honest total.

        Same projection the cached snapshot and the cold fallback emit:
        a validated issue block keeps only issue/repo/lane, a hostile or
        malformed primary list yields skipped entries so the UI falls back
        to legacy titles instead of showing a made-up issue title."""
        from openmainty import batches
        batch = {"repo": "fakerepo", "primaries": [
            {"lane": "next", "run_id": "run-1",
             "issue": {"issue": "856", "repo": "fakerepo", "lane": "next"}}]}
        items = batches._primary_items(batch)
        self.assertEqual(len(items), 1)
        self.assertEqual(set(items[0].keys()),
                         {"dep", "target", "lane", "run_id", "issue"})
        self.assertEqual(items[0]["issue"],
                         {"issue": "856", "repo": "fakerepo", "lane": "next"})
        self.assertEqual(items[0]["dep"], "")
        # mixed valid + malformed: malformed entries skipped, never collide.
        batch["primaries"] = [
            {"lane": "next", "run_id": "run-1",
             "issue": {"issue": "856", "repo": "fakerepo", "lane": "next"}},
            {"lane": "next", "run_id": "run-2",
             "issue": {"issue": "0", "repo": "fakerepo", "lane": "next"}},
            {"lane": "next", "run_id": "run-3",
             "issue": {"issue": "857", "repo": "other", "lane": "next"}},
            {"dep": "turbo", "target": "2.11.10", "lane": "next",
             "run_id": "run-4"},
        ]
        items = batches._primary_items(batch)
        self.assertEqual([i.get("issue", {}).get("issue") or i.get("dep")
                          for i in items],
                         ["856", "turbo"])
        # hostile strings are preserved verbatim here and escaped at the
        # esc() use site; names under the 256 bound keep full identity so
        # two same-prefix packages stay distinguishable in metadata.
        long_dep = "n" * 200
        items = batches._primary_items(
            {"repo": "r", "primaries": [{"dep": long_dep, "target": "1.0.0",
                                         "lane": "next"}]})
        self.assertEqual(items[0]["dep"], long_dep)
        over_dep = "n" * 300
        items = batches._primary_items(
            {"repo": "r", "primaries": [{"dep": over_dep, "target": "1.0.0",
                                         "lane": "next"}]})
        self.assertEqual(len(items[0]["dep"]), 256)

    def test_batch_detail_carries_live_block(self):
        self._seed()
        code, detail = self.get("/api/batch?id=api-batch-1")
        self.assertEqual(code, 200)
        live = detail.get("live", {})
        wt_keys = [k for k in live if k.startswith("wt:")]
        self.assertTrue(wt_keys)
        self.assertTrue(live[wt_keys[0]]["head"])

    def test_batch_detail_carries_queue_context(self):
        self._seed()
        from openmainty import batches
        b = batches.load_batch("api-batch-1")
        run_id = b["primaries"][0]["run_id"]
        code, detail = self.get("/api/batch?id=api-batch-1")
        self.assertEqual(code, 200)
        self.assertIn(run_id, detail.get("queue", {}))

    def _http_payload(self, batch_id):
        from openmainty import batches, runner, sched, store
        origin = Path(self.tmp.name) / "origin-http"
        origin.mkdir()
        for args in (["init", "-b", "main"],):
            self._git(origin, *args)
        (origin / "f.txt").write_text("v1\n")
        self._git(origin, "add", ".")
        self._git(origin, "commit", "-m", "base")
        self._git(origin, "checkout", "-b", "codex/next/demo-http")
        (origin / "f.txt").write_text("v2\n")
        self._git(origin, "commit", "-am", "cand")
        proof = Path(self.tmp.name) / "proof-http.json"
        proof.write_bytes(b'{"ok": true}')
        run = store.create("fakerepo", "jest@30.5.3", "t", None, {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-http", "repo": "fakerepo", "dep": "jest",
             "target": "30.5.3", "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/next/demo-http"})
        sched.save(st)
        snap = runner.source_snapshot(origin)
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        return {
            "id": batch_id, "repo": "fakerepo",
            "target_branch": "next", "purpose": "stable",
            "origin": {"checkout": str(origin), "base_branch": "main",
                       "base_head": _base_head(origin)},
            "primaries": [{"dep": "jest", "target": "30.5.3", "lane": "next",
                           "run_id": run["id"],
                           "source": {"worktree": str(origin),
                                      "branch": "codex/next/demo-http",
                                      "head": snap["head"],
                                      "fingerprint": snap["fingerprint"]},
                           "review": {"verdict": "accept"}}],
            "integrations": [{"lane": "next", "worktree": str(origin),
                              "status": "testing",
                              "gates": [{"key": "unit", "status": "pass",
                                         "inputs": {"head": snap["head"],
                                                    "fingerprint": snap["fingerprint"]},
                                         "evidence": [
                                             {"label": "log", "kind": "file",
                                              "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }

    def test_http_import_accepts_valid_manifest(self):
        code, body = self.post("/api/batch/import",
                               {"manifest": self._http_payload("api-batch-http")})
        self.assertEqual(code, 200)
        self.assertEqual(body.get("id"), "api-batch-http")
        self.assertEqual(body.get("primaries"), 1)
        code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        self.assertTrue(any(b.get("id") == "api-batch-http"
                            for b in body.get("batches", [])))

    def test_http_import_refuses_invalid_manifest(self):
        payload = self._http_payload("api-batch-bad")
        payload["primaries"][0]["run_id"] = "run-does-not-exist"
        code, body = self.post("/api/batch/import", {"manifest": payload})
        self.assertEqual(code, 409)

    def test_http_import_rejects_non_object(self):
        code, _ = self.post("/api/batch/import", {"manifest": []})
        self.assertEqual(code, 400)


class ListObservationTests(ServeMixin, BatchSeedMixin):
    """Read-path observability: same-evaluation cards/summary with a
    unique per-envelope observation id and bounded ORIGINAL failure
    context. No readiness/fingerprint policy change."""

    def _origin(self, name, branch, dep, target, key, proof):
        wt = Path(self.tmp.name) / name
        wt.mkdir()
        self._git(wt, "init", "-b", "main")
        (wt / "f.txt").write_text("v1\n")
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "base")
        self._git(wt, "checkout", "-b", branch)
        (wt / "f.txt").write_text("v2\n")
        self._git(wt, "commit", "-am", "cand")
        run = store.create("fakerepo", dep + "@" + target, "t", None,
                           {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": key, "repo": "fakerepo", "dep": dep,
             "target": target, "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": branch})
        sched.save(st)
        snap = runner.source_snapshot(wt)
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        return {
            "repo": "fakerepo", "target_branch": "next",
            "purpose": "stable",
            "origin": {"checkout": str(wt), "base_branch": "main",
                       "base_head": _base_head(wt)},
            "primaries": [
                {"dep": dep, "target": target, "lane": "next",
                 "run_id": run["id"],
                 "source": {"worktree": str(wt), "branch": branch,
                            "head": snap["head"],
                            "fingerprint": snap["fingerprint"]},
                 "review": {"verdict": "accept"}}],
            "integrations": [
                {"lane": "next", "worktree": str(wt),
                 "status": "testing",
                 "gates": [{"key": "unit", "status": "pass",
                            "inputs": {"head": snap["head"],
                                       "fingerprint": snap["fingerprint"]},
                            "evidence": [{"label": "log", "kind": "file",
                                          "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }

    def _seed_chain(self):
        from openmainty import batches
        proof = Path(self.tmp.name) / "proof-obs.json"
        proof.write_bytes(b'{"ok": true}')
        grand = self._origin("origin-g", "codex/next/demo-g", "gdep",
                             "1.0.0", "k-g", proof)
        grand["id"] = "api-grand"
        batches.import_batch(grand)
        mid = self._origin("origin-m", "codex/next/demo-m", "mdep",
                           "1.0.0", "k-m", proof)
        mid["id"] = "api-mid"
        mid["inherited"] = [{"group": "g", "count": 1,
                             "batch": "api-grand"}]
        batches.import_batch(mid)
        top = self._origin("origin-t", "codex/next/demo-t", "tdep",
                           "1.0.0", "k-t", proof)
        top["id"] = "api-top3"
        top["inherited"] = [{"group": "m", "count": 1, "batch": "api-mid"}]
        batches.import_batch(top)
        return ("api-grand", "api-mid", "api-top3")

    def test_envelope_obs_unique_per_request(self):
        self._seed()
        _, first = self.get("/api/batches")
        _, second = self.get("/api/batches")
        self.assertTrue(first.get("obs", "").startswith("obs-"), first.get("obs"))
        self.assertTrue(second.get("obs", "").startswith("obs-"))
        self.assertNotEqual(first["obs"], second["obs"])
        card_obs = {c["obs"] for c in first["batches"]}
        self.assertEqual(card_obs, {first["obs"]})
        _, detail = self.get("/api/batch?id=api-batch-1")
        self.assertTrue(detail.get("obs", "").startswith("obs-"))
        self.assertNotEqual(detail["obs"], first["obs"])

    def test_cards_and_summary_share_one_evaluation(self):
        # S7-a: /api/batches serves one precomputed observation (written
        # by the import refresh during seeding). Cards and summary share
        # that observation; the request itself evaluates nothing.
        from openmainty import batches
        from openmainty.serve import batches as serve_batches
        self._seed()
        real = serve_batches.evaluate
        calls = []

        def _counting(batch, live=None, _chain=(), **kw):
            calls.append((batch or {}).get("id"))
            return real(batch, live, _chain, **kw)

        with mock.patch.object(serve_batches, "evaluate",
                               side_effect=_counting):
            code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        self.assertEqual(calls, [])
        card = body["batches"][0]
        row = body["summary"]["fakerepo"]
        self.assertEqual(card["ready"], row["ready"])
        self.assertEqual(card["evaluation"]["ready"], row["ready"])
        self.assertIsNone(card["evaluation"]["first_failure"])
        self.assertEqual(card["obs"], body["obs"])
        _ = batches

    def test_missing_precomputed_fails_closed_without_evaluate(self):
        from openmainty import batches
        b = self._seed()
        live = {"api-batch-1": batches.live_map([b])}
        with mock.patch.object(batches, "evaluate",
                               side_effect=AssertionError(
                                   "must not re-evaluate")):
            row = batches.summarize([b], live, precomputed={})["fakerepo"]
        self.assertEqual(row["ready"], 0)
        self.assertEqual(row["validating"], 1)

    def test_multilevel_inherited_failure_names_failing_component(self):
        from openmainty import batches
        grand_id, mid_id, top_id = self._seed_chain()
        origin_g = Path(self.tmp.name) / "origin-g"
        (origin_g / "f.txt").write_text("v3 drift\n")
        self._git(origin_g, "commit", "-am", "drift-g")
        # S7-a: freshness comes from a background refresh (stamp + manual
        # refresh), never from per-request evaluation.
        batches.refresh_batches_snapshot("test")
        code, body = self.get("/api/batches")
        self.assertEqual(code, 200)
        cards = {c["id"]: c for c in body["batches"]}
        top = cards[top_id]
        self.assertEqual(top["ready"], 0)
        ff = top["evaluation"]["first_failure"]
        self.assertIsNotNone(ff)
        self.assertTrue(ff["inherited"], ff)
        descs = ff.get("descendants") or []
        self.assertTrue(descs, ff)
        mid = next(d for d in descs if d.get("batch") == mid_id)
        nested = mid.get("first_failure")
        self.assertIsNotNone(nested)
        g_descs = nested.get("descendants") or []
        self.assertTrue(g_descs, nested)
        g = next(d for d in g_descs if d.get("batch") == grand_id)
        gff = g.get("first_failure")
        self.assertIsNotNone(gff)
        self.assertIn("gdep", gff["key"])
        failing = [s for s in (gff.get("snapshots") or [])
                   if s.get("match") != "match"]
        self.assertTrue(failing, gff.get("snapshots"))
        self.assertTrue(all(s.get("scope") in (
            "integration", "candidate", "base", "inherited")
            for s in gff["snapshots"]))
        blob = json.dumps(body)
        self.assertNotIn("None", blob)
        for snap in gff["snapshots"]:
            self.assertLessEqual(len(snap["worktree"]), 161)
            self.assertNotEqual(snap.get("live_fingerprint"), "None")
        self.assertLessEqual(len(ff["snapshots"]), 6)
        self.assertLessEqual(len(ff["reasons"]), 3)
        for reason in ff["reasons"]:
            self.assertLessEqual(len(reason), 161)
        self.assertLessEqual(len(ff["leaf"]), 501)
        row = body["summary"]["fakerepo"]
        self.assertEqual(row["ready"], 0)
        _ = batches


class OnDemandFastcheckTests(ServeMixin):
    """S18b: POST /api/integration/fastcheck {op, confirm:true} runs
    the same runner on a done op with no receipt. All offline: a
    fake runner, a temp home, no registry and no real worktree."""

    OP = "prep-run-od1-base-1"

    def write_op(self, status="done"):
        from openmainty import batches
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        wt = Path(self.tmp.name) / "wt-od1"
        wt.mkdir(exist_ok=True)
        rec = {"op": "prepare", "op_id": self.OP, "run_id": "run-od1",
               "status": status,
               "result": {"path": str(wt), "branch": "codex/od1",
                          "head": "h" * 40, "fingerprint": "f" * 64},
               "gates": [{"key": "fastchecks", "status": "pending",
                          "note": ""}]}
        (ops / (self.OP + ".json")).write_text(
            json.dumps(rec), encoding="utf-8")
        return rec

    def fakes(self, runner_box):
        from openmainty import batches, fastcheck, m2, repos
        store_p = mock.patch.object(
            store, "load", return_value={"repo": "fakerepo"})
        repos_p = mock.patch.object(
            repos, "load",
            return_value={"fastcheck": {
                "steps": [{"argv": ["true"], "gate": "unit",
                           "install": True}]}})
        run_p = mock.patch.object(
            fastcheck, "run",
            lambda op_id, worktree, profile, head, fp, origin=None: (
                runner_box.append(
                    {"op": op_id, "origin": origin}),
                {"op": op_id, "ok": True, "steps": [],
                 "failed": None})[1])
        for p in (store_p, repos_p, run_p):
            p.start()
            self.addCleanup(p.stop)
        return batches, m2

    def test_no_confirm_starts_nothing(self):
        self.write_op()
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": self.OP})
        self.assertEqual(code, 400)
        self.assertEqual(runner_box, [])

    def test_done_op_runs_with_origin_ui(self):
        self.write_op()
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": self.OP, "confirm": True})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(runner_box,
                         [{"op": self.OP, "origin": "ui"}])

    def test_existing_receipt_refuses(self):
        self.write_op()
        runner_box = []
        self.fakes(runner_box)
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        (ops / (self.OP + ".fastcheck.json")).write_text(
            json.dumps({"ok": True}), encoding="utf-8")
        code, body = self.post("/api/integration/fastcheck",
                               {"op": self.OP, "confirm": True})
        self.assertEqual(code, 409)
        self.assertIn("이미 검증 기록 있음", body.get("error", ""))
        self.assertEqual(runner_box, [])

    def test_blocked_op_refuses(self):
        self.write_op(status="blocked")
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": self.OP, "confirm": True})
        self.assertEqual(code, 409)
        self.assertEqual(runner_box, [])

    def test_lease_busy_reports_409_nothing_started(self):
        self.write_op()
        runner_box = []
        self.fakes(runner_box)
        busy = mock.patch.object(
            sched, "acquire_worker_lease", return_value=None)
        busy.start()
        self.addCleanup(busy.stop)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": self.OP, "confirm": True})
        self.assertEqual(code, 409)
        self.assertEqual(runner_box, [])

    def test_op_busy_maps_to_409(self):
        from openmainty import batches, fastcheck, m2, repos
        self.write_op()
        store_p = mock.patch.object(
            store, "load", return_value={"repo": "fakerepo"})
        repos_p = mock.patch.object(
            repos, "load",
            return_value={"fastcheck": {
                "steps": [{"argv": ["true"], "gate": "unit",
                           "install": True}]}})

        def boom(op_id, worktree, profile, head, fp, origin=None):
            raise m2.OpBusy(op_id)

        run_p = mock.patch.object(fastcheck, "run", boom)
        for p in (store_p, repos_p, run_p):
            p.start()
            self.addCleanup(p.stop)
        with self.assertRaises(batches.IntegrationError) as ctx:
            batches.run_fastcheck_for_op(self.OP, origin="ui")
        self.assertEqual(ctx.exception.http_status, 409)
        self.assertIn("실행 중입니다", str(ctx.exception))

    def test_non_ui_origin_refused(self):
        from openmainty import batches
        self.write_op()
        with self.assertRaises(batches.IntegrationError):
            batches.run_fastcheck_for_op(self.OP, origin="m2")

    def write_reresolve_op(self, derived_from="prep-run-od1-base-1",
                           run="run-od1", status="done"):
        # FIX-b3: a manual reresolve done derived from the same
        # run's prepare, plus that source prepare record.
        from openmainty import batches
        ops = Path(os.environ["OPENMAINTY_HOME"]) / "integration_ops"
        ops.mkdir(parents=True, exist_ok=True)
        wt = Path(self.tmp.name) / "wt-od1"
        wt.mkdir(exist_ok=True)
        (ops / "prep-run-od1-base-1.json").write_text(
            json.dumps({"op": "prepare",
                        "op_id": "prep-run-od1-base-1",
                        "run_id": "run-od1", "status": "blocked"}),
            encoding="utf-8")
        op_id = "prep-run-od1-base-1-lockfix"
        rec = {"op": "reresolve", "op_id": op_id, "run_id": run,
               "derived_from": derived_from, "status": status,
               "result": {"path": str(wt), "branch": "codex/od1x",
                        "head": "h" * 40, "fingerprint": "f" * 64},
               "gates": [{"key": "fastchecks", "status": "pending",
                        "note": ""}]}
        (ops / (op_id + ".json")).write_text(
            json.dumps(rec), encoding="utf-8")
        return op_id

    def test_done_reresolve_runs_with_origin_ui(self):
        op_id = self.write_reresolve_op()
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": op_id, "confirm": True})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(runner_box,
                         [{"op": op_id, "origin": "ui"}])

    def test_reresolve_without_source_refuses(self):
        op_id = self.write_reresolve_op(derived_from="")
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": op_id, "confirm": True})
        self.assertEqual(code, 409)
        self.assertIn("준비 작업이 아님", body.get("error", ""))
        self.assertEqual(runner_box, [])

    def test_reresolve_for_other_run_refuses(self):
        op_id = self.write_reresolve_op(
            derived_from="prep-run-od1-base-1", run="run-other")
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": op_id, "confirm": True})
        self.assertEqual(code, 409)
        self.assertIn("준비 작업이 아님", body.get("error", ""))
        self.assertEqual(runner_box, [])

    def test_reresolve_without_confirm_starts_nothing(self):
        op_id = self.write_reresolve_op()
        runner_box = []
        self.fakes(runner_box)
        code, body = self.post("/api/integration/fastcheck",
                               {"op": op_id})
        self.assertEqual(code, 400)
        self.assertEqual(runner_box, [])


class HoldCarryTests(ServeMixin, BatchSeedMixin):
    # F6: a hold survives reimport — only hold_batch(hold=False)
    # releases it, never an evidence refresh.
    def test_hold_survives_reimport(self):
        from openmainty import batches
        self._seed()
        batches.hold_batch("api-batch-1", True, "owner",
                           reason="waiting on root")
        reread = batches.load_batch("api-batch-1")
        self.assertEqual(reread.get("hold", {}).get("by"), "owner")
        redo = batches.import_batch(self._last_payload)
        self.assertEqual(redo.get("hold", {}).get("by"), "owner")
        self.assertEqual(redo.get("hold", {}).get("reason"),
                         "waiting on root")

    def test_explicit_release_then_reimport_stays_released(self):
        from openmainty import batches
        self._seed()
        batches.hold_batch("api-batch-1", True, "owner",
                           reason="waiting on root")
        batches.hold_batch("api-batch-1", False, "owner")
        redo = batches.import_batch(self._last_payload)
        self.assertNotIn("hold", redo)


class CorruptJsonContainmentTests(ServeMixin, BatchSeedMixin):
    # F9: a corrupt inherited batch (SystemExit from read_json)
    # degrades one card, never the envelope; both refresh halves
    # run independently.
    def _seed_corrupt_inherited(self):
        import json as _json
        from openmainty import batches
        self._seed()
        bdir = batches.batches_dir()
        src = bdir / "api-batch-1.json"
        base = _json.loads(src.read_text(encoding="utf-8"))
        base["id"] = "api-batch-0"
        (bdir / "api-batch-0.json").write_text(
            _json.dumps(base), encoding="utf-8")
        base["id"] = "api-batch-1"
        base["inherited"] = [{"batch": "api-batch-0"}]
        src.write_text(_json.dumps(base), encoding="utf-8")
        (bdir / "api-batch-0.json").write_text("{invalid-json",
                                               encoding="utf-8")
        return batches

    def test_envelope_contains_corrupt_inherited(self):
        from openmainty import batches
        self._seed_corrupt_inherited()
        # Must not raise SystemExit: the corrupt inherited batch
        # degrades this card only.
        env = batches.build_batches_envelope()
        cards = env.get("batches") or []
        ids = [c.get("id") for c in cards]
        self.assertIn("api-batch-1", ids)
        for c in cards:
            if c.get("id") == "api-batch-1":
                self.assertEqual(c.get("ready"), 0)

    def test_refresh_halves_run_independently(self):
        batches = self._seed_corrupt_inherited()
        out = batches.refresh_all_snapshots("sec-test")
        self.assertTrue(str(out.get("obs") or "").startswith("obs-"))
        self.assertIsInstance(out.get("batches"), dict)
        self.assertIsInstance(out.get("summary"), dict)


class HoldFreshnessTests(ServeMixin, BatchSeedMixin):
    # Hold freshness: POST /api/batch/hold answers at once and the
    # background all-refresh carries the hold into /api/batches.
    def test_hold_reaches_batches_snapshot(self):
        import time as _time
        from openmainty import batches
        self._seed()
        code, body = self.post("/api/batch/hold", {
            "batch": "api-batch-1", "hold": True,
            "attestor": "owner", "reason": "waiting on root",
            "confirm": True})
        self.assertEqual(code, 200, body)
        deadline = _time.time() + 90
        seen = False
        while _time.time() < deadline:
            snap = batches.read_json(
                batches.batches_snapshot_path(), default=None)
            if isinstance(snap, dict):
                for b in snap.get("batches") or []:
                    if b.get("id") == "api-batch-1" and \
                            (b.get("hold") or {}).get("by") == "owner":
                        seen = True
                        break
            if seen:
                break
            _time.sleep(1)
        self.assertTrue(seen, "hold never landed in batches snapshot")


class AutoSupersedeMapTests(unittest.TestCase):
    """M2.15: an older auto batch names its newer same-dep/lane
    auto batch (display only — holds and files untouched).

    M2.15b: own temp home — the default (None) list_batches() path
    must never resolve the real home, in any module order.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _batch(self, bid, dep, lane, upd, auto=True, nprim=1):
        primaries = [{"dep": dep, "target": f"t{i}", "lane": lane,
                      "run_id": f"r{i}"} for i in range(nprim)]
        return {"id": bid, "repo": "fakerepo", "origin_m2": "op-1"
                if auto else "", "primaries": primaries,
                "updated_at": upd}

    def test_older_maps_to_newest(self):
        from openmainty import batches
        out = batches._auto_supersede_map([
            self._batch("b1", "webpack", "next",
                        "2026-10-23T12:00:00Z"),
            self._batch("b2", "webpack", "next",
                        "2026-10-23T13:00:00Z")])
        self.assertEqual(out, {"b1": "b2"})

    def test_single_has_no_map(self):
        from openmainty import batches
        self.assertEqual(batches._auto_supersede_map(
            [self._batch("b1", "webpack", "next",
                         "2026-10-23T12:00:00Z")]), {})

    def test_manual_multi_lane_tie_never_map(self):
        from openmainty import batches
        out = batches._auto_supersede_map([
            self._batch("b1", "webpack", "next",
                        "2026-10-23T12:00:00Z"),
            self._batch("b2", "webpack", "main",
                        "2026-10-23T13:00:00Z"),
            self._batch("b3", "other", "next",
                        "2026-10-23T13:00:00Z"),
            self._batch("b4", "webpack", "next",
                        "2026-10-23T12:00:00Z", auto=False),
            self._batch("b5", "webpack", "next",
                        "2026-10-23T12:00:00Z", nprim=2),
            self._batch("b6", "webpack", "next",
                        "2026-10-23T12:00:00Z"),
            self._batch("b7", "webpack", "next",
                        "2026-10-23T12:00:00Z")])
        # b6/b7 tie (same updated_at): fail-closed, no map.
        self.assertEqual(out, {})

    def test_garbage_never_raises(self):
        from openmainty import batches
        self.assertEqual(batches._auto_supersede_map(None), {})
        self.assertEqual(batches._auto_supersede_map(
            [None, "x", {}]), {})


class JointRefreshHintTests(unittest.TestCase):
    """M2.17b C: the old joint's card hints a queued newer member
    (display only — the manual joint-refresh inputs ride along).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _joint(self, op_id="prep-jx-base-1-joint", y_target="58.0.17",
               y_run="run-jy17", status="done"):
        from openmainty.util import write_json
        at = "2026-10-24T00:00:00+00:00"
        write_json(batches.op_file(op_id),
                   {"op": "reresolve", "op_id": op_id,
                    "derived_from": "prep-jx-base-1", "kind": "joint",
                    "joint_with": ["prep-jx-base-1"],
                    "runs": ["run-jx", y_run],
                    "joint_candidates": [
                        {"run": "run-jx", "dep": "react-native",
                         "target": "0.88.0", "head": "xhead"},
                        {"run": y_run, "dep": "expo",
                         "target": y_target, "head": "yhead"}],
                    "repo": "fakerepo", "run_id": "run-jx",
                    "batch_id": "base-1", "lane": "next",
                    "status": status, "created_at": at,
                    "updated_at": at,
                    "result": {"head": "h", "fingerprint": "f"}})

    def _batch(self, bid="b-joint", op_id="prep-jx-base-1-joint"):
        return {"id": bid, "repo": "fakerepo", "origin_m2": op_id}

    def _queue(self, items):
        st = sched.load("fakerepo")
        st["queue"] = items
        sched.save(st)

    def _item(self, run, target):
        return {"run_id": run, "status": "waiting-review",
                "lane": "next", "repo": "fakerepo", "dep": "expo",
                "target": target,
                "created_at": "2026-10-24T00:00:00+00:00"}

    def test_hint_renders_for_newer_member(self):
        self._joint()
        self._queue([self._item("run-jy19", "58.0.19")])
        out = batches._joint_refresh_hints([self._batch()])
        self.assertEqual(out, {"b-joint": {
            "dep": "expo", "from": "58.0.17", "to": "58.0.19",
            "y_run": "run-jy19", "x_op": "prep-jx-base-1",
            "base": "base-1"}})

    def test_no_hint_without_newer_member(self):
        self._joint()
        self._queue([self._item("run-jy17", "58.0.17")])
        self.assertEqual(
            batches._joint_refresh_hints([self._batch()]), {})

    def test_no_hint_when_pair_already_covered(self):
        self._joint()
        self._queue([self._item("run-jy19", "58.0.19")])
        self._joint(op_id="prep-cover-joint", y_target="58.0.19",
                    y_run="run-jy19")
        self.assertEqual(
            batches._joint_refresh_hints([self._batch()]), {})


class QAPackDiffcheckTests(unittest.TestCase):
    """M2.18 pack diffcheck on a fixture git repo (no network)."""

    LOCK_BASE = "\n".join([
        '"webpack@npm:^5.112.0":',
        "  version: 5.112.0",
        '  resolution: "webpack@npm:5.112.0"',
        "",
    ])
    LOCK_HEAD = "\n".join([
        '"webpack@npm:^5.112.2":',
        "  version: 5.112.2",
        '  resolution: "webpack@npm:5.112.2"',
        "",
        '"left-pad@npm:^1.3.0":',
        "  version: 1.3.0",
        '  resolution: "left-pad@npm:1.3.0"',
        "",
    ])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))
        self.origin = Path(self.tmp.name) / "origin"
        self.origin.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "t@t.t")
        self._git("config", "user.name", "t")
        (self.origin / "example").mkdir()
        (self.origin / "example" / "package.json").write_text(
            '{"name": "example"}\n', encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            self.LOCK_BASE, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "base")
        self.base = self._rev("HEAD")
        (self.origin / "example" / "package.json").write_text(
            '{"name": "example", "bump": 1}\n', encoding="utf-8")
        (self.origin / "yarn.lock").write_text(
            self.LOCK_HEAD, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-qm", "head")
        self.head = self._rev("HEAD")

    def _git(self, *args):
        import subprocess
        return subprocess.run(
            ["git", "-c", "user.email=t@t.t",
             "-c", "user.name=t", *args],
            cwd=str(self.origin), check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)

    def _rev(self, rev):
        import subprocess
        return subprocess.run(
            ["git", "rev-parse", rev], cwd=str(self.origin),
            check=True, stdout=subprocess.PIPE, text=True,
            stderr=subprocess.DEVNULL).stdout.strip()

    def _batch(self):
        return {
            "id": "b-qa", "repo": "fakerepo",
            "origin_m2": "op-qa",
            "primaries": [{"dep": "webpack", "target": "5.112.2",
                           "lane": "next", "run_id": "r1"}],
            "integrations": [{"lane": "next",
                              "worktree": str(self.origin),
                              "head": self.head}],
            "inherited": [{"batch": "base-qa"}],
        }

    def _base(self):
        return {"id": "base-qa", "repo": "fakerepo",
                "integrations": [{"lane": "next",
                                  "worktree": str(self.origin),
                                  "head": self.base}]}

    def test_outside_lock_entry_flagged(self):
        out = batches._qa_diffcheck(self._batch(), self._base())
        self.assertEqual(out["status"], "ok")
        self.assertIn("example/package.json", out["files"])
        by_desc = {e["desc"]: e for e in out["lock_entries"]}
        # A range bump reads as remove+add at descriptor level.
        old = by_desc.get("webpack@npm:^5.112.0")
        new = by_desc.get("webpack@npm:^5.112.2")
        self.assertEqual((old["before"], old["after"]),
                         ("5.112.0", None))
        self.assertEqual((new["before"], new["after"]),
                         (None, "5.112.2"))
        self.assertTrue(old["in_dep_tree"])
        self.assertTrue(new["in_dep_tree"])
        left = by_desc.get("left-pad@npm:^1.3.0")
        self.assertEqual((left["before"], left["after"]),
                         (None, "1.3.0"))
        self.assertFalse(left["in_dep_tree"])

    def test_unreadable_base_gives_note(self):
        batch = self._batch()
        batch["integrations"][0]["worktree"] = "/nonexistent-wt"
        out = batches._qa_diffcheck(batch, self._base())
        self.assertEqual(out["status"], "unavailable")
        self.assertTrue(out["note"])


class QAPackPeerTests(unittest.TestCase):
    """M2.18 peer explain with canned yarn outputs (no network)."""

    BASE_OUT = ("webpack@npm:5.112.0\n"
                "└─ peer ✘ react@npm:*: missing peer\n"
                "└─ peer ✘ scheduler@npm:*: missing peer\n")
    HEAD_OUT = ("webpack@npm:5.112.2\n"
                "└─ peer ✘ react@npm:*: missing peer\n"
                "└─ peer ✘ caniuse@npm:*: missing peer\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _scratch(self, name):
        dest = Path(self.tmp.name) / name
        (dest / ".yarn" / "releases").mkdir(parents=True)
        (dest / ".yarn" / "releases" / "yarn-4.18.0.cjs").write_text(
            "x", encoding="utf-8")
        return dest

    def test_added_removed_diff(self):
        base = self._scratch("base")
        head = self._scratch("head")

        def fake_run(argv, **kwargs):
            import subprocess
            cwd = str(kwargs.get("cwd", ""))
            if cwd.endswith("base"):
                return subprocess.CompletedProcess(
                    argv, 0, self.BASE_OUT, "")
            return subprocess.CompletedProcess(
                argv, 0, self.HEAD_OUT, "")

        with mock.patch("subprocess.run", side_effect=fake_run):
            b = batches._qa_run_yarn_explain(base)
            h = batches._qa_run_yarn_explain(head)
        self.assertEqual(b["status"], "ok")
        bset = set(b["incorrectly_met"])
        hset = set(h["incorrectly_met"])
        self.assertEqual(sorted(hset - bset),
                         ["└─ peer ✘ caniuse@npm:*: missing peer"])
        self.assertEqual(sorted(bset - hset),
                         ["└─ peer ✘ scheduler@npm:*: missing peer"])

    def test_timeout_reported_not_raised(self):
        import subprocess
        scratch = self._scratch("slow")
        with mock.patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired("yarn", 1)):
            out = batches._qa_run_yarn_explain(scratch, timeout=1)
        self.assertEqual(out["status"], "timeout")
        self.assertTrue(out["note"])

    def test_peer_orchestration_diff(self):
        batch = {"id": "b-qa", "repo": "fakerepo"}

        def fake_archive(worktree, sha, dest):
            return ""

        def fake_explain(scratch):
            name = str(scratch)
            if name.endswith("base"):
                return {"status": "ok", "incorrectly_met": ["b1"],
                        "output": "", "note": ""}
            return {"status": "ok", "incorrectly_met": ["b1", "h9"],
                    "output": "", "note": ""}

        with mock.patch.object(batches, "_qa_archive",
                               side_effect=fake_archive), \
            mock.patch.object(batches, "_qa_run_yarn_explain",
                              side_effect=fake_explain):
            out = batches._qa_peer(batch, "bsha", "hsha", "/wt",
                                   "20261024T000000")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["added"], ["h9"])
        self.assertEqual(out["removed"], [])


class QAPackSourcePackTests(unittest.TestCase):
    """M2.18 source fail-soft + pack write-once (no network)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        use_temp_home(self, str(Path(self.tmp.name) / "home"))

    def _batch_file(self, bid="b-qa"):
        from openmainty.util import write_json
        write_json(batches.batch_file("base-qa"),
                   {"id": "base-qa", "repo": "fakerepo",
                    "integrations": []})
        write_json(batches.batch_file(bid),
                   {"id": bid, "repo": "fakerepo",
                    "origin_m2": "op-qa-missing",
                    "primaries": [{"dep": "webpack",
                                   "target": "5.112.2",
                                   "lane": "next", "run_id": "r1"}],
                    "integrations": [], "inherited": [],
                    "policy": {"required_gates": ["diffcheck",
                                                  "peer-explain"]}})

    def test_failing_tools_give_note_never_raise(self):
        batch = {"id": "b-qa",
                 "primaries": [{"dep": "webpack",
                                "target": "5.112.2"}]}
        with mock.patch("subprocess.run",
                        side_effect=OSError("no net")):
            out = batches._qa_source(batch, "")
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["releases"][0]["note"],
                         "릴리스 노트 읽기 실패 — 직접 확인")

    def test_target_tag_included_base_pin_excluded(self):
        base_lock = "\n".join([
            '"widget@npm:^1.0.0":',
            "  version: 1.0.0",
            '  resolution: "widget@npm:1.0.0"',
            "",
        ])
        batch = {"id": "b-qa",
                 "primaries": [{"dep": "widget",
                                "target": "1.2.0"}]}

        def fake_gh(args):
            if args[0] == "api":
                return "v1.0.0\nv1.1.0\nv1.2.0\n"
            return "notes for %s" % args[2]

        with mock.patch.object(batches, "_qa_registry_repo",
                               return_value="o/r"), \
            mock.patch.object(batches, "_qa_gh",
                              side_effect=fake_gh):
            out = batches._qa_source(batch, base_lock)
        self.assertEqual(out["status"], "ok")
        got = [n["tag"] for n in
               out["releases"][0]["notes"]]
        self.assertEqual(got, ["v1.1.0", "v1.2.0"])

    def _flags_for_body(self, body):
        base_lock = "\n".join([
            '"widget@npm:^1.0.0":',
            "  version: 1.0.0",
            '  resolution: "widget@npm:1.0.0"',
            "",
        ])
        batch = {"id": "b-qa",
                 "primaries": [{"dep": "widget",
                                "target": "1.1.0"}]}

        def fake_gh(args):
            if args[0] == "api":
                return "v1.0.0\nv1.1.0\n"
            return body

        with mock.patch.object(batches, "_qa_registry_repo",
                               return_value="o/r"), \
            mock.patch.object(batches, "_qa_gh",
                              side_effect=fake_gh):
            out = batches._qa_source(batch, base_lock)
        self.assertEqual(out["status"], "ok")
        notes = out["releases"][0]["notes"]
        self.assertEqual([n["tag"] for n in notes], ["v1.1.0"])
        return notes[0]["flags"]

    def test_remove_stem_flagged(self):
        # Live: v5.111.0 "Remove `experiments.outputModule`".
        self.assertEqual(
            self._flags_for_body("Remove `experiments.outputModule`"),
            ["remove"])

    def test_flag_case_variants_deduped(self):
        self.assertEqual(
            self._flags_for_body("Removed X\nremoved Y"),
            ["removed"])

    def test_no_longer_drop_deprecation_breaking_flagged(self):
        self.assertEqual(
            self._flags_for_body(
                "no longer supports Node 18\n"
                "Dropped support\n"
                "deprecation\n"
                "BREAKING CHANGE"),
            ["breaking", "deprecation", "dropped", "no longer"])

    def test_stem_substrings_do_not_flag(self):
        self.assertEqual(
            self._flags_for_body("dropdown remover removable"), [])

    def test_non_breaking_and_drop_in_do_not_flag(self):
        self.assertEqual(
            self._flags_for_body(
                "non-breaking change\ndrop-in replacement"), [])
        self.assertEqual(
            self._flags_for_body("Non-Breaking Fix"), [])

    def test_breaking_and_drop_still_flag(self):
        self.assertEqual(
            self._flags_for_body("BREAKING CHANGE\ndrop support"),
            ["breaking", "drop"])

    def test_pack_written_once_never_overwritten(self):
        self._batch_file()
        # This pack integration asserts write-once identity, not registry
        # content: keep the real npm transport offline with an explicit
        # empty mapping (honest unavailable note).
        offline_qa_registry(self, {})
        first = batches.build_qa_pack("b-qa", by="owner")
        self.assertEqual(first["batch"], "b-qa")
        path = Path(first["path"])
        before = path.read_bytes()
        second = batches.build_qa_pack("b-qa", by="owner")
        self.assertNotEqual(second["path"], first["path"])
        self.assertEqual(path.read_bytes(), before)
        notes = first["suggested_notes"]
        for gate in ("install", "prepare", "typecheck", "lint",
                     "unit", "loaded-config", "peer-explain",
                     "diffcheck", "source-review", "root-qa"):
            self.assertIn(gate, notes)
            self.assertLessEqual(len(notes[gate]), 200)


class QAPackStaticTests(unittest.TestCase):
    """M2.18 guards: attest surface and driver separation."""

    REPO = Path(__file__).resolve().parent.parent / "src" \
        / "openmainty"

    def _call_files(self, name):
        import re
        found = set()
        for path in self.REPO.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(name + r"\s*\(", text):
                found.add(path.name)
        return found

    def test_attest_only_through_human_paths(self):
        users = self._call_files("attest_gate") - {"batches.py"}
        self.assertEqual(users, {"cli.py", "serve.py"})

    def test_driver_never_builds_or_attests(self):
        import re
        text = (self.REPO / "m2.py").read_text(encoding="utf-8")
        for name in ("build_qa_pack", "newest_qa_pack",
                     "attest_gate"):
            self.assertIsNone(re.search(
                r"(?<![\w.])" + name + r"\(", text))

    def test_pack_button_and_confirm_wired(self):
        html = (self.REPO / "dashboard.html").read_text(
            encoding="utf-8")
        for token in ("data-qp-go", "data-qp-fill", "QAPACK_LOGS",
                      "log_text: log", "게이트를 확인 기록합니다",
                      "이 내용으로 확인 기록"):
            self.assertIn(token, html)


class NewerAutoCardTests(ServeMixin, BatchSeedMixin):
    # The envelope carries the display keys; a crafted newer auto
    # batch supersedes the seeded one on its card only.
    def _write_auto_pair(self):
        import json as _json
        from openmainty import batches
        self._seed()
        bdir = batches.batches_dir()
        first = _json.loads((bdir / "api-batch-1.json")
                            .read_text(encoding="utf-8"))
        first["id"] = "auto-op1"
        first["origin_m2"] = "op-aa-1"
        first["updated_at"] = "2026-10-23T12:00:00Z"
        first["primaries"] = [{"dep": "webpack", "target": "5.112.1",
                               "lane": "next", "run_id": "r1"}]
        (bdir / "auto-op1.json").write_text(_json.dumps(first),
                                            encoding="utf-8")
        second = dict(first)
        second["id"] = "auto-op2"
        second["origin_m2"] = "op-aa-2"
        second["updated_at"] = "2026-10-23T13:00:00Z"
        second["primaries"] = [{"dep": "webpack", "target": "5.112.2",
                                "lane": "next", "run_id": "r2"}]
        (bdir / "auto-op2.json").write_text(_json.dumps(second),
                                            encoding="utf-8")
        return batches

    def test_card_names_newer_auto_only(self):
        batches = self._write_auto_pair()
        env = batches.build_batches_envelope()
        cards = {c.get("id"): c for c in env.get("batches") or []}
        self.assertEqual(cards["auto-op1"]["newer_auto"], "auto-op2")
        self.assertFalse(cards["auto-op2"].get("newer_auto"))
        self.assertIn("consumer_label", cards["auto-op1"])
        self.assertEqual(cards["auto-op1"]["consumer_label"], "")
        # Holds untouched: neither card carries one.
        self.assertFalse(cards["auto-op1"].get("hold"))
        self.assertFalse(cards["auto-op2"].get("hold"))

    def test_card_shows_consumer_label(self):
        import json as _json
        from openmainty import fastcheck
        batches = self._write_auto_pair()
        opf = fastcheck.op_fastcheck_path("op-aa-2")
        opf.parent.mkdir(parents=True, exist_ok=True)
        opf.write_text(_json.dumps(
            {"op": "op-aa-2", "ok": True,
             "consumer": {"status": "pass",
                          "script": "example: build:web",
                          "label": "소비 스크립트 build:web 통과 (59초)"}}),
            encoding="utf-8")
        env = batches.build_batches_envelope()
        cards = {c.get("id"): c for c in env.get("batches") or []}
        self.assertEqual(cards["auto-op2"]["consumer_label"],
                         "소비 스크립트 build:web 통과 (59초)")


class BatchReviewTests(ServeMixin, BatchSeedMixin):
    """M2.16c: POST /api/batch/review records one human verdict on
    one batch primary — the missing piece that left auto-batches
    READY-unreachable ("independent review verdict is 'pending'")."""

    def _seed_review(self, verdict="pending", bid="api-review-1"):
        origin = Path(self.tmp.name) / "origin"
        origin.mkdir()
        for args in (["init", "-b", "main"],):
            self._git(origin, *args)
        (origin / "f.txt").write_text("v1\n")
        self._git(origin, "add", ".")
        self._git(origin, "commit", "-m", "base")
        self._git(origin, "checkout", "-b", "codex/cand-1")
        (origin / "f.txt").write_text("v2\n")
        self._git(origin, "commit", "-am", "cand")
        proof = Path(self.tmp.name) / "proof.json"
        proof.write_bytes(b'{"ok": true}')
        run = store.create("fakerepo", "jest@30.5.2", "t", None,
                           {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k", "repo": "fakerepo", "dep": "jest",
             "target": "30.5.2", "lane": "next", "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/cand-1"})
        sched.save(st)
        snap = runner.source_snapshot(origin)
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        payload = {
            "id": bid, "repo": "fakerepo",
            "target_branch": "next", "purpose": "stable",
            "origin": {"checkout": str(origin), "base_branch": "main",
                       "base_head": _base_head(origin)},
            "primaries": [{"dep": "jest", "target": "30.5.2", "lane": "next",
                           "run_id": run["id"],
                           "source": {"worktree": str(origin),
                                      "branch": "codex/cand-1",
                                      "head": snap["head"],
                                      "fingerprint": snap["fingerprint"]},
                           "review": {"verdict": verdict,
                                      "findings": ["자동 등록 — 독립 검토 대기"]}}],
            "integrations": [{"lane": "next", "worktree": str(origin),
                              "status": "testing",
                              "gates": [{"key": "unit", "status": "pass",
                                         "inputs": {"head": snap["head"],
                                                    "fingerprint": snap["fingerprint"]},
                                         "evidence": [
                                             {"label": "log", "kind": "file",
                                              "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }
        batches.import_batch(payload)
        return bid, run["id"], origin

    def _review_of(self, bid):
        return batches.load_batch(bid)["primaries"][0]["review"]

    def test_worker_never_sets_verdict(self):
        self._seed_review()
        with self.assertRaises(batches.BatchError) as ctx:
            batches.set_review("api-review-1", "r", "accept",
                               "worker", [])
        self.assertIn("never set a verdict", str(ctx.exception))

    def test_unknown_run_409_nothing_written(self):
        bid, run_id, _origin = self._seed_review()
        with self.assertRaises(batches.BatchError) as ctx:
            batches.set_review(bid, "run-nope-9", "accept",
                               "root", [])
        self.assertIn("unknown run", str(ctx.exception))
        self.assertEqual(self._review_of(bid)["verdict"], "pending")
        from openmainty import events as _events
        self.assertEqual(
            [e for e in _events.list_events("fakerepo")
             if e.get("kind") == "review"], [])

    def test_moved_source_refuses_nothing_rebound(self):
        bid, run_id, _origin = self._seed_review()
        moved = {"head": "f" * 40, "fingerprint": "0" * 40,
                 "clean": True}
        with self.assertRaises(batches.BatchError) as ctx:
            batches.set_review(bid, run_id, "accept", "root", [],
                               _snap_fn=lambda _wt: moved)
        self.assertIn("다시 검토", str(ctx.exception))
        self.assertEqual(self._review_of(bid)["verdict"], "pending")

    def test_stale_proof_blocks_accept_not_request_changes(self):
        bid, run_id, _origin = self._seed_review()
        st = store.load(run_id)
        st["verifiedCommits"] = {"review": "0" * 40, "e2e": "0" * 40}
        st["verifiedFingerprints"] = {"review": "0" * 40,
                                      "e2e": "0" * 40}
        store.save(st)
        with self.assertRaises(batches.BatchError) as ctx:
            batches.set_review(bid, run_id, "accept", "root", [])
        self.assertIn("증명 부족", str(ctx.exception))
        out = batches.set_review(bid, run_id, "request-changes",
                                 "owner", ["소스 바뀜 확인"])
        self.assertEqual(out["verdict"], "request-changes")

    def test_notes_need_findings(self):
        self._seed_review()
        bid = "api-review-1"
        _, run_id = bid, batches.load_batch(bid)["primaries"][0][
            "run_id"]
        with self.assertRaises(batches.BatchError) as ctx:
            batches.set_review(bid, run_id, "accept-with-notes",
                               "owner", [])
        self.assertIn("at least one finding", str(ctx.exception))
        with self.assertRaises(batches.BatchError):
            batches.set_review(bid, run_id, "accept", "owner",
                               ["x" * 201])

    def test_accept_keeps_previous_records_event_flips_ready(self):
        from openmainty import events as _events
        bid, run_id, _origin = self._seed_review()
        code, before = self.get(f"/api/batch?id={bid}")
        self.assertEqual(code, 200)
        res0 = before["evaluation"]["results"][0]
        self.assertFalse(res0["ready"])
        self.assertTrue(any("pending" in r for r in
                            res0.get("reasons") or []))
        out = batches.set_review(bid, run_id, "accept", "root", [])
        self.assertEqual(out["verdict"], "accept")
        self.assertEqual(out["previous"]["verdict"], "pending")
        self.assertIn("자동 등록", out["previous"]["findings"][0])
        got = self._review_of(bid)
        self.assertEqual(got["verdict"], "accept")
        self.assertEqual(got["by"], "root")
        self.assertEqual(got["previous"]["verdict"], "pending")
        found = [e for e in _events.list_events("fakerepo")
                 if e.get("kind") == "review"]
        self.assertTrue(found)
        self.assertIn("검토 판정: accept — root",
                      found[-1].get("title") or "")
        code, after = self.get(f"/api/batch?id={bid}")
        self.assertEqual(code, 200)
        self.assertTrue(after["evaluation"]["results"][0]["ready"])

    def test_driver_never_calls_set_review(self):
        import pathlib as _pl
        src = (_pl.Path(__file__).resolve().parent.parent / "src"
               / "openmainty" / "m2.py").read_text(encoding="utf-8")
        self.assertNotIn("set_review", src)


class MismatchAttentionTests(ServeMixin, BatchSeedMixin):
    """M2.16d A: a co-located compiler/runtime major mismatch warns
    on an existing batch — readiness unchanged (manual batches are
    never blocked; the M2 batch stage keeps refusing auto ones).
    """

    LOCK_8_7 = "\n".join([
        "__metadata:",
        "  version: 8",
        "  cacheKey: 8",
        "",
        '"@babel/runtime@^8.0.8":',
        "  version: 8.0.8",
        '  resolution: "@babel/runtime@npm:8.0.8"',
        "",
        '"@babel/core@^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
    ])
    LOCK_7_7 = "\n".join([
        "__metadata:",
        "  version: 8",
        "  cacheKey: 8",
        "",
        '"@babel/runtime@^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/runtime@npm:7.29.7"',
        "",
        '"@babel/core@^7.29.7":',
        "  version: 7.29.7",
        '  resolution: "@babel/core@npm:7.29.7"',
        "",
    ])

    def _seed_dep(self, bid, dep, target, manifests=None):
        from openmainty import runner
        origin = Path(self.tmp.name) / ("origin-%s" % bid)
        origin.mkdir()
        self._git(origin, "init", "-b", "main")
        (origin / "f.txt").write_text("v1\n")
        if manifests is not None:
            runtime_range, core_range, lock_text = manifests
            ex = origin / "example"
            ex.mkdir()
            (ex / "package.json").write_text(json.dumps(
                {"devDependencies":
                 {"@babel/core": core_range,
                  "@babel/runtime": runtime_range}}))
            (origin / "yarn.lock").write_text(lock_text)
        self._git(origin, "add", ".")
        self._git(origin, "commit", "-m", "base")
        self._git(origin, "checkout", "-b", "codex/cand-%s" % bid)
        (origin / "f.txt").write_text("v2\n")
        self._git(origin, "commit", "-am", "cand")
        proof = Path(self.tmp.name) / ("proof-%s.json" % bid)
        proof.write_bytes(b'{"ok": true}')
        run = store.create("fakerepo", "%s@%s" % (dep, target),
                           "t", None, {"by": "t"})
        st = sched.load("fakerepo")
        st.setdefault("queue", []).append(
            {"key": "k-%s" % bid, "repo": "fakerepo", "dep": dep,
             "target": target, "lane": "next",
             "status": "waiting-review",
             "run_id": run["id"], "branch": "codex/cand-%s" % bid})
        sched.save(st)
        snap = runner.source_snapshot(origin)
        _bind_accept(run["id"], snap["head"], snap["fingerprint"])
        payload = {
            "id": bid, "repo": "fakerepo",
            "target_branch": "next", "purpose": "stable",
            "origin": {"checkout": str(origin), "base_branch": "main",
                       "base_head": _base_head(origin)},
            "primaries": [{"dep": dep, "target": target, "lane": "next",
                           "run_id": run["id"],
                           "source": {"worktree": str(origin),
                                      "branch": "codex/cand-%s" % bid,
                                      "head": snap["head"],
                                      "fingerprint": snap["fingerprint"]},
                           "review": {"verdict": "accept", "by": "root",
                                      "findings": ["ok"]}}],
            "integrations": [{"lane": "next", "worktree": str(origin),
                              "status": "testing",
                              "gates": [{"key": "unit", "status": "pass",
                                         "inputs": {"head": snap["head"],
                                                    "fingerprint": snap[
                                                        "fingerprint"]},
                                         "evidence": [
                                             {"label": "log", "kind": "file",
                                              "origin": str(proof)}]}]}],
            "policy": {"required_gates": ["unit"],
                       "gates": {"unit": {}}},
        }
        return batches.import_batch(payload)

    def _result_of(self, bid):
        code, doc = self.get("/api/batch?id=%s" % bid)
        self.assertEqual(code, 200)
        return doc["evaluation"]["results"][0]

    def _cards_by_id(self):
        env = batches.build_batches_envelope()
        return {c.get("id"): c for c in env.get("batches") or []}

    def test_colocated_mismatch_warns_and_stays_ready(self):
        bid = "mm-a"
        self._seed_dep(bid, "@babel/runtime", "8.0.8",
                       ("^8.0.8", "^7.29.7", self.LOCK_8_7))
        res = self._result_of(bid)
        self.assertTrue(res["ready"])
        self.assertEqual(res["attention"], [
            "컴파일러와 메이저 불일치 "
            "(@babel/core 7 · @babel/runtime 8) — 같은 매니페스트"])
        card = self._cards_by_id()[bid]
        self.assertIn(
            "컴파일러와 메이저 불일치 "
            "(@babel/core 7 · @babel/runtime 8) — 같은 매니페스트",
            card.get("compiler_notes") or [])
        self.assertEqual(card.get("ready"), 1)

    def test_same_major_has_no_attention(self):
        bid = "mm-b"
        self._seed_dep(bid, "@babel/runtime", "7.29.7",
                       ("^7.29.7", "^7.29.7", self.LOCK_7_7))
        res = self._result_of(bid)
        self.assertTrue(res["ready"])
        self.assertEqual(res.get("attention") or [], [])

    def test_unreadable_manifest_has_no_attention(self):
        bid = "mm-c"
        self._seed_dep(bid, "jest", "30.5.2")
        res = self._result_of(bid)
        self.assertTrue(res["ready"])
        self.assertEqual(res.get("attention") or [], [])


class NewerReadyHintUnitTests(unittest.TestCase):
    """M2.16d B: the newer-READY-leaf hint matrix, pure."""

    def _hint(self, primaries=("jest", "30.5.1", "next"),
              inherited=("base-A",),
              others=(("b-new", "r", "next", "jest", "30.5.2",
                       {"base-A"}, (1, 1)),)):
        own_inh = [{"batch": h} for h in inherited]
        idx = [(o[0], o[1], o[2], o[3], o[4], set(o[5]))
               for o in others]
        ready_of = {o[0]: o[6] for o in others}
        return batches._newer_ready_hints(
            "b-old", "r",
            [{"dep": primaries[0], "target": primaries[1],
              "lane": primaries[2]}],
            own_inh, idx, ready_of)

    def test_newer_ready_same_lane_base_hints(self):
        self.assertEqual(self._hint(), [
            {"dep": "jest", "from": "30.5.1", "to": "30.5.2",
             "batch": "b-new"}])

    def test_newer_not_ready_no_hint(self):
        others = (("b-new", "r", "next", "jest", "30.5.2",
                   {"base-A"}, (0, 1)),)
        self.assertEqual(self._hint(others=others), [])

    def test_newer_older_target_no_hint(self):
        others = (("b-new", "r", "next", "jest", "30.4.9",
                   {"base-A"}, (1, 1)),)
        self.assertEqual(self._hint(others=others), [])

    def test_newer_different_lane_no_hint(self):
        others = (("b-new", "r", "main", "jest", "30.5.2",
                   {"base-A"}, (1, 1)),)
        self.assertEqual(self._hint(others=others), [])

    def test_newer_different_base_no_hint(self):
        others = (("b-new", "r", "next", "jest", "30.5.2",
                   {"base-B"}, (1, 1)),)
        self.assertEqual(self._hint(others=others), [])

    def test_empty_base_vs_inherited_no_hint(self):
        others = (("b-new", "r", "next", "jest", "30.5.2",
                   {"base-A"}, (1, 1)),)
        self.assertEqual(
            self._hint(inherited=(), others=others), [])

    def test_equal_target_no_hint(self):
        others = (("b-new", "r", "next", "jest", "30.5.1",
                   {"base-A"}, (1, 1)),)
        self.assertEqual(self._hint(others=others), [])


class NewerReadyHintEnvelopeTests(MismatchAttentionTests):
    """M2.16d B: the envelope carries the hint on the older card."""

    def test_envelope_hint_points_at_newer_ready_batch(self):
        old = self._seed_dep("nh-old", "jest", "30.5.1")
        new = self._seed_dep("nh-new", "jest", "30.5.2")
        self.assertTrue(self._result_of(old["id"])["ready"])
        self.assertTrue(self._result_of(new["id"])["ready"])
        cards = self._cards_by_id()
        hints = cards[old["id"]].get("newer_ready") or []
        self.assertEqual(hints, [
            {"dep": "jest", "from": "30.5.1", "to": "30.5.2",
             "batch": new["id"]}])
        self.assertEqual(
            cards[new["id"]].get("newer_ready") or [], [])


class RollupMismatchNoticeTests(MismatchAttentionTests):
    """M2.16d A: the redraft's 확인 필요 section lists per-leaf
    co-location warnings; the automatic draft never carries one."""

    def _leaves(self, bid, dep="@babel/runtime"):
        return [{"dep": dep, "target": "8.0.8", "batch": bid,
                 "head": "h" * 40}]

    def test_notices_name_leaf_and_label(self):
        bid = "rn-a"
        self._seed_dep(bid, "@babel/runtime", "8.0.8",
                       ("^8.0.8", "^7.29.7",
                        self.LOCK_8_7))
        self.assertEqual(batches._rollup_mismatch_notices(
            self._leaves(bid)), [
            "@babel/runtime (%s): 컴파일러와 메이저 불일치 "
            "(@babel/core 7 · @babel/runtime 8) — 같은 매니페스트"
            % bid])

    def test_notices_skip_clean_unknown_and_garbage(self):
        bid = "rn-b"
        self._seed_dep(bid, "jest", "30.5.2")
        self.assertEqual(batches._rollup_mismatch_notices(
            self._leaves(bid, dep="jest")), [])
        self.assertEqual(batches._rollup_mismatch_notices(
            [{"dep": "@babel/runtime", "batch": "no-such-batch"}]),
            [])
        self.assertEqual(
            batches._rollup_mismatch_notices([None, "x", {}]), [])

    def test_redraft_carries_notices_auto_does_not(self):
        leaves = self._leaves("rn-c")
        _title, body, _cmd = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", lang="ko", lang_src="profile",
            notices=["n1"])
        self.assertIn("\n확인 필요\n", body)
        self.assertIn("\n- n1", body)
        _title2, body2, _cmd2 = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", lang="ko", lang_src="profile")
        self.assertNotIn("\n확인 필요\n", body2)
        self.assertNotIn("\n- n1", body2)
        _title3, body3, _cmd3 = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", notices=["n1"])
        self.assertIn("Needs attention", body3)
        self.assertIn("- n1", body3)


class RollupPodCommandTests(ServeMixin, BatchSeedMixin):
    """M2.16e2: the rollup PR draft names the exact pod update
    owner command first only on REAL prebuilt drift (declared
    RN vs locked prebuilt pin). An in-sync lock gets nothing,
    even though it pins the same pods."""

    PODLOCK = ("PODS:\n"
               "  - ReactNativeDependencies (0.86.2)\n"
               "  - React-Core-prebuilt (0.86.2):\n"
               "  - hermes-engine (0.86.2):\n"
               "\n"
               "DEPENDENCIES:\n"
               "  - ReactNativeDependencies\n")

    PODCMD = ("cd example/ios && pod update "
              "ReactNativeDependencies React-Core-prebuilt "
              "hermes-engine --no-repo-update")

    def _leaves(self):
        return [{"dep": "react-native", "target": "0.88.0",
                 "batch": "rn-j", "head": "h" * 40}]

    def _tree(self, lock_text=None, name="podwt", rn=None,
              rn_garbage=False):
        wt = Path(self.tmp.name) / name
        if lock_text is not None:
            ios = wt / "example" / "ios"
            ios.mkdir(parents=True)
            (ios / "Podfile.lock").write_text(
                lock_text, encoding="utf-8")
        if rn_garbage:
            ex = wt / "example"
            ex.mkdir(parents=True, exist_ok=True)
            (ex / "package.json").write_text(
                "{unparsable", encoding="utf-8")
        elif rn is not None:
            ex = wt / "example"
            ex.mkdir(parents=True, exist_ok=True)
            (ex / "package.json").write_text(json.dumps(
                {"dependencies": {"react-native": rn}}),
                encoding="utf-8")
        return wt

    def _commands(self, wt=None):
        _title, _body, commands = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", self._leaves(), {},
            "br", "h" * 40, "op-1",
            new_path=str(wt) if wt is not None else "")
        return commands

    def test_drifted_tree_gives_command_first(self):
        commands = self._commands(self._tree(self.PODLOCK,
                                             rn="0.88.0"))
        self.assertEqual(commands[0], self.PODCMD)
        self.assertTrue(any(c.startswith("git push") for c in
                            commands))

    def test_in_sync_lock_gives_no_command(self):
        commands = self._commands(self._tree(self.PODLOCK,
                                             rn="0.86.2"))
        self.assertFalse(any("pod update" in c for c in commands))
        self.assertTrue(any(c.startswith("git push") for c in
                            commands))

    def test_unreadable_manifest_gives_no_command(self):
        commands = self._commands(self._tree(
            self.PODLOCK, rn_garbage=True))
        self.assertFalse(any("pod update" in c for c in commands))

    def test_pod_free_tree_keeps_push_only(self):
        commands = self._commands(self._tree(rn="0.88.0"))
        self.assertFalse(any("pod update" in c for c in commands))
        commands2 = self._commands()
        self.assertFalse(any("pod update" in c for c in
                             commands2))


class RollupLeafCaveatTests(MismatchAttentionTests):
    """M2.24: a finished rollup draft shows what changed in its
    leaves since it was made — compiler notes, newer READY leaves,
    holds, lost readiness and pod drift. Read-only; the op file is
    never rewritten by display."""

    BABEL_BID = "cav-babel"
    JEST_OLD = "cav-jest-old"
    JEST_NEW = "cav-jest-new"

    BABEL_NOTE = ("컴파일러와 메이저 불일치 "
                  "(@babel/core 7 · @babel/runtime 8) — 같은 매니페스트")

    def _seed_leaves(self):
        self._seed_dep(self.BABEL_BID, "@babel/runtime", "8.0.8",
                       ("^8.0.8", "^7.29.7", self.LOCK_8_7))
        self._seed_dep(self.JEST_OLD, "jest", "30.5.1")
        self._seed_dep(self.JEST_NEW, "jest", "30.5.2")

    def _eval_map(self, *bids):
        by_id, evals = {}, {}
        for bid in bids:
            by_id[bid] = batches.load_batch(bid)
            code, doc = self.get("/api/batch?id=%s" % bid)
            self.assertEqual(code, 200)
            evals[bid] = doc["evaluation"]
        return by_id, evals

    def _op_leaves(self, *triples):
        return [{"dep": dep, "target": tgt, "batch": bid,
                 "head": "h" * 40} for dep, tgt, bid in triples]

    def _write_op(self, op_id, leaves):
        from openmainty.util import write_json
        rec = {"op": "rollup", "op_id": op_id, "repo": "fakerepo",
               "lane": "next", "status": "done", "leaves": leaves,
               "result": {"branch": "b", "head": "h" * 40},
               "pr_draft": {"title": "t", "body": "b"},
               "updated_at": "2026-10-24T00:00:00Z"}
        path = batches.integration_ops_dir() / (op_id + ".json")
        write_json(path, rec)
        return path

    def test_compiler_and_newer_caveats_exact(self):
        self._seed_leaves()
        by_id, evals = self._eval_map(self.BABEL_BID, self.JEST_OLD,
                                      self.JEST_NEW)
        states = batches._rollup_eval_states("fakerepo", by_id, evals)
        leaves = self._op_leaves(
            ("@babel/runtime", "8.0.8", self.BABEL_BID),
            ("jest", "30.5.1", self.JEST_OLD))
        self.assertEqual(
            batches._rollup_leaf_caveats("fakerepo", leaves, states),
            [{"leaf": self.BABEL_BID, "kind": "compiler",
              "text": "@babel/runtime (%s): %s"
                      % (self.BABEL_BID, self.BABEL_NOTE)},
             {"leaf": self.JEST_OLD, "kind": "newer_ready",
              "text": "새 버전 준비됨: jest 30.5.1 → 30.5.2 (%s) "
                      "— 다시 합치기 가능" % self.JEST_NEW}])

    def test_card_states_match_eval_states(self):
        # The snapshot-card path normalizes to the same shape the
        # eval path produces, so both surfaces read one logic.
        card = {"id": "cav-card", "repo": "fakerepo",
                "compiler_notes": [self.BABEL_NOTE],
                "newer_ready": [{"dep": "jest", "from": "30.5.1",
                                 "to": "30.5.2", "batch": "cav-new"}],
                "hold": {"by": "owner", "at": "t",
                         "reason": "root QA 전"},
                "ready": 1, "primaries": 1,
                "evaluation": {"ready": 0, "total": 1,
                               "first_failure": {}},
                "pod_drift": {"label": "pod-label-x"}}
        state = batches._rollup_leaf_state_from_card(card)
        self.assertEqual(state["compiler_notes"], [self.BABEL_NOTE])
        self.assertEqual(len(state["newer_ready"]), 1)
        self.assertEqual(state["hold"]["reason"], "root QA 전")
        self.assertEqual((state["ready"], state["total"],
                          state["primaries"]), (1, 1, 1))
        self.assertEqual(state["pod_label"], "pod-label-x")
        self.assertEqual(
            batches._rollup_leaf_state_from_card(None)["total"], 0)
        self.assertEqual(
            batches._rollup_leaf_state_from_card({})["ready"], 0)

    def test_status_row_carries_caveats_and_file_untouched(self):
        self._seed_leaves()
        by_id, evals = self._eval_map(self.BABEL_BID, self.JEST_OLD,
                                      self.JEST_NEW)
        leaves = self._op_leaves(
            ("@babel/runtime", "8.0.8", self.BABEL_BID),
            ("jest", "30.5.1", self.JEST_OLD))
        opf = self._write_op("rollup-next-fakerepo-cav1", leaves)
        before = opf.read_bytes()
        row = batches.m1_rollup_status("fakerepo", by_id,
                                       evals)["next"]
        self.assertEqual(row["op"], "rollup-next-fakerepo-cav1")
        self.assertEqual(len(row["caveats"]), 2)
        self.assertEqual(row["caveats"][0]["kind"], "compiler")
        self.assertIn(self.BABEL_NOTE, row["caveats"][0]["text"])
        self.assertEqual(row["caveats"][1]["kind"], "newer_ready")
        self.assertIn(self.JEST_NEW, row["caveats"][1]["text"])
        self.assertEqual(opf.read_bytes(), before)

    def test_clean_rollup_has_no_caveats(self):
        self._seed_dep("cav-clean", "jest", "30.5.2")
        by_id, evals = self._eval_map("cav-clean")
        states = batches._rollup_eval_states("fakerepo", by_id, evals)
        leaves = self._op_leaves(("jest", "30.5.2", "cav-clean"))
        self.assertEqual(
            batches._rollup_leaf_caveats("fakerepo", leaves, states),
            [])
        opf = self._write_op("rollup-next-fakerepo-cavclean", leaves)
        before = opf.read_bytes()
        row = batches.m1_rollup_status("fakerepo", by_id, evals)["next"]
        self.assertEqual(row["op"], "rollup-next-fakerepo-cavclean")
        self.assertEqual(row["caveats"], [])
        self.assertEqual(opf.read_bytes(), before)

    def test_held_leaf_caveat(self):
        self._seed_dep("cav-held", "jest", "30.5.2")
        batches.hold_batch("cav-held", True, "owner",
                           reason="waiting on root")
        by_id, evals = self._eval_map("cav-held")
        states = batches._rollup_eval_states("fakerepo", by_id, evals)
        leaves = self._op_leaves(("jest", "30.5.2", "cav-held"))
        # A hold also flips the current evaluation to not-ready;
        # both lines are honest (the hold names the cause).
        self.assertEqual(
            batches._rollup_leaf_caveats("fakerepo", leaves, states),
            [{"leaf": "cav-held", "kind": "held",
              "text": "jest (cav-held): 보류 중 — waiting on root"},
             {"leaf": "cav-held", "kind": "not_ready",
              "text": ("jest (cav-held): 현재 평가에서 "
                       "준비 완료가 아님")}])

    def test_not_ready_and_unreadable_leaf(self):
        states = {"cav-nr": {"compiler_notes": [], "newer_ready": [],
                             "hold": None, "ready": 0, "total": 1,
                             "primaries": 1, "pod_label": None}}
        leaves = self._op_leaves(("jest", "1.0.0", "cav-nr"),
                                 ("jest", "9.9.9", "no-such-batch"))
        self.assertEqual(
            batches._rollup_leaf_caveats("fakerepo", leaves, states),
            [{"leaf": "cav-nr", "kind": "not_ready",
              "text": "jest (cav-nr): 현재 평가에서 준비 완료가 아님"}])

    def test_pod_drift_caveat_from_receipt(self):
        from openmainty import fastcheck as _fc
        from openmainty.util import write_json
        op_id = "prep-run-cavtest1-fakerepo-20261019"
        label = ("iOS Podfile.lock 갱신 필요 — `cd example/ios && "
                 "pod update react-native-video --no-repo-update`")
        write_json(_fc.op_fastcheck_path(op_id),
                   {"pod_drift": {"pod": "react-native-video",
                                  "pinned": "6.19.2",
                                  "target": "6.19.3",
                                  "label": label,
                                  "command": "cd example/ios && "
                                  "pod update react-native-video "
                                  "--no-repo-update"}})
        batch = {"id": "cav-pod", "repo": "fakerepo",
                 "origin_m2": op_id,
                 "primaries": [{"dep": "react-native-video",
                                "target": "6.19.3", "lane": "next"}],
                 "integrations": []}
        ev = {"results": [{"ready": True, "reasons": [],
                            "attention": []}]}
        state = batches._rollup_leaf_state_from_eval(
            "fakerepo", batch, ev, [], {})
        self.assertEqual(state["pod_label"], label)
        self.assertEqual((state["ready"], state["total"],
                          state["primaries"]), (1, 1, 1))
        leaves = self._op_leaves(("react-native-video", "6.19.3",
                                  "cav-pod"))
        self.assertEqual(
            batches._rollup_leaf_caveats("fakerepo", leaves,
                                         {"cav-pod": state}),
            [{"leaf": "cav-pod", "kind": "pod_drift",
              "text": "react-native-video (cav-pod): %s" % label}])

    def test_draft_caveats_section_and_clean_identity(self):
        leaves = self._op_leaves(("jest", "30.5.2", "cav-clean"))
        caveats = [{"leaf": "cav-clean", "kind": "held",
                    "text": "jest (cav-clean): 보류 중 — root QA 전"}]
        _t, body, _c = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", lang="en", lang_src="default",
            caveats=caveats)
        self.assertTrue(body.endswith(
            "\nCaveats\n- jest (cav-clean): 보류 중 — root QA 전"))
        _t, kobody, _c = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", lang="ko", lang_src="profile",
            caveats=caveats)
        self.assertIn("\n초안 이후 확인할 점\n- jest (cav-clean)",
                      kobody)
        _t, plain, _c = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", lang="en", lang_src="default")
        _t, empty, _c = batches._rollup_pr_draft(
            "fakerepo", "next", "base-1", leaves, {}, "br",
            "h" * 40, "op-1", lang="en", lang_src="default",
            caveats=[])
        self.assertEqual(plain, empty)
        self.assertNotIn("Caveats", plain)


class StoredRollupRowTests(MismatchAttentionTests):
    """M2.25: the status row carries a fully shaped stored record
    (result, slim leaves, owner commands, gates) and builds caveat
    states only for leaf batch ids."""

    def _write_op(self, op_id, leaves, extra=None):
        from openmainty.util import write_json
        rec = {"op": "rollup", "op_id": op_id, "repo": "fakerepo",
               "lane": "next", "status": "done", "leaves": leaves,
               "result": {"branch": "codex/rollup-next",
                          "head": "a" * 40},
               "gates": [{"key": "source-prepared", "status": "pass"},
                         {"key": "fastchecks", "status": "pending"}],
               "owner_commands": ["git push -u origin codex/rollup"],
               "pr_draft": {"title": "t", "body": "b",
                            "stack_line": "s"},
               "stacked_drafts": {"summary_ko": "sum",
                                  "pr1": {"title": "p1", "body": "bb1"},
                                  "pr2": {"title": "p2", "body": "bb2"}},
               "ship_order": "chain-first",
               "updated_at": "2026-10-25T00:00:00Z"}
        if extra:
            rec.update(extra)
        path = batches.integration_ops_dir() / (op_id + ".json")
        write_json(path, rec)
        return path

    def test_row_shape_and_bytes_untouched(self):
        self._seed_dep("row-leaf", "jest", "30.5.2")
        by_id = {"row-leaf": batches.load_batch("row-leaf")}
        code, doc = self.get("/api/batch?id=row-leaf")
        self.assertEqual(code, 200)
        leaves = [{"dep": "jest", "target": "30.5.2",
                   "batch": "row-leaf", "head": "h" * 40}]
        opf = self._write_op("rollup-next-fakerepo-row1", leaves)
        before = opf.read_bytes()
        row = batches.m1_rollup_status(
            "fakerepo", by_id,
            {"row-leaf": doc["evaluation"]})["next"]
        self.assertEqual(row["op"], "rollup-next-fakerepo-row1")
        self.assertEqual(row["result"],
                         {"branch": "codex/rollup-next",
                          "head": "a" * 40})
        self.assertEqual(row["leaves_list"],
                         [{"batch": "row-leaf", "dep": "jest",
                           "target": "30.5.2"}])
        self.assertEqual(row["owner_commands"],
                         ["git push -u origin codex/rollup"])
        self.assertEqual([g["key"] for g in row["gates"]],
                         ["source-prepared", "fastchecks"])
        self.assertEqual(row["caveats"], [])
        self.assertEqual(opf.read_bytes(), before)

    def test_states_scoped_to_leaf_ids(self):
        # Five batches, two leaves: _batch_pod_drift runs exactly
        # twice (counted through a delegating wrapper).
        from unittest import mock
        by_id = {}
        evals = {}
        for i in range(5):
            bid = "scope-b%d" % i
            by_id[bid] = {"id": bid, "repo": "fakerepo",
                          "primaries": [{"dep": "jest",
                                         "target": "30.5.%d" % i,
                                         "lane": "next"}],
                          "integrations": []}
            evals[bid] = {"results": [{"ready": True, "reasons": [],
                                        "attention": []}]}
        leaves = [{"dep": "jest", "target": "30.5.0",
                   "batch": "scope-b0", "head": "h" * 40},
                  {"dep": "jest", "target": "30.5.1",
                   "batch": "scope-b1", "head": "h" * 40}]
        self._write_op("rollup-next-fakerepo-scope1", leaves)
        states = batches._rollup_eval_states(
            "fakerepo", by_id, evals,
            only_ids={"scope-b0", "scope-b1"})
        self.assertEqual(sorted(states.keys()),
                         ["scope-b0", "scope-b1"])
        real = batches._batch_pod_drift
        calls = []
        def counting(batch):
            calls.append(batch.get("id"))
            return real(batch)
        with mock.patch.object(batches, "_batch_pod_drift",
                               side_effect=counting):
            row = batches.m1_rollup_status(
                "fakerepo", by_id, evals)
        self.assertEqual(sorted(calls), ["scope-b0", "scope-b1"])
        self.assertIn("next", row)


class ReviewErrorMappingTests(ServeMixin, BatchSeedMixin):
    """M2.16d C: validation failures raise ReviewValidationError
    (mapped to 400); state conflicts raise plain BatchError (409).
    ReviewValidationError subclasses BatchError so old except paths
    still catch."""

    def _write_batch(self, bid="api-rm-1", run_id="run-rm-1"):
        from openmainty import runner as _runner
        from openmainty.util import write_json as _write_json
        wt = Path(self.tmp.name) / "wt"
        wt.mkdir()
        self._git(wt, "init", "-b", "main")
        (wt / "f.txt").write_text("v1\n")
        self._git(wt, "add", ".")
        self._git(wt, "commit", "-m", "base")
        import subprocess
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(wt),
            check=True, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL).stdout.strip()
        fp = _runner.source_snapshot(wt)["fingerprint"]
        _write_json(batches.batch_file(bid), {
            "id": bid, "repo": "fakerepo", "target_branch": "main",
            "origin": {"checkout": str(wt), "base_branch": "main",
                       "base_head": head},
            "primaries": [{"dep": "jest", "target": "30.5.2",
                           "lane": "next", "run_id": run_id,
                           "source": {"worktree": str(wt),
                                      "branch": "main", "head": head,
                                      "fingerprint": fp},
                           "review": {"verdict": "pending"}}],
            "integrations": [], "required_gates": []})
        return bid, run_id

    def test_validation_errors_are_review_validation_error(self):
        self._write_batch()
        for kwargs in ({"verdict": "bogus", "by": "root",
                        "findings": []},
                       {"verdict": "accept-with-notes", "by": "root",
                        "findings": []},
                       {"verdict": "accept", "by": "root",
                        "findings": ["x" * 201]},
                       {"verdict": "accept", "by": "root",
                        "findings": ["f%d" % i for i in range(21)]}):
            with self.assertRaises(batches.ReviewValidationError):
                batches.set_review("api-rm-1", "run-rm-1",
                                   kwargs["verdict"], kwargs["by"],
                                   kwargs["findings"])

    def test_state_conflict_is_plain_batch_error(self):
        self._write_batch()
        with self.assertRaises(batches.BatchError) as ctx:
            batches.set_review("api-rm-1", "run-nope-9", "accept",
                               "root", [])
        self.assertNotIsInstance(ctx.exception,
                                 batches.ReviewValidationError)


if __name__ == "__main__":
    unittest.main()
