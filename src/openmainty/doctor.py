"""Doctor: environment, auth, tools, devices — without leaking secrets.

Two levels:
- quick (default): binary presence, OS, pc.json, repo checkout, auth *names*.
- --live: also runs one tiny subscription worker call to prove headless
  execution works (spends a small amount of subscription usage).

Muse runtime support vs project build/device support are reported in
separate sections so one is never mistaken for the other.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys

from . import muse, pcconfig, repos
from .util import run_cmd


def _present(value: bool) -> str:
    return "ok" if value else "missing"


def quick() -> dict:
    pc = pcconfig.load()
    result = {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "verified_os": platform.system() in ("Darwin", "Linux"),
        "muse_runtime": {
            "cli": _present(muse.which() is not None),
            "version": muse.version(),
            "model": muse.MODEL,
            "contributor_mode": "supported flag (--contributor-mode on)",
            "billing_path": muse.billing_path(),
            "meta_api_key_set": bool(os.environ.get("META_API_KEY")),
        },
        "project_tools": {
            "git": _present(shutil.which("git") is not None),
            "gh": _present(shutil.which("gh") is not None),
            "node": _present(shutil.which("node") is not None),
            "bun": _present(shutil.which("bun") is not None),
            "xcodebuild": _present(shutil.which("xcodebuild") is not None),
        },
        "repos": {},
        "capabilities": pc.get("capabilities", {}),
    }
    for name in repos.list_all():
        co = (pc.get("checkouts", {}) or {}).get(name)
        ok = bool(co) and os.path.isdir(co or "")
        result["repos"][name] = {"checkout": co or "(not linked)", "status": _present(ok)}
    result["note"] = (
        "Only macOS is verified in this session; other OS rows are informational, "
        "not support claims. Capability flags come from pc.json on this PC."
    )
    return result


def live(model=None) -> dict:
    """One tiny subscription run to prove headless execution + Contributor."""
    result = quick()
    code, out, usage = muse.exec_prompt("Reply with exactly: DOCTOR_OK", model=model)
    result["requested_model"] = muse.resolve_model(model)
    result["live_subscription_run"] = {
        "exit_code": code,
        "model_configured": (usage or {}).get("model"),
        "model_served": (usage or {}).get("provider_model"),
        "contributor_applied": (usage or {}).get("contributor_applied"),
        "session_id": (usage or {}).get("session_id"),
        "tokens": "not measurable via exec stream (recorded as unknown, not estimated)",
    }
    return result


def render_human(result: dict) -> str:
    lines = [f"OS: {result['os']}  (verified here: {result['verified_os']})",
             f"python: {result['python']}", "",
             "[muse runtime (subscription path)]"]
    for k, v in result["muse_runtime"].items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("[project build/device tools]")
    for k, v in result["project_tools"].items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append("[repos]")
    for name, info in result.get("repos", {}).items():
        lines.append(f"  {name}: {info['status']} ({info['checkout']})")
    lines.append("")
    lines.append("[capabilities on this PC]")
    for k, v in result.get("capabilities", {}).items():
        lines.append(f"  {k}: {v}")
    if "live_subscription_run" in result:
        lines.append("")
        lines.append("[live subscription run]")
        for k, v in result["live_subscription_run"].items():
            lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(result.get("note", ""))
    return "\n".join(lines)
