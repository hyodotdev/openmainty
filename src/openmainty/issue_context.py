"""Read-only GitHub Issue context for old runs with a missing trigger.

A run whose trigger snapshot was never recorded shows
"트리거 원문 기록 없음" with only an external link. This module fetches
the CURRENT upstream issue body/state through the supported read-only
`gh issue view` transport and stores each NEW observation immutably in
its own namespace. It never writes the old run record, its trigger,
prompts, logs, or proofs. A current observation is explicitly NOT the
historical trigger; fetched_at-only change is not content change.

Comments, applied labels, and node identity are NOT collected by the
underlying helper: they stay explicitly uncollected/unknown, never
zero/none/full-thread claims.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from . import paths as _paths
from . import util as _util

ISSUE_VIEW_ARGV = ["gh", "issue", "view"]
ISSUE_VIEW_FIELDS = "number,title,body,state,url"

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,6}$")
_VALID_STATES = ("OPEN", "CLOSED")
_FETCH_TIMEOUT_S = 30


class IssueContextError(RuntimeError):
    """Typed failure; bytes on disk are always preserved."""

    def __init__(self, message="", status=500, code="error"):
        super().__init__(message)
        self.http_status = status
        self.code = code


def safe_issue_number(number) -> int:
    """Positive-int issue numbers only; rejects bools/URLs/node ids."""
    if isinstance(number, bool):
        raise IssueContextError(f"bad issue number: {number!r}",
                                status=400, code="bad-number")
    text = str(number or "").strip()
    if not _NUMBER_RE.fullmatch(text):
        raise IssueContextError(f"bad issue number: {number!r}",
                                status=400, code="bad-number")
    return int(text)


def _slug_dir(github: str) -> Path:
    text = str(github or "").strip()
    if "/" not in text:
        raise IssueContextError(f"bad repo slug: {github!r}",
                                status=400, code="bad-slug")
    owner, name = text.split("/", 1)
    for part in (owner, name):
        if not _SLUG_RE.fullmatch(part or "") or part in (".", ".."):
            raise IssueContextError(f"bad repo slug: {github!r}",
                                    status=400, code="bad-slug")
    return _paths.home() / "issue_context" / f"{owner}__{name}"


def _obs_id(number: int, content_fp: str) -> str:
    return f"issue-{number}-{content_fp[:16]}"


def obs_path(github: str, number, content_fp: str) -> Path:
    return _slug_dir(github) / f"{_obs_id(safe_issue_number(number), content_fp)}.json"


def latest_path(github: str, number) -> Path:
    return _slug_dir(github) / f"issue-{safe_issue_number(number)}-latest.json"


def issue_view_argv(github: str, number: int) -> list:
    """Exact argv for one read-only `gh issue view` (pinned by test).

    Matches the supported transport shape used by the shared
    fetch_issue_content helper: number before --repo."""
    slug = str(github).strip()
    if "/" not in slug:
        raise IssueContextError(f"bad repo slug: {github!r}",
                                status=400, code="bad-slug")
    return (ISSUE_VIEW_ARGV + [str(int(number)), "--repo", slug]
            + ["--json", ISSUE_VIEW_FIELDS])


def _run_gh(argv: list, timeout: int = _FETCH_TIMEOUT_S) -> str:
    """One read-only gh call. Non-ok exits raise typed errors."""
    try:
        code, out, err = _util.run_cmd(argv, timeout=timeout)
    except (OSError, ValueError) as exc:
        raise IssueContextError(f"gh call failed: {exc}",
                                status=502, code="transport") from exc
    if code != 0:
        raise IssueContextError(
            f"gh failed (exit {code}): {(err or '').strip()[-200:]}",
            status=502, code="transport")
    return out or ""


def content_fp(number: int, title: str, body: str, state: str,
               url: str) -> str:
    """Content fingerprint: fetched_at/provenance excluded by design."""
    canonical = {"number": int(number), "title": str(title),
                 "body": str(body), "state": str(state), "url": str(url)}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True,
                                     ensure_ascii=True).encode("utf-8")
                          ).hexdigest()


def validate_observation(raw, github: str, number: int) -> dict:
    """Strict raw validation BEFORE any string coercion.

    Returns the normalized current observation (without fetched_at).
    Closed issues are VALID data (state CLOSED), not fetch errors.
    A present-but-empty body is explicit empty; a missing/non-string
    body is unreadable. The canonical URL must match exactly.
    """
    if not isinstance(raw, dict):
        raise IssueContextError("gh issue view returned a non-object "
                                "envelope; prior data kept",
                                status=502, code="bad-envelope")
    if raw.get("errors"):
        detail = json.dumps(raw["errors"])[:200]
        raise IssueContextError(f"gh issue view errors: {detail}",
                                status=502, code="bad-envelope")
    got_number = raw.get("number")
    if not isinstance(got_number, int) or isinstance(got_number, bool) \
            or got_number != int(number):
        raise IssueContextError("gh issue view number mismatch; prior "
                                "data kept", status=502, code="mismatch")
    title = raw.get("title")
    if not isinstance(title, str):
        raise IssueContextError("gh issue view title unreadable; prior "
                                "data kept", status=502, code="bad-shape")
    if "body" not in raw:
        raise IssueContextError("gh issue view body uncollected; prior "
                                "data kept", status=502, code="bad-shape")
    body = raw.get("body")
    if not isinstance(body, str):
        # Only an actual string empty body is empty. Null/missing/non-string
        # is unreadable and never coerced to a healthy empty body.
        raise IssueContextError("gh issue view body unreadable; prior "
                                "data kept", status=502, code="bad-shape")
    state = raw.get("state")
    if not isinstance(state, str) or not state.strip() \
            or state.strip().upper() not in _VALID_STATES:
        raise IssueContextError("gh issue view state unreadable; prior "
                                "data kept", status=502, code="bad-shape")
    url = raw.get("url")
    want = f"https://github.com/{str(github).strip()}/issues/{int(number)}"
    if not isinstance(url, str) or url.strip() != want:
        raise IssueContextError("gh issue view URL mismatch; prior data "
                                "kept", status=502, code="mismatch")
    state = state.strip().upper()
    return {"number": int(number), "title": title, "body": body,
            "state": state, "url": want,
            "content_fp": content_fp(int(number), title, body, state,
                                     want),
            "historical_trigger": False}


def _read_json_safe(path: Path):
    """(missing|ok|error, data). Corrupt bytes are preserved, never fixed.

    FileNotFoundError reads as missing (never-fetched). Any other OSError
    (permission, symlink, dir-in-place, …) reads as error. Symlinks are
    never followed.
    """
    _check_ancestors(path)
    import stat as _stat
    try:
        st = os.lstat(os.fspath(path))
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "error", None
    if not _stat.S_ISREG(st.st_mode):
        return "error", None
    try:
        data = _util.read_json(path, default=None)
    except (Exception, SystemExit):
        return "error", None
    if data is None:
        return "error", None
    return "ok", data


def _ensure_dir(path: Path) -> Path:
    _check_ancestors(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IssueContextError("issue-context namespace unwritable "
                                f"(kept in place): {exc}",
                                status=502, code="storage") from exc
    try:
        st = os.lstat(os.fspath(path))
    except OSError as exc:
        raise IssueContextError(f"issue context dir unreadable: {exc}",
                                status=502, code="storage") from exc
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode):
        # Symlinked leaf included: S_ISDIR is false for symlinks via lstat.
        raise IssueContextError("issue context dir blocked (kept in place)",
                                status=502, code="storage")
    return path


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_component(value: str, kind: str) -> str:
    """Single-path-component names only: no absolute paths, traversal,
    separators, or dot names. Shared store/repos helpers join names
    directly, so this runs BEFORE any of their file reads."""
    text = str(value or "").strip()
    if not text or text in (".", "..") or not _NAME_RE.fullmatch(text):
        raise IssueContextError(f"bad {kind} name: {value!r}",
                                status=400,
                                code="bad-repo" if kind == "repo"
                                else "bad-run")
    if "/" in text or "\\" in text or "\x00" in text:
        raise IssueContextError(f"bad {kind} name: {value!r}",
                                status=400,
                                code="bad-repo" if kind == "repo"
                                else "bad-run")
    return text


def resolve_run_context(repo: str, run_id: str) -> dict:
    """Server-side run+profile binding. Never trusts client slug/number.

    Names are validated to a single safe path component BEFORE the
    shared helpers join them into file paths; the loaded records are
    then bound back (run.id == requested run, profile.name == repo).
    Returns {slug, number, run_id, repo}. Non-numeric run issues raise
    code not-applicable (the caller maps it to a 200 applicable:false);
    unknown repo/run, repo mismatch, and boolean numbers are hard 404/400.
    """
    from . import repos as _repos
    from . import store as _store
    name = _safe_component(repo, "repo")
    try:
        profile = _repos.load(name)
    except SystemExit as exc:
        raise IssueContextError(str(exc),
                                status=404, code="unknown-repo") from exc
    if not isinstance(profile, dict) \
            or str(profile.get("name") or "") != name:
        raise IssueContextError("repo profile identity mismatch",
                                status=400, code="mismatch")
    slug = str(profile.get("github") or "").strip()
    if "/" not in slug:
        raise IssueContextError(f"repo '{name}' has no github slug",
                                status=400, code="bad-slug")
    rid = _safe_component(run_id, "run")
    try:
        run = _store.load(rid)
    except SystemExit as exc:
        raise IssueContextError(str(exc),
                                status=404, code="unknown-run") from exc
    if not isinstance(run, dict) or str(run.get("id") or "") != rid:
        raise IssueContextError("stored run identity mismatch",
                                status=400, code="mismatch")
    if str(run.get("repo") or "") != name:
        raise IssueContextError("run does not belong to this repo",
                                status=400, code="mismatch")
    issue = (run or {}).get("issue")
    if isinstance(issue, bool):
        raise IssueContextError(f"bad issue number: {issue!r}",
                                status=400, code="bad-number")
    if not isinstance(issue, str) or not issue.strip():
        raise IssueContextError("run has no numeric issue",
                                status=200, code="not-applicable")
    try:
        number = safe_issue_number(issue)
    except IssueContextError as exc:
        if exc.code == "bad-number":
            raise IssueContextError("run has no numeric issue",
                                    status=200,
                                    code="not-applicable") from exc
        raise
    return {"slug": slug, "number": number, "run_id": rid, "repo": name}


_OBS_ID_RE = re.compile(r"^issue-([1-9][0-9]{0,6})-([0-9a-f]{16})$")


def _validate_state_shape(state, number: int):
    """Stored latest-pointer schema check. Wrong shapes are storage errors,
    never healthy empty reads."""
    if not isinstance(state, dict):
        return False
    if set(state.keys()) - {"latest", "last_attempt", "last_success"}:
        return False
    if "latest" not in state or "last_attempt" not in state:
        return False
    latest = state.get("latest")
    if latest is not None:
        if not isinstance(latest, str):
            return False
        m = _OBS_ID_RE.fullmatch(latest)
        if not m or int(m.group(1)) != int(number):
            return False
    attempt = state.get("last_attempt")
    if attempt is not None:
        if not isinstance(attempt, dict):
            return False
        if set(attempt.keys()) - {"at", "ok", "error"}:
            return False
        if not isinstance(attempt.get("ok"), bool):
            return False
        if not isinstance(attempt.get("at"), str) \
                or not isinstance(attempt.get("error"), str):
            return False
    success = state.get("last_success")
    if success is not None:
        if not isinstance(success, dict):
            return False
        if set(success.keys()) - {"at", "obs"}:
            return False
        if not isinstance(success.get("at"), str):
            return False
        obs_ref = success.get("obs")
        if not isinstance(obs_ref, str):
            return False
        m = _OBS_ID_RE.fullmatch(obs_ref)
        if not m or int(m.group(1)) != int(number):
            return False
        if obs_ref != state.get("latest"):
            # last_success must bind the currently cached observation.
            # A dangling success reference (different content id than
            # latest) is a malformed combination, never a healthy read.
            return False
    return True


def _canonical_url(slug: str, number: int) -> str:
    return (f"https://github.com/{str(slug).strip()}/issues/{int(number)}")


def _validate_stored_observation(obs, slug: str, number: int):
    """Full revalidation of a stored observation: schema, canonical URL,
    recomputed content fingerprint, and allowed obs-id binding."""
    if not isinstance(obs, dict):
        return None
    want_url = _canonical_url(slug, number)
    if obs.get("url") != want_url:
        return None
    # Strict stored identity: a bool True must never pass for issue 1
    # (True == 1 in Python), matching the raw validator contract.
    stored_number = obs.get("number")
    if type(stored_number) is not int or stored_number != int(number):
        return None
    if obs.get("historical_trigger") is not False:
        return None
    for key in ("title", "body", "state"):
        if not isinstance(obs.get(key), str):
            return None
    if obs.get("state") not in _VALID_STATES:
        return None
    recomputed = content_fp(int(number), obs["title"], obs["body"],
                            obs["state"], want_url)
    if obs.get("content_fp") != recomputed:
        return None
    if obs.get("provenance") is not None and not isinstance(
            obs.get("provenance"), str):
        return None
    return "issue-%d-%s" % (int(number), recomputed[:16])


def read_cached(repo: str, run_id: str) -> dict:
    """Cached read only: latest observation + fetch state. No network.

    Never writes. Wrong-shaped state or a foreign/tampered observation
    reads as an explicit storage error with bytes kept in place — never
    as healthy empty or as another issue's data.
    """
    ctx = resolve_run_context(repo, run_id)
    slug, number = ctx["slug"], ctx["number"]

    def _store_error(note, latest=None, attempt=None):
        return {"applicable": True, "cached": False, "observation": None,
                "state": {"latest": latest, "last_attempt": attempt,
                          "store": "error", "note": note},
                "issue": number, "repo": ctx["repo"], "run": ctx["run_id"]}

    latest_status, state = _read_json_safe(latest_path(slug, number))
    if latest_status == "missing":
        return {"applicable": True, "cached": False, "observation": None,
                "state": {"latest": None, "last_attempt": None},
                "issue": number, "repo": ctx["repo"],
                "run": ctx["run_id"]}
    if latest_status == "error" or not _validate_state_shape(state,
                                                             number):
        return _store_error("stored issue-context state unreadable "
                            "(kept in place)")
    obs_id = state.get("latest")
    if not obs_id:
        return {"applicable": True, "cached": False, "observation": None,
                "state": state, "issue": number, "repo": ctx["repo"],
                "run": ctx["run_id"]}
    obs_status, obs = _read_json_safe(_slug_dir(slug) / f"{obs_id}.json")
    if obs_status != "ok":
        return _store_error("stored issue-context observation unreadable "
                            "(kept in place)",
                            latest=obs_id,
                            attempt=state.get("last_attempt"))
    bound = _validate_stored_observation(obs, slug, number)
    if bound is None or bound != obs_id:
        return _store_error("stored issue-context observation identity "
                            "mismatch (kept in place)",
                            latest=obs_id,
                            attempt=state.get("last_attempt"))
    return {"applicable": True, "cached": True, "observation": obs,
            "state": state, "issue": number, "repo": ctx["repo"],
            "run": ctx["run_id"]}


def _preflight(slug: str, number: int) -> None:
    """Storage preflight BEFORE any gh network call: namespace ancestors
    must be real dirs, the latest state must be missing-or-valid, and a
    referenced observation must fully revalidate (identity + content
    fingerprint) before any transport is allowed.

    A cached store:error — or a corrupt/tampered observation — is never
    silently rebound by a new successful body; it surfaces here first,
    with every byte preserved and zero transport calls."""
    _check_ancestors(latest_path(slug, number))
    status, state = _read_json_safe(latest_path(slug, number))
    if status == "error" or (status == "ok"
                             and not _validate_state_shape(state, number)):
        raise IssueContextError("stored issue-context state unreadable "
                                "(kept in place)",
                                status=502, code="storage")
    if status == "ok" and state.get("latest"):
        obs_id = state["latest"]
        obs_status, obs = _read_json_safe(
            _slug_dir(slug) / f"{obs_id}.json")
        if obs_status != "ok" or _validate_stored_observation(
                obs, slug, number) != obs_id:
            raise IssueContextError("stored issue-context observation "
                                    "unreadable (kept in place)",
                                    status=502, code="storage")


def fetch_and_store(repo: str, run_id: str,
                    timeout: int = _FETCH_TIMEOUT_S) -> dict:
    """Explicit model-free fetch: one gh read, immutable new observation.

    Observation files carry content only — no fetched_at — so an
    unchanged-content refetch reuses the persisted bytes (changed False)
    and the fresh time lands honestly in last_attempt metadata. Never
    touches the old run record. Returns the ACTUAL persisted observation
    (re-read from disk), not the in-memory fetch dict.
    """
    ctx = resolve_run_context(repo, run_id)
    slug, number = ctx["slug"], ctx["number"]
    _preflight(slug, number)
    try:
        raw_text = _run_gh(issue_view_argv(slug, number),
                           timeout=timeout)
    except IssueContextError as exc:
        _fail(slug, number, exc)
    try:
        raw = json.loads(raw_text or "{}")
    except ValueError as exc:
        _fail(slug, number, IssueContextError(
            f"gh issue view returned non-JSON: {exc}; prior data kept",
            status=502, code="bad-envelope"))
    try:
        fresh = validate_observation(raw, slug, number)
    except IssueContextError as exc:
        _fail(slug, number, exc)
    obs = dict(fresh)
    obs["provenance"] = (f"gh issue view {slug}#{number} (read-only; "
                         f"current observation, not the historical trigger)")
    obs_id = _obs_id(number, obs["content_fp"])
    path = _slug_dir(slug) / f"{obs_id}.json"
    _ensure_dir(_slug_dir(slug))
    prev = read_cached(repo, run_id)
    prev_latest = (prev.get("state") or {}).get("latest")
    _write_immutable(path, obs)
    _write_latest(slug, number, obs_id, ok=True, error="",
                  success={"at": _util.utcnow(), "obs": obs_id})
    _status, persisted = _read_json_safe(path)
    if _status != "ok" or not isinstance(persisted, dict):
        raise IssueContextError("stored issue-context observation "
                                "unreadable right after write (kept in "
                                "place)", status=502, code="storage")
    return {"observation": persisted, "changed": prev_latest != obs_id,
            "previous_latest": prev_latest}


def _write_immutable(path: Path, obs: dict) -> None:
    """Exclusive immutable write. Matching bytes are reused; anything else
    already at that path is a kept-in-place identity mismatch (symlinks,
    dirs, and foreign bytes are never followed or overwritten)."""
    payload = json.dumps(obs, indent=2, sort_keys=True,
                         ensure_ascii=True).encode("utf-8")
    _check_ancestors(path)
    import stat as _stat
    try:
        st = os.lstat(os.fspath(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise IssueContextError("stored issue-context observation "
                                f"unwritable: {exc} (kept in place)",
                                status=502, code="storage") from exc
    else:
        if not _stat.S_ISREG(st.st_mode):
            raise IssueContextError("stored issue-context observation "
                                    "identity mismatch (kept in place)",
                                    status=502, code="mismatch")
        try:
            if path.read_bytes() == payload:
                return
        except OSError as exc:
            raise IssueContextError("stored issue-context observation "
                                    f"unreadable: {exc} (kept in place)",
                                    status=502, code="storage") from exc
        raise IssueContextError("stored issue-context observation identity "
                                "mismatch (kept in place)",
                                status=502, code="mismatch")
    try:
        fd = os.open(os.fspath(path),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise IssueContextError("stored issue-context observation "
                                f"unwritable: {exc} (kept in place)",
                                status=502, code="storage") from exc
    try:
        view = memoryview(payload)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError("short write")
            view = view[n:]
    except OSError as exc:
        # Only this call created the path (O_EXCL); remove the partial
        # bytes so a later read cannot mistake them for an observation.
        try:
            os.unlink(os.fspath(path))
        except OSError:
            pass
        raise IssueContextError("stored issue-context observation "
                                f"unwritable: {exc} (kept in place)",
                                status=502, code="storage") from exc
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            raise IssueContextError("stored issue-context observation "
                                    f"unwritable on close: {exc} (kept in "
                                    "place)",
                                    status=502, code="storage") from exc


_PRESERVE = object()


def _check_ancestors(path: Path) -> None:
    """Every ancestor from the app home down must be a real directory.

    Symlinked namespace ancestors are followed by shared helpers, so
    this runs before any read/write in the new namespace. The well-known
    macOS /tmp -> /private/tmp alias needs no special case: the walk
    starts at paths.home() itself and only descends, never comparing
    spellings."""
    import stat as _stat
    root = _paths.home()
    rel = os.path.relpath(os.fspath(path), os.fspath(root))
    if rel.startswith(".."):
        raise IssueContextError("issue-context path escapes the app home",
                                status=500, code="storage")
    cur = os.fspath(root)
    for part in os.path.dirname(rel).split(os.sep):
        if not part or part == ".":
            continue
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return  # not yet created; parents so far are safe
        except OSError as exc:
            raise IssueContextError("issue-context namespace unreadable "
                                    f"(kept in place): {exc}",
                                    status=502, code="storage") from exc
        if not _stat.S_ISDIR(st.st_mode):
            # Covers symlinks, files, and anything that is not a real dir.
            raise IssueContextError("issue-context namespace blocked by a "
                                    "non-directory (kept in place)",
                                    status=502, code="storage")


def _write_state_json(path: Path, state: dict) -> None:
    """State write through a uniquely-named temp + atomic replace.

    Never uses a fixed temp name (no clobber of preexisting temp
    artifacts) and never follows a symlinked destination: a symlink or
    non-regular file at the target refuses instead of being replaced
    or written through."""
    import stat as _stat
    _check_ancestors(path)
    try:
        st = os.lstat(os.fspath(path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise IssueContextError("stored issue-context state unreadable "
                                "(kept in place)",
                                status=502, code="storage") from exc
    else:
        if not _stat.S_ISREG(st.st_mode):
            raise IssueContextError("stored issue-context state identity "
                                    "mismatch (kept in place)",
                                    status=502, code="storage")
    payload = json.dumps(state, indent=2, sort_keys=True,
                         ensure_ascii=True).encode("utf-8")
    tmp = os.fspath(path) + f".tmp-{os.getpid()}-{id(payload):x}"
    if os.path.lexists(tmp):
        raise IssueContextError("issue-context temp artifact already "
                                "present (kept in place)",
                                status=502, code="storage")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise IssueContextError("stored issue-context state unwritable "
                                "(kept in place)",
                                status=502, code="storage") from exc
    try:
        view = memoryview(payload)
        while view:
            n = os.write(fd, view)
            if n <= 0:
                raise OSError("short write")
            view = view[n:]
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise IssueContextError("stored issue-context state unwritable "
                                "(kept in place)",
                                status=502, code="storage") from exc
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            raise IssueContextError("stored issue-context state "
                                    "unwritable on close (kept in place)",
                                    status=502, code="storage") from exc
    try:
        os.replace(tmp, os.fspath(path))
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise IssueContextError("stored issue-context state unwritable "
                                "(kept in place)",
                                status=502, code="storage") from exc


def _check_obs_id(obs_id, number: int) -> None:
    if not isinstance(obs_id, str):
        raise IssueContextError("refusing to record a non-string "
                                "observation pointer",
                                status=500, code="storage")
    m = _OBS_ID_RE.fullmatch(obs_id)
    if not m or int(m.group(1)) != int(number):
        raise IssueContextError("refusing to record a foreign "
                                "observation pointer",
                                status=500, code="storage")


def _write_latest(slug: str, number: int, obs_id, ok: bool,
                  error: str, success=_PRESERVE) -> dict:
    """Latest-pointer + last-attempt update with an independent validated
    last_success binding: failures preserve the prior success binding;
    only an explicit success refreshes it. Corrupt state is never
    overwritten: unreadable artifacts stay in place and surface a typed
    storage failure."""
    from . import util as _u
    if obs_id is not None:
        _check_obs_id(obs_id, number)
    path = latest_path(slug, number)
    _check_ancestors(path)
    _ensure_dir(_slug_dir(slug))
    status, state = _read_json_safe(path)
    if status == "error" or (status == "ok"
                             and not _validate_state_shape(state, number)):
        raise IssueContextError("stored issue-context state unreadable "
                                "(kept in place)",
                                status=502, code="storage")
    state = dict(state) if isinstance(state, dict) else {}
    if success is _PRESERVE:
        success = state.get("last_success")
    elif success is not None:
        if not isinstance(success, dict) or success.get("obs") != obs_id:
            raise IssueContextError("refusing to bind success to a "
                                    "different observation",
                                    status=500, code="storage")
    state["latest"] = obs_id
    state["last_attempt"] = {"at": _u.utcnow(), "ok": bool(ok),
                             "error": str(error or "")}
    if success is None:
        state.pop("last_success", None)
    else:
        state["last_success"] = success
    if not _validate_state_shape(state, number):
        raise IssueContextError("refusing to write a malformed "
                                "issue-context state",
                                status=500, code="storage")
    _write_state_json(path, state)
    return state


def _fail(slug: str, number: int, exc: IssueContextError):
    """Fetch-failure path: record the attempt, then raise the ORIGINAL
    fetch error. If the attempt record itself hits unreadable storage,
    the storage failure surfaces chained behind the fetch error."""
    try:
        _record_attempt(slug, number, ok=False, error=str(exc))
    except IssueContextError as store_exc:
        raise store_exc from exc
    raise exc


def _record_attempt(slug: str, number: int, ok: bool, error: str) -> None:
    """Record a fetch attempt. Storage failures propagate (never hidden);
    callers chain the original fetch error behind them."""
    try:
        status, state = _read_json_safe(latest_path(slug, number))
    except (Exception, SystemExit) as exc:
        raise IssueContextError("stored issue-context state unreadable "
                                "(kept in place)",
                                status=502, code="storage") from exc
    if status == "error" or (status == "ok" and not _validate_state_shape(
            state, number)):
        raise IssueContextError("stored issue-context state unreadable "
                                "(kept in place)",
                                status=502, code="storage")
    latest = state.get("latest") if isinstance(state, dict) else None
    _write_latest(slug, number, latest, ok=ok, error=error)
