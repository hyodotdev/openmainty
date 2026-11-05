"""GitHub Discussions ingestion + app-owned review (read-only network).

Discussions are a community surface, not maintenance work: they are
ingested through the supported `gh` GraphQL API on the normal scan
tick, cached as typed snapshots, and reviewed only by an explicit
app-owned action. Nothing here creates runs, branches, or worktrees,
and nothing posts, comments, or labels anything upstream.

Typed identity: {kind: "discussion", repo, number, node_id, url}.
Queue keys would read repo#discussion-<number> and never collide
with issue keys — but discussion records never enter the main
dep/issue queue at all (R3): the scan persists snapshots only, and
reviews run in this module's own namespace under the global worker
lease.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

from pathlib import Path

from . import paths
from . import util as _util
from .util import utcnow, write_json, read_json

LIST_FIRST = 25
COMMENTS_FIRST = 50
REPLIES_FIRST = 20
LABELS_PER_PAGE = 100
LABEL_PAGES_CAP = 10
SCAN_PAGES_CAP = 4
REVIEW_TIMEOUT_S = 600
EVIDENCE_FILES_CAP = 5
EVIDENCE_BYTES_CAP = 20000


class DiscussionError(RuntimeError):
    """Unreadable upstream or snapshot. Callers keep last-known data."""

    def __init__(self, message: str = "", status: int = 500):
        super().__init__(message)
        self.http_status = status


class ReviewBusy(RuntimeError):
    """A worker already holds the lease; nothing started."""


class ReviewInputError(ValueError):
    """Bad caller input (worker caller, missing confirm, unknown id)."""


def discussions_dir() -> Path:
    return paths.home() / "discussions"


def _slug_dir(github: str) -> Path:
    owner, name = _repo_owner_name(github)
    for part in (owner, name):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                or part in (".", "..")):
            raise DiscussionError(f"bad repo slug: {github!r}")
    return discussions_dir() / f"{owner}__{name}"


def snapshot_path(github: str) -> Path:
    return _slug_dir(github) / "snapshot.json"


def _safe_number(number) -> int:
    """Positive-int discussion numbers only; rejects node ids/URLs."""
    text = str(number or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,6}", text):
        raise DiscussionError(f"bad discussion number: {number!r}")
    return int(text)


def node_path(github: str, number) -> Path:
    return _slug_dir(github) / f"discussion-{_safe_number(number)}.json"


def catalog_path(github: str) -> Path:
    return _slug_dir(github) / "labels.json"


def review_path(github: str, number) -> Path:
    return _slug_dir(github) / f"review-{_safe_number(number)}.json"


def job_path(github: str, number) -> Path:
    return _slug_dir(github) / f"review-job-{_safe_number(number)}.json"


def valid_number(raw) -> str | None:
    """Positive integer discussion numbers only — never a node id or URL."""
    text = str(raw or "").strip()
    if re.fullmatch(r"[1-9][0-9]{0,6}", text):
        return text
    return None


def discussion_key(repo: str, number) -> str:
    return f"{repo}#discussion-{valid_number(number) or 'invalid'}"


def _read(path, default=None):
    try:
        return read_json(path, default=default)
    except (Exception, SystemExit):  # noqa: BLE001 - corrupt reads degrade
        return default


def _gh_graphql(query: str, variables: dict | None = None) -> dict:
    """One read-only `gh api graphql` call. Raises DiscussionError."""
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        if value is None:
            continue
        argv += ["-F", f"{key}={value}"]
    # Late-bound through the util module (not a private copy) so a
    # caller-level mock of openmainty.util.run_cmd applies uniformly.
    try:
        code, out, err = _util.run_cmd(argv, timeout=20)
    except Exception as exc:
        raise DiscussionError(f"gh call failed: {exc}") from exc
    if code != 0:
        raise DiscussionError(
            f"gh graphql failed (exit {code}): {err.strip()[-200:]}")
    try:
        parsed = json.loads(out or "{}")
    except ValueError as exc:
        raise DiscussionError(f"gh graphql non-JSON: {exc}") from exc
    # The raw stdout shape is validated BEFORE any .get: a bare list,
    # string, or scalar stdout is a source error (typed
    # DiscussionError, prior data kept), never an AttributeError.
    if not isinstance(parsed, dict):
        raise DiscussionError(
            "gh graphql returned a non-object envelope "
            f"({type(parsed).__name__}); prior data kept")
    if parsed.get("errors"):
        detail = json.dumps(parsed["errors"])[:200]
        raise DiscussionError(f"gh graphql errors: {detail}")
    data = parsed.get("data") or {}
    if not isinstance(data, dict):
        raise DiscussionError("gh graphql returned no data object")
    return data


_LIST_QUERY = """query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    hasDiscussionsEnabled
    discussions(first: $first, after: $after,
                orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { id number title updatedAt
        category { name } comments { totalCount } }
    }
  }
}"""

_NODE_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      id number title body bodyHTML url createdAt updatedAt closed closedAt
      upvoteCount viewerHasUpvoted
      category { name emoji description }
      author { login }
      answerChosenAt answer {
        id databaseId author { login } body createdAt updatedAt url }
      labels(first: 20) { totalCount
        pageInfo { hasNextPage } nodes { name } }
      comments(first: 50) { totalCount
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId author { login } body createdAt updatedAt
                url isAnswer
          replies(first: 20) { totalCount
            pageInfo { hasNextPage }
            nodes { id databaseId author { login } body createdAt updatedAt
                    url } } } }
    }
  }
}"""

_LABEL_CATALOG = {
    "status": [("unreviewed", "🤖 미검토"), ("reviewed", "✅ 검토됨"),
               ("stale-review", "🔁 재검토 필요"),
               ("needs-action", "🛠 후속 필요"),
               ("needs-review", "🤖 검토 요청")],
    "platform": [("android", "🤖 Android"), ("ios", "📱 iOS"),
                 ("web", "🌐 웹"), ("unknown", "❔ 플랫폼 미정")],
    "type": [("feature", "✨ 기능 요청"), ("question", "❓ 질문"),
             ("bug", "🐞 버그"), ("discussion", "💬 논의"),
             ("docs", "📄 문서"), ("unknown", "❔ 유형 미정")],
}


def _repo_owner_name(github: str) -> tuple[str, str]:
    parts = (github or "").split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise DiscussionError(f"bad repo slug: {github!r}")
    return parts[0], parts[1]


_SCAN_PER_PAGE = 25
_SCAN_MAX_PAGES = 3
# Default per-pass item budget: head refresh + two backfill pages.
SCAN_DEFAULT_BUDGET = _SCAN_PER_PAGE * _SCAN_MAX_PAGES


def _carry(prev: dict, *keys) -> dict:
    """Carry snapshot bookkeeping across error/disabled rewrites."""
    return {key: prev.get(key) for key in keys}


def scan_source(github: str, limit: int = SCAN_DEFAULT_BUDGET) -> dict:
    """One model-free scan pass: list discussions, refresh snapshot file.

    Each pass refreshes the head page, then continues the archived
    backfill from the stored cursor up to the bounded budget — older
    pages progress across ticks while recent rows stay fresh. When
    more pages remain the snapshot is marked partial with the
    backfill cursor; equal timestamps never drop (merge is by node
    id). Network errors are recorded and re-raised as DiscussionError
    for sched to turn into degraded status; prior items are preserved.
    """
    owner, name = _repo_owner_name(github)
    stamp = utcnow()
    prev = _read_snapshot(github)
    try:
        items, page = _scan_pages(github, owner, name, limit,
                                  prev.get("backfill_after"))
        disabled = page.get("disabled", False)
    except DiscussionError as exc:
        kept = [it for it in (prev.get("items") or [])
                if isinstance(it, dict)]
        _write_snapshot(github, kept, stamp,
                        error=str(exc),
                        partial=True,
                        total_hint=prev.get("total_hint", limit),
                        **_carry(prev, "watermark", "cursor", "pages",
                                "backfill_after", "note"))
        raise
    if disabled:
        kept = [it for it in (prev.get("items") or [])
                if isinstance(it, dict)]
        _write_snapshot(github, kept, stamp,
                        error="disabled",
                        partial=False,
                        total_hint=prev.get("total_hint"),
                        **_carry(prev, "watermark", "cursor", "pages",
                                "backfill_after", "note"))
        raise DiscussionError("disabled")
    merged = _merge_scan(prev, items, stamp)
    _write_snapshot(github, merged["items"], stamp,
                    partial=not page["complete"],
                    note=page.get("note"),
                    total_hint=page["total_count"],
                    watermark=page["watermark"],
                    cursor=page["cursor"],
                    backfill_after=page.get("backfill_after"),
                    pages=page["pages"])
    return {"items": merged["items"],
            "status": snapshot_status(github)}


def _envelope_repo(data, what: str) -> dict:
    """Validate a GraphQL data envelope; return the repository block.

    A missing/null repository (renamed repo, no access, type change)
    is a source ERROR, never a healthy empty result — the prior items
    are kept and the UI shows degraded. Anything that is not a JSON
    object envelope (bad stdout, a bare list, GraphQL errors) raises
    DiscussionError instead of AttributeError.
    """
    if not isinstance(data, dict):
        raise DiscussionError(
            f"{what}: unexpected GraphQL envelope "
            f"({type(data).__name__}); prior data kept")
    if data.get("errors"):
        detail = data["errors"][0] if isinstance(
            data.get("errors"), list) else data["errors"]
        raise DiscussionError(f"{what}: GraphQL error: {detail}"[:300])
    repo = data.get("repository")
    if repo is None:
        raise DiscussionError(
            f"{what}: no repository in response "
            "(renamed, removed, or no access); prior data kept")
    if not isinstance(repo, dict):
        raise DiscussionError(
            f"{what}: repository block is not an object; prior data kept")
    return repo


def _fetch_list_page(owner: str, name: str, first: int,
                     after) -> tuple[dict, dict]:
    """One list page: (repository block, discussions block)."""
    data = _gh_graphql(_LIST_QUERY,
                       {"owner": owner, "name": name,
                        "first": first, "after": after})
    repo = _envelope_repo(data, "discussion list")
    disc = repo.get("discussions") or {}
    if not isinstance(disc, dict):
        raise DiscussionError(
            "discussion list: discussions block is not an object; "
            "prior data kept")
    return repo, disc


