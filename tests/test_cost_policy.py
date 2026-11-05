"""Cost-policy guard: subscription CLI only, never paid model APIs.

Frozen policy (user directive, top priority):
- AI work runs only through the existing Muse Code subscription login
  via the `muse exec` CLI. No direct model API calls, no pay-per-use keys.
- On subscription quota: save state and wait. Never switch to a paid API.
- If the subscription path is unverifiable, hold AI execution and keep
  cost-free implementation/mock tests going.
- Build/test locally first; no new paid servers, CI, or services.

This test scans the product source so a future edit cannot silently add a
paid path. Run with: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "openmainty"

# Substrings that must never appear in product source (case-insensitive).
BANNED = [
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "openai.api_key",
    "anthropic.api_key",
    "api-key-stdin",          # must not feed a key into muse (login path only)
    "META_API_KEY=",          # must never set/export a key value
    "sk-ant-",
    "sk-proj-",
]

# Credential names that code may *mention* (presence check by name only).
# Reading an actual secret value out of the environment is banned; checking
# presence with os.environ.get("META_API_KEY") truthiness is allowed.
ALLOWED_NAME_REFS = {"META_API_KEY"}


class CostPolicyTests(unittest.TestCase):
    def test_no_paid_model_api_paths_in_source(self):
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            for marker in BANNED:
                if marker.lower() in lowered:
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [], f"paid API paths found: {offenders}")

    def test_no_secret_values_read(self):
        offenders = []
        for path in sorted(SRC.glob("*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                up = line.upper()
                if "OS.ENVIRON" in up and ("KEY" in up or "TOKEN" in up or "SECRET" in up):
                    if "META_API_KEY" not in line:
                        offenders.append(f"{path.name}:{i}: {line.strip()}")
                stripped = line.strip()
                if "GETPASS" in up and not stripped.startswith(("import ", "from ")):
                    # only getpass.getuser() (OS login name for pc-id) is allowed
                    if "GETUSER" not in up or "GETPASS(" in up.replace(" ", ""):
                        offenders.append(f"{path.name}:{i}: {stripped}")
                if "NETRC" in up or ".PEM" in up:
                    offenders.append(f"{path.name}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], f"secret reads found: {offenders}")

    def test_quota_path_waits_instead_of_falling_back(self):
        """QuotaHit must propagate to a saved wait state; no fallback call."""
        import sys as _sys
        _sys.path.insert(0, str(SRC.parent))
        from openmainty import runner  # noqa: E402
        import inspect
        src = inspect.getsource(runner.run_worker_step)
        self.assertIn("QuotaHit", src)
        self.assertIn("waitReason", src)
        # the worker step has exactly one worker-call site (no fallback API)
        self.assertEqual(src.count("executor.muse_step"), 1)

    def test_usage_parser_detects_contributor_without_token_fabrication(self):
        import sys as _sys
        _sys.path.insert(0, str(SRC.parent))
        from openmainty import muse as m  # noqa: E402
        import json
        stream = "\n".join([
            json.dumps({"stream": {"kind": "session", "id": "sess-1"},
                        "payload": {"kind": "run_model_configured", "model_id": "muse-spark-1.3"}}),
            json.dumps({"payload": {"kind": "x", "details": {"facets": [
                {"detail": {"provider": "meta", "request_id": "r",
                            "response_id": "resp", "model": "muse-spark-1.3-contributor"}}]}}}),
        ])
        usage = m.parse_usage(stream)
        self.assertEqual(usage["model"], "muse-spark-1.3")
        self.assertEqual(usage["provider_model"], "muse-spark-1.3-contributor")
        self.assertTrue(usage["contributor_applied"])
        self.assertFalse(usage["tokens_measurable"])
        self.assertIsNone(usage["tokens"])

    def test_model_traffic_only_via_muse_cli(self):
        import ast as _ast
        import sys as _sys
        _sys.path.insert(0, str(SRC.parent))
        from openmainty import muse as m  # noqa: E402
        import inspect
        src = inspect.getsource(m)
        self.assertIn('"muse", "exec"', src)
        for net in ("urllib", "http.client", "socket.create_connection"):
            self.assertNotIn(net, src, f"network client in muse worker: {net}")
        # "requests" also occurs as ordinary prose ("Server requests
        # are never answered ..."), so a substring scan false-positives.
        # What the policy forbids is importing the network client:
        # check real import statements instead.
        tree = _ast.parse(src)
        imported = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, _ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("requests", imported,
                         "network client in muse worker: requests")
        # A dynamic import smuggles the same client past the
        # statement scan: importlib.import_module("requests") and
        # __import__("requests") are forbidden too.
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            func = node.func
            dynamic = False
            if (isinstance(func, _ast.Attribute)
                    and func.attr == "import_module"
                    and isinstance(func.value, _ast.Name)
                    and func.value.id == "importlib"):
                dynamic = True
            elif (isinstance(func, _ast.Name)
                    and func.id == "__import__"):
                dynamic = True
            if dynamic and node.args and isinstance(node.args[0],
                                                    _ast.Constant):
                first = str(node.args[0].value).split(".")[0]
                self.assertNotEqual(first, "requests",
                                    "dynamic network client in muse "
                                    "worker: requests")


if __name__ == "__main__":
    unittest.main()
