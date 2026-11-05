"""Durable event timeline: every work item answers what/why/evidence/next.

Append-only JSONL at <home>/events.jsonl — survives restarts, never rewritten.
Each event: {at, repo, kind, ref, title, detail, links, actor}.
"""
from __future__ import annotations

from . import paths
from .util import read_json, utcnow


def _file():
    return paths.home() / "events.jsonl"


def append(repo: str, kind: str, ref: str = "", title: str = "",
           detail: str = "", links: list | None = None,
           actor: str = "openmainty") -> dict:
    ev = {"at": utcnow(), "repo": repo or "", "kind": kind, "ref": ref or "",
          "title": title or "", "detail": (detail or "")[:2000],
          "links": links or [], "actor": actor}
    import json
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def list_events(repo: str = "", limit: int = 100) -> list:
    f = _file()
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    import json
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if repo and ev.get("repo") not in ("", repo):
            continue
        out.append(ev)
        if len(out) >= limit:
            break
    return out


def read_legacy_compat(path=None) -> list:
    """Tolerate old event files: one JSON array or JSONL; malformed lines skipped."""
    import json
    from pathlib import Path
    p = Path(path) if path else _file()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except ValueError:
        pass
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out
