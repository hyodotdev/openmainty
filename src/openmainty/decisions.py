"""Decision desk: read-only projection over app snapshot + store state.

No writes, no Git/evaluation/registry/model calls, no network. Reads
the precomputed summary snapshot bytes (the same file
``api_summary`` serves) plus bounded single-run reads for the
selected decision. Volatile observation stamps (served_at,
fetched_at, snapshot ``at``, age text, obs counters) are revision
labels only and never part of content identity.

Two separate concepts, never conflated:

* decision identity (repo + kind + ref) answers *which* decision;
* input revision answers *whether its content changed*. A changed
  waitReason/fingerprint/commit/step on the same run is stale, never
  current, and comparing two records from different decisions (or
  empty/shape-mismatched records) is unknown, never current.
"""

from __future__ import annotations

import re

SUPPORTED_KINDS = ("question", "issue-review")

#: Bumped whenever the revision input family changes shape. Records
#: built under a different version are incomparable (unknown).
DECISION_INPUT_VERSION = 1

_DECISION_ID_RE = re.compile(r"^([^:]+):([^:]+):(.+)$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Fields that change without any content changing. Never part of an
# input key, a fingerprint, or a staleness verdict.
VOLATILE_KEYS = ("served_at", "fetched_at", "at", "age_days", "age_text",
                 "obs", "refresh_obs")


def _snapshot():
    from . import batches
    try:
        snap = batches.read_json(batches.summary_snapshot_path(),
                                 default=None)
    except (Exception, SystemExit):
        return None, "summary snapshot unreadable (kept in place)"
    if not isinstance(snap, dict) or not snap.get("at"):
        return None, "status summary not yet built"
    return snap, None


def _valid_repos_shape(snap):
    repos = snap.get("repos")
    if not isinstance(repos, list):
        return None, "snapshot repos is not a list"
    for row in repos:
        if not isinstance(row, dict):
            return None, "snapshot repo row is not an object"
        name = row.get("name")
        if not isinstance(name, str) or not _SAFE_SEGMENT_RE.match(name):
            return None, "snapshot repo row has no valid name"
        need_you = row.get("need_you", [])
        if not isinstance(need_you, list):
            return None, "snapshot need_you is not a list for %s" % name
        for entry in need_you:
            if not isinstance(entry, dict):
                return None, "snapshot need_you row is not an object"
        blocked = row.get("blocked", [])
        if not isinstance(blocked, list):
            return None, "snapshot blocked is not a list for %s" % name
    return repos, None


def _entry_inputs(repo_name, entry):
    """Typed content-bound identity for one need_you row.

    Only the stable decision coordinates: repository, kind, and the
    run/item reference. Presentation text, ages, and suggestion prose
    are evidence to display, not identity to compare.
    """
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "")
    ref = str(entry.get("ref") or "")
    if not kind or not ref:
        return None
    return {"repo": str(repo_name or ""), "kind": kind, "ref": ref}


def decision_id(repo_name, entry):
    """Stable decision identity, separate from input revision."""
    inputs = _entry_inputs(repo_name, entry)
    if inputs is None:
        return None
    return "%s:%s:%s" % (inputs["repo"], inputs["kind"], inputs["ref"])


def revision_inputs(repo, kind, ref, wait_reason, fingerprint,
                    base_commit, work_commit, steps):
    """Complete like-shaped content inputs for one decision.

    Absent values stay None (absent is a comparable state, distinct
    from any present value); the key set is always complete so two
    records built here are comparable. Coordinates are part of the
    shape: records from different decisions never compare current.
    """
    step_map = {}
    if isinstance(steps, dict):
        for name, state in steps.items():
            if isinstance(state, dict):
                step_map[str(name)] = str(state.get("status") or "unknown")
    return {
        "version": DECISION_INPUT_VERSION,
        "repo": str(repo or ""),
        "kind": str(kind or ""),
        "ref": str(ref or ""),
        "wait_reason": wait_reason if isinstance(wait_reason, str)
        else None,
        "work_tree_fingerprint": fingerprint if isinstance(fingerprint,
                                                           str)
        else None,
        "base_commit": base_commit if isinstance(base_commit, str)
        else None,
        "work_commit": work_commit if isinstance(work_commit, str)
        else None,
        "steps": step_map,
    }


#: The exact required revision family. Anything else — empty dicts,
#: missing keys, extra keys, bool/int-confused versions, non-string
#: coordinates, non-string-or-null content, non-string step maps —
#: is an invalid shape, never comparable content.
_REVISION_KEYS = frozenset({"version", "repo", "kind", "ref",
                            "wait_reason", "work_tree_fingerprint",
                            "base_commit", "work_commit", "steps"})


