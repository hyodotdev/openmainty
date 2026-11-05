"""Read-only GitHub PR detail collection (PR inbox, M3.5+pr).

Complements prs.py (list-only awareness): fetches one PR's stored
snapshot — head/base SHAs, body, file list, bounded diff, CI/checks,
reviews and inline review threads — into app-owned files. Nothing here
posts, comments, merges, approves, labels, checks out PR refs, pushes,
or writes target repositories. The only processes ever started are
read-only `gh` verbs with pinned exact argv (see PR_*_ARGV, tested in
test_pr_detail.TestPrArgvAllowList) plus the shared read-only GraphQL
adapter for inline threads. Transport is read-only only for these
fixed queries: any mutation text would be refused before sending (see
_assert_readonly). PR text is untrusted display evidence: bounded, kept
as plain strings, escaped again by the dashboard, never executed and
never treated as worker instructions.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from . import paths as _paths
from . import util as _util

# Exact argv shapes the PR code may run. Tests pin these; no shell
# interpolation anywhere (argv lists only, validated parts).
PR_VIEW_ARGV = ["gh", "pr", "view"]
PR_DIFF_ARGV = ["gh", "pr", "diff"]
PR_CHECKS_ARGV = ["gh", "pr", "checks"]

PR_VIEW_FIELDS = ("number,title,body,author,state,isDraft,createdAt,"
                  "updatedAt,closedAt,mergedAt,headRefName,headRefOid,"
                  "baseRefName,baseRefOid,url,labels,assignees,"
                  "reviewRequests,reviews,comments,statusCheckRollup,"
                  "mergeable,mergeStateStatus,reviewDecision,"
                  "additions,deletions,changedFiles,files,"
                  "closingIssuesReferences")

PR_IDENTITY_FIELDS = "number,headRefOid,baseRefOid,updatedAt"

# Bounded transport: one page per section per fetch; the rest is an
# explicit partial note, never a silent empty claim.
_THREADS_FIRST = 50
_THREAD_COMMENTS_FIRST = 20
_THREAD_BYTES_MAX = 200000
_DIFF_MAX_BYTES = 120000
_BODY_MAX_BYTES = 20000
_FILE_LIST_MAX = 200
_COMMENT_BODY_MAX_BYTES = 8000

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,6}$")
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# One finite contract for immutable review-attempt outcomes. Both the
# backend validator (_attempt_record_ok) and the API gate
# (api_prs_attempt) consult this, so a record can never be a readable
# success for one and rejected by the other. Error-type records keep
# their explicit error instead of a report; only reviewed records may
# be linked as healthy review evidence, and only with the required
# report shape.
PR_ATTEMPT_OUTCOMES = frozenset({"reviewed", "stale", "error",
                                 "interrupted"})

# A REVIEWED record counts as healthy review evidence only with the
# required report shape and a digest fingerprint. Non-reviewed
# outcomes (error/stale/interrupted) carry explicit reasons, never
# a report. One shared checker serves history derivation and the
# attempt API alike, so the two can never disagree.
PR_RECOMMENDATIONS = frozenset({"adopt-external", "keep-local",
                                "needs-changes", "needs-info"})
_ATTEMPT_FP_RE = re.compile(r"^[0-9a-f]{64}$")


def attempt_review_evidence_ok(data) -> tuple:
    """Shared stored-attempt structural contract.

    Returns (True, data) when the record may be linked as review
    evidence, else (False, reason). The record's own bytes are never
    modified here. Non-reviewed outcomes need no report; a reviewed
    record without the required report shape and digest is an
    explicit error, never a healthy success.
    """
    if not isinstance(data, dict):
        return False, "attempt record is not a JSON object"
    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in PR_ATTEMPT_OUTCOMES:
        return False, (f"attempt record has unsupported outcome "
                       f"{outcome!r}")
    if outcome != "reviewed":
        return True, data
    report = data.get("report")
    if not isinstance(report, dict):
        return False, "reviewed attempt lacks a report object"
    if (not isinstance(report.get("summary"), str)
            or not report.get("summary").strip()):
        return False, "reviewed report lacks a summary"
    findings = report.get("findings")
    if (not isinstance(findings, list) or not findings
            or not all(isinstance(item, str) and item.strip()
                       for item in findings)):
        return False, "reviewed report lacks findings"
    labels = report.get("labels")
    if not isinstance(labels, dict):
        return False, "reviewed report lacks labels"
    for facet in ("status", "platform", "type"):
        vals = labels.get(facet)
        if (not isinstance(vals, list)
                or not all(isinstance(val, str) for val in vals)):
            return False, (f"reviewed report labels lack {facet} "
                           f"array")
    evidence = report.get("evidence")
    if (not isinstance(evidence, list)
            or not all(isinstance(item, dict)
                       and isinstance(item.get("url"), str)
                       and item.get("url").strip()
                       for item in evidence)):
        return False, "reviewed report lacks evidence with URLs"
    sources = report.get("source_files")
    if (not isinstance(sources, list)
            or not all(isinstance(item, str) for item in sources)):
        return False, "reviewed report lacks source_files paths"
    if "reply_draft" not in report:
        return False, "reviewed report lacks a reply key"
    draft = report.get("reply_draft")
    if draft is not None and not isinstance(draft, str):
        return False, "reviewed report has a non-text reply draft"
    recommendation = report.get("recommendation")
    if (not isinstance(recommendation, str)
            or recommendation not in PR_RECOMMENDATIONS):
        return False, "reviewed report has an unknown recommendation"
    basis = report.get("recommendation_basis")
    if not isinstance(basis, str) or not basis.strip():
        return False, "reviewed report lacks a recommendation basis"
    fp = data.get("attempt_fp")
    if not isinstance(fp, str) or not _ATTEMPT_FP_RE.fullmatch(fp):
        return False, "reviewed attempt lacks a digest fingerprint"
    return True, data


class PrError(RuntimeError):
    def __init__(self, message: str = "", status: int = 500):
        super().__init__(message)
        self.http_status = status


def prs_dir() -> Path:
    return _paths.home() / "prs"


def _slug_dir(github: str) -> Path:
    text = str(github or "").strip()
    if "/" not in text:
        raise PrError(f"bad repo slug: {github!r}", status=400)
    owner, name = text.split("/", 1)
    for part in (owner, name):
        if not _SLUG_RE.fullmatch(part or "") or part in (".", ".."):
            raise PrError(f"bad repo slug: {github!r}", status=400)
    return prs_dir() / f"{owner}__{name}"


def safe_pr_number(number) -> int:
    """Positive-int PR numbers only; rejects node ids/URLs."""
    text = str(number or "").strip()
    if not _NUMBER_RE.fullmatch(text):
        raise PrError(f"bad PR number: {number!r}", status=400)
    return int(text)


def pr_path(github: str, number) -> Path:
    return _slug_dir(github) / f"pr-{safe_pr_number(number)}.json"


def review_path(github: str, number) -> Path:
    return _slug_dir(github) / f"pr-review-{safe_pr_number(number)}.json"


def job_path(github: str, number) -> Path:
    return _slug_dir(github) / f"pr-review-job-{safe_pr_number(number)}.json"


def attempt_path(github: str, number, rid: str) -> Path:
    text = str(rid or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", text):
        raise PrError(f"bad attempt id: {rid!r}", status=400)
    return _slug_dir(github) / f"pr-attempt-{safe_pr_number(number)}-{text}.json"


def _repo_flag(github: str) -> list:
    slug = str(github).strip()
    if "/" not in slug:
        raise PrError(f"bad repo slug: {github!r}", status=400)
    return ["--repo", slug]


def pr_view_argv(github: str, number: int) -> list:
    """Exact argv for one read-only `gh pr view` (pinned by test)."""
    return (PR_VIEW_ARGV + _repo_flag(github) + [str(int(number))]
            + ["--json", PR_VIEW_FIELDS])


def pr_identity_argv(github: str, number: int) -> list:
    """Exact argv for the light before/after coherence read."""
    return (PR_VIEW_ARGV + _repo_flag(github) + [str(int(number))]
            + ["--json", PR_IDENTITY_FIELDS])


def pr_diff_argv(github: str, number: int) -> list:
    """Exact argv for one read-only `gh pr diff` (pinned by test)."""
    return PR_DIFF_ARGV + _repo_flag(github) + [str(int(number))]


def pr_checks_argv(github: str, number: int) -> list:
    """Exact argv for one read-only `gh pr checks` (pinned by test)."""
    return (PR_CHECKS_ARGV + _repo_flag(github) + [str(int(number))]
            + ["--json", "bucket,description,name,state,link,startedAt,"
                          "completedAt"])


def _valid_code_path(rel: str) -> bool:
    if not isinstance(rel, str) or not rel or len(rel) > 500:
        return False
    if rel.startswith("/") or "//" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return True


def contents_argv(github: str, rel: str, ref: str) -> list:
    """Exact argv for one read-only blob fetch at a captured full SHA.

    GET is pinned explicitly: `gh api` defaults parameters to POST,
    which must never carry an issue/PR path. Each path segment is
    percent-encoded so untrusted filenames (?, #, {}, query text) can
    never escape the contents path or inject parameters. The ref must
    be a full 40-hex commit SHA.
    """
    slug = str(github).strip()
    if "/" not in slug:
        raise PrError(f"bad repo slug: {github!r}", status=400)
    if not _valid_code_path(rel):
        raise PrError(f"rejected code path: {rel!r}"[:160], status=400)
    if not _FULL_SHA_RE.fullmatch(str(ref or "")):
        raise PrError("contents ref must be a full 40-hex SHA", status=400)
    encoded = "/".join(urllib.parse.quote(seg, safe="")
                       for seg in str(rel).split("/"))
    return ["gh", "api", "--method", "GET",
            f"repos/{slug}/contents/{encoded}", "-f", f"ref={ref}"]


def _assert_readonly_graphql(query: str) -> None:
    lowered = str(query or "").lower()
    for verb in ("mutation", "mutation(", "delete", "createsponsorship"):
        if verb in lowered:
            raise PrError("refusing non-read GraphQL transport", status=500)


def _run_gh(argv: list, timeout: int = 60, ok_codes=(0,)) -> str:
    """One read-only gh call. Non-ok exits raise typed PrError."""
    try:
        code, out, err = _util.run_cmd(argv, timeout=timeout)
    except (OSError, ValueError) as exc:
        raise PrError(f"gh call failed: {exc}", status=502) from exc
    if code not in ok_codes:
        raise PrError(f"gh failed (exit {code}): {(err or '').strip()[-200:]}",
                      status=502)
    return out or ""


def _parse_json_object(text: str, what: str) -> dict:
    try:
        parsed = json.loads(text or "{}")
    except ValueError as exc:
        raise PrError(f"gh {what} non-JSON: {exc}", status=502) from exc
    if not isinstance(parsed, dict):
        raise PrError(f"gh {what} returned a non-object envelope; "
                      "prior data kept", status=502)
    if parsed.get("errors"):
        detail = json.dumps(parsed["errors"])[:200]
        raise PrError(f"gh {what} errors: {detail}", status=502)
    return parsed


_THREADS_QUERY = """query($owner: String!, $name: String!, $number: Int!,
$first: Int!, $cfirst: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id number url
      reviewThreads(first: $first) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes { id isResolved isOutdated
          path line originalLine diffSide
          comments(first: $cfirst) {
            totalCount pageInfo { hasNextPage }
            nodes { id databaseId author { login } body createdAt updatedAt
                    url } } } }
    }
  }
}"""


def fetch_threads(github: str, number: int) -> tuple:
    """Inline review threads via read-only GraphQL.

    Returns (threads_list, partial_notes). A missing reviewThreads
    connection is an explicit unavailable note — never a silent "no
    threads". Non-dict pageInfo/comments degrade to unknown (partial),
    never to complete. Totals are bounded with truncation notes.
    """
    from . import discussions as _disc
    owner, _, name = str(github).partition("/")
    _assert_readonly_graphql(_THREADS_QUERY)
    try:
        data = _disc._gh_graphql(
            _THREADS_QUERY,
            {"owner": owner, "name": name, "number": int(number),
             "first": _THREADS_FIRST, "cfirst": _THREAD_COMMENTS_FIRST})
    except Exception as exc:
        return [], [f"threads unavailable: {exc}"[:200]]
    partial: list[str] = []
    try:
        repo = data.get("repository")
        pr = (repo or {}).get("pullRequest") if isinstance(repo, dict) \
            else None
        conn = (pr or {}).get("reviewThreads")
        if conn is None:
            return [], ["threads section unavailable (not returned)"]
        if not isinstance(conn, dict):
            return [], ["threads shape unexpected (not an object)"]
        nodes = conn.get("nodes")
        if not isinstance(nodes, list):
            return [], ["threads shape unexpected (nodes not a list)"]
    except AttributeError as exc:
        return [], [f"threads shape unexpected: {exc}"[:200]]
    threads = []
    thread_bytes = 0
    for node in nodes[:_THREADS_FIRST + 1]:
        if len(threads) >= _THREADS_FIRST:
            partial.append(f"more review threads than fetched "
                           f"({_THREADS_FIRST} shown)")
            break
        if not isinstance(node, dict):
            continue
        comms = node.get("comments")
        if not isinstance(comms, dict):
            partial.append("a thread has an unreadable comments section")
            cnodes: list = []
            more_comments = None
        else:
            raw_cnodes = comms.get("nodes")
            cnodes = raw_cnodes if isinstance(raw_cnodes, list) else []
            if not isinstance(raw_cnodes, list):
                partial.append("a thread has an unreadable comment list")
            page = comms.get("pageInfo")
            if isinstance(page, dict):
                more_comments = bool(page.get("hasNextPage"))
            else:
                more_comments = None
                partial.append("a thread page has unknown completeness")
        if more_comments:
            partial.append("a thread has more comments than fetched")
        comments = []
        for comment in cnodes[:_THREAD_COMMENTS_FIRST + 1]:
            if len(comments) >= _THREAD_COMMENTS_FIRST:
                partial.append("a thread has more comments than fetched")
                break
            if not isinstance(comment, dict):
                continue
            author = comment.get("author")
            body, body_cut = _byte_cap(comment.get("body"),
                                       _COMMENT_BODY_MAX_BYTES)
            if body_cut:
                partial.append("a thread comment body was truncated")
            thread_bytes += len(body.encode("utf-8"))
            comments.append({
                "id": str(comment.get("databaseId")
                           or comment.get("id") or ""),
                "author": str((author.get("login")
                               if isinstance(author, dict) else "") or ""),
                "body": body,
                "created_at": str(comment.get("createdAt") or ""),
                "updated_at": str(comment.get("updatedAt") or ""),
                "url": str(comment.get("url") or ""),
            })
            if thread_bytes > _THREAD_BYTES_MAX:
                partial.append("thread text truncated at "
                               f"{_THREAD_BYTES_MAX} bytes")
                break
        if thread_bytes > _THREAD_BYTES_MAX:
            break
        threads.append({
            "id": str(node.get("id") or ""),
            "resolved": bool(node.get("isResolved")),
            "outdated": bool(node.get("isOutdated")),
            "path": str(node.get("path") or "")[:260],
            "line": node.get("line"),
            "comments": comments,
            "comments_complete": more_comments is False,
        })
    page = conn.get("pageInfo")
    if isinstance(page, dict):
        if page.get("hasNextPage"):
            partial.append("more review threads than fetched "
                           f"({_THREADS_FIRST} shown)")
    else:
        partial.append("thread list completeness unknown")
    return threads, partial


def _bytes_len(value) -> int:
    return len(str(value or "").encode("utf-8"))


def _author_login(author) -> str:
    if isinstance(author, dict):
        return str(author.get("login") or "")[:80]
    return str(author or "")[:80]


def _byte_cap(text: str, cap: int) -> tuple:
    """Truncate to cap ENCODED bytes (never mid-character); returns
    (text, was_cut). Character slicing would corrupt multibyte text."""
    raw = str(text or "").encode("utf-8")
    if len(raw) <= cap:
        return str(text or ""), False
    return raw[:cap].decode("utf-8", errors="ignore"), True


def _comment_list(raw, cap: int, body_cap: int) -> tuple:
    """Parse a real `gh pr view` comments/reviews LIST (not a connection).

    Returns (items, complete_bool, note_or_None). Bodies are capped by
    encoded bytes. A dict-shaped connection is parsed defensively; a
    pageInfo dict WITHOUT the hasNextPage key means completeness is
    UNKNOWN (never complete). Anything else is unavailable, never an
    empty success.
    """
    if isinstance(raw, list):
        items: list[dict] = []
        cut = False
        for entry in raw[:cap + 1]:
            if len(items) >= cap:
                cut = True
                break
            if not isinstance(entry, dict):
                continue
            body, body_cut = _byte_cap(entry.get("body"), body_cap)
            cut = cut or body_cut
            items.append({
                "author": _author_login(entry.get("author")),
                "body": body,
                "body_truncated": body_cut,
                "state": str(entry.get("state") or "")[:40],
                "created_at": str(entry.get("createdAt")
                                   or entry.get("created_at") or ""),
                "updated_at": str(entry.get("updatedAt")
                                   or entry.get("updated_at") or ""),
                "url": str(entry.get("url") or "")[:300],
            })
        note = (f"only first {cap} shown"
                if (len(items) >= cap and len(raw) > cap) else None)
        if cut and note is None:
            note = "a comment body was truncated"
        return items, (note is None), note
    if isinstance(raw, dict):
        nodes = raw.get("nodes")
        if not isinstance(nodes, list):
            return [], False, "comment section shape unexpected"
        items, complete, note = _comment_list(nodes, cap, body_cap)
        page = raw.get("pageInfo")
        if not isinstance(page, dict) or "hasNextPage" not in page:
            return items, False, ((note + "; " if note else "")
                                  + "comment completeness unknown")
        if page.get("hasNextPage"):
            return items, False, ((note + "; " if note else "")
                                  + "comments paginated: first page only")
        return items, complete, note
    return [], False, "comment section unavailable"


def sanitize_pr_detail(row: dict) -> dict:
    """Bound one raw PR view row into stored snapshot shape.

    Handles the real `gh pr view --json` shapes: labels is a LIST of
    objects ({name}), comments/reviews are LISTs of objects (parsed
    with bodies, never dropped), closingIssuesReferences is a list of
    {number}, changedFiles is compared against the files list. SHAs
    must be full 40-hex or read "unresolved". Pure.
    """
    if not isinstance(row, dict):
        return {}
    try:
        number = int(row.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    head = str(row.get("headRefOid") or "")
    base = str(row.get("baseRefOid") or "")
    body = str(row.get("body") or "")
    body_bytes = _bytes_len(body)
    body_capped = body_bytes > _BODY_MAX_BYTES
    if body_capped:
        body = body.encode("utf-8")[:_BODY_MAX_BYTES].decode(
            "utf-8", errors="ignore")
    files_in = row.get("files")
    files: list[dict] = []
    files_note = None
    if isinstance(files_in, list):
        for entry in files_in[:_FILE_LIST_MAX + 1]:
            if len(files) >= _FILE_LIST_MAX:
                files_note = (f"file list shows {_FILE_LIST_MAX} of "
                              f"{len(files_in)} entries")
                break
            if not isinstance(entry, dict):
                continue
            files.append({
                "path": str(entry.get("path") or "")[:500],
                "additions": _nonneg_int(entry.get("additions")),
                "deletions": _nonneg_int(entry.get("deletions")),
            })
    elif files_in is not None:
        files_note = "file list shape unexpected"
    try:
        changed = int(row.get("changedFiles")
                      if row.get("changedFiles") is not None else -1)
    except (TypeError, ValueError):
        changed = -1
    if changed >= 0 and isinstance(files_in, list) and len(files_in) \
            != changed:
        files_note = ((files_note + "; " if files_note else "")
                      + f"files list has {len(files_in)} entries but "
                        f"changedFiles is {changed}")
    if changed < 0 and not isinstance(files_in, list):
        files_note = ((files_note + "; " if files_note else "")
                      + "changed-file bounds unknown")
    if (isinstance(files_in, list) and len(files_in) == 0
            and changed > 0):
        files_note = ((files_note + "; " if files_note else "")
                      + f"file list empty but changedFiles is {changed} "
                        f"(file inventory unavailable)")
    labels, labels_note = _label_list(row.get("labels"))
    comments, comments_complete, comments_note = _comment_list(
        row.get("comments"), 50, _COMMENT_BODY_MAX_BYTES)
    reviews, reviews_complete, reviews_note = _comment_list(
        row.get("reviews"), 30, _COMMENT_BODY_MAX_BYTES)
    closes = []
    for ref in (row.get("closingIssuesReferences") or []):
        if isinstance(ref, dict):
            ref = ref.get("number")
        try:
            closes.append(int(ref))
        except (TypeError, ValueError):
            continue
        if len(closes) >= 20:
            break
    return {
        "number": number,
        "title": str(row.get("title") or "")[:300],
        "body": body,
        "body_bytes_total": body_bytes,
        "body_truncated": body_capped,
        "author": _author_login(row.get("author")),
        "state": str(row.get("state") or ""),
        "is_draft": bool(row.get("isDraft")),
        "created_at": str(row.get("createdAt") or ""),
        "updated_at": str(row.get("updatedAt") or ""),
        "closed_at": str(row.get("closedAt") or ""),
        "merged_at": str(row.get("mergedAt") or ""),
        "head_ref": str(row.get("headRefName") or "")[:160],
        "head_sha": head if _FULL_SHA_RE.fullmatch(head) else "unresolved",
        "base_ref": str(row.get("baseRefName") or "")[:160],
        "base_sha": base if _FULL_SHA_RE.fullmatch(base) else "unresolved",
        "url": str(row.get("url") or "")[:300],
        "labels": labels,
        "labels_note": labels_note,
        "comment_count": len(comments),
        "comments": comments,
        "comments_complete": comments_complete,
        "comments_note": comments_note,
        "reviews": reviews,
        "reviews_complete": reviews_complete,
        "reviews_note": reviews_note,
        "closes": closes[:20],
        "additions": _nonneg_int(row.get("additions")),
        "deletions": _nonneg_int(row.get("deletions")),
        "changed_files": changed if changed >= 0 else None,
        "files": files,
        "files_note": files_note,
    }


def _nonneg_int(value) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, num)


def _label_list(raw) -> tuple:
    """Real label shapes: list of {name} objects (also tolerates a
    connection dict or a plain string list). Truncation past 50 is an
    explicit note, never silent."""
    if isinstance(raw, list):
        names = []
        for entry in raw[:51]:
            if len(names) >= 50:
                return names, "labels truncated to 50"
            if isinstance(entry, dict):
                name = entry.get("name")
            else:
                name = entry
            if name:
                names.append(str(name)[:80])
        return names, None
    if isinstance(raw, dict):
        nodes = raw.get("nodes")
        if isinstance(nodes, list):
            names, _ = _label_list(nodes)
            page = raw.get("pageInfo")
            if isinstance(page, dict) and page.get("hasNextPage"):
                return names, "labels paginated: first page only"
            return names, None
        return [], "label section shape unexpected"
    if raw is None:
        return [], None
    return [], "label section unavailable"


def _read_json_safe(path: Path):
    try:
        from .util import read_json as _read_json
        data = _read_json(path)
    except (Exception, SystemExit):
        return "corrupt", None
    if data is None:
        return "missing", None
    if not isinstance(data, dict):
        return "shape", None
    return "ok", data


def snapshot_status(github: str, number) -> dict:
    """Stored PR snapshot state: ready / never / error (never healthy zeros)."""
    try:
        num = safe_pr_number(number)
        _slug_dir(str(github or "").strip())
    except PrError as exc:
        return {"state": "error", "error": str(exc)[:200]}
    status, data = _read_json_safe(pr_path(str(github), num))
    if status == "missing":
        return {"state": "never", "error": None}
    if status != "ok" or not isinstance((data or {}).get("detail"), dict):
        return {"state": "error",
                "error": "stored PR snapshot unreadable (kept in place)"}
    return {"state": "ready", "head_sha": (data or {}).get("head_sha"),
            "base_sha": (data or {}).get("base_sha"),
            "fetched_at": (data or {}).get("fetched_at")}


def store_snapshot(github: str, snapshot: dict) -> dict:
    """Persist one coherent fetched snapshot. Raises PrError on bad input."""
    slug = str(github or "").strip()
    if not isinstance(snapshot, dict):
        raise PrError("snapshot is not an object", status=400)
    num = safe_pr_number(snapshot.get("number"))
    detail = snapshot.get("detail")
    if not isinstance(detail, dict) or detail.get("number") != num:
        raise PrError("snapshot/detail number mismatch", status=400)
    for key in ("head_sha", "base_sha"):
        value = snapshot.get(key)
        snapshot[key] = value if _FULL_SHA_RE.fullmatch(
            str(value or "")) else "unresolved"
    from .util import utcnow as _utcnow, write_json as _write_json
    snapshot["repo"] = slug
    snapshot["fetched_at"] = _utcnow()
    path = pr_path(slug, num)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, snapshot)
    return snapshot


def semantic_input(snapshot: dict) -> dict:
    """Canonical review content: edits here change the fingerprint.

    Includes the PR identity (number/url/head/base refs), actual
    body/comment/review/thread edits, the file inventory, the diff,
    check states and the delivered source-file census. `fetched_at`
    and attempt bookkeeping are excluded: time-only refreshes must not
    read as new input.
    """
    detail = snapshot.get("detail") if isinstance(snapshot, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    threads = snapshot.get("threads") if isinstance(snapshot, dict) else []
    files = detail.get("files") if isinstance(detail, dict) else []
    return {
        "number": snapshot.get("number") if isinstance(snapshot, dict)
        else None,
        "url": detail.get("url"),
        "title": detail.get("title"),
        "body": detail.get("body"),
        "head_ref": detail.get("head_ref"),
        "head_sha": snapshot.get("head_sha")
        if isinstance(snapshot, dict) else None,
        "base_ref": detail.get("base_ref"),
        "base_sha": snapshot.get("base_sha")
        if isinstance(snapshot, dict) else None,
        "labels": detail.get("labels"),
        "comments": detail.get("comments"),
        "reviews": detail.get("reviews"),
        "closes": detail.get("closes"),
        "files": ([{"path": f.get("path"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions")}
                   for f in files if isinstance(f, dict)]
                  if isinstance(files, list) else files),
        "checks": snapshot.get("checks") if isinstance(snapshot, dict)
        else None,
        "threads": threads if isinstance(threads, list) else [],
        "source_census": snapshot.get("source_census")
        if isinstance(snapshot, dict) else None,
        "partial": snapshot.get("partial") if isinstance(snapshot, dict)
        else None,
    }


def pr_fingerprint(snapshot: dict) -> str:
    """Stable identity of the reviewable input (body/checks/threads/diff)."""
    import hashlib
    canonical = json.dumps(semantic_input(snapshot), ensure_ascii=False,
                           sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _identity_of(view: dict) -> tuple:
    """(number, head_sha_or_None, base_sha_or_None) from a view payload."""
    try:
        number = int(view.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    head = str(view.get("headRefOid") or "")
    base = str(view.get("baseRefOid") or "")
    return (number,
            head if _FULL_SHA_RE.fullmatch(head) else None,
            base if _FULL_SHA_RE.fullmatch(base) else None)


def _read_identity(github: str, number: int) -> tuple:
    raw = _run_gh(pr_identity_argv(github, number), timeout=30)
    return _identity_of(_parse_json_object(raw, "identity"))


def fetch_pr_detail(github: str, number: int) -> dict:
    """One coherent model-free PR snapshot (view + diff + checks + threads).

    Coherence: head/base/number are read BEFORE the section calls and
    again AFTER; any mismatch (upstream moved mid-collection) refuses
    with PrError and stores nothing, preserving prior bytes. Sections
    that fail degrade to explicit partial/unavailable notes. Raises
    PrError; prior stored bytes are never touched here.
    """
    slug = str(github or "").strip()
    num = safe_pr_number(number)
    _slug_dir(slug)  # validate identity before any subprocess
    raw_view = _run_gh(pr_view_argv(slug, num))
    view = _parse_json_object(raw_view, "view")
    detail = sanitize_pr_detail(view)
    if detail.get("number") != num:
        raise PrError("view returned a different PR number; refusing "
                      "mixed collection", status=502)
    before = _identity_of(view)
    if before[1] is None or before[2] is None:
        raise PrError("view did not resolve full head/base SHAs; "
                      "refusing to snapshot an unidentified revision — "
                      "prior bytes kept", status=502)
    try:
        diff_text = _run_gh(pr_diff_argv(slug, num), timeout=60)
        diff_error = ""
    except PrError as exc:
        diff_text = ""
        diff_error = f"diff unavailable: {exc}"[:200]
    diff_bytes = _bytes_len(diff_text)
    diff_capped = diff_bytes > _DIFF_MAX_BYTES
    if diff_capped:
        diff_text = diff_text.encode("utf-8")[:_DIFF_MAX_BYTES].decode(
            "utf-8", errors="ignore")
    # `gh pr checks` exits 8 while checks are still pending, and may
    # exit nonzero on failed runs: stdout that still parses as a JSON
    # list is VALID check state (failed/pending are states, not transport
    # errors) and is kept with an exit-code note. Only unparseable or
    # empty output on a bad exit degrades to unavailable.
    checks_note = ""
    checks_pending_exit = False
    checks_exit = 0
    try:
        try:
            code, raw_checks, err = _util.run_cmd(
                pr_checks_argv(slug, num), timeout=60)
        except (OSError, ValueError) as exc:
            raise PrError(f"gh call failed: {exc}", status=502) from exc
        checks_exit = code
        checks_pending_exit = (code == 8)
        if code not in (0, 8):
            checks_note = (f"checks command exited {code}; "
                           f"stderr: {(err or '').strip()[-120:]}")
    except PrError as exc:
        raw_checks = ""
        checks_note = f"checks unavailable: {exc}"[:200]
    if raw_checks:
        try:
            parsed_checks = json.loads(raw_checks)
        except ValueError:
            parsed_checks = None
        if isinstance(parsed_checks, list):
            checks = parsed_checks
            if checks_exit not in (0, 8) and not checks_note:
                checks_note = (f"checks command exited {checks_exit}; "
                               "states kept as reported")
        else:
            checks = []
            checks_note = (checks_note + "; " if checks_note else "") + \
                "checks shape unexpected (not a list)"
    else:
        checks = []
        if not checks_note:
            checks_note = "checks returned no output"
    threads, thread_notes = fetch_threads(slug, num)
    # After-read: refuse mixed revisions. (None,None) on either side
    # can never read as coherent: full SHAs are required on both.
    after = _read_identity(slug, num)
    if (after[0] != num or after[1] is None or after[2] is None
            or after[1:] != before[1:]):
        raise PrError("upstream PR moved during collection "
                      f"(before={before} after={after}); nothing stored, "
                      "prior bytes kept", status=502)
    partial = list(thread_notes)
    if not detail.get("comments_complete", True):
        note = detail.get("comments_note") or "comments incomplete"
        partial.append(note)
    if not detail.get("reviews_complete", True):
        note = detail.get("reviews_note") or "reviews incomplete"
        partial.append(note)
    if detail.get("labels_note"):
        partial.append(detail["labels_note"])
    if detail.get("files_note"):
        partial.append(detail["files_note"])
    if detail.get("body_truncated"):
        partial.append(f"body truncated to {_BODY_MAX_BYTES} of "
                       f"{detail.get('body_bytes_total')} bytes")
    if diff_capped:
        partial.append(f"diff truncated to {_DIFF_MAX_BYTES} of "
                       f"{diff_bytes} bytes")
    if diff_error:
        partial.append(diff_error)
    if checks_note:
        partial.append(checks_note)
    if checks_pending_exit:
        partial.append("checks still pending at fetch time")
    if len(checks) >= 100:
        partial.append("checks list truncated to 100")
    return {
        "schema": 1, "repo": slug, "kind": "pull-request",
        "number": num,
        "head_sha": detail.get("head_sha", "unresolved"),
        "base_sha": detail.get("base_sha", "unresolved"),
        "detail": detail,
        "diff": diff_text,
        "diff_bytes_total": diff_bytes,
        "diff_truncated": diff_capped,
        "checks": [
            {"name": str(c.get("name") or "")[:160],
             "state": str(c.get("state") or c.get("bucket") or ""),
             "description": str(c.get("description") or "")[:300],
             "link": str(c.get("link") or "")[:300]}
            for c in checks if isinstance(c, dict)][:100],
        "threads": threads,
        "partial": partial,
        "checks_truncated": len(checks) >= 100,
    }
