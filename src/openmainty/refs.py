"""Repo-scoped upstream reference metadata, cached on demand (stdlib only).

A bare `#774` chip means nothing; the UI needs the actual kind (issue vs
PR), title, state, and URL. This module resolves those through the
supported `gh` read operation — never model-invented — and caches official
metadata locally so routine polls never spam the network.

Cache: `<home>/refs/<owner>__<repo>__<n>.json` with a 24h TTL. Stored
metadata wins; a fresh fetch refreshes it; a fetch failure returns the
stale cache explicitly marked, or `unknown` with the error when nothing
was ever stored. Failures are cached too (bounded negative cache with a
retry timestamp) so repeated UI opens/polls while offline never hammer
`gh`. Only positive integer issue numbers are ever fetched — anything
else is an explicit invalid entry with no network call. No writes, no
posts, no merges — read-only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import paths
from .util import run_cmd, utcnow

CACHE_TTL_S = 24 * 3600
# Failed refresh of KNOWN metadata: quiet window before the next attempt
# (repeated polls serve the stale entry without network).
STALE_RETRY_S = 300
# Unknown refs whose fetch failed: bounded negative-cache window before
# the next attempt (repeated polls serve the cached failure, no hammer).
NEG_RETRY_S = 900
# Every gh read is bounded: a hung helper must never stall the dashboard.
GH_TIMEOUT_S = 20

_NUMBER_RE = re.compile(r"^[0-9]+$")


def valid_number(raw) -> str | None:
    """Normalized positive issue number, or None when not a real ID.

    Guards against fabricated numeric refs: only actual positive
    integer IDs are ever fetched or cached. Returns the canonical
    digits (no `#`, no whitespace, no signs, no decimals).
    """
    s = str(raw or "").strip().lstrip("#").strip()
    if not s or not _NUMBER_RE.match(s):
        return None
    try:
        n = int(s, 10)
    except ValueError:
        return None
    if n < 1:
        return None
    return str(n)


def cache_file(github: str, number) -> Path:
    owner, _, repo = (github or "").partition("/")
    safe = f"{owner}__{repo}__{number}".replace("/", "_")
    return paths.home() / "refs" / f"{safe}.json"


def _read_cache(github: str, number) -> dict | None:
    try:
        data = json.loads(cache_file(github, number).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _cache_fresh(entry: dict | None) -> bool:
    if not entry or not entry.get("fetched_at"):
        return False
    try:
        import datetime
        stamp = datetime.datetime.fromisoformat(entry["fetched_at"])
        now = datetime.datetime.now(datetime.timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return (now - stamp).total_seconds() < CACHE_TTL_S
    except (ValueError, TypeError):
        return False


def _now_s() -> float:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def _parse_time(value) -> float | None:
    import datetime
    try:
        stamp = datetime.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


def _retry_due(entry: dict | None) -> bool:
    """True when a cached failure's quiet window has passed (or was
    never set, e.g. legacy cache files): a fresh attempt is allowed."""
    if not entry:
        return True
    nxt = _parse_time(entry.get("next_retry_at"))
    if nxt is None:
        return True
    return _now_s() >= nxt


def _has_metadata(entry: dict | None) -> bool:
    return bool(entry) and entry.get("kind") not in (None, "", "unknown")


def _write_cache(github: str, number: str, entry: dict) -> None:
    try:
        dest = cache_file(github, number)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(entry, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def _gh_api_issue(github: str, number):
    """(kind, title, state, url, error) via `gh api` (read-only).

    The REST issue endpoint covers PRs too: a `pull_request` key marks a
    PR. State comes from the object itself. The read is time-bounded.
    """
    code, out, err = run_cmd(
        ["gh", "api", f"repos/{github}/issues/{number}"],
        timeout=GH_TIMEOUT_S)
    if code != 0:
        return None, "", "unknown", "", f"gh api failed (exit {code}): {err.strip()[-120:]}"
    try:
        obj = json.loads(out or "{}")
    except ValueError as exc:
        return None, "", "unknown", "", f"gh api returned non-JSON: {exc}"
    if not isinstance(obj, dict) or "number" not in obj:
        return None, "", "unknown", "", "gh api returned no issue object"
    kind = "pr" if isinstance(obj.get("pull_request"), dict) else "issue"
    return (kind, str(obj.get("title") or ""), str(obj.get("state") or "unknown"),
            str(obj.get("html_url") or ""), None)


def _invalid_entry(number_raw, reason: str) -> dict:
    """Explicit invalid-number entry: no fetch, no cache write, no gh."""
    return {"number": str(number_raw), "kind": "unknown", "title": "",
            "state": "unknown", "url": "", "fetched_at": utcnow(),
            "source": "none", "fresh": False, "stale": False,
            "error": reason, "last_success_at": None,
            "last_attempt_at": None, "next_retry_at": None, "failures": 0}


def lookup(github: str, number) -> dict:
    """Resolve one `#number` to {kind, title, state, url, ...}.

    Keys are stable for the UI contract: number/kind/title/state/url/
    fetched_at/source/fresh/error. Added state: stale/last_success_at/
    last_attempt_at/next_retry_at/failures (+fetch_error on stale).
    `source` is `cache` or `gh-api`; `fresh` True ONLY for within-TTL
    metadata or a just-completed fetch — never faked after a failure.
    Unknown refs (never stored, fetch failed) carry kind `unknown` plus
    the error — never an invented title — and the failure itself is
    cached with a bounded retry timestamp so offline polls do not
    hammer `gh`. Invalid (non-positive-integer) numbers never reach
    the network. Manual refresh: delete the cache file (or wait out
    the TTL / retry window); the next lookup refetches.
    """
    norm = valid_number(number)
    if norm is None:
        return _invalid_entry(number, f"invalid reference number: {number!r}")
    number = norm
    cached = _read_cache(github, number)
    if cached is not None and _cache_fresh(cached) and not cached.get("error"):
        out = dict(cached)
        out["source"] = "cache"
        out["fresh"] = True
        out["stale"] = False
        if not out.get("last_success_at") and out.get("fetched_at"):
            out["last_success_at"] = out["fetched_at"]
        return out
    if cached is not None and not _retry_due(cached):
        # Inside the quiet window: serve the cached entry untouched —
        # stale metadata or a cached failure, explicitly marked, with
        # zero network. `fresh` is never faked here.
        out = dict(cached)
        out["source"] = "cache" if _has_metadata(cached) else "none"
        out["fresh"] = False
        out["stale"] = _has_metadata(cached)
        return out
    kind, title, state, url, error = _gh_api_issue(github, number)
    fetched_at = utcnow()
    if error is not None:
        failures = int((cached or {}).get("failures") or 0) + 1
        if _has_metadata(cached):
            out = dict(cached)
            out["source"] = "cache"
            out["fresh"] = False
            out["stale"] = True
            out["fetch_error"] = error
            out["last_attempt_at"] = fetched_at
            out["failures"] = failures
            out["next_retry_at"] = _plus_seconds(fetched_at, STALE_RETRY_S)
            _write_cache(github, number, out)
            return out
        out = {"number": number, "kind": "unknown", "title": "",
               "state": "unknown", "url": "", "fetched_at": fetched_at,
               "source": "none", "fresh": False, "stale": False,
               "error": error, "last_success_at": None,
               "last_attempt_at": fetched_at, "failures": failures,
               "next_retry_at": _plus_seconds(fetched_at, NEG_RETRY_S)}
        _write_cache(github, number, out)
        return out
    entry = {"number": number, "kind": kind, "title": title,
             "state": state, "url": url, "fetched_at": fetched_at,
             "source": "gh-api", "fresh": True, "stale": False,
             "error": None, "last_success_at": fetched_at,
             "last_attempt_at": fetched_at, "failures": 0,
             "next_retry_at": None}
    _write_cache(github, number, entry)
    return entry


def _plus_seconds(iso: str, seconds: int) -> str:
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        dt = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (dt + datetime.timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


def lookup_many(github: str, numbers: list) -> list:
    """Batch resolve (deduped, order-stable, at most 50 per call).

    Invalid numbers resolve to explicit invalid entries locally —
    never fetched, never cached, never invented.
    """
    seen = []
    for n in numbers or []:
        s = str(n).strip()
        if s and s not in seen:
            seen.append(s)
        if len(seen) >= 50:
            break
    return [lookup(github, n) for n in seen]
