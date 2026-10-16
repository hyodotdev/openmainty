"""Muse worker wrapper.

Primary path: subscription login (`muse login`) + `muse exec` with
`--provider meta --model muse-spark-1.3 --contributor-mode on`.
No model API is called directly and no API key is required; when
META_API_KEY is set in the environment it takes priority over the login
(report only its presence, never its value).

Usage honesty: the `--json` event stream exposes model id, session id,
stream byte counts and request/response ids, but no token counts.
Record exactly those; tokens are stored as {"tokens_measurable": false}.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from . import __version__
from .util import run_cmd

DEFAULT_MODEL = "muse-spark-1.3"
# Back-compat alias: existing callers/tests reference muse.MODEL.
MODEL = DEFAULT_MODEL


def resolve_model(name=None) -> str:
    """Return the explicit model or the default. No catalog validation."""
    if isinstance(name, str) and name.strip():
        return name.strip()
    return DEFAULT_MODEL


def which() -> str | None:
    return shutil.which("muse")


def version() -> str | None:
    path = which()
    if not path:
        return None
    code, out, _ = run_cmd([path, "--version"])
    if code != 0:
        return None
    return out.strip().splitlines()[0] if out.strip() else "unknown"


def billing_path() -> str:
    """Which billing path would `muse exec` take? Name only, no values."""
    if os.environ.get("META_API_KEY"):
        return "api-key (META_API_KEY is set; takes priority over login)"
    return "subscription login (no META_API_KEY in environment)"


def build_cmd(prompt: str, workdir=None, contributor: bool = True, extra=None, model=None) -> list:
    cmd = [
        "muse", "exec",
        "--provider", "meta",
        "--model", resolve_model(model),
        "--contributor-mode", "on" if contributor else "off",
        "--disable-request-user-input",
        "--json",
    ]
    if workdir:
        cmd += ["--workspace", str(workdir)]
    if extra:
        cmd += list(extra)
    cmd.append(prompt)
    return cmd


def parse_usage(jsonl_text: str) -> dict:
    """Extract only measurable fields from an exec --json stream."""
    model_id = None
    provider_model = None
    session_id = None
    request_id = None
    response_id = None
    stream_bytes = len(jsonl_text.encode("utf-8"))
    for line in jsonl_text.splitlines():
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        payload = evt.get("payload", {}) if isinstance(evt, dict) else {}
        if not isinstance(payload, dict):
            continue
        if payload.get("kind") == "run_model_configured":
            model_id = payload.get("model_id", model_id)
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        details = payload.get("details") or event.get("details") or {}
        if isinstance(details, dict):
            for facet in details.get("facets", []) or []:
                if not isinstance(facet, dict):
                    continue
                detail = facet.get("detail", {}) or {}
                if isinstance(detail, dict):
                    if detail.get("request_id"):
                        request_id = detail["request_id"]
                    if detail.get("response_id"):
                        response_id = detail["response_id"]
                    if detail.get("model"):
                        provider_model = detail["model"]
        stream = evt.get("stream", {}) if isinstance(evt, dict) else {}
        if isinstance(stream, dict) and stream.get("kind") == "session" and not session_id:
            session_id = stream.get("id")
    usage = {
        "model": model_id,
        "provider_model": provider_model,
        "contributor_applied": (provider_model or "").endswith("-contributor"),
        "session_id": session_id,
        "stream_bytes": stream_bytes,
        "tokens_measurable": False,
        "tokens": None,
    }
    if request_id:
        usage["request_id"] = request_id
    if response_id:
        usage["response_id"] = response_id
    return usage


def exec_prompt(prompt: str, workdir=None, contributor: bool = True, timeout: int = 1200, model=None):
    """Run one headless Muse step. Returns (exit, output_text, usage)."""
    path = which()
    if not path:
        return 127, "", {"error": "muse CLI not found"}
    cmd = build_cmd(prompt, workdir=workdir, contributor=contributor, model=model)
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir) if workdir else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", {"timeout_s": timeout, "tokens_measurable": False, "tokens": None}
    usage = parse_usage(proc.stdout)
    usage["exit_code"] = proc.returncode
    usage["cli_version"] = version()
    # Human-readable tail is stdout's last run_output_delta text; the raw
    # stream is large, so callers persist a trimmed log separately.
    return proc.returncode, proc.stdout, usage


def agent_info(model=None) -> dict:
    return {
        "openmainty": __version__,
        "muse": version(),
        "model": resolve_model(model),
        "contributor_mode": True,
        "billing": billing_path(),
    }
