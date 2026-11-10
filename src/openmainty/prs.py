"""Read-only GitHub PR awareness (M3.5).

OpenMainty never sees PRs: the scan reads issues, drift and its own
runs. This module adds a read-only PR feed plus deterministic,
model-free linkage (an external PR closing an issue we ran, an
issue deferred to an external PR, dependabot bumps matching our
queue). Nothing here posts, comments, reviews, merges, closes,
checks out PR refs, or pushes. The only processes ever started
are `gh pr list` (exact argv in PR_LIST_ARGV) and a read-only
`git diff --stat` inside a run work dir (hardened argv built via
runner.hardened_git_argv, pinned when a prep record is passed);
see test_prs.TestPrArgvAllowList.
"""
from __future__ import annotations

import re
import subprocess

# Exact argv shapes the PR code may run. Tests pin these.
PR_LIST_ARGV = ["gh", "pr", "list"]
# 301: work_diffstat builds its git argv via
# runner.hardened_git_argv (pinned by test_prs); no bare git argv
# lives here anymore.

PR_LIST_FIELDS = ("number,title,author,isDraft,headRefName,createdAt,"
                  "updatedAt,closingIssuesReferences,additions,"
                  "deletions,changedFiles")

_BOT_SUFFIX = "[bot]"
_BOT_LOGINS = frozenset({"dependabot", "dependabot-preview"})

_DEP_BUMP_RE = re.compile(
    r"\bbump\s+(?P<dep>[A-Za-z0-9_@/.\-]+)\s+from\s+"
    r"(?P<frm>v?[0-9][A-Za-z0-9_.\-+]*)\s+to\s+"
    r"(?P<to>v?[0-9][A-Za-z0-9_.\-+]*)\b",
    re.IGNORECASE)


def pr_list_argv(github: str, limit: int) -> list:
    """Exact argv for the PR scan (pinned by test)."""
    return PR_LIST_ARGV + ["--repo", str(github), "--state", "open",
                           "--limit", str(limit), "--json",
                           PR_LIST_FIELDS]


def is_bot(author) -> bool:
    """A PR author counts as a bot when gh or its login says so.

    The author object gh returns carries an explicit is_bot flag
    (live: true for "app/dependabot", false for humans) — that
    flag wins. Without it, fall back to the login: a trailing
    "[bot]" (e.g. "dependabot[bot]"), an "app/<name>" GitHub-App
    login, or a known bare bot login. Anything else (including
    missing) is human. Display-only either way.
    """
    if isinstance(author, dict) and author.get("is_bot") is True:
        return True
    login = ""
    if isinstance(author, dict):
        login = str(author.get("login") or "")
    else:
        login = str(author or "")
    low = login.strip().lower()
    return (low.endswith(_BOT_SUFFIX) or low in _BOT_LOGINS
            or low.startswith("app/"))


def sanitize_pr(row: dict) -> dict:
    """Bound an untrusted PR row for display/storage.

    Titles and author names are untrusted display text: capped and
    kept as plain strings here; the dashboard escapes them again
    with esc(). Never executed, never sent to a model.
    """
    if not isinstance(row, dict):
        return {}
    refs = row.get("closingIssuesReferences")
    if not isinstance(refs, list):
        refs = []
    closed = []
    for ref in refs:
        num = (ref.get("number") if isinstance(ref, dict) else ref)
        try:
            closed.append(int(num))
        except (TypeError, ValueError):
            continue
    author = row.get("author")
    login = ""
    if isinstance(author, dict):
        login = str(author.get("login") or "")[:80]
    else:
        login = str(author or "")[:80]
    try:
        number = int(row.get("number"))
    except (TypeError, ValueError):
        number = 0
    return {
        "number": number,
        "title": str(row.get("title") or "")[:300],
        "author": login,
        "bot": is_bot(author),
        "is_draft": bool(row.get("isDraft")),
        "head": str(row.get("headRefName") or "")[:160],
        "created_at": str(row.get("createdAt") or ""),
        "updated_at": str(row.get("updatedAt") or ""),
        "closes": closed[:20],
        "additions": _as_int(row.get("additions")),
        "deletions": _as_int(row.get("deletions")),
        "files": _as_int(row.get("changedFiles")),
    }


def _as_int(value) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, num)


def parse_dependabot(title: str) -> dict | None:
    """Parse 'bump <dep> from <a> to <b>' from a bot PR title.

    Returns {"dep", "from", "to"} or None when the title does not
    carry a parseable bump. Comments and URLs never match: the dep
    must sit in a bump/from/to triple.
    """
    match = _DEP_BUMP_RE.search(str(title or ""))
    if not match:
        return None
    return {"dep": match.group("dep"), "from": match.group("frm"),
            "to": match.group("to")}