def _page_items(disc: dict, watermark) -> tuple[list[dict], object]:
    """Normalize one page's nodes; return (items, max updated_at)."""
    items: list[dict] = []
    top = watermark
    for node in disc.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        updated = node.get("updatedAt")
        items.append({
            "id": node.get("id"), "type": "discussion",
            "number": node.get("number"), "title": node.get("title"),
            "category": (node.get("category") or {}).get("name"),
            "comments": ((node.get("comments") or {}).get("totalCount")
                         or 0),
            "updated_at": updated})
        if updated and (top is None or updated > top):
            top = updated
    return items, top


def _scan_pages(github: str, owner: str, name: str, limit: int,
                resume_after=None) -> tuple[list[dict], dict]:
    """Read bounded list pages; return (items, page-state).

    page-state: complete, pages, total_count, watermark (max
    updated_at), cursor (endCursor of the last fully read page),
    backfill_after (cursor to resume from on the next pass),
    disabled. Every pass starts with a 1-page head refresh
    (updated-DESC order), then continues any archived backfill from
    the stored cursor — recent rows stay fresh while older pages
    progress across ticks. Merge (by node id, never timestamp) keeps
    equal-timestamp rows. A hasNextPage with no cursor to continue
    from is partial, never complete.
    """
    items: list[dict] = []
    pages = 0
    disabled = False
    total_count = None
    watermark = None
    cursor = None
    note = None
    budget = max(1, min(limit, _SCAN_PER_PAGE * _SCAN_MAX_PAGES))

    def _read(after, first):
        nonlocal pages, total_count, watermark, cursor
        repo, disc = _fetch_list_page(owner, name, first, after)
        if total_count is None:
            total_count = disc.get("totalCount")
        chunk, watermark = _page_items(disc, watermark)
        items.extend(chunk)
        pages += 1
        info = disc.get("pageInfo") or {}
        cursor = info.get("endCursor") or cursor
        return repo, disc, info

    # Phase 1: head refresh (one page from the top).
    repo, disc, info = _read(None, min(_SCAN_PER_PAGE, budget))
    if "hasDiscussionsEnabled" in repo:
        disabled = not repo.get("hasDiscussionsEnabled", True)
        if disabled:
            return items, {"complete": True, "pages": pages,
                           "total_count": total_count,
                           "watermark": watermark, "cursor": cursor,
                           "backfill_after": None, "disabled": True,
                           "note": note}
    # Phase 2: continue the archived backfill (stored cursor first,
    # else this pass's head cursor) while budget and page cap allow.
    after = resume_after or info.get("endCursor")
    while (info.get("hasNextPage", False) and after
           and len(items) < budget and pages < _SCAN_MAX_PAGES):
        _, _, info = _read(after, min(_SCAN_PER_PAGE,
                                      budget - len(items)))
        after = info.get("endCursor")
    pending = bool(info.get("hasNextPage", False))
    if pending and not after:
        # Nowhere to resume from: partial, never complete — the next
        # pass restarts from the head and whatever is merged stays.
        note = "hasNextPage without a cursor; backfill restarts at head"
        after = None
    complete = not pending
    backfill_after = after if pending else None
    return items, {"complete": complete, "pages": pages,
                   "total_count": total_count, "watermark": watermark,
                   "cursor": cursor, "backfill_after": backfill_after,
                   "disabled": disabled, "note": note}


def _merge_scan(prev: dict, items: list[dict], stamp: str) -> dict:
    """Union current list items with previously stored ones (by id).

    Backlog archival: items that fell off this page are kept so their
    stored reviews/label states stay visible until a later pass confirms
    them absent. Nothing is dropped here.
    """
    seen = {it.get("id") for it in items if it.get("id")}
    old = [it for it in (prev.get("items") or [])
           if isinstance(it, dict) and it.get("id") not in seen]
    return {"items": items + old, "checked_at": stamp}


def _read_json_safe(path: Path):
    """Read JSON, distinguishing missing / corrupt / wrong-shape.

    Returns (status, data) with status one of "ok", "missing",
    "corrupt", "shape". A corrupt file is NEVER moved aside on read:
    every poll reports it corrupt (persistent error visibility until
    a successful refresh overwrites it), so a list read followed by
    a status read in the same request cannot flip from error to
    healthy-never. Historical bytes stay in place. Never raises.
    """
    try:
        data = read_json(path)
    except FileNotFoundError:
        return "missing", None
    except (Exception, SystemExit):
        return "corrupt", None
    if data is None:
        return "missing", None
    if not isinstance(data, dict):
        return "shape", None
    return "ok", data


def _read_snapshot(github: str) -> dict:
    status, data = _read_json_safe(snapshot_path(github))
    if status == "ok":
        return data
    if status in ("corrupt", "shape"):
        return {"_unreadable": True}
    return {}


def _snapshot_item(github: str, number) -> dict | None:
    """List row for one discussion number, or None."""
    try:
        want = _safe_number(number)
    except DiscussionError:
        return None
    snap = _read_snapshot(github)
    for item in snap.get("items") or []:
        if isinstance(item, dict) and item.get("number") == want:
            return item
    return None


def _snapshot_problems(snap: dict) -> str | None:
    """Validate a discussion-list record; None when it is usable.

    A valid-JSON file with the wrong shape (hand-edited, truncated,
    a different record) must surface as a persistent source error —
    never as a healthy zero-item list.
    """
    if not isinstance(snap, dict) or snap.get("kind") != "discussion-list":
        return "not a discussion-list record"
    items = snap.get("items")
    if not isinstance(items, list):
        return "items is not a list"
    for row in items:
        if not isinstance(row, dict):
            return "a row is not an object"
        if not isinstance(row.get("number"), int):
            return "a row lacks a numeric discussion number"
        if not isinstance(row.get("id"), str):
            return "a row lacks a node id"
    return None


def _valid_rows(items: list[dict]) -> list[dict]:
    """Rows with a usable typed identity."""
    out = []
    for row in items:
        if (isinstance(row, dict) and isinstance(row.get("number"), int)
                and isinstance(row.get("id"), str)):
            out.append(row)
    return out


def _preserve_once(path: Path) -> str | None:
    """Preserve corrupt JSON bytes once, before they are replaced.

    A successful refresh must not silently destroy the only copy of
    the raw bad bytes the error state was reporting. The first
    replacement copies them to a stable hash-named sibling
    (<name>.corrupt-<sha12>.json); repeats are a no-op so a stuck
    corrupt file cannot spam one copy per poll. Healthy or empty
    files are never copied. Returns the backup name or None.
    """
    name = getattr(path, "name", "") or ""
    if ".corrupt-" in name:
        return None
    try:
        raw = path.read_bytes() if path.is_file() else b""
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        pass
    else:
        return None
    keep = path.with_name(
        f"{name}.corrupt-"
        f"{hashlib.sha256(raw).hexdigest()[:12]}.json")
    if keep.exists():
        return keep.name
    try:
        keep.write_bytes(raw)
    except OSError:
        return None
    return keep.name


def _write_snapshot(github: str, items: list[dict], stamp: str,
                    error: str | None = None, partial: bool = False,
                    total_hint=None, watermark=None, cursor=None,
                    backfill_after=None, pages: int = 0,
                    note=None) -> dict:
    """Write the discussions snapshot. Volatile meta lives OUTSIDE items."""
    recovered = _preserve_once(snapshot_path(github))
    snap = {
        "schema": 1, "repo": github, "kind": "discussion-list",
        "checked_at": stamp, "error": error,
        "partial": bool(partial), "total_hint": total_hint,
        "watermark": watermark, "cursor": cursor,
        "backfill_after": backfill_after, "pages": pages,
        "note": note,
        "items": items,
        "labels_catalog": label_catalog(),
    }
    if recovered:
        snap["recovered_from"] = recovered
    write_json(snapshot_path(github), snap)
    return snap


def snapshot_status(github: str) -> dict:
    """Cached poll state for UI: freshness, error, disabled, pagination."""
    snap = _read_snapshot(github)
    if not snap:
        return {"state": "never", "checked_at": None, "error": None,
                "disabled": False, "partial": False}
    if snap.get("_unreadable"):
        return {"state": "error", "checked_at": None,
                "error": "snapshot unreadable (kept in place)",
                "disabled": False, "partial": True}
    problem = _snapshot_problems(snap)
    if problem is not None:
        return {"state": "error",
                "checked_at": (snap.get("checked_at")
                               if isinstance(snap, dict) else None),
                "error": f"snapshot invalid ({problem}); kept in place "
                         "until a successful refresh replaces it",
                "disabled": False, "partial": True}
    err = snap.get("error")
    disabled = (err == "disabled")
    # Disabled is a normal capability state, not a fetch failure: it
    # reports its own state (with no error text) while sched keeps the
    # disabled signal from the scan_source exception contract.
    if disabled:
        state, err = "disabled", None
    else:
        state = ("error" if err
                 else ("partial" if snap.get("partial") else "ok"))
    return {"state": state, "checked_at": snap.get("checked_at"),
            "error": err, "disabled": disabled,
            "partial": bool(snap.get("partial")),
            "total_hint": snap.get("total_hint"),
            "watermark": snap.get("watermark"),
            "cursor": snap.get("cursor"),
            "backfill_after": snap.get("backfill_after"),
            "note": snap.get("note"),
            "pages": snap.get("pages", 0)}


def fetch_node(github: str, number) -> dict:
    """Fetch one full discussion node (detail + comments + labels).

    Returns the normalized node dict and persists it to its node file.
    Raises DiscussionError on network failure (nothing is persisted).
    """
    owner, name = _repo_owner_name(github)
    want = _safe_number(number)
    data = _gh_graphql(_NODE_QUERY,
                       {"owner": owner, "name": name, "number": want})
    repo = _envelope_repo(data, f"discussion #{want}")
    raw = repo.get("discussion") or {}
    if not isinstance(raw, dict) or not raw.get("id"):
        raise DiscussionError(
            f"discussion #{want} not found in {github}")
    if raw.get("number") != want:
        raise DiscussionError(
            f"discussion identity mismatch: asked #{want}, "
            f"got #{raw.get('number')}")
    node = _normalize_node(github, raw)
    write_json(node_path(github, want), node)
    return node