def _valid_revision_shape(record):
    """Strict schema for one comparable revision record.

    Only records built by :func:`revision_inputs` under the current
    contract pass: exact key set, integer (never bool) contract
    version, non-empty string coordinates with a supported kind, and
    typed content fields where legitimate nulls stay null. Anything
    else compares as unknown, never current.
    """
    if not isinstance(record, dict) or set(record) != _REVISION_KEYS:
        return False
    if type(record.get("version")) is not int:
        return False
    if record.get("version") != DECISION_INPUT_VERSION:
        return False
    if not isinstance(record.get("repo"), str) or not record.get("repo"):
        return False
    if record.get("kind") not in SUPPORTED_KINDS:
        return False
    if not isinstance(record.get("ref"), str) or not record.get("ref"):
        return False
    for key in ("wait_reason", "work_tree_fingerprint", "base_commit",
                "work_commit"):
        value = record.get(key)
        if value is not None and not isinstance(value, str):
            return False
    steps = record.get("steps")
    if not isinstance(steps, dict):
        return False
    for name, status in steps.items():
        if not isinstance(name, str) or not isinstance(status, str):
            return False
    return True


def inputs_equal(first, second):
    """True only for valid-shaped records with equal values.

    Empty, version-mismatched (bool is not int 1), key-mismatched,
    coordinate-missing, or cross-decision records are never equal —
    such comparisons are unknown, and this function only answers the
    equality half.
    """
    if not _valid_revision_shape(first):
        return False
    if not _valid_revision_shape(second):
        return False
    return all(first[key] == second[key] for key in _REVISION_KEYS)


def derive_status(current_inputs, baseline_inputs):
    """Read-time freshness: current / stale / unknown.

    A changed wall-clock stamp alone never marks content changed;
    only a typed input difference on the SAME decision does. Nothing
    comparable (missing, foreign, incomplete, or shape-mismatched
    baseline) is unknown, never a silent current. Recorded stored
    fingerprints describe what was observed at collection time; they
    are not live-worktree proof.
    """
    if not _valid_revision_shape(current_inputs):
        return "unknown"
    if not _valid_revision_shape(baseline_inputs):
        return "unknown"
    for key in ("repo", "kind", "ref"):
        if current_inputs.get(key) != baseline_inputs.get(key):
            return "unknown"
    if inputs_equal(current_inputs, baseline_inputs):
        return "current"
    return "stale"


def _project_entry(repo_name, entry, revision):
    inputs = _entry_inputs(repo_name, entry)
    if inputs is None:
        return None
    kind = inputs["kind"]
    supported = kind in SUPPORTED_KINDS
    title = entry.get("question") if kind == "question" \
        else entry.get("title")
    projected = {
        "id": decision_id(repo_name, entry),
        "repo": inputs["repo"],
        "kind": kind,
        "ref": inputs["ref"],
        "title_excerpt": str(title or ""),
        "title_is_excerpt": True,
        "age_days": entry.get("age_days"),
        "age_text": entry.get("age_text"),
        "why": str(entry.get("why") or ""),
        "suggested": str(entry.get("suggested") or ""),
        "suggested_basis": str(entry.get("suggested_basis") or ""),
        "supported": supported,
        "revision": revision,
    }
    if not supported:
        projected["support_note"] = (
            "이 종류(%s)는 읽기 전용 결정 대상으로 아직 지원되지 "
            "않습니다. 전체 목록에서 원문을 확인하세요." % kind)
    return projected


def desk_snapshot(repo=None):
    """Project the decision desk from snapshot bytes only.

    One healthy GET must not recompute the world: no run/store reads
    here. Full text, fingerprints, and step state resolve lazily in
    ``decision_detail`` on explicit selection. Wrong snapshot shapes
    yield typed unknown, never healthy empty lists; the supported
    denominator counts only valid projected entries.
    """
    snap, error = _snapshot()
    if snap is None:
        return {"unknown": True, "error": error, "repos": [],
                "decisions": [], "supported": [], "unsupported": [],
                "revision": None}
    repos, shape_error = _valid_repos_shape(snap)
    if repos is None:
        return {"unknown": True, "error": shape_error, "repos": [],
                "decisions": [], "supported": [], "unsupported": [],
                "revision": snap.get("at")}
    revision = snap.get("at")
    out_repos = []
    supported = []
    unsupported = []
    for repo_row in repos:
        name = str(repo_row.get("name"))
        if repo is not None and name != repo:
            continue
        need_you = repo_row.get("need_you", [])
        blocked = repo_row.get("blocked", [])
        counts = {
            "need_you_total": len(need_you),
            "need_you_supported": 0,
            "blocked_total": len(blocked),
        }
        out_repos.append({"name": name, "counts": counts})
        for entry in need_you:
            projected = _project_entry(name, entry, revision)
            if projected is None:
                continue
            if projected["supported"]:
                supported.append(projected)
                counts["need_you_supported"] += 1
            else:
                unsupported.append(projected)
    return {"unknown": False, "error": None, "repos": out_repos,
            "decisions": supported,
            "supported": supported, "unsupported": unsupported,
            "revision": revision}