def board_decision(decision: str, issue_number: str, closers: dict,
                   has_run: bool) -> str:
    """The issue-board decision with external PRs (pure).

    An issue OpenMainty never ran that an external PR closes reads
    "skip-external-pr" instead of "start", so the board shows the
    PR and offers no duplicate run. Anything else is unchanged.
    """
    if decision != "start" or has_run:
        return decision
    if not str(issue_number or "").isdigit():
        return decision
    try:
        closer = (closers or {}).get(int(issue_number))
    except (TypeError, ValueError):
        return decision
    return "skip-external-pr" if closer is not None else decision


def closing_map(prs: list) -> dict:
    """Map issue number -> closing PR (lowest PR number wins).

    Pure and deterministic. Only non-draft, non-bot PRs close
    issues here; bot bumps never claim an issue.
    """
    out = {}
    for pr in prs or []:
        if not isinstance(pr, dict):
            continue
        if pr.get("bot") or pr.get("is_draft"):
            continue
        try:
            num = int(pr.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        for issue in pr.get("closes") or []:
            try:
                key = int(issue)
            except (TypeError, ValueError):
                continue
            if key not in out or num < out[key].get("number", 0):
                out[key] = pr
    return out


_REF_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _safe_ref(base: str) -> str:
    """The diff ref: a hex sha or HEAD, nothing else (M2.15).

    Only a hex sha (7-40 chars) or HEAD ever reaches the command
    line; anything else (branch names, URLs, flags, empty) falls
    back to HEAD. Pure.
    """
    ref = str(base or "").strip()
    if ref == "HEAD" or _REF_RE.match(ref):
        return ref
    return "HEAD"


def work_diffstat(workdir: str, base: str = "", *,
                  git_bound=None) -> str:
    """One read-only `git diff --stat` inside a run work dir.

    Compares the work tree against base (a commit sha) or HEAD when
    no base is given. Output is capped; failures read as a plain
    note, never an exception. 301: runs over the hardened path
    (fsmonitor/hooks/drivers off, system/global config off),
    pinned to the prep record when one is passed — a rewritten
    pointer reads as failure, never a forged stat.
    301b: the diff runs against a private per-call index copy
    (M2.21b), bound and unbound alike, so the read never writes
    the worktree's index. When the copy cannot be made the read
    keeps the env rule only — never a refusal for that.
    302: the diff ignores in-tree .gitattributes (--attr-source)
    plus unbound --no-ext-diff/--no-textconv, so no planted
    filter/diff driver runs. A git too old for the flag fails
    closed: SourceUnreadable propagates when bound, the failure
    note below when unbound.
    302c: a hung probe surfaces as this read's own failure (the
    note), never as a cached refusal.
    """
    try:
        from . import runner as _runner
        from .util import git_readonly_child_env as _ro_env
        ref = _safe_ref(base)
        if git_bound is not None:
            try:
                paths = _runner.reprove_git_pointer(workdir, git_bound)
            except Exception:  # noqa: BLE001 - fail closed
                return "실행 내역 읽기 실패"
            argv = _runner.hardened_git_argv(
                ["diff", "--stat", ref, "--", "."],
                gitdir=paths["gitdir"],
                worktree=paths["worktree"])
            with _ro_env(argv, _runner.hardened_git_env(),
                         cwd=None) as env:
                proc = subprocess.run(
                    argv, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=60, text=True, env=env)
        else:
            try:
                supported = _runner.attr_source_supported()
            except subprocess.TimeoutExpired:
                # 302c: hung probe — this read's own failure
                # (the note), never a cached refusal.
                return "실행 내역 읽기 실패"
            if not supported:
                return "실행 내역 읽기 실패"
            argv = _runner.hardened_git_argv(
                ["-C", str(workdir), "diff", "--stat", ref, "--", "."])
            with _ro_env(argv, _runner.hardened_git_env(),
                         cwd=str(workdir)) as env:
                proc = subprocess.run(
                    argv, cwd=str(workdir),
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=60, text=True, env=env)
    except (OSError, subprocess.SubprocessError):
        return "실행 내역 읽기 실패"
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return "변경 없음" if proc.returncode == 0 else "실행 내역 읽기 실패"
    lines = (proc.stdout or "").strip().splitlines()
    kept = lines[:10]
    if len(lines) > 10:
        kept.append(f"… 외 {len(lines) - 10}줄")
    return " / ".join(line.strip() for line in kept)[:500]