_LABELS_PER_PAGE = 100
_LABELS_MAX_PAGES = 3


def _gh_rest(api_path: str):
    """One read-only `gh api` REST call with a ~20s timeout."""
    try:
        code, out, err = _util.run_cmd(["gh", "api", api_path],
                                       timeout=20)
    except Exception as exc:
        raise DiscussionError(f"gh call failed: {exc}") from exc
    if code != 0:
        raise DiscussionError(
            f"gh api failed (exit {code}): {(err or '').strip()[-200:]}")
    try:
        parsed = json.loads(out or "null")
    except ValueError as exc:
        raise DiscussionError(f"gh api non-JSON: {exc}") from exc
    return parsed


def fetch_labels(github: str) -> dict:
    """Census the repo's actual label set with bounded pagination.

    Stores labels.json: {labels, labels_complete, pages}. A partial
    census is flagged partial — never presented as the full set.
    Raises DiscussionError on network failure (nothing is persisted).
    """
    owner, name = _repo_owner_name(github)
    labels: list[dict] = []
    pages = 0
    complete = True
    while pages < _LABELS_MAX_PAGES:
        chunk = _gh_rest(
            f"repos/{owner}/{name}/labels"
            f"?per_page={_LABELS_PER_PAGE}&page={pages + 1}")
        if not isinstance(chunk, list):
            raise DiscussionError("gh labels returned a non-list page")
        pages += 1
        for entry in chunk:
            if isinstance(entry, dict) and entry.get("name"):
                labels.append({"name": entry.get("name"),
                               "color": entry.get("color"),
                               "description": entry.get("description")})
        if len(chunk) < _LABELS_PER_PAGE:
            break
    else:
        complete = False
    if pages >= _LABELS_MAX_PAGES and len(chunk) == _LABELS_PER_PAGE:
        complete = False
    record = {"schema": 1, "repo": github, "kind": "label-census",
              "checked_at": utcnow(), "labels": labels,
              "labels_complete": complete, "pages": pages}
    write_json(catalog_path(github), record)
    return record


def label_census(github: str) -> dict:
    """Stored label census, or an empty never-checked record."""
    status, data = _read_json_safe(catalog_path(github))
    if status == "ok":
        if (isinstance(data, dict) and isinstance(data.get("labels"), list)
                and data.get("kind") == "label-census"):
            return data
        # Valid JSON but the wrong record: a persistent source error,
        # never a silent empty census.
        return {"labels": [], "labels_complete": False, "pages": 0,
                "error": "label census invalid (kept in place until "
                         "a successful refresh replaces it)"}
    if status in ("corrupt", "shape"):
        return {"labels": [], "labels_complete": False, "pages": 0,
                "error": "label census unreadable (kept in place)"}
    return {"labels": [], "labels_complete": False, "pages": 0}


def _normalize_node(github: str, raw: dict) -> dict:
    def _login(obj):
        return (obj or {}).get("login")

    def _comment(node):
        replies = [ _reply(r) for r in
                    (((node or {}).get("replies") or {}).get("nodes") or [])
                    if isinstance(r, dict)]
        return {
            "id": (node or {}).get("id"),
            "database_id": (node or {}).get("databaseId"),
            "author": _login((node or {}).get("author")),
            "body": (node or {}).get("body"),
            "created_at": (node or {}).get("createdAt"),
            "updated_at": (node or {}).get("updatedAt"),
            "url": (node or {}).get("url"),
            "is_answer": bool((node or {}).get("isAnswer")),
            "replies": replies,
            "reply_count": (((node or {}).get("replies") or {})
                            .get("totalCount") or len(replies)),
        }

    def _reply(node):
        return {
            "id": (node or {}).get("id"),
            "database_id": (node or {}).get("databaseId"),
            "author": _login((node or {}).get("author")),
            "body": (node or {}).get("body"),
            "created_at": (node or {}).get("createdAt"),
            "updated_at": (node or {}).get("updatedAt"),
            "url": (node or {}).get("url"),
        }

    comments_raw = raw.get("comments") or {}
    comments = [_comment(c) for c in (comments_raw.get("nodes") or [])
                if isinstance(c, dict)]
    comments_total = comments_raw.get("totalCount")
    comments_complete = not (comments_raw.get("pageInfo") or {}).get(
        "hasNextPage", False)
    for entry, raw_c in zip(comments, [c for c in
                                       (comments_raw.get("nodes") or [])
                                       if isinstance(c, dict)]):
        replies_raw = (raw_c.get("replies") or {})
        entry["replies_complete"] = not (replies_raw.get("pageInfo") or {}
                                         ).get("hasNextPage", False)
    answer = raw.get("answer") or {}
    labels_raw = raw.get("labels") or {}
    label_nodes = labels_raw.get("nodes") or []
    labels_total = labels_raw.get("totalCount")
    labels_complete = not (labels_raw.get("pageInfo") or {}).get(
        "hasNextPage", False)
    return {
        "schema": 1, "repo": github, "kind": "discussion",
        "id": raw.get("id"), "number": raw.get("number"),
        "title": raw.get("title"), "body": raw.get("body"),
        "url": raw.get("url"),
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "closed": bool(raw.get("closed")), "closed_at": raw.get("closedAt"),
        "upvotes": raw.get("upvoteCount") or 0,
        "category": (raw.get("category") or {}).get("name"),
        "author": _login(raw.get("author")),
        "upstream_labels": sorted(
            {str(n.get("name")) for n in label_nodes if n.get("name")}),
        "labels_complete": bool(labels_complete),
        "labels_total": labels_total,
        "proposed_labels": review_state(github, raw.get("number") or 0).get(
            "labels") or [],
        "comment_count": comments_total if comments_total is not None
        else len(comments),
        "comments": comments,
        "comments_complete": bool(comments_complete),
        "answer": ({
            "id": answer.get("id"),
            "database_id": answer.get("databaseId"),
            "author": _login(answer.get("author")),
            "body": answer.get("body"),
            "created_at": answer.get("createdAt"),
            "updated_at": answer.get("updatedAt"),
            "url": answer.get("url")} if answer else None),
        "answer_chosen_at": raw.get("answerChosenAt"),
        "fetched_at": utcnow(),
    }


def label_catalog() -> dict:
    """Full original label catalog (name -> chip text) for UI rendering."""
    out: dict[str, dict[str, str]] = {}
    for facet, pairs in _LABEL_CATALOG.items():
        out[facet] = {name: text for name, text in pairs}
    return out


def semantic_input(node: dict) -> dict:
    """Canonical reviewed content of a node.

    Only semantic fields participate in the review fingerprint:
    title, body, upstream labels, category, answer (with identity), and
    every comment and reply (id + text + updated timestamps +
    completeness counts and flags). Pagination cursors, fetched_at,
    cache age, evaluation IDs, local audit stamps, and votes/reactions
    are EXCLUDED, so an identical re-poll — or a vote change — keeps
    the same fingerprint and never spends model quota.
    """
    node = node or {}

    def _text(entry):
        return {
            "id": entry.get("id"),
            "author": entry.get("author"),
            "body": entry.get("body"),
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
        }

    return {
        "id": node.get("id"),
        "number": node.get("number"),
        "title": node.get("title"),
        "body": node.get("body"),
        "url": node.get("url"),
        "updated_at": node.get("updated_at"),
        "closed": bool(node.get("closed")),
        "category": node.get("category"),
        "author": node.get("author"),
        "upstream_labels": sorted(node.get("upstream_labels") or []),
        "labels_complete": node.get("labels_complete", True),
        "answer": (_text(node.get("answer") or {}) | {"is_answer": True}
                   if node.get("answer") else None),
        "comments": [_text(c) | {
            "is_answer": bool(c.get("is_answer")),
            "replies": [_text(r) for r in (c.get("replies") or [])],
            "reply_count": c.get("reply_count"),
            "replies_complete": c.get("replies_complete", True),
        } for c in (node.get("comments") or [])],
        "comment_count": node.get("comment_count"),
        "comments_complete": node.get("comments_complete", True),
    }