def _contained_regular(root_dir, *parts):
    """Expected file under a canonical root, symlink/escape refused.

    The shared store/repos readers follow symlinks; this projection
    boundary resolves the expected path first and refuses anything
    that is not the same regular file (symlinked components, escapes,
    missing files). No actual-home probing beyond the bound read.
    """
    import os
    root_real = os.path.realpath(str(root_dir))
    candidate = os.path.join(root_real, *parts)
    if os.path.islink(candidate) or not os.path.isfile(candidate):
        return None
    if os.path.realpath(candidate) != candidate:
        return None
    return candidate


def _current_member(repo_name, kind, ref):
    """Verify the requested coordinates are a current summary member.

    Returns (True, None) on membership, (False, reason) otherwise.
    A caller-invented same-repo run/kind is not a current decision.
    """
    snap, error = _snapshot()
    if snap is None:
        return False, "cannot verify membership: %s" % error
    repos, shape_error = _valid_repos_shape(snap)
    if repos is None:
        return False, "cannot verify membership: %s" % shape_error
    for repo_row in repos:
        if str(repo_row.get("name")) != repo_name:
            continue
        for entry in repo_row.get("need_you", []):
            if (str(entry.get("kind") or "") == kind
                    and str(entry.get("ref") or "") == ref):
                return True, None
    return False, "not a current decision in the latest summary"


def decision_detail(repo_name, decision_id_text):
    """Bounded selected-run read for one supported decision.

    Validates safe identifiers, then binds the requested coordinates
    to actual current summary membership BEFORE any run/profile
    read; exact run.id + run.repo binding is rechecked after the
    read. Missing/corrupt/foreign records are explicit typed errors,
    never fabricated healthy content. Unsupported kinds resolve to a
    typed pointer, never a run read disguised as a decision. No
    reader runs on rejection paths.
    """
    repo_name = str(repo_name or "")
    match = _DECISION_ID_RE.match(str(decision_id_text or ""))
    if match is None:
        return {"unknown": True,
                "error": "bad decision id (want repo:kind:ref)"}
    _id_repo, kind, ref = match.group(1), match.group(2), match.group(3)
    if _id_repo != repo_name:
        return {"unknown": True,
                "error": "decision id does not belong to this repo"}
    if (not _SAFE_SEGMENT_RE.match(repo_name)
            or not _SAFE_SEGMENT_RE.match(ref)
            or kind not in SUPPORTED_KINDS):
        return {"unknown": True,
                "error": "unsafe or unsupported decision coordinates"}
    member, member_error = _current_member(repo_name, kind, ref)
    if not member:
        return {"unknown": True, "inactive": True, "error": member_error}
    from . import paths as _paths
    from . import store
    if _contained_regular(_paths.runs_dir(), ref,
                          "run.json") is None:
        return {"unknown": True,
                "error": "linked run %s unreadable (kept in place)" % ref}
    try:
        run = store.load(ref)
    except (Exception, SystemExit):
        return {"unknown": True,
                "error": "linked run %s unreadable (kept in place)" % ref}
    if (not isinstance(run, dict) or run.get("id") != ref
            or run.get("repo") != repo_name):
        return {"unknown": True,
                "error": "linked run %s is not this repo's record" % ref}
    wait = run.get("waitReason")
    wait_text = wait if isinstance(wait, str) else None
    steps = run.get("steps") if isinstance(run.get("steps"), dict) else {}
    issue_no = str(run.get("issue") or "")
    issue_url = None
    if issue_no.isdigit():
        try:
            from . import paths as _profile_paths
            from . import repos as _repos
            if _contained_regular(_profile_paths.repos_dir(),
                                  repo_name + ".json") is None:
                raise SystemExit("profile not a bound regular file")
            slug = str((_repos.load(repo_name) or {}).get("github") or "")
            if _SAFE_SLUG_RE.match(slug):
                issue_url = "https://github.com/%s/issues/%s" % (
                    slug, issue_no)
        except (Exception, SystemExit):
            issue_url = None
    return {
        "unknown": False, "error": None,
        "id": "%s:%s:%s" % (repo_name, kind, ref),
        "repo": repo_name, "kind": kind, "ref": ref,
        "question_full": wait_text[len("question:"):].strip()
        if isinstance(wait_text, str)
        and wait_text.startswith("question:") else "",
        "question_pending": isinstance(wait_text, str)
        and wait_text.startswith("question:"),
        "issue": issue_no,
        "title": str(run.get("title") or ""),
        "inputs": revision_inputs(
            repo_name, kind, ref, wait_text,
            run.get("workTreeFingerprint"), run.get("baseCommit"),
            run.get("workCommit"), steps),
        "steps": {name: str((state or {}).get("status") or "unknown")
                  for name, state in steps.items()
                  if isinstance(state, dict)},
        "issue_url": issue_url,
    }