def semantic_fingerprint(node: dict) -> str:
    """Stable sha256 of the canonical semantic input. C1."""
    canonical = json.dumps(semantic_input(node), sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_rel(text) -> str | None:
    """Repo-relative path, or None when absolute/escaping/empty."""
    text = str(text or "")
    if (not text or text.startswith("/") or "/../" in f"/{text}/"
            or text in (".", "..")):
        return None
    return text


_SOURCE_FILE_CAP = 25
_SOURCE_BYTES_CAP = 512 * 1024


def _resolve_under(repo_root, rel: str):
    """Resolve rel under repo_root; None on escape/missing root.

    Symlinks are resolved BEFORE any read so a cited path can never
    escape the repo through a link.
    """
    try:
        root = Path(repo_root).resolve()
    except (OSError, ValueError):
        return None
    try:
        full = (root / rel).resolve()
    except (OSError, ValueError):
        return None
    try:
        full.relative_to(root)
    except ValueError:
        return None
    return full


def hash_source_files(repo_root, rel_paths) -> list[dict]:
    """Hash cited local source files (read-only).

    Paths must be relative and stay inside repo_root; anything else
    is recorded as rejected, never read. Missing/unreadable files are
    recorded with sha256 None so later checks see them as changed.
    """
    out: list[dict] = []
    for rel in list(rel_paths or [])[:_SOURCE_FILE_CAP]:
        text = _safe_rel(rel)
        full = _resolve_under(repo_root, text) if text else None
        if full is None:
            out.append({"path": str(rel or ""), "sha256": None,
                        "rejected": True})
            continue
        try:
            if full.stat().st_size > _SOURCE_BYTES_CAP:
                out.append({"path": text, "sha256": None,
                            "rejected": True, "reason": "too-large"})
                continue
            digest = hashlib.sha256(full.read_bytes()).hexdigest()
        except (OSError, ValueError):
            digest = None
        out.append({"path": text, "sha256": digest})
    return out


def _current_source_hashes(github: str, number,
                           repo_root) -> list[dict] | None:
    """Current on-disk hashes for the review's cited source paths.

    Returns None when the review cites no source evidence or the root
    is unavailable/unreadable (unknown, not fresh). Each entry carries
    the live sha (None when missing/unreadable) so the UI can show
    review-time hash vs CURRENT hash per file instead of a blanket
    "verified" claim.
    """
    state = review_state(github, number)
    evidence = state.get("source_evidence") or []
    if not evidence:
        return None
    if not (repo_root and Path(repo_root).is_dir()):
        return None
    try:
        hashed = hash_source_files(
            repo_root, [e.get("path") for e in evidence
                        if isinstance(e, dict)])
    except (Exception, SystemExit):
        return None
    return [{"path": e.get("path"), "sha256": e.get("sha256"),
             "reason": e.get("reason")}
            for e in hashed]


def source_changed(github: str, number, repo_root) -> bool | None:
    """Re-check cited source evidence against a repo root.

    Returns None when the review cites no source evidence (no code
    claims to verify) or the root is unavailable; True when any cited
    file changed, vanished, or was rejected; False when all match.
    """
    state = review_state(github, number)
    evidence = state.get("source_evidence") or []
    if not evidence:
        return None
    current = _current_source_hashes(github, number, repo_root)
    if current is None:
        return None
    live = {e.get("path"): e.get("sha256") for e in current}
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        if live.get(entry.get("path")) != entry.get("sha256"):
            return True
    return False


def _blank_review(github: str, number) -> dict:
    return {"schema": 1, "repo": github, "kind": "discussion-review",
            "number": number, "status": "unreviewed", "labels": [],
            "fingerprint": None, "history": []}


def review_state(github: str, number) -> dict:
    """Durable per-discussion review state (never auto-dropped).

    A corrupt review file is reported with status "error" —
    never shown as healthy "unreviewed"; its bytes stay in place
    until a later review overwrites them.
    """
    status, data = _read_json_safe(review_path(github, number))
    if status == "missing":
        return _blank_review(github, number)
    if status in ("corrupt", "shape"):
        blank = _blank_review(github, number)
        blank["status"] = "error"
        blank["_unreadable"] = True
        return blank
    if data.get("kind") != "discussion-review":
        return _blank_review(github, number)
    data.setdefault("history", [])
    return data


def _write_review(github: str, number, state: dict) -> dict:
    state["repo"] = github
    state["kind"] = "discussion-review"
    state["number"] = number
    state.setdefault("schema", 1)
    state.setdefault("history", [])
    recovered = _preserve_once(review_path(github, number))
    if recovered:
        state["recovered_from"] = recovered
    write_json(review_path(github, number), state)
    return state


def review_outcome(github: str, number, repo_root=None) -> dict:
    """Public review summary for UI: status, staleness, labels, draft.

    A stored "reviewed" verdict with missing/unreadable CURRENT input
    is reported as "unknown" (source unavailable), never as fresh —
    the old report and history are kept for the operator to inspect.
    When repo_root is given and the review cites local source evidence,
    the cited files are re-hashed; changed sources mark the review
    stale even if the discussion text is identical.
    """
    state = review_state(github, number)
    if state.get("_unreadable"):
        return {"status": "error", "stale": False,
                "detail_complete": False,
                "partial": False, "partial_reasons": [],
                "artifact": None, "rid": None,
                "label_facets": {},
                "source_unavailable": True,
                "source_changed": False, "source_checked": False,
                "error": "review record unreadable (kept in place)",
                "fingerprint": None, "current_fingerprint": None,
                "labels": [], "report": None, "evidence": [],
                "source_evidence": [],
                "reply_draft": None, "job": job_state(github, number),
                "history": []}
    nstatus, node = _read_json_safe(node_path(github, number))
    current = (semantic_fingerprint(node)
               if nstatus == "ok" and node.get("id") else None)
    stored = state.get("fingerprint")
    source_unavailable = current is None and bool(stored)
    stale = bool(current and stored and current != stored)
    if not stale and stored and nstatus == "ok" and node.get("id"):
        # An ordinary list scan never triggers a model review, but a
        # list row newer than the stored detail means the detail the
        # review certified is no longer current.
        item = _snapshot_item(github, number)
        listed = (item or {}).get("updated_at")
        if listed and listed != node.get("updated_at"):
            stale = True
    root_usable = bool(repo_root) and Path(repo_root).is_dir()
    src_changed = (source_changed(github, number, repo_root)
                   if root_usable else None)
    src_current = (_current_source_hashes(github, number, repo_root)
                   if root_usable else None)
    if src_changed:
        stale = True
    # A review that cites source evidence cannot be shown fresh when
    # its sources cannot be re-checked. No linked checkout (None) is
    # unavailable, not checked-fresh — same as a missing/unreadable
    # root.
    sources_opaque = not root_usable and bool(
        state.get("source_evidence"))
    status = state.get("status") or "unreviewed"
    if source_unavailable and status == "reviewed":
        status = "unknown"
    elif sources_opaque and status == "reviewed":
        status = "unknown"
    elif stale and status == "reviewed":
        status = "stale"
    detail_complete = True
    if nstatus == "ok" and node.get("id"):
        detail_complete = bool(node.get("comments_complete", True)
                              and node.get("labels_complete", True)
                              and all(c.get("replies_complete", True)
                                      for c in (node.get("comments")
                                                or [])))
    facets = state.get("label_facets")
    if not isinstance(facets, dict):
        facets = {}
    return {"status": status,
            "stale": stale,
            "detail_complete": detail_complete,
            "partial": bool(state.get("partial")),
            "partial_reasons": state.get("partial_reasons") or [],
            "artifact": state.get("artifact"),
            "rid": state.get("rid"),
            "label_facets": facets,
            "source_unavailable": source_unavailable or sources_opaque,
            "source_changed": bool(src_changed),
            "source_checked": src_changed is not None,
            "fingerprint": stored,
            "current_fingerprint": current,
            "labels": state.get("labels") or [],
            "report": state.get("report"),
            "evidence": state.get("evidence") or [],
            "source_evidence": state.get("source_evidence") or [],
            "source_cited": state.get("source_cited") or [],
            "source_scope": state.get("source_scope") or {},
            "source_current": src_current or [],
            "reply_draft": state.get("reply_draft"),
            "job": job_state(github, number),
            "history": state.get("history") or []}


_REVIEW_BY_OK = ("owner", "root")
_REVIEW_TIMEOUT_S = 600
_REVIEW_HISTORY_CAP = 20
# Bounded model steps for a review run (single JSON verdict, no tools).
_REVIEW_MAX_MODEL_STEPS = 5
_REVIEW_MODEL_FLAGS = ["--disable-write", "--disable-shell",
                       "--disable-web-tools",
                       "--max-model-steps", str(_REVIEW_MAX_MODEL_STEPS)]
_RAW_STREAM_CAP = 200000


def _classify_model_failure(exit_code: int, raw_stream
                              ) -> tuple[str, str]:
    """Machine kind + honest user-facing reason for a non-zero run.

    Returns (kind, message) with kind in quota/auth/billing/offline/
    timeout/failed. Each kind carries the CORRECT next action — a quota
    reset never asks for another sign-in, and the billing route never
    suggests buying an API key (this product runs on the subscription;
    the key env is scrubbed). Quota/auth/offline share no branch: a
    quota-only string and an auth-only string classify differently.
    Precedence on mixed signals: explicit 401/unauthorized (auth) beats
    quota words. Every branch notes the attempt IS saved (the
    per-attempt artifact keeps the raw stream) — never "nothing
    recorded" — and no branch retries automatically.
    """
    lowered = (raw_stream or "").lower()
    saved = "시도 기록은 보관됨 — 수동으로 다시 요청"
    if exit_code == 124:
        return ("timeout",
                f"모델 제한 시간 초과({_REVIEW_TIMEOUT_S}초); "
                f"{saved}")
    billing = ("billing", "payment", "api key", "api_key", "past due",
               "invoice", "insufficient credit")
    if any(key in lowered for key in billing):
        return ("billing",
                f"모델 계정 결제 문제(exit {exit_code}); 구독/결제 "
                f"상태를 확인한 뒤 수동으로 다시 요청 (API 키 구매가 "
                f"아님). {saved}")
    authish = ("unauthorized", " 401", " 403", "forbidden",
               "authentication", "auth failed", "not authenticated",
               "invalid token", "re-auth", "reauth")
    if any(key in lowered for key in authish):
        return ("auth",
                f"모델 인증 실패(exit {exit_code}); 다시 로그인한 뒤 "
                f"수동으로 다시 요청. {saved}")
    quota = ("quota", "rate limit", "ratelimit", " 429",
             "too many requests", "limit exceeded")
    if any(key in lowered for key in quota):
        return ("quota",
                f"모델 할당량 소진(quota/rate limit, exit {exit_code}); "
                f"한도 회복 후 수동으로 다시 요청 — 다시 로그인할 "
                f"필요 없음. {saved}")
    offline = ("network", "offline", "enotfound", "eai_again", "econn",
               "etimedout", "ehostunreach", "socket hang",
               "dns", "econnrefused")
    if any(key in lowered for key in offline):
        return ("offline",
                f"모델 연결 불가(network/offline, exit {exit_code}); "
                f"연결을 확인한 뒤 수동으로 다시 요청. {saved}")
    return ("failed",
            f"모델 실패(exit {exit_code}); 원시 출력 보관됨 — "
            f"수동으로 다시 요청. {saved}")


def job_state(github: str, number) -> dict:
    """Current review-job record: idle / running / done / error.

    Liveness is DERIVED on every poll, never trusted from the stored
    "running" value: a running record whose worker lease is no longer
    live-held (restart, crash, or an abandoned same-process thread
    whose finally already released the lease) reads as "interrupted"
    with manual retry required. No automatic model call ever follows
    from this derivation.
    """
    status, data = _read_json_safe(job_path(github, number))
    if status == "missing":
        return {"status": "idle"}
    if status in ("corrupt", "shape"):
        return {"status": "error",
                "error": "job record unreadable (kept in place)"}
    if not isinstance(data, dict) or data.get("kind") != "discussion-job":
        return {"status": "idle"}
    if data.get("status") == "running" and not _lease_live_for(
            data.get("lease_owner")):
        derived = dict(data)
        derived["status"] = "interrupted"
        derived["derived"] = True
        derived["error"] = (
            derived.get("error")
            or "previous review worker gone; manual retry required")
        return derived
    return data


def _write_job(github: str, number, record: dict) -> dict:
    record["repo"] = github
    record["kind"] = "discussion-job"
    record["number"] = _safe_number(number)
    record.setdefault("schema", 1)
    recovered = _preserve_once(job_path(github, number))
    if recovered:
        record["recovered_from"] = recovered
    write_json(job_path(github, number), record)
    return record


def _history_append(state: dict, entry: dict) -> None:
    history = state.setdefault("history", [])
    history.append({"at": utcnow(), **entry})
    del history[:max(0, len(history) - _REVIEW_HISTORY_CAP)]


def _scrubbed_env() -> dict:
    """Child env for the review model: subscription login only.

    META_API_KEY is dropped so a review can never spend paid API
    billing; the model runs under the app's own subscription login.
    """
    return {k: v for k, v in os.environ.items() if k != "META_API_KEY"}


_PROMPT_DISCUSSION_CAP = 60000
_PROMPT_SOURCE_EACH_CAP = 20000
_PROMPT_SOURCE_TOTAL_CAP = 60000


def _build_review_prompt(node: dict, sources: list[dict]) -> dict:
    """Build the review prompt with tracked truncation.

    Returns {"prompt", "input_snapshot", "truncated", "partial"}.
    input_snapshot holds the EXACT text sent to the model (never the
    full untruncated input), so a stored review certifies only what
    the model actually received. partial lists human-readable reasons
    (incomplete pagination, truncation) that mark the review partial.
    """
    # Byte units are real UTF-8 bytes everywhere (never character
    # counts labeled as bytes): caps slice encoded bytes and the
    # snapshot records actual sent/total bytes per file.
    canonical = semantic_input(node)
    full_brief = json.dumps(canonical, ensure_ascii=False, indent=1)
    full_brief_raw = full_brief.encode("utf-8")
    partial: list[str] = []
    if not canonical.get("comments_complete", True):
        partial.append("comments paginated: only the first page was sent")
    for comment in canonical.get("comments") or []:
        if not comment.get("replies_complete", True):
            partial.append("some replies paginated: first page only")
            break
    if not canonical.get("labels_complete", True):
        partial.append("labels paginated: first page only")
    truncated = False
    brief_raw = full_brief_raw[:_PROMPT_DISCUSSION_CAP]
    brief = brief_raw.decode("utf-8", errors="ignore")
    brief_bytes = len(brief.encode("utf-8"))
    if brief_bytes < len(full_brief_raw):
        truncated = True
        partial.append(f"discussion input truncated to "
                       f"{brief_bytes} of {len(full_brief_raw)} bytes")
    src_parts: list[str] = []
    sent_sources: list[dict] = []
    excluded_sources: list[dict] = []
    requested_paths = [e.get("path") for e in (sources or [])
                       if isinstance(e, dict)]
    budget = _PROMPT_SOURCE_TOTAL_CAP
    for entry in sources or []:
        content = entry.get("content") or ""
        total = len(content.encode("utf-8"))
        if budget <= 0 and total:
            # Never silently claim an unread file as reviewed input:
            # it is recorded as excluded (with the reason), never
            # added to the sent list, and a citation of it is
            # rejected by validate_report.
            excluded_sources.append({
                "path": entry.get("path"), "sha256": entry.get("sha256"),
                "bytes_total": total, "bytes_sent": 0,
                "reason": "source budget exhausted: file not sent",
            })
            truncated = True
            continue
        allowed = min(_PROMPT_SOURCE_EACH_CAP, budget, total)
        chunk_raw = content.encode("utf-8")[:allowed]
        # Only the cut tail can be incomplete; ignore drops at most
        # that partial character, never earlier content.
        chunk = chunk_raw.decode("utf-8", errors="ignore")
        sent_bytes = len(chunk.encode("utf-8"))
        budget -= sent_bytes
        cut = sent_bytes < total
        if cut:
            truncated = True
            bound = ("per-file cap" if total > _PROMPT_SOURCE_EACH_CAP
                     else "budget")
            partial.append(
                f"source {entry.get('path')}: {sent_bytes} of {total} "
                f"bytes sent ({bound})")
        sent_sources.append({
            "path": entry.get("path"), "sha256": entry.get("sha256"),
            "bytes_total": total, "bytes_sent": sent_bytes,
            "truncated": cut,
            # The exact bytes the model saw for this file, so the
            # in-app reviewer can inspect the sent context without
            # re-parsing the prompt text.
            "excerpt": chunk,
        })
        src_parts.append(
            f"\n--- source: {entry.get('path')} "
            f"(sha256 {entry.get('sha256')}) ---\n{chunk}\n")
        if budget <= 0:
            rest = (sources or [])[len(sent_sources)
                                   + len(excluded_sources):]
            for leftover in rest:
                if not isinstance(leftover, dict):
                    continue
                rest_total = len((leftover.get("content") or "")
                                 .encode("utf-8"))
                if not rest_total:
                    continue
                excluded_sources.append({
                    "path": leftover.get("path"),
                    "sha256": leftover.get("sha256"),
                    "bytes_total": rest_total,
                    "bytes_sent": 0,
                    "reason": "source budget exhausted: file not sent",
                })
            if any(e.get("bytes_total") for e in excluded_sources):
                partial.append("source budget exhausted: later files cut")
            break
    if excluded_sources and not any("later files cut" in p
                                     for p in partial):
        # Exclusions recorded through the skip path (no break ran):
        # the review is still honestly marked partial.
        partial.append("source budget exhausted: later files cut")
    src_text = "".join(src_parts)
    # The exact structural urls validate_report will accept, computed
    # by the same _known_urls helper so the prompt list and the
    # validator can never drift apart. Recorded additively for
    # per-attempt provenance; never part of the semantic fingerprint.
    permitted_urls = sorted(_known_urls(node))
    input_snapshot = {
        "discussion": brief,
        "discussion_bytes_total": len(full_brief_raw),
        "discussion_bytes_sent": brief_bytes,
        # ONLY the files whose bytes reached the model. Citations are
        # validated against this list, never against every requested
        # or gathered file.
        "sources": sent_sources,
        "sources_requested": requested_paths,
        "sources_excluded": excluded_sources,
        "permitted_evidence_urls": permitted_urls,
    }
    example = {"summary": "one paragraph",
               "findings": ["one finding"],
               "labels": {"status": ["needs-review"],
                          "platform": ["android"], "type": ["feature"]},
               "evidence": [{"url": node.get("url"),
                             "note": "why it matters"}],
               "reply_draft": None, "source_files": []}
    prompt = (
        "You are doing a READ-ONLY triage review of one GitHub "
        "Discussion for the maintainers of this repository. You cannot "
        "post, comment, label, or change anything. Output JSON ONLY with "
        "exactly these keys: summary (one paragraph), findings (list of "
        "short non-empty strings, at least one), labels (object with any "
        "of status/platform/type; EVERY facet value is an ARRAY of "
        "strings — a bare string is malformed and will be rejected. "
        "status may only contain needs-review or needs-action "
        "(for example [\"needs-review\"]); you must never set reviewed, "
        "the app owns that; platform values are any of android, ios, "
        "web, unknown (for example [\"android\"]); type values are any "
        "of feature, question, bug, discussion, docs, unknown "
        "(for example [\"feature\"])), evidence "
        "(list of objects with url and a non-empty note, using ONLY "
        "urls copied verbatim from the PERMITTED EVIDENCE URLS list "
        "below — that list is complete; a url that merely appears "
        "elsewhere in the input text is not permitted), reply_draft (a short "
        "public reply draft, or null when no reply is warranted), "
        "source_files (list of repo-relative paths you actually relied "
        "on, from the sources section only; empty list when none). "
        "Hard limits (anything beyond is rejected): summary at most "
        "2000 characters; findings non-empty, at most 20 items of at "
        "most 200 characters each; evidence notes non-empty, at most "
        "500 characters each; reply_draft null or a non-empty string "
        "of at most 2000 characters.\n\n"
        "A complete valid example (arrays required, copy the shape; "
        "the url below is this discussion's own url, copied "
        "verbatim):\n"
        f"{json.dumps(example, ensure_ascii=False, indent=1)}\n\n"
        "Honesty rules: quote the exact discussion text; never claim a "
        "runtime verification you were not given; if the app mapping for "
        "a platform exists but runtime behavior is unverified, say so "
        "explicitly; never invent citations — every evidence url must "
        "be copied verbatim from the PERMITTED EVIDENCE URLS list "
        "below, not merely from anywhere in the input text. A link "
        "mentioned inside a discussion or comment body that is NOT in "
        "that list (for example a link a commenter pasted) was not "
        "opened or read: you may discuss it as a mentioned-but-unread "
        "link, never cite it as evidence. A listed structural url "
        "that also appears in body text stays eligible. When the "
        "input notes truncation or "
        "paginated pages, say the review is partial.\n\n"
        "PERMITTED EVIDENCE URLS (complete — copy verbatim; these are "
        "the discussion, comment, reply, and answer urls supplied as "
        "structural locators. Membership alone proves no claim and "
        "shows no unseen content: every claim must be supported by "
        "the discussion/source excerpts in this input, honoring any "
        "stated partial limits):\n"
        f"{json.dumps(permitted_urls, ensure_ascii=False, indent=1)}\n\n"
        "DISCUSSION (canonical reviewed input):\n"
        f"{brief}\n\n"
        "LOCAL SOURCES (read-only snapshots gathered by the app):\n"
        f"{src_text or '(none cited)'}\n"
    )
    return {"prompt": prompt, "input_snapshot": input_snapshot,
            "truncated": truncated, "partial": partial}


def _known_urls(node: dict) -> set[str]:
    urls = {node.get("url")}
    for comment in node.get("comments") or []:
        urls.add(comment.get("url"))
        for reply in comment.get("replies") or []:
            urls.add(reply.get("url"))
    answer = node.get("answer") or {}
    urls.add(answer.get("url"))
    return {u for u in urls if u}


def validate_report(node: dict, report, sent_sources: list[dict],
                    repo_root=None) -> list[str]:
    """Check a model report against the evidence it was given.

    sent_sources is the ACTUALLY SENT context (the input snapshot's
    "sources" list: path/sha256/bytes_sent per file) — never every
    requested or gathered file. A citation of a file whose bytes never
    reached the model (budget-excluded, unread, unrequested) is
    rejected as "not sent to the model", so a guessed unsent file can
    never become reviewed source proof.

    Returns a list of rejection reasons (empty when valid). Rejects
    invented citations: every evidence url must appear verbatim in the
    input node, and every source_file must be a sent path that exists
    under repo_root (or no repo_root was given, in which case any
    source_file claim is invented).
    """
    problems: list[str] = []
    if not isinstance(report, dict):
        return ["report is not a JSON object"]
    for key in ("summary", "findings", "labels", "evidence",
                "reply_draft", "source_files"):
        if key not in report:
            problems.append(f"report missing key: {key}")
    if problems:
        return problems
    summary = report.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("report summary must be a non-empty string")
    elif len(summary) > 2000:
        problems.append("report summary exceeds 2000 chars")
    findings = report.get("findings")
    if not isinstance(findings, list) or not findings:
        problems.append("report findings must be a non-empty list")
    else:
        if len(findings) > 20:
            problems.append("report findings exceed 20 items")
        for finding in findings:
            if not isinstance(finding, str) or not finding.strip():
                problems.append("report finding must be a non-empty string")
                break
            if len(finding) > 200:
                problems.append("report finding exceeds 200 chars")
                break
    draft = report.get("reply_draft")
    if draft is not None and (
            not isinstance(draft, str) or not draft.strip()
            or len(draft) > 2000):
        problems.append("report reply_draft must be null or a "
                        "non-empty string up to 2000 chars")
    # Per-facet enums. The lifecycle status (reviewed/stale-review) is
    # owned by the APP, never self-certified by the model: a model may
    # only suggest needs-review or needs-action.
    facet_enums = {
        "status": {"needs-review", "needs-action"},
        "platform": {"android", "ios", "web", "unknown"},
        "type": {"feature", "question", "bug", "discussion",
                 "docs", "unknown"},
    }
    # The raw labels type is validated FIRST: a falsy non-object
    # (null, [], "", 0) must not coerce into a valid empty object.
    # A missing key or {} stays legal (unclassified, quiet 미분류).
    raw_labels = report.get("labels")
    if "labels" in report and not isinstance(raw_labels, dict):
        problems.append("report labels is not an object")
        labels = {}
    else:
        labels = raw_labels or {}
    for facet, names in labels.items():
        if not isinstance(facet, str) or facet not in facet_enums:
            problems.append(f"unknown label facet: {facet!r}"[:120])
            continue
        if not isinstance(names, list):
            problems.append(f"report labels.{facet} is not a list")
            continue
        for name in names:
            if not isinstance(name, str) or (
                    name not in facet_enums[facet]):
                problems.append(
                    f"rejected labels.{facet} value: {name!r}"[:160])
    if problems:
        return problems
    evidence = report.get("evidence")
    if not isinstance(evidence, list):
        return ["report evidence must be a list"]
    sources_list = report.get("source_files")
    if not isinstance(sources_list, list):
        return ["report source_files must be a list"]
    known = _known_urls(node)
    for entry in evidence:
        if not isinstance(entry, dict):
            problems.append("report evidence entry must be an object")
            continue
        url = entry.get("url")
        if not isinstance(url, str) or url not in known:
            problems.append(f"invented evidence url: {url!r}"[:200])
            continue
        note = entry.get("note")
        if not isinstance(note, str) or not note.strip():
            problems.append(f"evidence entry missing note: {url}")
        elif len(note) > 500:
            problems.append(f"evidence note exceeds 500 chars: {url}")
    sent = {e.get("path") for e in (sent_sources or [])
            if isinstance(e, dict) and isinstance(e.get("path"), str)}
    sent_bytes = {e.get("path"): e.get("bytes_sent", 0)
                  for e in (sent_sources or []) if isinstance(e, dict)}
    for path in sources_list:
        if not isinstance(path, str) or _safe_rel(path) is None:
            problems.append(f"rejected source file: {path!r}"[:160])
        elif path not in sent:
            problems.append(f"source file not sent to the model: {path}; "
                            "cite only files listed in the sent context")
        elif not sent_bytes.get(path):
            problems.append(f"source file sent empty (0 bytes): {path}; "
                            "no bytes reached the model")
        elif repo_root is not None and not (
                _resolve_under(repo_root, path) or Path()).is_file():
            problems.append(f"cited source file missing: {path}")
        elif repo_root is None:
            problems.append(f"unverifiable source file: {path}")
    return problems


# Prefixes of validate_report problems that genuinely mean a fabricated
# or unverifiable citation. Anything else (wrong JSON shape, missing
# keys, scalar labels, bad enums, empty strings) is a schema problem:
# malformed output, not an invented citation.
_CITATION_PROBLEM_PREFIXES = (
    "invented evidence url",
    "source file not sent to the model",
    "source file sent empty",
    "cited source file missing",
    "unverifiable source file",
    "rejected source file",
)


def _split_report_problems(problems: list[str]):
    """Split validator output into (schema, citation) problems."""
    schema, citation = [], []
    for problem in problems or []:
        if problem.startswith(_CITATION_PROBLEM_PREFIXES):
            citation.append(problem)
        else:
            schema.append(problem)
    return schema, citation


def _extract_model_text(raw_stream: str):
    """Pull the model text out of a `muse exec --json` JSONL stream.

    Prefers the last run_terminal payload; falls back to concatenated
    run_output_delta chunks. Returns (text, kind) where kind is
    "terminal", "delta" or "none". Uses the shared parse_delta_line
    contract, never a bespoke event shape.
    """
    from . import muse as muse_exec
    deltas: list[str] = []
    terminal = None
    for line in (raw_stream or "").splitlines():
        kind, text = muse_exec.parse_delta_line(line)
        if kind == "terminal":
            terminal = text
        elif kind == "delta":
            deltas.append(text)
    if terminal:
        return terminal, "terminal"
    if deltas:
        return "".join(deltas), "delta"
    return "", "none"


def _extract_report_json(output: str):
    """Parse the model's JSON report; return (report, error)."""
    text = (output or "").strip()
    if not text:
        return None, "empty model output"
    try:
        return json.loads(text), None
    except ValueError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1]), None
        except ValueError:
            pass
    return None, "model output is not JSON"


def _lease_live_for(owner_token) -> bool:
    """True when the global worker lease is live-held by owner_token."""
    from . import sched
    try:
        status = sched.global_lock_status() or {}
    except (Exception, SystemExit):
        return False
    return bool(status.get("live")) and status.get("owner") == owner_token


def _read_job_record(github: str, number) -> dict | None:
    """Raw stored job record without liveness derivation."""
    status, data = _read_json_safe(job_path(github, number))
    if (status == "ok" and isinstance(data, dict)
            and data.get("kind") == "discussion-job"):
        return data
    return None


def start_review(github: str, number, by: str = "owner", repo_root=None,
                 evidence_paths=None) -> dict:
    """Queue one app-owned review job for a discussion.

    An explicit review action always refreshes the discussion detail
    (model-free) first, so upstream edits can never no-op into an
    already-reviewed verdict. A failed refresh is an explicit error
    (the cached input may be stale, and a cached "already reviewed"
    after an auth failure would lie) — the job never silently reviews
    old input. At most _SOURCE_FILE_CAP evidence paths are accepted;
    more is a 400 with nothing started, so no requested file can be
    silently discarded before the prompt records it.
    The M2 driver never calls this (only the UI route does, as
    owner/root). Returns {"started": bool, ...}; raises DiscussionError
    with http_status for bad caller, unknown discussion, or a busy
    worker (409).
    """
    from . import sched
    if by not in _REVIEW_BY_OK:
        raise DiscussionError(f"review by must be owner/root: {by!r}",
                              status=400)
    want = _safe_number(number)
    _slug_dir(github)  # validate the typed identity before anything else
    raw_running = _read_job_record(github, want)
    if raw_running is not None and raw_running.get("status") == "running":
        if _lease_live_for(raw_running.get("lease_owner")):
            return {"started": False, "reason": "review already running",
                    "job": job_state(github, want)}
        # The previous worker is gone (restart/crash): mark the
        # interruption truthfully, then allow a manual retry. No
        # automatic model spend happens here.
        state = review_state(github, want)
        _history_append(state, {
            "event": "job-interrupted",
            "fingerprint": raw_running.get("fingerprint"),
            "rid": raw_running.get("rid"),
            "detail": "previous review worker gone at restart; "
                      "manual retry required",
        })
        _write_review(github, want, state)
        _write_job(github, want, {
            "status": "interrupted",
            "error": "previous review worker gone at restart; "
                     "manual retry required",
            "fingerprint": raw_running.get("fingerprint"),
            "rid": raw_running.get("rid"),
            "finished_at": utcnow(), "usage": None,
        })
    # An explicit review always works from a fresh fetch. A failed
    # refresh is an explicit error (the cached input may be stale, and
    # a cached "already reviewed" after an auth failure would lie) —
    # the job never silently reviews old input.
    try:
        node = fetch_node(github, want)
    except DiscussionError as exc:
        raise DiscussionError(
            f"refresh failed; not reviewing possibly-stale cached "
            f"input: {exc}",
            status=exc.http_status) from exc
    refresh_error = None
    fingerprint = semantic_fingerprint(node)
    # Evidence context is review input too: changing, adding, or
    # removing cited sources is a NEW input, never a dedup hit. The
    # requested paths are hashed here (exact bytes, resolved scope);
    # anything unverifiable fails closed instead of silently
    # reviewing a different context.
    for rel in (evidence_paths or []):
        if not isinstance(rel, str) or _safe_rel(rel) is None:
            raise DiscussionError(f"rejected evidence path: {rel!r}"[:160],
                                  status=400)
    requested_paths = [p for p in (evidence_paths or [])
                       if isinstance(p, str)]
    if len(requested_paths) > _SOURCE_FILE_CAP:
        raise DiscussionError(
            f"too many evidence files ({len(requested_paths)} > "
            f"{_SOURCE_FILE_CAP}); narrow the selection — nothing "
            "started, so no requested file is silently discarded",
            status=400)
    if requested_paths and not (
            repo_root and Path(repo_root).is_dir()):
        raise DiscussionError(
            "cannot verify requested source context without a linked "
            "checkout; link one before reviewing with evidence",
            status=409)
    requested_evidence = (
        hash_source_files(repo_root, requested_paths)
        if requested_paths else [])
    bad_context = [e for e in requested_evidence
                   if e.get("sha256") is None]
    if bad_context:
        first = bad_context[0]
        raise DiscussionError(
            f"evidence unavailable: {first.get('path')} "
            f"({first.get('reason', 'missing/unreadable')}); "
            "not reviewing a reduced context silently",
            status=400)
    state = review_state(github, want)
    stored_sent = [
        (e.get("path"), e.get("sha256"))
        for e in (state.get("source_evidence") or [])
        if isinstance(e, dict)]
    stored_requested = [
        (e.get("path"), e.get("sha256"))
        for e in (state.get("source_requested") or [])
        if isinstance(e, dict)]
    requested_pairs = sorted(
        (e.get("path"), e.get("sha256")) for e in requested_evidence)
    if stored_requested or requested_pairs:
        # Compare against the ORIGINAL requested identity (every
        # requested path+hash, sent or excluded): an unchanged partial
        # request dedups instead of spending another model call, while
        # any changed/added/removed file is new input. An excluded
        # file is never called reviewed or cited by this path.
        same_request = (sorted(stored_requested) == requested_pairs)
    else:
        # Legacy record without a requested identity: only an exact
        # match against the stored sent proof may dedup; anything
        # wider starts a new attempt rather than risk a false fresh.
        same_request = (sorted(stored_sent) == requested_pairs)
    context_same = (same_request
                    and not source_changed(github, want, repo_root))
    if (state.get("status") == "reviewed"
            and state.get("fingerprint") == fingerprint
            and context_same):
        return {"started": False, "reason": "already reviewed (fresh)",
                "fingerprint": fingerprint}
    stamp = re.sub(r"[^0-9A-Za-z-]", "", utcnow())
    rid = f"{fingerprint[:12]}-{stamp}"
    owner = f"discussion-review:{github}#{want}"
    token = sched.acquire_worker_lease(owner)
    if token is None:
        try:
            busy = (sched.global_lock_status() or {}).get("owner")
        except (Exception, SystemExit):
            busy = None
        raise DiscussionError(
            f"worker busy ({busy or 'another worker'}); nothing started",
            status=409)
    try:
        job = _write_job(github, want, {
            "status": "running", "by": by, "fingerprint": fingerprint,
            "rid": rid, "lease_owner": token, "pid": os.getpid(),
            "refreshed": refresh_error is None,
            "refresh_error": refresh_error,
            "started_at": utcnow(), "repo_root": (str(repo_root)
                                                  if repo_root else None),
            "evidence_paths": [p for p in (evidence_paths or [])
                               if _safe_rel(p)],
        })
    except (Exception, SystemExit) as exc:
        # Every post-acquire failure releases the lease: a job record
        # that cannot be persisted must never pin the worker.
        try:
            sched.release_worker_lease(token)
        except (Exception, SystemExit):
            pass
        raise DiscussionError(
            f"review job record unwritable; nothing started: {exc}"
        ) from exc

    def _release():
        try:
            sched._stop_heartbeat(token)
        except (Exception, SystemExit):
            pass
        sched.release_worker_lease(token)

    def _run():
        try:
            try:
                _run_review(github, want, fingerprint, job, repo_root, rid)
            except Exception as exc:  # noqa: BLE001 - record, never raise
                try:
                    _finish_job(
                        github, want, "error",
                        f"review worker failed: {exc}", fingerprint,
                        None, {"exit_code": None}, None,
                        rid=rid, input_snapshot=None)
                except (Exception, SystemExit):
                    pass
        finally:
            _release()

    thread = threading.Thread(target=_run, daemon=True,
                              name=f"discussion-review-{want}")
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001 - lease must not leak
        _release()
        _write_job(github, want, {
            "status": "error",
            "error": f"review worker failed to start: {exc}",
            "fingerprint": fingerprint, "rid": rid,
            "finished_at": utcnow(), "usage": None,
        })
        raise DiscussionError(
            f"review worker failed to start: {exc}") from exc
    # Renew the operator-visible heartbeat while the bounded model
    # run executes (no-op when this process does not hold the lease).
    # _release stops it on every path, and the lease is idle after
    # the job finishes — the cached poll then derives interruption
    # from the actual lease/PID/generation state.
    try:
        sched._start_heartbeat(token)
    except (Exception, SystemExit):
        pass
    sched.disown_worker_lease(token)
    return {"started": True, "job": job_state(github, want)}


def _gather_sources(repo_root, rel_paths) -> list[dict]:
    """Read-only local source snapshots for the review prompt.

    Keeps the FULL file bytes (already capped at _SOURCE_BYTES_CAP
    by hash_source_files); truncation happens ONLY in
    _build_review_prompt, which records bytes_sent/truncated per
    file — so the artifact certifies exactly what the model saw.
    """
    if not repo_root:
        return []
    hashed = hash_source_files(repo_root, rel_paths)
    out = []
    for entry in hashed:
        if entry.get("sha256") is None:
            continue
        full = _resolve_under(repo_root, entry["path"])
        if full is None:
            continue
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        out.append({**entry, "content": content,
                    "bytes_total": len(content)})
    return out


def _run_review(github: str, number, fingerprint: str, job: dict,
                repo_root=None, rid: str = "") -> None:
    """Worker-thread body: one bounded model review, then persist."""
    from . import muse as muse_exec
    sources = _gather_sources(
        repo_root or job.get("repo_root"), job.get("evidence_paths"))
    nstatus, node = _read_json_safe(node_path(github, number))
    if nstatus != "ok" or not node.get("id"):
        # The refresh-time fetch was mocked or the cache was cleared
        # after queueing: heal with one model-free fetch rather than
        # failing a job the operator explicitly started.
        try:
            node = fetch_node(github, number)
        except DiscussionError as exc:
            _finish_job(
                github, number, "error",
                "discussion input vanished before the review ran "
                f"({exc})",
                fingerprint, None, None, None, rid=rid or job_rid(job))
            return
    built = _build_review_prompt(node, sources)
    prompt_text = built["prompt"]
    env = _scrubbed_env()
    try:
        exit_code, raw_stream, usage = muse_exec.exec_prompt(
            prompt_text, workdir=None, contributor=True,
            timeout=_REVIEW_TIMEOUT_S, env=env,
            extra=_REVIEW_MODEL_FLAGS)
    except Exception as exc:  # noqa: BLE001 - record, never raise
        _finish_job(github, number, "error",
                    f"review execution failed: {exc}", fingerprint,
                    None, None, None, rid=rid or job_rid(job),
                    input_snapshot=built["input_snapshot"],
                    prompt=prompt_text, raw_output="",
                    raw_output_meta={"total": 0, "sent": 0,
                                     "capped": False},
                    partial=built["partial"])
        return
    raw_total = len(raw_stream or "")
    raw_capped = (raw_stream or "")[:_RAW_STREAM_CAP]
    stream_meta = {"total": raw_total, "sent": len(raw_capped),
                   "capped": raw_total > len(raw_capped)}
    if exit_code != 0:
        kind, reason = _classify_model_failure(exit_code, raw_stream)
        _finish_job(
            github, number, "error", reason, fingerprint,
            None, {"exit_code": exit_code, "usage": usage}, None,
            rid=rid or job_rid(job),
            input_snapshot=built["input_snapshot"],
            prompt=prompt_text, raw_output=raw_capped,
            raw_output_meta=stream_meta,
            partial=built["partial"], error_kind=kind)
        return
    model_text, stream_kind = _extract_model_text(raw_stream)
    if stream_kind == "none" or not model_text.strip():
        _finish_job(github, number, "error",
                    "model produced no output text "
                    "(no terminal/delta in stream); no review recorded; "
                    "attempt saved",
                    fingerprint, None,
                    {"exit_code": exit_code, "usage": usage}, None,
                    rid=rid or job_rid(job),
                    input_snapshot=built["input_snapshot"],
                    prompt=prompt_text, raw_output=raw_capped,
                    raw_output_meta=stream_meta,
                    partial=built["partial"])
        return
    report, parse_error = _extract_report_json(model_text)
    if parse_error:
        _finish_job(github, number, "error",
                    f"unparseable model output ({stream_kind}): "
                    f"{parse_error}; no review recorded; attempt saved",
                    fingerprint, None,
                    {"exit_code": exit_code, "usage": usage}, None,
                    rid=rid or job_rid(job),
                    input_snapshot=built["input_snapshot"],
                    prompt=prompt_text, raw_output=raw_capped,
                    raw_output_meta=stream_meta,
                    partial=built["partial"])
        return
    # Citations are checked against what the model ACTUALLY received
    # (the snapshot's sent list), never the full requested set.
    problems = validate_report(node, report,
                               built["input_snapshot"]["sources"],
                               repo_root)
    if problems:
        # A schema problem (wrong shape, scalar labels, bad enum) is
        # malformed output — never an invented citation. Only genuine
        # fabrication (an unsent/unverifiable source or an unknown url)
        # keeps the invented-citation wording.
        schema, citation = _split_report_problems(problems)
        if schema:
            message = ("model output failed validation: "
                       + "; ".join(problems[:5]))
            kind = "invalid-report"
        else:
            message = ("rejected invented citations: "
                       + "; ".join(citation[:5]))
            kind = None
        _finish_job(github, number, "error", message,
                    fingerprint, None,
                    {"exit_code": exit_code, "usage": usage}, report,
                    rid=rid or job_rid(job),
                    input_snapshot=built["input_snapshot"],
                    prompt=prompt_text, raw_output=raw_capped,
                    raw_output_meta=stream_meta,
                    partial=built["partial"], error_kind=kind)
        return
    # Update-during-model race: re-fetch the CURRENT discussion AND
    # re-hash the cited sources. Anything unverifiable or changed means
    # the output was reviewed against stale input — keep it as history,
    # never as the latest verified review.
    effective_root = repo_root or job.get("repo_root")
    try:
        fresh = fetch_node(github, number)
    except DiscussionError as exc:
        _finish_job(github, number, "error",
                    "could not re-verify current input after model run "
                    f"({exc}); output kept as history only, nothing promoted",
                    fingerprint, None,
                    {"exit_code": exit_code, "usage": usage}, report,
                    rid=rid or job_rid(job),
                    input_snapshot=built["input_snapshot"],
                    prompt=prompt_text, raw_output=raw_capped,
                    partial=built["partial"])
        return
    current_fp = semantic_fingerprint(fresh)
    if current_fp != fingerprint:
        _finish_job(github, number, "stale-input",
                    "discussion changed while the model ran; output kept "
                    "as history only, current review left untouched",
                    fingerprint, None,
                    {"exit_code": exit_code, "usage": usage}, report,
                    rid=rid or job_rid(job),
                    input_snapshot=built["input_snapshot"],
                    prompt=prompt_text, raw_output=raw_capped,
                    partial=built["partial"])
        return
    if effective_root:
        rehashed = {e.get("path"): e.get("sha256")
                    for e in hash_source_files(
                        effective_root, job.get("evidence_paths") or [])}
        cited_now = {e.get("path"): e.get("sha256") for e in sources}
        if rehashed != cited_now:
            _finish_job(github, number, "stale-input",
                        "cited local sources changed while the model ran; "
                        "output kept as history only, current review left "
                        "untouched",
                        fingerprint, None,
                        {"exit_code": exit_code, "usage": usage}, report,
                        rid=rid or job_rid(job),
                        input_snapshot=built["input_snapshot"],
                        prompt=prompt_text, raw_output=raw_capped,
                        raw_output_meta=stream_meta,
                        partial=built["partial"])
            return
    # Review-time source proof covers ONLY what the model received.
    # source_cited is the validated intersection (report claims that
    # survived the sent-context check); the scope block lets the UI
    # show requested vs sent vs excluded vs cited honestly.
    sent = built["input_snapshot"]["sources"]
    sent_paths = [e.get("path") for e in sent
                  if isinstance(e, dict)]
    claimed = report.get("source_files") or []
    cited = sorted({p for p in claimed if p in sent_paths})
    # Immutable requested identity (path+hash for EVERY requested
    # file, sent or excluded): the dedup compares a new request
    # against what was originally asked, not just what was sent.
    # Order follows the original request.
    excluded = built["input_snapshot"]["sources_excluded"]
    requested_identity = [
        {"path": e.get("path"), "sha256": e.get("sha256")}
        for e in list(sent) + list(excluded or [])
        if isinstance(e, dict) and isinstance(e.get("path"), str)]
    src_evidence = [{"path": e.get("path"), "sha256": e.get("sha256"),
                     "bytes_sent": e.get("bytes_sent", 0),
                     "bytes_total": e.get("bytes_total", 0),
                     "truncated": bool(e.get("truncated"))}
                    for e in sent if isinstance(e, dict)]
    scope = {"requested": built["input_snapshot"].get(
        "sources_requested", []),
        "sent": sent_paths,
        "excluded": built["input_snapshot"].get("sources_excluded",
                                                []),
        "cited": cited}
    _finish_job(github, number, "done", None, fingerprint, report,
                {"exit_code": exit_code, "usage": usage}, report,
                source_evidence=src_evidence,
                source_cited=cited, source_scope=scope,
                source_requested=requested_identity,
                rid=rid or job_rid(job),
                input_snapshot=built["input_snapshot"],
                prompt=prompt_text, raw_output=raw_capped,
                raw_output_meta=stream_meta,
                partial=built["partial"])


def job_rid(job: dict) -> str:
    """Review ID carried by a job record (stable per attempt)."""
    rid = (job or {}).get("rid")
    return rid if isinstance(rid, str) and rid else "no-rid"


def _artifact_path(github: str, number, rid: str) -> Path:
    """Append-only per-attempt path; never overwrites a previous attempt.

    On rid collision (same timestamp), suffixes -2, -3, ... up to a
    bound, so error/stale attempts keep full payloads too.
    """
    safe = re.sub(r"[^0-9A-Za-z-]+", "", rid or "no-rid") or "no-rid"
    base = f"review-{_safe_number(number)}-{safe}"
    candidate = _slug_dir(github) / f"{base}.json"
    for suffix in range(2, 100):
        if not candidate.exists():
            return candidate
        candidate = _slug_dir(github) / f"{base}-{suffix}.json"
    # Namespace exhausted: fail closed. Returning the existing
    # last name would silently overwrite a previous attempt's full
    # payload on the next write.
    raise DiscussionError(
        f"artifact namespace exhausted for review #{number} rid {safe!r}; "
        "refusing to overwrite a previous attempt")


def _finish_job(github: str, number, outcome: str, error: str | None,
                fingerprint: str, report, usage, raw_report,
                source_evidence=None, source_cited=None,
                source_scope=None, source_requested=None, rid: str = "",
                input_snapshot=None, partial=None,
                prompt: str | None = None,
                raw_output: str | None = None,
                raw_output_meta: dict | None = None,
                error_kind: str | None = None) -> bool:
    """Persist a finished review job; promote valid reports to review.

    Every attempt writes an append-only per-review artifact holding
    the EXACT prompt text sent to the model, the raw JSONL stream, the
    parsed report, and the receipt — older review payloads are never
    overwritten, and the UI history window only reads the last N
    entries. Returns True when the artifact was saved; when the save
    fails the review is NOT promoted (error outcome instead).
    """
    rid = rid or "no-rid"
    partial = list(partial or [])
    state = review_state(github, number)
    entry: dict = {"event": f"job-{outcome}", "fingerprint": fingerprint,
                   "rid": rid}
    if error:
        entry["error"] = error
    artifact = {
        "schema": 1, "repo": github, "kind": "discussion-review-attempt",
        "number": _safe_number(number), "rid": rid, "outcome": outcome,
        "fingerprint": fingerprint, "at": utcnow(),
        "prompt": prompt,
        "input_snapshot": input_snapshot,
        "partial": partial,
        "raw_output": raw_output,
        "raw_output_meta": raw_output_meta or {"total": None,
                                               "sent": None,
                                               "capped": None},
        "report": report if isinstance(report, dict) else None,
        "raw_report_keys": (sorted(raw_report.keys())
                            if isinstance(raw_report, dict) else None),
        "error": error,
        "usage": usage,
        "source_evidence": source_evidence or [],
    }
    artifact_ok = False
    try:
        target = _artifact_path(github, number, rid)
        write_json(target, artifact)
        entry["artifact"] = target.name
        artifact_ok = True
    except (Exception, SystemExit):
        entry["artifact_error"] = "artifact write failed"
    if outcome == "done" and not artifact_ok:
        outcome = "error"
        error = ((error + "; " if error else "") +
                 "artifact save failed; review not promoted")
        entry["event"] = "job-error"
        entry["error"] = error
    if outcome == "done" and isinstance(report, dict):
        labels = report.get("labels") or {}
        flat = sorted({n for names in labels.values()
                       if isinstance(names, list)
                       for n in names if isinstance(n, str)})
        facets = {facet: sorted({n for n in (names or [])
                                 if isinstance(n, str)})
                  for facet, names in labels.items()
                  if isinstance(facet, str) and isinstance(names, list)}
        state.update({
            "status": "reviewed",
            "fingerprint": fingerprint,
            "partial": bool(partial),
            "partial_reasons": partial,
            "rid": rid,
            "artifact": entry.get("artifact"),
            "report": {"summary": report.get("summary"),
                       "findings": report.get("findings")},
            "labels": flat,
            "label_facets": facets,
            "evidence": report.get("evidence") or [],
            "reply_draft": report.get("reply_draft"),
            "source_evidence": source_evidence or [],
            "source_cited": source_cited or [],
            "source_scope": source_scope or {},
            "source_requested": source_requested or [],
            "reviewed_at": utcnow(),
            "input_bytes_sent": ((input_snapshot or {}).get(
                "discussion_bytes_sent")),
            "previous": {
                "status": state.get("status"),
                "fingerprint": state.get("fingerprint"),
            } if state.get("status") not in (None, "unreviewed") else None,
        })
        # Keep the stored node file's proposed labels in sync for list
        # rendering without re-normalizing the whole node.
        try:
            nstatus, node = _read_json_safe(node_path(github, number))
            if nstatus == "ok" and node.get("id"):
                node["proposed_labels"] = flat
                write_json(node_path(github, number), node)
        except (Exception, SystemExit):
            pass
        entry["labels"] = flat
    else:
        state.update({"status": ("error" if outcome == "error"
                                 else state.get("status") or "unreviewed")})
    _history_append(state, entry)
    _write_review(github, number, state)
    # The audit actor (owner/root from the explicit action) survives
    # finalization: carry it from the running record so the UI can show
    # it in the secondary record instead of the primary flow. The
    # machine error kind (quota/auth/billing/offline/timeout/failed)
    # stays on the job record next to the human message.
    prev_status, prev_job = _read_json_safe(job_path(github, number))
    prev_by = (prev_job.get("by") if prev_status == "ok"
               and isinstance(prev_job, dict) else None)
    job_record = {
        "status": outcome, "error": error, "fingerprint": fingerprint,
        "rid": rid, "artifact": entry.get("artifact"),
        "by": prev_by, "finished_at": utcnow(), "usage": usage,
    }
    if outcome == "error" and error_kind:
        job_record["error_kind"] = error_kind
    _write_job(github, number, job_record)
    return artifact_ok
