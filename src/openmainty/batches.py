"""Review batches: generic, persistent tracking of independent reviews plus
integration/runtime gate evidence.

A batch groups reviewed candidates by repo, target branch and purpose
(stable, next-tooling, Expo-dedicated, Calendar-next, ...). Each batch
records PRIMARY candidates (unique reviews owned by the batch) and
INHERITED coverage (candidates reviewed elsewhere, executed here), plus
integration snapshots with explicit required gates and evidence.

Nothing here hardcodes today's candidates: ids, repos, gates and evidence
are all data supplied through `import_batch` (trusted local operator —
CLI, or the dashboard's thin /api/batch/import wrapper which runs the
same validator unchanged). Readiness is evaluated
against LIVE source at read time using the same actual-bytes contract as
runner.source_snapshot:

- integration: live HEAD + tracked-source fingerprint must match the
  recorded values with a clean tree, else every bound gate blocks;
- each PASS gate carries its own inputs snapshot; inputs that predate the
  current integration source read STALE (old proof is never borrowed);
- original candidate: the bound worktree's live HEAD/fingerprint must
  match, and the target/base identity must be unchanged;
- run provenance: each primary's run_id must resolve to a real run in the
  same repo whose queue metadata (dep/target/lane/branch) matches.

Imports are two-phase (validate everything, then write) inside the repo
scheduler lock: concurrent imports cannot both own one candidate, and a
rejected import cannot corrupt a prior batch, its history, or its stored
evidence (content-addressed, never overwritten).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import events, m2, muse, paths, promote, repos, runner, sched, store
from . import versions as _versions
from .util import (git_readonly_child_env,
                   read_json, utcnow, write_json)

SCHEMA = 3

GATE_STATES = {"pending", "pass", "fail", "blocked"}
REVIEW_VERDICTS = {"pending", "accept", "accept-with-notes", "request-changes"}

# Acceptance is proven by the review-proof steps only: a dependency-review
# ACCEPT stands on fresh review+e2e bound to the reviewed bytes. The
# reproduce step predates content fingerprinting on review runs (legacy =
# executed, honest history, never current proof) and never greens an
# accept by itself; the full per-step map is always recorded.
ACCEPT_PROOF_STEPS = {"review", "e2e"}


def _accept_proof_gaps(vstat: dict) -> list:
    return sorted(s for s in ACCEPT_PROOF_STEPS
                  if vstat.get(s) not in ("fresh", "skipped"))


def _candidate_proof(state: dict, run_id: str):
    """Shared review-accepted predicate (R1): the fresh-proof gate the
    UI prepare path enforces server-side (prepare_integration) and the
    M2 driver pre-flights. Review+e2e must be FRESH against the live
    bytes; status-done alone never greens. Returns
    (work_commit, work_fp, error); error None means accepted."""
    work_commit = str(state.get("workCommit") or "")
    work_fp = str(state.get("workTreeFingerprint") or "")
    if not work_commit or not work_fp:
        return "", "", ("run has no recorded source identity "
                        "(insufficient evidence)")
    cand_worktree = str(store.run_dir(run_id) / "work")
    cand_snap = _snapshot(cand_worktree)
    live_head = cand_snap.get("head") if cand_snap else None
    live_fp = cand_snap.get("fingerprint") if cand_snap else None
    vstat = store.verification_status(state, live_fp, live_head)
    gaps = _accept_proof_gaps(vstat)
    if gaps:
        detail = ", ".join(f"{step}={vstat.get(step)}" for step in gaps)
        return "", "", ("candidate review proof not fresh "
                        f"({detail}); re-verify, do not integrate")
    if (cand_snap is None or live_head != work_commit
            or live_fp != work_fp or not cand_snap.get("clean")):
        return "", "", ("candidate source drifted or unreadable; "
                        "re-verify, do not integrate")
    return work_commit, work_fp, None


# Source13: typed issue-candidate identity. An issue candidate is keyed
# "issue:<repo>:<lane>:<number>" — a structurally distinct namespace from
# legacy dep keys "repo|dep|target|lane", so an issue can never collide
# with a dep candidate (the literal "issue:" prefix cannot equal a repo
# path shaped like "repo|dep..." and "/" never appears inside the number).
_ISSUE_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_ISSUE_BRANCH_RE = re.compile(r"^codex/(?:[A-Za-z0-9_.-]+/)*"
                              r"[A-Za-z0-9_.-]+$")


def _strict_issue_branch(value: object) -> str | None:
    """Owned codex/ branch name, else None (fail closed)."""
    if not isinstance(value, str):
        return None
    if _ISSUE_BRANCH_RE.fullmatch(value) is None:
        return None
    if ".." in value:
        return None
    if any(seg in (".", "..") for seg in value.split("/")):
        return None
    return value


def _strict_issue_number(value: object) -> str | None:
    """Return the positive ASCII issue number, else None (fail closed).

    bool/int/zero/leading-zero/unicode/newline values never parse: only
    an exact 1-20 digit ASCII string starting with a nonzero digit.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return None
    if _ISSUE_NUMBER_RE.fullmatch(value) is None:
        return None
    return value


def issue_candidate_key(repo: str, lane: str, issue: str) -> str:
    """Typed issue candidate key; inputs come from admission only."""
    return f"issue:{repo}:{lane}:{issue}"


_GITHUB_SLUG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/"
                             r"[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_KNOWN_LANES = ("main", "next")


def _strict_github_slug(value: object) -> str | None:
    """Canonical exact owner/repo slug, else None (fail closed).

    Rejects whitespace/control/query/scheme/extra separators: only one
    slash, no leading/trailing dots or dashes per segment beyond what
    the character class allows. Never split a loose string into a URL.
    """
    if not isinstance(value, str):
        return None
    if _GITHUB_SLUG_RE.fullmatch(value) is None:
        return None
    return value


def primary_result_key(repo: str, primary: dict) -> str | None:
    """Typed display/dedup key for a stored batch primary.

    Legacy dep primaries keep their exact "repo|dep|target|lane" key;
    issue primaries get the distinct "issue:..." namespace. Malformed,
    mixed or unknown records yield None — a display-only unknown, never
    a skip: import_ rejects them and evaluate() marks them explicitly
    not-ready (see blocked-unknown-primary).
    """
    if not isinstance(primary, dict):
        return None
    dep = primary.get("dep")
    target = primary.get("target")
    has_dep = (isinstance(dep, str) and dep != "") \
        or (isinstance(target, str) and target != "")
    blk = primary.get("issue")
    if isinstance(blk, dict):
        if has_dep:
            return None  # mixed dep+issue identity: invalid
        # Non-dict issue blocks never reach here; a dict with wrong
        # fields is invalid (import_ rejects, evaluate marks not-ready).
        num = blk.get("issue")
        if _strict_issue_number(num) is None:
            return None
        nrepo = blk.get("repo")
        nlane = blk.get("lane")
        plane = primary.get("lane")
        if nrepo != repo:
            return None  # foreign-repo block: invalid
        if not isinstance(nlane, str) or nlane not in _KNOWN_LANES \
                or nlane != plane:
            return None  # unknown lane or block/lane mismatch: invalid
        return issue_candidate_key(repo, nlane, str(num))
    if has_dep:
        # A partial legacy identity (empty dep or empty target) is
        # invalid, never a valid legacy key: import rejects it and
        # evaluate marks the record explicitly not-ready.
        if not isinstance(dep, str) or not dep \
                or not isinstance(target, str) or not target:
            return None
        return candidate_key(repo, dep, target,
                             str(primary.get("lane") or ""))
    return None


def issue_queue_effective_lane(queue_item: dict, run: dict) -> str:
    """Effective lane for an issue-triage queue item, else "".

    Pure/read-only: reconciles ALL present queue/run lane, base_branch
    and owned-branch fields. A missing key is absent (real #856 has no
    queue lane); a PRESENT value — including an empty string, None, or
    a wrong type — must be a known "main"/"next" lane and all must
    agree; the owned branch must be present and identical on both
    sides. Actual #856 (queue base_branch "next", no queue lane, run
    lane "next", exact owned branch) yields "next". No missing-queue
    fallback.
    """
    if not isinstance(queue_item, dict) or not isinstance(run, dict):
        return ""
    cands = []
    for src in (queue_item, run):
        for k in ("lane", "base_branch"):
            if k not in src:
                continue  # absent key (real #856 has no queue lane)
            v = src[k]
            # A PRESENT value — including an empty string, None, a
            # wrong type, or an unknown lane — must be a known lane.
            # It is never silently dropped to force agreement among
            # the remaining fields; only a missing key is absent.
            if not isinstance(v, str) or v not in _KNOWN_LANES:
                return ""
            cands.append(v)
    if not cands:
        return ""
    if len(set(cands)) != 1:
        return ""
    branch = str(queue_item.get("branch") or "")
    run_branch = str(run.get("branch") or "")
    if not branch or branch != run_branch:
        return ""
    return cands[0]


def _resolve_issue_candidate(repo: str, run_id: str) -> dict:
    """Fail-closed admission for a typed issue candidate.

    Requires the existing app-owned queue record with kind exactly
    "issue-triage" and agreement on run_id/repo/positive-ASCII issue/
    owned branch/effective lane, plus a registered GitHub owner/repo
    profile and a stored run whose id and repo bind. Returns the context
    {number, lane, branch, base_branch, queue, run, profile, slug, url}
    or raises IntegrationError before any write.
    """
    try:
        profile = repos.load(repo)
    except (Exception, SystemExit) as exc:
        raise IntegrationError(
            f"registered repo {repo!r} unreadable; issue prepare "
            f"refused: {exc}") from exc
    if not isinstance(profile, dict):
        raise IntegrationError(
            f"registered repo {repo!r} unknown; issue prepare refused")
    slug = _strict_github_slug(profile.get("github"))
    if slug is None:
        raise IntegrationError(
            "registered repo has no canonical GitHub owner/repo; "
            "issue prepare refused")
    owner, _, name = slug.partition("/")
    queue_item = _queue_item_for_run(repo, run_id)
    if not queue_item or str(queue_item.get("kind") or "") \
            != "issue-triage":
        raise IntegrationError(
            "no app-owned issue-triage queue record for this run; "
            "issue prepare refused")
    if str(queue_item.get("run_id") or "") != run_id \
            or str(queue_item.get("repo") or "") != repo:
        raise IntegrationError(
            "queue/run identity mismatch; issue prepare refused")
    try:
        run = store.load(run_id)
    except (Exception, SystemExit) as exc:
        raise IntegrationError(
            f"stored run {run_id!r} unreadable; issue prepare "
            f"refused: {exc}") from exc
    if not isinstance(run, dict) or not run \
            or str(run.get("id") or "") != run_id \
            or str(run.get("repo") or "") != repo:
        raise IntegrationError(
            "stored run identity mismatch; issue prepare refused")
    num_raw = queue_item.get("issue", run.get("issue"))
    number = _strict_issue_number(num_raw)
    if number is None:
        raise IntegrationError(
            "issue number not a positive ASCII number; issue prepare "
            "refused")
    qissue = _strict_issue_number(queue_item.get("issue"))
    rissue = _strict_issue_number(run.get("issue"))
    if qissue != number or rissue != number:
        raise IntegrationError(
            "queue/run issue identity mismatch; issue prepare refused")
    # App-owned queue key binding (actual #856:
    # "react-native-nitro-sound#issue-856"): the queue record key must
    # name this exact repo issue, never an unrelated or forged row.
    want_key = f"{repo}#issue-{number}"
    qkey = queue_item.get("key", queue_item.get("queue_key"))
    if not isinstance(qkey, str) or qkey != want_key:
        raise IntegrationError(
            "queue record key does not bind this repo issue; issue "
            "prepare refused")
    # The stored run carries the same app-owned queue key (actual #856:
    # run.queue_key "react-native-nitro-sound#issue-856"). This typed
    # issue path requires the nonempty canonical stored key; a
    # missing, empty, malformed, or mismatched stored key refuses.
    # Nothing may rebind the row elsewhere.
    rqkey = run.get("queue_key")
    if not isinstance(rqkey, str) or not rqkey or rqkey != want_key:
        raise IntegrationError(
            "stored run queue_key does not bind this repo issue; "
            "issue prepare refused")
    # Strict owned branch, no string coercion: the queue-owned
    # original branch must be a valid codex/ branch and identical on
    # both sides. A missing/non-string/foreign branch refuses.
    branch = queue_item.get("branch")
    run_branch = run.get("branch")
    if not isinstance(branch, str) \
            or _strict_issue_branch(branch) is None \
            or branch != run_branch:
        raise IntegrationError(
            "queue/run branch identity mismatch or not a strict owned "
            "branch; issue prepare refused")
    # Source identity itself stays with the existing proof contract:
    # _candidate_proof reads the canonical store.run_dir(run_id)/"work"
    # tree. Actual stored runs (e.g. run-42fd6a6a) carry no worktree
    # field, so no such field is required here.
    lane = issue_queue_effective_lane(queue_item, run)
    if not lane:
        raise IntegrationError(
            "issue queue lane unknown/contradictory; issue prepare "
            "refused")
    run_lane = str(run.get("lane") or "")
    if run_lane and run_lane != lane:
        raise IntegrationError(
            "run lane contradicts queue lane; issue prepare refused")
    return {"number": number, "lane": lane, "branch": branch,
            "base_branch": str(queue_item.get("base_branch") or ""),
            "queue": queue_item, "run": run, "profile": profile,
            "slug": slug, "repo": repo, "run_id": run_id,
            "url": f"https://github.com/{owner}/{name}"
                   f"/issues/{number}"}


def candidate_proof_error(run_id: str) -> str | None:
    """Driver pre-flight (R1): None when the run is review-accepted
    under exactly the predicate prepare_integration enforces;
    otherwise the Korean-logged reason. Read-only."""
    try:
        state = store.load(run_id)
    except SystemExit:
        return f"unknown run '{run_id}'"
    except Exception as exc:  # noqa: BLE001 - unreadable never acts
        return f"run record unreadable: {exc}"[:200]
    _, _, error = _candidate_proof(state, run_id)
    return error


# Diagnostic leaf causes: bounded nested-failure context.
#
# Readiness decisions are unchanged by everything below: these helpers
# only format the already-computed sub-result (never read, never
# write) and only add bounded text when a result is already
# not-ready. A missing/unreadable source still fails closed as
# "unreadable"/"unknown" — never borrowed success. Proven defect fixed
# here is the loss of the underlying reason; intermittent triggers
# (I/O, resource pressure, concurrent edits, fingerprint read
# failures) remain unconfirmed and are NOT claimed fixed.
_LEAF_REASON_CHARS = 160
_LEAF_CAUSES_MAX = 3
_LEAF_TOTAL_CHARS = 500


def _bound_text(text, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)].rstrip() + "…"


def _join_leaf(head: str, deeper: str) -> str | None:
    """Wrapper plus grandchild leaf; the deepest leaf always survives.

    A long wrapper never consumes the budget hiding the actual leaf:
    the wrapper shrinks first, and when even a stub wrapper cannot fit,
    the tail (deepest cause) is kept with an ellipsis marker.
    """
    if not head:
        return _bound_text(deeper, _LEAF_TOTAL_CHARS) or None
    if not deeper:
        return _bound_text(head, _LEAF_TOTAL_CHARS) or None
    sep = " <- "
    if len(head) + len(sep) + len(deeper) <= _LEAF_TOTAL_CHARS:
        return head + sep + deeper
    room = _LEAF_TOTAL_CHARS - len(sep) - len(deeper)
    if room >= 24:
        return _bound_text(head, room) + sep + deeper
    tail = deeper[-(_LEAF_TOTAL_CHARS - 2):]
    return "… " + tail


# Bounded list-card diagnostic budgets (read-path observability only).
#
# Everything below formats the SAME already-computed evaluation with the
# SAME already-read live map: no source reads, no batch reads, no extra
# evaluations. Explicit truncation markers are emitted whenever a bound
# cuts content, so a short field is never mistaken for a complete one.
_DIAG_REASONS_MAX = 3
_DIAG_INHERITED_MAX = 3
_DIAG_SNAPSHOTS_MAX = 6
_DIAG_PATH_CHARS = 160

_CLEAN_FAIL_LABEL = "source cleanliness check failed"


def _diag_match_label(recorded_head, recorded_fp, live) -> str:
    """Known-only match class: unreadable, mismatch, or match.

    A differing fingerprint is reported as a mismatch only — never as
    proof of a particular file change. A live entry without any
    readable identity fields is unreadable, never a match, even when
    the recorded side is also empty. No content is dumped here.
    """
    if not isinstance(live, dict):
        return "unreadable"
    live_head = live.get("head") or ""
    live_fp = live.get("fingerprint") or ""
    if not live_head and not live_fp:
        return "unreadable"
    if (str(recorded_head or ""), str(recorded_fp or "")) != (
            str(live_head), str(live_fp)):
        return "mismatch"
    return "match"


def _diag_clean_label(live) -> str:
    """Cleanliness wording without overclaiming.

    clean=false currently means the git-status cleanliness check did
    not pass, which covers both actual worktree dirt and an unreadable
    status read. Label it exactly as a failed check, never as proven
    file edits.
    """
    if isinstance(live, dict) and live.get("clean") is True:
        return "clean"
    return _CLEAN_FAIL_LABEL


def _diag_short12(value) -> str:
    text = str(value or "")
    return text[:12]


_DIAG_DESCEND_DEPTH = 2
_DIAG_DESCEND_MAX = 3


def _bound_diag(diag) -> dict | None:
    """Bounded copy of one result leaf diag for card envelopes.

    Same already-computed evaluation, strings only: no reads, no
    re-evaluation. Category/op stay short bounded text, rc stays an
    int-or-None, paths stay repo-relative and bounded — never stderr,
    file bytes, or secrets.
    """
    if not isinstance(diag, dict):
        return None
    chain_in = diag.get("chain") or []
    chain = [_bound_text(c, 80) for c in chain_in[:8]
             if c] if isinstance(chain_in, list) else []
    rc = diag.get("rc")
    vstat_in = diag.get("vstat")
    vstat = ({str(k)[:24]: str(v)[:24] for k, v in vstat_in.items()}
             if isinstance(vstat_in, dict) else None)
    out = {"chain": chain,
           "key": _bound_text(diag.get("key") or "",
                              _DIAG_PATH_CHARS),
           "kind": _bound_text(diag.get("kind") or "", 24),
           "map_key": _bound_text(diag.get("map_key") or "",
                                  _DIAG_PATH_CHARS),
           "worktree": _bound_text(diag.get("worktree") or "",
                                   _DIAG_PATH_CHARS),
           "path": _bound_text(diag.get("path") or "",
                               _DIAG_PATH_CHARS),
           "reason_code": _bound_text(diag.get("reason_code") or "",
                                      80),
           "category": (_bound_text(diag.get("category") or "", 40)
                        or None) if diag.get("category") is not None
           else None,
           "op": (_bound_text(diag.get("op") or "", _DIAG_PATH_CHARS)
                  or None) if diag.get("op") is not None else None,
           "rc": rc if isinstance(rc, int) else None,
           "expected_head": _diag_short12(diag.get("expected_head")),
           "expected_fp": _diag_short12(diag.get("expected_fp")),
           "actual_head": _diag_short12(diag.get("actual_head")),
           "actual_fp": _diag_short12(diag.get("actual_fp")),
           "clean": (diag.get("clean")
                     if diag.get("clean") is None
                     or isinstance(diag.get("clean"), bool) else None)}
    if vstat is not None:
        out["vstat"] = vstat
    sample_in = diag.get("untracked_sample")
    if isinstance(sample_in, list):
        out["untracked_sample"] = [
            str(x)[:_DIAG_PATH_CHARS] for x in sample_in[:5] if x]
    return out


def card_diagnostic(batch: dict, ev_result: dict | None, live: dict | None,
                    batch_by_id: dict | None = None,
                    envelope_evals: dict | None = None,
                    _depth: int = 0) -> dict:
    """Bounded failure context from the SAME computed evaluation+live map.

    Returns {"ready": int, "total": int, "first_failure": None | {...}}.
    first_failure carries the first non-ready primary (key/dep/target/
    lane), up to _DIAG_REASONS_MAX bounded reasons, the result's own
    bounded leaf, up to _DIAG_INHERITED_MAX inherited entries, up to
    _DIAG_SNAPSHOTS_MAX recorded-vs-live snapshot comparisons with
    explicit omission counts, and up to _DIAG_DESCEND_MAX nested
    descendant diagnostics. Descendants reuse only same-envelope
    evaluations already computed by the caller (never a new evaluate,
    load, or source read): when the cause rides an inherited batch, the
    nested entries name the failing descendant batch/primary and its
    failing source component, or carry an explicit truncation marker.
    Snapshot budget prioritizes non-matching evidence over matching
    wrapper snapshots. Every string field is bounded; absent live
    fingerprints stay empty, never the literal "None".
    """
    results = (ev_result or {}).get("results") if isinstance(
        ev_result, dict) else None
    results = results if isinstance(results, list) else []
    ready = sum(1 for r in results if isinstance(r, dict) and r.get("ready"))
    total = len(results)
    first = next((r for r in results
                  if isinstance(r, dict) and not r.get("ready")), None)
    if first is None:
        return {"ready": ready, "total": total, "first_failure": None}
    reasons_in = first.get("reasons") if isinstance(
        first.get("reasons"), list) else []
    reasons = [_bound_text(r, _LEAF_REASON_CHARS)
               for r in reasons_in[:_DIAG_REASONS_MAX] if r]
    reasons_omitted = max(0, len([r for r in reasons_in if r]) - len(reasons))
    inherited_in = first.get("inherited") if isinstance(
        first.get("inherited"), list) else []
    inherited = []
    for h in inherited_in[:_DIAG_INHERITED_MAX]:
        if not isinstance(h, dict):
            continue
        inherited.append({
            "batch": _bound_text(h.get("batch") or "",
                                 _DIAG_PATH_CHARS),
            "ready": h.get("ready"), "total": h.get("total"),
            "stale": bool(h.get("stale")),
            "cause": _bound_text(h.get("cause") or "",
                                 _LEAF_TOTAL_CHARS),
        })
    inherited_omitted = max(0, len(inherited_in) - len(inherited))
    snapshots = _diag_snapshots(batch, first, live or {},
                                batch_by_id or {})
    descendants = _diag_descendants(
        inherited_in, live or {}, batch_by_id or {},
        envelope_evals or {}, _depth)
    return {"ready": ready, "total": total,
            "first_failure": {
                "key": _bound_text(first.get("key") or "",
                                   _DIAG_PATH_CHARS),
                "dep": _bound_text(first.get("dep") or "",
                                   _DIAG_PATH_CHARS),
                "target": _bound_text(first.get("target") or "",
                                      _DIAG_PATH_CHARS),
                "lane": _bound_text(first.get("lane") or "",
                                    _DIAG_PATH_CHARS),
                "reasons": reasons, "reasons_omitted": reasons_omitted,
                "leaf": _bound_text(first.get("leaf") or "",
                                    _LEAF_TOTAL_CHARS),
                "diag": _bound_diag(first.get("diag")),
                "inherited": inherited,
                "inherited_omitted": inherited_omitted,
                "snapshots": snapshots["entries"],
                "snapshots_omitted": snapshots["omitted"],
                "descendants": descendants["entries"],
                "descendants_omitted": descendants["omitted"],
            }}


def _diag_descendants(inherited_in: list, live: dict, batch_by_id: dict,
                      envelope_evals: dict, depth: int) -> dict:
    """Nested same-envelope failure context for stale inherited refs.

    Only reuses evaluations the caller already computed for this
    envelope (no evaluate/load/source reads). Entries that cannot be
    resolved from the envelope carry an explicit truncation marker
    instead of a fabricated cause. Depth is capped at
    _DIAG_DESCEND_DEPTH; deeper chains are named with a truncation
    marker, never silently dropped.
    """
    entries: list = []
    omitted = 0
    stale = [h for h in inherited_in
             if isinstance(h, dict) and h.get("stale")]
    for h in stale[:_DIAG_DESCEND_MAX]:
        ref = h.get("batch") or ""
        if not ref:
            continue
        ref_ev = envelope_evals.get(ref)
        ref_batch = batch_by_id.get(ref)
        if depth >= _DIAG_DESCEND_DEPTH:
            entries.append({
                "batch": _bound_text(ref, _DIAG_PATH_CHARS),
                "provenance": "parent recursion cause only; deeper "
                              "envelope reuse truncated",
                "truncated": "deeper diagnostic truncated at depth "
                             f"{_DIAG_DESCEND_DEPTH}",
                "cause": _bound_text(h.get("cause") or "",
                                     _LEAF_TOTAL_CHARS),
            })
            continue
        if not isinstance(ref_ev, dict) or not isinstance(ref_batch,
                                                           dict):
            entries.append({
                "batch": _bound_text(ref, _DIAG_PATH_CHARS),
                "provenance": "parent recursion cause only; no "
                              "same-envelope card evaluation reused",
                "truncated": "recorded binding or same-envelope "
                             "evaluation unavailable (truncated)",
                "cause": _bound_text(h.get("cause") or "",
                                     _LEAF_TOTAL_CHARS),
            })
            continue
        nested = card_diagnostic(ref_batch, ref_ev, live, batch_by_id,
                                 envelope_evals, _depth=depth + 1)
        nested_ready = nested.get("ready")
        nested_total = nested.get("total")
        child_ready = (isinstance(nested_total, int)
                       and nested_total > 0
                       and nested_ready == nested_total)
        entry = {"batch": _bound_text(ref, _DIAG_PATH_CHARS),
                 "provenance": "same-envelope card evaluation: a "
                               "separate computation from the parent "
                               "recursion, formatted with the same live "
                               "map (no re-evaluation, no new reads)",
                 "cause": _bound_text(h.get("cause") or "",
                                      _LEAF_TOTAL_CHARS),
                 "ready": nested_ready, "total": nested_total,
                 "first_failure": nested.get("first_failure"),
                 "disagreement": None}
        if child_ready:
            # The parent recursion observed this batch not fully
            # proven. A ready same-envelope card is a LATER separate
            # observation (run/evidence reads can move between the
            # two computations): it never clears or explains the
            # parent's ORIGINAL failure, whose cause above stays
            # authoritative.
            entry["disagreement"] = (
                "observation disagreement: parent recursion saw "
                f"{ref} not fully proven ({h.get('ready')}/"
                f"{h.get('total')} ready), but the same-envelope card "
                "evaluation is ready; original parent cause retained")
        entries.append(entry)
    omitted = max(0, len(stale) - len(entries))
    return {"entries": entries, "omitted": omitted}


def _diag_snapshots(batch: dict, result: dict, live: dict,
                    batch_by_id: dict) -> dict:
    """Recorded-vs-live comparisons from the SAME already-read map.

    Candidates are collected first, then non-matching evidence is
    budgeted ahead of matching wrapper snapshots, so a failing
    component is never crowded out by parent matches. Absent live
    fingerprints stay empty, never the literal "None".
    """
    collected: list = []

    def _push(entry: dict) -> None:
        collected.append(entry)

    lane = result.get("lane") or ""
    for integ in (batch.get("integrations") or []):
        if not isinstance(integ, dict):
            continue
        if (integ.get("lane") or "") != lane:
            continue
        wt = integ.get("worktree") or ""
        snap = live.get("wt:" + wt) if wt else None
        _push({"scope": "integration", "lane": _bound_text(lane, 80),
               "worktree": _bound_text(wt, _DIAG_PATH_CHARS),
               "recorded_head": _diag_short12(integ.get("head")),
               "recorded_fingerprint": str(integ.get("fingerprint") or ""),
               "live_head": _diag_short12(
                   (snap or {}).get("head") if isinstance(snap, dict)
                   else ""),
               "live_fingerprint": str(((snap or {}).get("fingerprint")
                                        if isinstance(snap, dict) else "")
                                       or ""),
               "match": _diag_match_label(integ.get("head"),
                                          integ.get("fingerprint"), snap),
               "clean": _diag_clean_label(snap)})
    key = result.get("key") or ""
    for p in (batch.get("primaries") or []):
        if not isinstance(p, dict):
            continue
        pkey = primary_result_key(batch.get("repo") or "", p)
        if pkey is None or pkey != key:
            continue
        src = p.get("source") if isinstance(p.get("source"), dict) else {}
        wt = src.get("worktree") or ""
        snap = live.get("cand:" + wt) if wt else None
        _push({"scope": "candidate", "lane": _bound_text(lane, 80),
               "worktree": _bound_text(wt, _DIAG_PATH_CHARS),
               "recorded_head": _diag_short12(src.get("head")),
               "recorded_fingerprint": str(src.get("fingerprint") or ""),
               "live_head": _diag_short12(
                   (snap or {}).get("head") if isinstance(snap, dict)
                   else ""),
               "live_fingerprint": str(((snap or {}).get("fingerprint")
                                        if isinstance(snap, dict) else "")
                                       or ""),
               "match": _diag_match_label(src.get("head"),
                                          src.get("fingerprint"), snap),
               "clean": _diag_clean_label(snap)})
    origin = batch.get("origin") if isinstance(batch.get("origin"),
                                              dict) else {}
    origin_co = origin.get("checkout") or ""
    base_branch = origin.get("base_branch") or ""
    recorded_base = origin.get("base_head") or ""
    base_live = live.get(_base_key(origin_co, base_branch)) \
        if origin_co and base_branch else None
    _push({"scope": "base",
           "worktree": _bound_text(
               (origin_co + " [" + base_branch + "]") if origin_co
               else base_branch, _DIAG_PATH_CHARS),
           "recorded_head": _diag_short12(recorded_base),
           "recorded_fingerprint": "",
           "live_head": _diag_short12(
               (base_live or {}).get("head")
               if isinstance(base_live, dict) else ""),
           "live_fingerprint": "",
           "match": ("unreadable" if not isinstance(base_live, dict)
                     else ("match" if (base_live.get("head") or "")
                           == str(recorded_base or "") else "mismatch")),
           "clean": "n/a"})
    for h in (result.get("inherited") or []):
        if not isinstance(h, dict):
            continue
        ref = h.get("batch") or ""
        if not ref:
            continue
        target = batch_by_id.get(ref)
        if not isinstance(target, dict):
            _push({"scope": "inherited",
                   "worktree": _bound_text(ref, _DIAG_PATH_CHARS),
                   "recorded_head": "", "recorded_fingerprint": "",
                   "live_head": "", "live_fingerprint": "",
                   "match": "recorded binding unavailable in list "
                            "scope (truncated)",
                   "clean": "n/a"})
            continue
        shown = 0
        for integ in (target.get("integrations") or []):
            if not isinstance(integ, dict) or shown >= 1:
                continue
            shown += 1
            wt = integ.get("worktree") or ""
            snap = live.get("wt:" + wt) if wt else None
            _push({"scope": "inherited",
                   "worktree": _bound_text(
                       ref + " :: " + (integ.get("lane") or "") + " :: "
                       + wt, _DIAG_PATH_CHARS),
                   "recorded_head": _diag_short12(integ.get("head")),
                   "recorded_fingerprint": str(
                       integ.get("fingerprint") or ""),
                   "live_head": _diag_short12(
                       (snap or {}).get("head")
                       if isinstance(snap, dict) else ""),
                   "live_fingerprint": str(
                       ((snap or {}).get("fingerprint")
                        if isinstance(snap, dict) else "") or ""),
                   "match": _diag_match_label(
                       integ.get("head"), integ.get("fingerprint"), snap),
                   "clean": _diag_clean_label(snap)})
    ordered = sorted(collected,
                     key=lambda e: 0 if e.get("match") != "match" else 1)
    entries = ordered[:_DIAG_SNAPSHOTS_MAX]
    omitted = max(0, len(ordered) - len(entries))
    return {"entries": entries, "omitted": omitted}


def _cause_from_sub(ref: str, sub: dict | None) -> str | None:
    """Bounded failing-leaf text for one inherited batch evaluation.

    Uses ONLY the already-computed sub-evaluation: no source reads, no
    batch reads, no extra evaluations — so this reports the cause of
    the evaluation being shown, never a later re-observation. The
    grandchild context rides the sub-result's own bounded "leaf" field,
    which the existing recursion already computed with the same live
    map. Returns None when the sub-evaluation itself failed to compute
    (the caller's own reasons already cover that case).
    """
    results = (sub or {}).get("results") if isinstance(sub, dict) else None
    if not results:
        return None
    for r in results:
        if not isinstance(r, dict) or r.get("ready"):
            continue
        first = next((x for x in (r.get("reasons") or []) if x), "")
        context = "[%s %s %s %s]" % (ref, r.get("dep"), r.get("target"),
                                     r.get("lane"))
        head = (_bound_text(context, _LEAF_REASON_CHARS) + " "
                + _bound_text(first, _LEAF_REASON_CHARS)).strip()
        return _join_leaf(head, r.get("leaf") or "")
    return None

BATCH_ID_RE = re.compile(r"[a-z0-9][a-z0-9\-_]{0,63}")
MAX_IMPORT_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024

# Only review-terminal dependency items can surface as unvalidated.
UNCOVERED_STATUSES = {"waiting-review"}


class BatchError(Exception):
    """Invalid import payload. Message is safe to display."""


class ReviewValidationError(BatchError):
    """A review verdict's input shape is wrong (verdict value,
    findings shape/length/count). A BatchError subclass so old
    `except BatchError` paths still catch it; the review endpoint
    maps it to 400 (client shape), while plain BatchError stays
    409 (state conflicts: unknown batch/run, moved source,
    stale proof)."""


def batches_dir() -> Path:
    return paths.home() / "batches"


def batch_file(batch_id: str) -> Path:
    _check_id(batch_id)
    return batches_dir() / (batch_id + ".json")


def evidence_dir(batch_id: str) -> Path:
    _check_id(batch_id)
    return batches_dir() / batch_id / "evidence"


def _check_id(batch_id: str) -> None:
    if not isinstance(batch_id, str) or not BATCH_ID_RE.fullmatch(batch_id):
        raise BatchError(f"invalid batch id: {batch_id!r}")


def _check_repo(name: str, known: set | None = None) -> None:
    if not isinstance(name, str) or not name:
        raise BatchError("batch.repo is required")
    known_repos = known if known is not None else set(repos.list_all())
    if name not in known_repos:
        raise BatchError(f"unknown repo: {name!r}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def candidate_key(repo: str, dep: str, target: str, lane: str) -> str:
    """Stable identity for one reviewed candidate across queue/runs/batches."""
    return "|".join([str(repo or ""), str(dep or ""), str(target or ""),
                     str(lane or "")])


def find_candidate_batch(repo: str, dep: str, target: str,
                         lane: str) -> str:
    """Newest batch id already carrying this candidate as a primary,
    or "" (read-only). M2.4c: the batch stage skips duplicates
    instead of blocking on them."""
    want = candidate_key(repo, dep, target, lane)
    try:
        files = sorted(batches_dir().glob("*.json"))
    except OSError:
        return ""
    found = ""
    found_at = ""
    for path in files:
        if path.name == "index.json":
            continue
        try:
            batch = load_batch(path.stem)
        except Exception:  # noqa: BLE001 - one bad file skips it
            continue
        if (batch.get("repo") or "") != repo:
            continue
        hit = any(
            isinstance(primary, dict)
            and primary_result_key(repo, primary) == want
            for primary in batch.get("primaries") or [])
        if hit and str(batch.get("updated_at") or "") >= found_at:
            found = str(batch.get("id") or path.stem)
            found_at = str(batch.get("updated_at") or "")
    return found


def joint_candidate_clash(repo: str, lane: str, rec: dict) -> str:
    """M2.17f: newest batch holding a member key or run of this
    done joint that is NOT superseded by it, or "".

    The stage pre-check: a non-superseded clash skips with the
    dup note instead of walking into the import refusal (and the
    lane cooldown). Holders the supersede map marks replaced by
    this joint op never clash here — the import gate's held
    precondition decides those. Read-only; never raises."""
    try:
        rec = rec or {}
        if rec.get("kind") != "joint" \
                or rec.get("op") != "reresolve" \
                or str(rec.get("status") or "") != "done":
            return ""
        op_id = str(rec.get("op_id") or "")
        if not op_id:
            return ""
        cands = [c for c in (rec.get("joint_candidates") or [])
                 if isinstance(c, dict)]
        if not cands:
            return ""
        try:
            sup = _joint_supersede_map() or {}
        except (Exception, SystemExit):  # noqa: BLE001 - no map
            sup = {}
        want_keys = set()
        want_runs = set()
        for c in cands:
            want_keys.add(candidate_key(
                repo, c.get("dep"), c.get("target"), lane))
            rid = str(c.get("run_id") or c.get("run") or "")
            if rid:
                want_runs.add(rid)
        found = ""
        found_at = ""
        try:
            files = sorted(batches_dir().glob("*.json"))
        except OSError:
            return ""
        for path in files:
            if path.name == "index.json":
                continue
            try:
                batch = load_batch(path.stem)
            except Exception:  # noqa: BLE001 - one bad file skips it
                continue
            if (batch.get("repo") or "") != repo:
                continue
            bid = str(batch.get("id") or path.stem)
            if (sup or {}).get(bid) == op_id:
                continue
            hit = any(
                isinstance(p, dict)
                and (primary_result_key(repo, p) in want_keys
                     or str(p.get("run_id") or "") in want_runs)
                for p in batch.get("primaries") or [])
            if hit and str(batch.get("updated_at") or "") >= found_at:
                found = bid
                found_at = str(batch.get("updated_at") or "")
        return found
    except (Exception, SystemExit):
        return ""


def _git_head_diag(cwd: str, rev: str) -> tuple:
    """(head_or_None, ok). _git_head() === [0]; ok=False keeps no reason."""
    argv = ["git", "rev-parse", "--verify", rev]
    try:
        with git_readonly_child_env(argv, cwd=cwd) as child_env:
            out = subprocess.run(argv,
                                 cwd=str(cwd), stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=60,
                                 text=True, env=child_env)
    except (OSError, subprocess.SubprocessError):
        return None, False
    if out.returncode != 0:
        return None, False
    head = out.stdout.strip()
    return (head or None), bool(head)


def _git_head(cwd: str, rev: str) -> str | None:
    head, _ = _git_head_diag(cwd, rev)
    return head


def _snapshot_status(worktree: str) -> tuple:
    """(core_snap_or_None, category, op, rc). _snapshot() === core + clean.

    Categories are a fixed enum (missing_dir, snapshot_error,
    source_unreadable, empty_identity); only the SourceUnreadable op/rc
    pair travels outward — never stderr text, file bytes, or secrets.
    """
    if not worktree or not Path(worktree).is_dir():
        return None, "missing_dir", None, None
    try:
        snap = runner.source_snapshot(Path(worktree))
    except runner.SourceUnreadable as exc:
        return None, "source_unreadable", exc.op, exc.rc
    except (OSError, ValueError):
        return None, "snapshot_error", None, None
    if not isinstance(snap, dict):
        return None, "snapshot_error", None, None
    head = snap.get("head") or ""
    fingerprint = snap.get("fingerprint") or ""
    if head in ("", "unknown") or not fingerprint:
        return None, "empty_identity", None, None
    sample = [str(x)[:_DIAG_PATH_CHARS]
              for x in (snap.get("untracked") or [])[:5] if x]
    return {"head": head, "fingerprint": fingerprint,
            "untracked_sample": sample}, None, None, None


def _snapshot(worktree: str) -> dict | None:
    """Actual-bytes source snapshot (runner contract) or None unreadable."""
    snap, _, _, _ = _snapshot_status(worktree)
    if snap is None:
        return None
    return {"head": snap["head"], "fingerprint": snap["fingerprint"],
            "clean": _source_clean(worktree)}


def _git_text(args, cwd) -> str | None:
    argv = ["git"] + list(args)
    try:
        with git_readonly_child_env(argv, cwd=cwd) as child_env:
            out = subprocess.run(argv, cwd=str(cwd),
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL,
                                 timeout=60, text=True,
                                 env=child_env)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _git_lines(args, cwd) -> list | None:
    """Porcelain output as raw lines: leading XY spaces preserved.

    _git_text strips the whole output, which corrupts fixed-width
    porcelain columns (" M file" -> "M file"); status parsing must use
    these raw lines instead.
    """
    argv = ["git"] + list(args)
    try:
        with git_readonly_child_env(argv, cwd=cwd) as child_env:
            out = subprocess.run(argv, cwd=str(cwd),
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL,
                                 timeout=60, text=True,
                                 env=child_env)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n").splitlines()


def _source_clean_diag(worktree: str) -> tuple:
    """(clean, status_ok, offender). _source_clean() === [0].

    offender is the first repo-relative path (bounded to _DIAG_PATH_CHARS)
    that failed the check, or "" when the status read itself failed
    (status_ok=False) or the tree is clean. Only the path travels
    outward — never status text or file bytes.
    """
    from . import runner
    lines = _git_lines(["-c", "core.quotePath=false", "status",
                        "--porcelain=v1", "--untracked-files=all"], worktree)
    if lines is None:
        return False, False, ""
    for line in lines:
        if len(line) < 4:
            continue
        xy, rel = line[:2], line[3:].strip().strip('"')
        if xy == "??":
            if runner._noise_rel(rel):
                continue
            if _git_text(["check-ignore", "-q", "--", rel],
                         worktree) is not None:
                continue
            return False, True, rel[:_DIAG_PATH_CHARS]
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        if runner._noise_rel(rel):
            continue
        return False, True, rel[:_DIAG_PATH_CHARS]
    return True, True, ""


def _source_clean(worktree: str):
    """Source-aware cleanliness under the runner noise contract.

    Reserved generated/cache paths (node_modules, .openmainty-cache,
    …) and git-ignored files never count as source dirt: the content
    fingerprint already excludes them, so cache-only activity must not
    block readiness. Tracked modifications, relevant new untracked
    source, and unreadable status block (False); unresolvable git
    status is fail-closed (False), never a silent clean.
    """
    clean, _, _ = _source_clean_diag(worktree)
    return clean


def load_batch(batch_id: str) -> dict:
    data = read_json(batch_file(batch_id), default=None)
    if data is None:
        raise BatchError(f"unknown batch: {batch_id!r}")
    return data


def list_batches() -> list:
    d = batches_dir()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name == "index.json":
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda b: b.get("updated_at", ""), reverse=True)
    return out


def _safe_basename(origin: str) -> str:
    base = Path(origin).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:120] or "evidence"
    return base


def _resolve_origin(origin: str, allowed_roots: list | None) -> Path:
    """Resolve an evidence origin. allowed_roots=None is operator-trusted
    local (CLI) use; any scoped caller passes explicit roots. Symlinks are
    fully resolved and the final target must be a regular file in scope."""
    if not isinstance(origin, str) or not origin or len(origin) > 1024:
        raise BatchError("evidence origin must be a path (<=1024 chars)")
    src = Path(origin)
    if not src.is_absolute():
        raise BatchError(f"evidence origin must be absolute: {origin!r}")
    try:
        resolved = src.resolve()
    except OSError:
        raise BatchError(f"evidence origin unreadable: {origin!r}")
    if not resolved.is_file():
        raise BatchError(f"evidence origin is not a file: {origin!r}")
    if allowed_roots is not None:
        for root in allowed_roots:
            try:
                resolved.relative_to(Path(root).resolve())
                break
            except (OSError, ValueError):
                continue
        else:
            raise BatchError("evidence origin outside supported scope")
    return resolved


def _validate_policy(policy) -> dict:
    """Explicit validated gate catalog. Unknown/empty is rejected."""
    if not isinstance(policy, dict):
        raise BatchError("policy must be an object")
    required = policy.get("required_gates") or []
    catalog = policy.get("gates") or {}
    if not isinstance(required, list) or not required:
        raise BatchError("policy.required_gates must be a non-empty list")
    if not isinstance(catalog, dict) or not catalog:
        raise BatchError("policy.gates catalog must be a non-empty object")
    clean_catalog = {}
    for name, spec in catalog.items():
        if not isinstance(name, str) or not name or len(name) > 80:
            raise BatchError(f"policy.gates has invalid gate name: {name!r}")
        if not isinstance(spec, dict):
            raise BatchError(f"policy.gates[{name!r}] must be an object")
        clean_catalog[name] = {
            "label": str(spec.get("label") or name)[:200],
            "root": bool(spec.get("root", False)),
            "runtime": bool(spec.get("runtime", False)),
        }
    for name in required:
        if name not in clean_catalog:
            raise BatchError(
                f"policy.required_gates has unknown gate: {name!r}")
    return {"required_gates": list(required),
            "gates": clean_catalog,
            "notes": str(policy.get("notes") or "")[:2000]}


def _queue_by_run(repo: str) -> tuple:
    try:
        queue = sched.load(repo).get("queue", [])
    except SystemExit:
        queue = []
    by_run = {}
    for it in queue or []:
        if isinstance(it, dict) and it.get("run_id"):
            by_run.setdefault(it["run_id"], it)
    return queue, by_run


def _validate_issue_primary(p, i: int, repo: str, by_run: dict,
                            seen_keys: set, seen_runs: set,
                            origin_checkout: str = "") -> dict:
    """Validate a typed issue primary against app-owned records only.

    The manifest's dep/target must be empty (mixed identity refuses);
    the issue block carries the strict number, the parent repo and a
    known lane equal to the primary lane, with the canonical URL derived
    from the registered profile (never a copied payload URL). Admission
    reuses _resolve_issue_candidate (queue kind, run/repo/issue/branch/
    lane agreement — no missing-queue fallback).

    The MINIMAL EXISTING CONTRACT: primary.source binds the ORIGINAL
    candidate (canonical store.run_dir(run_id)/"work" + the queue-owned
    original branch + the recorded run workCommit/workTreeFingerprint),
    with the unchanged _candidate_proof/fresh-review gates. The
    prepared integration binds separately at import level
    (_bind_issue_primaries) against the exact op record; review gates
    on integrated bytes stay pending and are never borrowed from the
    original/base. Legacy dep behavior is untouched.
    """
    lane = str(p.get("lane") or "")
    blk = p.get("issue")
    if not isinstance(blk, dict):
        raise BatchError(f"primaries[{i}] carries empty dep/target with "
                         "no valid issue block")
    if str(p.get("dep") or "") or str(p.get("target") or ""):
        raise BatchError(f"primaries[{i}] mixes dep/target with an "
                         "issue block")
    number = _strict_issue_number(blk.get("issue"))
    if number is None:
        raise BatchError(f"primaries[{i}].issue.issue is not a positive "
                         "ASCII number")
    if blk.get("repo") != repo:
        raise BatchError(f"primaries[{i}].issue.repo is not this batch's "
                         "repo")
    if blk.get("lane") != lane or lane not in _KNOWN_LANES:
        raise BatchError(f"primaries[{i}] issue lane is unknown or does "
                         "not match the primary lane")
    key = issue_candidate_key(repo, lane, number)
    if key in seen_keys:
        raise BatchError(f"primaries[{i}] duplicates candidate {key!r}")
    seen_keys.add(key)
    review = p.get("review") or {}
    if not isinstance(review, dict):
        raise BatchError(f"primaries[{i}].review must be an object")
    verdict = str(review.get("verdict") or "pending")
    if verdict not in REVIEW_VERDICTS:
        raise BatchError(f"primaries[{i}].review.verdict must be one of "
                         f"{sorted(REVIEW_VERDICTS)}")
    run_id = str(p.get("run_id") or "")
    if not run_id:
        raise BatchError(f"primaries[{i}].run_id is required: bind every "
                         "primary to its real review run")
    if run_id in seen_runs:
        raise BatchError(f"primaries[{i}] reuses run {run_id!r}")
    seen_runs.add(run_id)
    # Admission: the exact original run issue as well as the queue.
    try:
        ctx = _resolve_issue_candidate(repo, run_id)
    except IntegrationError as exc:
        raise BatchError(f"primaries[{i}] issue admission refused: "
                         f"{exc}") from exc
    if ctx["number"] != number or ctx["lane"] != lane:
        raise BatchError(f"primaries[{i}] issue does not match the "
                         f"admitted queue/run identity for run {run_id!r}")
    # Canonical URL from the registered profile, never a copied URL.
    if str(blk.get("url") or "") != ctx["url"]:
        raise BatchError(f"primaries[{i}].issue.url is not the registered "
                         "canonical issue URL")
    try:
        run = store.load(run_id)
    except (Exception, SystemExit) as exc:
        raise BatchError(f"primaries[{i}] references unreadable run "
                         f"{run_id!r}: {exc}")
    if not isinstance(run, dict) or not run:
        raise BatchError(f"primaries[{i}] run {run_id!r} is not a stored "
                         "run")
    source = p.get("source") or {}
    if not isinstance(source, dict):
        raise BatchError(f"primaries[{i}].source must be an object")
    # Original candidate binding: canonical run work dir, queue-owned
    # original branch, recorded original head/fingerprint.
    try:
        canon_wt = str(store.run_dir(run_id) / "work")
    except (Exception, SystemExit) as exc:
        raise BatchError(f"primaries[{i}] canonical candidate path "
                         f"unresolvable: {exc}")
    if str(source.get("worktree") or "") != canon_wt:
        raise BatchError(f"primaries[{i}].source.worktree must be the "
                         "canonical original candidate "
                         f"{canon_wt!r}, not another checkout")
    if str(source.get("branch") or "") != ctx["branch"]:
        raise BatchError(f"primaries[{i}].source.branch must equal the "
                         f"queue-owned original branch "
                         f"{ctx['branch']!r}")
    orig_head = str(run.get("workCommit") or "")
    orig_fp = str(run.get("workTreeFingerprint") or "")
    if not orig_head or not orig_fp:
        raise BatchError(f"primaries[{i}] run {run_id!r} has no recorded "
                         "original source identity")
    if str(source.get("head") or "") != orig_head \
            or str(source.get("fingerprint") or "") != orig_fp:
        raise BatchError(f"primaries[{i}] source identity must equal the "
                         "recorded original candidate head/fingerprint; "
                         "it is not re-read or rebound at import")
    if origin_checkout:
        if _shared_git_dir(canon_wt) != _shared_git_dir(origin_checkout):
            raise BatchError(
                f"primaries[{i}].source.worktree is not in the same repo "
                "as the batch origin checkout")
    snap = _snapshot(canon_wt)
    if snap is None:
        raise BatchError(f"primaries[{i}] candidate source unreadable")
    if snap["head"] != orig_head or snap["fingerprint"] != orig_fp:
        raise BatchError(
            f"primaries[{i}] candidate source moved or dirty since review "
            "(expected "
            f"{orig_head[:12]}/{orig_fp[:12]}, live {snap['head'][:12]}/"
            f"{snap['fingerprint'][:12]}): re-review, do not rebind")
    vstat = store.verification_status(run, snap["fingerprint"],
                                      snap["head"])
    if verdict in ("accept", "accept-with-notes"):
        bad = _accept_proof_gaps(vstat)
        if bad:
            raise BatchError(
                f"primaries[{i}] run {run_id!r} review proof not fresh: "
                + ", ".join(f"{s}={vstat[s]}" for s in bad))
    item = by_run.get(run_id) or {}
    return {"dep": "", "target": "", "lane": lane,
            "run_id": run_id,
            "issue": {"issue": number, "repo": repo, "lane": lane,
                      "url": ctx["url"]},
            "run_binding": {"run_id": run_id, "matched": True,
                            "queue_key": str(item.get("key") or ""),
                            "branch": ctx["branch"],
                            "verification": vstat},
            "source": {"worktree": canon_wt,
                       "branch": ctx["branch"],
                       "head": orig_head, "fingerprint": orig_fp},
            "review": {"verdict": verdict,
                       "at": str(review.get("at") or ""),
                       "findings": [str(x)[:500] for x in
                                    (review.get("findings") or [])][:50]},
            "required_gates": [str(x)[:80] for x in
                               (p.get("required_gates") or [])][:40],
            "note": str(p.get("note") or "")[:1000]}


def _validate_primary(p, i: int, repo: str, by_run: dict,
                      seen_keys: set, seen_runs: set,
                      origin_checkout: str = "") -> dict:
    if not isinstance(p, dict):
        raise BatchError(f"primaries[{i}] must be an object")
    dep = str(p.get("dep") or "")
    target = str(p.get("target") or "")
    if (not dep and not target) or isinstance(p.get("issue"), dict):
        # Typed issue primary (empty dep/target, or an explicit issue
        # block which a mixed dep/target record cannot satisfy).
        return _validate_issue_primary(p, i, repo, by_run, seen_keys,
                                       seen_runs, origin_checkout)
    lane = str(p.get("lane") or "")
    for field, val in (("dep", dep), ("target", target), ("lane", lane)):
        if not val or len(val) > 200:
            raise BatchError(f"primaries[{i}].{field} is required")
    key = candidate_key(repo, dep, target, lane)
    if key in seen_keys:
        raise BatchError(f"primaries[{i}] duplicates candidate {key!r}")
    seen_keys.add(key)
    review = p.get("review") or {}
    if not isinstance(review, dict):
        raise BatchError(f"primaries[{i}].review must be an object")
    verdict = str(review.get("verdict") or "pending")
    if verdict not in REVIEW_VERDICTS:
        raise BatchError(f"primaries[{i}].review.verdict must be one of "
                         f"{sorted(REVIEW_VERDICTS)}")
    run_id = str(p.get("run_id") or "")
    if not run_id:
        raise BatchError(f"primaries[{i}].run_id is required: bind every "
                         "primary to its real review run")
    if run_id in seen_runs:
        raise BatchError(f"primaries[{i}] reuses run {run_id!r}")
    seen_runs.add(run_id)
    try:
        run = store.load(run_id)
    except SystemExit:
        raise BatchError(f"primaries[{i}] references unknown run {run_id!r}")
    if not isinstance(run, dict) or run.get("repo") != repo:
        raise BatchError(f"primaries[{i}] run {run_id!r} is not owned by "
                         f"repo {repo!r}")
    item = by_run.get(run_id)
    if item is None:
        raise BatchError(f"primaries[{i}] run {run_id!r} has no queue "
                         "metadata to bind dep/target/lane/branch")
    for field, want in (("dep", dep), ("target", target), ("lane", lane)):
        if str(item.get(field) or "") != want:
            raise BatchError(
                f"primaries[{i}] {field} {want!r} does not match run "
                f"{run_id!r} queue metadata")
    branch = str(item.get("branch") or "")
    if not branch:
        raise BatchError(f"primaries[{i}] queue item has no branch")
    source = p.get("source") or {}
    if not isinstance(source, dict):
        raise BatchError(f"primaries[{i}].source must be an object")
    worktree = str(source.get("worktree") or "")
    if not worktree or not Path(worktree).is_dir():
        raise BatchError(f"primaries[{i}].source.worktree must be a live "
                         "checkout holding the candidate source")
    if str(source.get("branch") or "") != branch:
        raise BatchError(f"primaries[{i}].source.branch must equal the "
                         f"owned queue branch {branch!r}, not an arbitrary "
                         "payload branch")
    actual_branch = _git_text(["rev-parse", "--abbrev-ref", "HEAD"],
                              worktree)
    if actual_branch != branch:
        raise BatchError(
            f"primaries[{i}].source.worktree is actually on "
            f"{actual_branch!r}, not the owned queue branch {branch!r}")
    if origin_checkout:
        if _shared_git_dir(worktree) != _shared_git_dir(origin_checkout):
            raise BatchError(
                f"primaries[{i}].source.worktree is not in the same repo "
                "as the batch origin checkout")
    # Recorded identities are the REVIEWED expectations, never the live
    # observation: a stale ACCEPT rebased onto moved/dirty bytes by a
    # reimport must be rejected, not silently rebound.
    exp_head = str(source.get("head") or "")
    exp_fp = str(source.get("fingerprint") or "")
    if not exp_head or not exp_fp:
        raise BatchError(f"primaries[{i}].source.head/fingerprint are "
                         "required: the review subject identity travels "
                         "with the verdict, it is not re-read at import")
    snap = _snapshot(worktree)
    if snap is None:
        raise BatchError(f"primaries[{i}] candidate source unreadable")
    if snap["head"] != exp_head or snap["fingerprint"] != exp_fp:
        raise BatchError(
            f"primaries[{i}] candidate source moved or dirty since review "
            "(expected "
            f"{exp_head[:12]}/{exp_fp[:12]}, live {snap['head'][:12]}/"
            f"{snap['fingerprint'][:12]}): re-review, do not rebind")
    vstat = store.verification_status(run, snap["fingerprint"],
                                      snap["head"])
    if verdict in ("accept", "accept-with-notes"):
        bad = _accept_proof_gaps(vstat)
        if bad:
            raise BatchError(
                f"primaries[{i}] run {run_id!r} review proof not fresh: "
                + ", ".join(f"{s}={vstat[s]}" for s in bad))
    return {"dep": dep, "target": target, "lane": lane,
            "run_id": run_id,
            "run_binding": {"run_id": run_id, "matched": True,
                            "queue_key": str(item.get("key") or ""),
                            "branch": branch,
                            "verification": vstat},
            "source": {"worktree": worktree, "branch": branch,
                       "head": exp_head, "fingerprint": exp_fp},
            "review": {"verdict": verdict,
                       "at": str(review.get("at") or ""),
                       "findings": [str(x)[:500] for x in
                                    (review.get("findings") or [])][:50]},
            "required_gates": [str(x)[:80] for x in
                               (p.get("required_gates") or [])][:40],
            "note": str(p.get("note") or "")[:1000]}


def _validate_origin(origin) -> dict:
    """Target/base binding. Missing fields are rejected, never skipped."""
    if not isinstance(origin, dict):
        raise BatchError("origin must be an object")
    checkout = str(origin.get("checkout") or "")
    base_branch = str(origin.get("base_branch") or "")
    if not checkout or not Path(checkout).is_dir():
        raise BatchError("origin.checkout must be a live repo checkout")
    if not base_branch:
        raise BatchError("origin.base_branch is required")
    # The reviewed base identity is supplied, never re-read: a moved base
    # rebound by an unrelated-gate reimport must be rejected, not adopted.
    exp_base = str(origin.get("base_head") or "")
    if not exp_base:
        raise BatchError("origin.base_head is required: the reviewed "
                         "target/base identity travels with the receipt")
    base_head = _git_head(checkout, base_branch)
    if not base_head:
        raise BatchError(f"origin base {base_branch!r} unresolvable")
    if base_head != exp_base:
        raise BatchError(
            f"origin base {base_branch!r} moved since review (expected "
            f"{exp_base[:12]}, live {base_head[:12]}): re-review, do not "
            "rebind")
    return {"checkout": checkout, "base_branch": base_branch,
            "base_head": exp_base}


def _validate_inherited(entry, i: int, repo: str, seen: set,
                        chain: tuple) -> dict:
    if not isinstance(entry, dict):
        raise BatchError(f"inherited[{i}] must be an object")
    group = str(entry.get("group") or "")
    ref = str(entry.get("batch") or "")
    if not group or len(group) > 200:
        raise BatchError(f"inherited[{i}].group is required")
    if not ref:
        raise BatchError(f"inherited[{i}].batch is required: inherited "
                         "coverage must name its owning batch")
    if ref in chain:
        raise BatchError(f"inherited[{i}].batch {ref!r} forms a reference "
                         "cycle")
    try:
        target = load_batch(ref)
    except BatchError:
        raise BatchError(f"inherited[{i}].batch references unknown batch "
                         f"{ref!r}")
    if target.get("repo") != repo:
        raise BatchError(f"inherited[{i}].batch {ref!r} belongs to another "
                         "repo")
    actual = len(target.get("primaries") or [])
    if int(entry.get("count") or 0) != actual:
        raise BatchError(f"inherited[{i}].count must equal the referenced "
                         f"batch primary count ({actual})")
    return {"group": group, "count": actual, "batch": ref}


def _evidence_attestor(src) -> str | None:
    """'worker' when a file evidence is worker-backed: a fastcheck
    receipt by name, or JSON content attested by the worker. Either
    signal alone counts (D: the forged-flag refusal holds even with
    no attestor field). Anything else returns None."""
    try:
        name = Path(src).name
    except (TypeError, ValueError):
        return None
    if name.endswith(".fastcheck.json"):
        return "worker"
    # J2: per-step worker logs live next to the receipt as
    # <op>.fastcheck.<n>.log — also worker evidence, never
    # "unrecorded".
    if re.search(r"\.fastcheck\.\d+\.log$", name):
        return "worker"
    try:
        if Path(src).stat().st_size > 1024 * 1024:
            return None
        data = json.loads(Path(src).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and data.get("attestor") == "worker":
        return "worker"
    return None


def import_batch(payload: dict, allowed_roots: list | None = None) -> dict:
    """Validate fully under the repo lock, then write.

    allowed_roots=None is operator-trusted local use (CLI, or the
    dashboard's thin /api/batch/import wrapper). Raises BatchError
    before writing anything when validation fails; evidence files are
    content-addressed (`<sha256>-<basename>`) and never overwritten, so
    a failed reimport cannot corrupt a prior batch, its history, or its
    stored artifacts. Scheduler queue, runs and history are never
    touched.
    """
    if not isinstance(payload, dict):
        raise BatchError("import payload must be an object")
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise BatchError("import payload exceeds size budget")
    batch_id = str(payload.get("id") or "")
    _check_id(batch_id)
    known_repos = set(repos.list_all())
    repo = str(payload.get("repo") or "")
    _check_repo(repo, known_repos)

    # Source13: a typed issue import holds the op exclusion BEFORE the
    # repo lock and retains it through the whole commit, so the live
    # origin/result/receipt checks inside _bind_issue_primaries cannot
    # race validation vs the batch_file write below (the binder's own
    # inner acquisition is a re-entrant no-op here, never a self-409).
    # Dep-only imports keep the previous repo-lock-only behavior.
    raw_prims = payload.get("primaries") or []
    issue_op = None
    if isinstance(raw_prims, list) and any(
            isinstance(p, dict) and isinstance(p.get("issue"), dict)
            for p in raw_prims):
        cand = payload.get("origin_m2")
        if not isinstance(cand, str) or not cand or len(cand) > 200:
            raise BatchError("origin_m2 must be a non-empty string "
                             "(<=200 chars)")
        issue_op = cand
    if issue_op is not None:
        try:
            with _op_exclusion(issue_op):
                with sched.locked(repo):
                    batch = _import_locked(payload, batch_id, repo,
                                           allowed_roots)
        except m2.OpBusy as exc:
            raise BatchError(f"issue op {issue_op!r} is busy; import "
                             "refused") from exc
        except ValueError as exc:
            raise BatchError(
                f"invalid issue op id {issue_op!r}") from exc
    else:
        with sched.locked(repo):
            batch = _import_locked(payload, batch_id, repo, allowed_roots)
    try:
        refresh_all_snapshots("import")
    except Exception:  # noqa: BLE001 - import won; snapshots retry next tick
        pass
    return batch


def _import_locked(payload: dict, batch_id: str, repo: str,
                   allowed_roots: list | None) -> dict:
    queue, by_run = _queue_by_run(repo)
    policy = _validate_policy(payload.get("policy"))
    clean_origin = _validate_origin(payload.get("origin"))
    # M2.2-C: driver-built imports carry their prepare op id here.
    # Origin-m2 imports may never claim Root attestation (refused
    # outright below); manual imports simply carry no marker.
    origin_m2 = payload.get("origin_m2")
    if origin_m2 is not None and (
            not isinstance(origin_m2, str) or not origin_m2
            or len(origin_m2) > 200):
        raise BatchError("origin_m2 must be a non-empty string "
                         "(<=200 chars)")
    is_m2 = bool(origin_m2)
    # M2.14c: a done joint import exempts its held superseded
    # standalone batches (the _joint_supersede_map mapping) from the
    # duplicate seed — otherwise the joint batch can never import
    # while the held standalone exists. Narrow: only a DONE joint
    # op record, only its joint_with partners' auto batch ids, and
    # only when that batch is currently held. Anything else keeps
    # today's refusal.
    joint_exempt: set = set()
    if is_m2:
        try:
            _jrec = read_json(op_file(str(origin_m2)), default=None)
        except (Exception, SystemExit):  # noqa: BLE001 - no exempt
            _jrec = None
        if isinstance(_jrec, dict) and _jrec.get("op") == "reresolve" \
                and _jrec.get("kind") == "joint" \
                and _jrec.get("status") == "done":
            for _y in _jrec.get("joint_with") or []:
                try:
                    joint_exempt.add(
                        m2.batch_id_for_op(str(_y or "")))
                except (Exception, SystemExit):  # noqa: BLE001 - skip
                    continue
            # M2.17f: the old joint batch this op supersedes also
            # yields its keys and runs — same held precondition
            # below, same refusal text. Without it the refreshed
            # joint can never register while its own previous
            # batch still holds a member key.
            try:
                for _bid, _sup in _joint_supersede_map().items():
                    if str(_sup or "") == str(origin_m2 or ""):
                        joint_exempt.add(str(_bid))
            except (Exception, SystemExit):  # noqa: BLE001 - no map
                pass

    primaries_raw = payload.get("primaries") or []
    if not isinstance(primaries_raw, list) or not primaries_raw:
        raise BatchError("primaries must be a non-empty list")
    seen_keys: set = set()
    seen_runs: set = set()
    for other in list_batches():
        if other.get("id") == batch_id or other.get("repo") != repo:
            continue
        if other.get("id") in joint_exempt:
            _hold = other.get("hold") \
                if isinstance(other.get("hold"), dict) else {}
            if str((_hold or {}).get("by") or ""):
                continue
            raise BatchError(
                f"단독 묶음 {other.get('id')}가 보류되지 않아 함께 올림을 "
                "등록할 수 없음 — 먼저 보류하세요")
        for p in other.get("primaries") or []:
            if not isinstance(p, dict):
                continue
            _skey = primary_result_key(repo, p)
            if _skey is not None:
                seen_keys.add(_skey)
        for p in other.get("primaries") or []:
            if p.get("run_id"):
                seen_runs.add(p["run_id"])
    primaries = [_validate_primary(p, i, repo, by_run, seen_keys,
                                     seen_runs,
                                     clean_origin.get("checkout") or "")
                 for i, p in enumerate(primaries_raw)]
    for i, p in enumerate(primaries):
        merged = list(policy["required_gates"])
        for name in p["required_gates"]:
            if name not in policy["gates"]:
                raise BatchError(
                    f"primaries[{i}] requires unknown gate: {name!r}")
            if name not in merged:
                merged.append(name)
        p["required_gates"] = merged

    inherited = payload.get("inherited") or []
    if not isinstance(inherited, list):
        raise BatchError("inherited must be a list")
    clean_inherited = [_validate_inherited(h, i, repo, seen_keys,
                                           (batch_id,))
                       for i, h in enumerate(inherited)]

    integrations_raw = payload.get("integrations") or []
    if not isinstance(integrations_raw, list) or not integrations_raw:
        raise BatchError("integrations must be a non-empty list")
    # Source13: typed issue primaries bind to the exact app-owned
    # prepare op (flat registry, no rebind) plus its prepared result
    # and current passed fastcheck receipt. Dep primaries unaffected.
    _bind_issue_primaries(repo, str(origin_m2 or ""), primaries,
                          integrations_raw)
    seen_lanes: set = set()
    plan_gates: list = []
    batch_bytes = 0
    for n, integ in enumerate(integrations_raw):
        if not isinstance(integ, dict):
            raise BatchError(f"integrations[{n}] must be an object")
        lane = str(integ.get("lane") or "")
        if not lane or len(lane) > 80:
            raise BatchError(f"integrations[{n}].lane is required")
        if lane in seen_lanes:
            raise BatchError(f"integrations[{n}] duplicates lane {lane!r}")
        seen_lanes.add(lane)
        worktree = str(integ.get("worktree") or "")
        if not worktree or not Path(worktree).is_dir():
            raise BatchError(f"integrations[{n}].worktree must be a live "
                             "checkout")
        snap = _snapshot(worktree)
        if snap is None:
            raise BatchError(f"integrations[{n}] source unreadable")
        seen_gate_keys: set = set()
        gate_plan = []
        for i, g in enumerate(integ.get("gates") or []):
            if not isinstance(g, dict):
                raise BatchError(f"gates[{i}] must be an object")
            key = str(g.get("key") or "")
            status = str(g.get("status") or "")
            if not key or len(key) > 80:
                raise BatchError(f"gates[{i}].key is required (<=80 chars)")
            if key in seen_gate_keys:
                raise BatchError(f"duplicate gate key {key!r}")
            seen_gate_keys.add(key)
            if status not in GATE_STATES:
                raise BatchError(f"gates[{i}].status must be one of "
                                 f"{sorted(GATE_STATES)}")
            if key not in policy["gates"]:
                raise BatchError(
                    f"gate {key!r} is not in the validated policy catalog")
            inputs = g.get("inputs") or {}
            if not isinstance(inputs, dict):
                raise BatchError(f"gate {key!r}.inputs must be an object")
            if status == "pass":
                for f in ("head", "fingerprint"):
                    if not inputs.get(f):
                        raise BatchError(
                            f"passing gate {key!r} must record inputs.{f} "
                            "binding it to exact source")
            ev_entries = g.get("evidence")
            if not isinstance(ev_entries, list):
                raise BatchError(f"gate {key!r}.evidence must be a list")
            if len(ev_entries) > 200:
                raise BatchError(f"gate {key!r} has too many evidence entries")
            resolved_ev = []
            for j, ev in enumerate(ev_entries):
                if not isinstance(ev, dict):
                    raise BatchError(f"gate {key!r} evidence[{j}] must be "
                                     "an object")
                label = str(ev.get("label") or "")
                kind = str(ev.get("kind") or "file")
                if not label or len(label) > 200:
                    raise BatchError(f"gate {key!r} evidence[{j}].label is "
                                     "required (<=200 chars)")
                if kind == "note":
                    resolved_ev.append(
                        {"label": label, "kind": "note",
                         "text": str(ev.get("text") or "")[:2000]})
                    continue
                if kind != "file":
                    raise BatchError(f"gate {key!r} evidence[{j}].kind must "
                                     "be file or note")
                origin_path = ev.get("origin")
                if not isinstance(origin_path, str) or not origin_path:
                    raise BatchError(f"gate {key!r} evidence[{j}].origin "
                                     "is required")
                src = _resolve_origin(origin_path, allowed_roots)
                size = src.stat().st_size
                if size > MAX_EVIDENCE_BYTES:
                    raise BatchError(
                        f"evidence {label!r} exceeds size cap")
                batch_bytes += size
                if batch_bytes > MAX_BATCH_BYTES:
                    raise BatchError("batch evidence exceeds size budget")
                resolved_ev.append({"label": label, "kind": "file",
                                    "origin": origin_path,
                                    "sha256": sha256_file(src),
                                    "bytes": size,
                                    "attestor": _evidence_attestor(src)
                                    or "unrecorded"})
            root_attested = bool(g.get("root_attested", False))
            if is_m2 and root_attested:
                raise BatchError(
                    f"gate {key!r}: origin-m2 imports may never carry "
                    "root_attested:true")
            worker_backed = any(
                e.get("attestor") == "worker" for e in resolved_ev)
            if root_attested and worker_backed:
                raise BatchError(
                    f"gate {key!r}: worker receipts never back a Root "
                    "gate")
            # M2.4a J1, second direction (the builder maps worker
            # steps onto worker:<gate> companions; these guards hold
            # for every import path, including hand-written ones).
            if key.startswith("worker:") and root_attested:
                raise BatchError(
                    f"gate {key!r}: worker gates are never Root-attested")
            spec = (policy.get("gates") or {}).get(key) or {}
            if (status == "pass" and worker_backed
                    and not root_attested
                    and (spec.get("root") or spec.get("runtime"))):
                raise BatchError(
                    f"gate {key!r}: Root/실행 확인 게이트는 worker 증거만으로 "
                    "통과할 수 없음 — 사람이 확인해야 함")
            attestor = ("worker" if worker_backed
                        else "root" if root_attested else "unrecorded")
            gate_plan.append({"key": key, "status": status,
                              "at": str(g.get("at") or ""),
                              "note": str(g.get("note") or "")[:2000],
                              "root_attested": root_attested,
                              "attestor": attestor,
                              "inputs": {"head": str(inputs.get("head") or ""),
                                         "fingerprint": str(inputs.get(
                                             "fingerprint") or "")},
                              "evidence": resolved_ev})
        plan_gates.append((integ, worktree, snap, gate_plan))

    # ---- Phase 2: write (content-addressed, never overwrite). ----
    dest = evidence_dir(batch_id)
    dest.mkdir(parents=True, exist_ok=True)
    clean_integrations = []
    for integ, worktree, snap, gate_plan in plan_gates:
        clean_gates = []
        for g in gate_plan:
            clean_ev = []
            for ep in g["evidence"]:
                if ep["kind"] == "note":
                    clean_ev.append(ep)
                    continue
                src = _resolve_origin(ep["origin"], allowed_roots)
                target = dest / (ep["sha256"] + "-" + _safe_basename(
                    ep["origin"]))
                if not target.exists():
                    target.write_bytes(src.read_bytes())
                    if sha256_file(target) != ep["sha256"]:
                        target.unlink()
                        raise BatchError(
                            f"evidence {ep['label']!r} changed during copy")
                clean_ev.append(
                    {"label": ep["label"], "kind": "file",
                     "origin": ep["origin"], "sha256": ep["sha256"],
                     "bytes": ep["bytes"], "stored": True,
                     "stored_path": "evidence/" + target.name})
            g = dict(g)
            g["evidence"] = clean_ev
            clean_gates.append(g)
        # M2.5c: the duplicate-tree census rides along
        # (display only) — anything else shaped defaults empty.
        dupes = integ.get("lock_dupes")
        if not (isinstance(dupes, dict)
                and isinstance(dupes.get("items"), list)
                and isinstance(dupes.get("total"), int)):
            dupes = {"items": [], "total": 0}
        clean_integrations.append({
            "lane": str(integ.get("lane") or "")[:80],
            "worktree": worktree,
            "head": snap["head"], "fingerprint": snap["fingerprint"],
            "status": str(integ.get("status") or "")[:200],
            "gates": clean_gates,
            "lock_dupes": {
                "items": [str(x)[:200] for x in dupes["items"][:10]],
                "total": max(0, dupes["total"])},
        })

    now = utcnow()
    previous = read_json(batch_file(batch_id), default=None)
    history = (previous.get("history") or []) if previous else []
    if previous:
        history = history + [{
            "superseded_at": now,
            "integrations": previous.get("integrations"),
            "note": "replaced by re-import; prior verdicts preserved here",
        }]
    batch = {"schema": SCHEMA, "id": batch_id, "repo": repo,
             "target_branch": str(payload.get("target_branch") or "")[:80],
             "purpose": str(payload.get("purpose") or "")[:200],
             "created_at": (previous.get("created_at") if previous else now),
             "updated_at": now,
             "origin": clean_origin,
             "primaries": primaries, "inherited": clean_inherited,
             "integrations": clean_integrations,
             "policy": policy,
             "history": history[-10:]}
    # M2.2-C: every import from here on records per-gate attestors, so
    # the UI can mark unattested gates without re-grading batches
    # imported before the rule existed.
    batch["attestor_rule"] = 1
    if is_m2:
        batch["origin_m2"] = origin_m2
    if previous is not None and isinstance(previous.get("hold"), dict):
        # F6: a hold survives reimport — only hold_batch(hold=False)
        # releases it, never an evidence refresh.
        batch["hold"] = previous["hold"]
    write_json(batch_file(batch_id), batch)
    _write_index()
    return batch


def _write_index() -> None:
    idx = [{"id": b["id"], "repo": b.get("repo"),
            "target_branch": b.get("target_branch"),
            "purpose": b.get("purpose"),
            "updated_at": b.get("updated_at"),
            "primaries": len(b.get("primaries") or [])}
           for b in list_batches()]
    write_json(batches_dir() / "index.json", idx)


def _shared_git_dir(path: str):
    """Resolved shared object-store dir, or None when not a git checkout.

    Linked worktrees report <repo>/.git/worktrees/<name>; normalize to
    <repo>/.git so worktrees of one repo compare equal while separate
    clones never do.
    """
    d = _git_text(["rev-parse", "--git-common-dir"], path)
    if not d:
        return None
    p = Path(d) if Path(d).is_absolute() else Path(path) / d
    try:
        p = p.resolve()
    except OSError:
        return None
    parts = p.parts
    if "worktrees" in parts:
        p = Path(*parts[:parts.index("worktrees")])
    return str(p)


def _base_key(checkout: str, branch: str) -> str:
    # NUL-joined: the same checkout can track main and next bases, and a
    # moved next base must never hide behind a fresh main result.
    return "base\x00" + (checkout or "") + "\x00" + (branch or "")


def read_keys(batch_list: list) -> tuple:
    """Exact live-map read-key set for these batch dicts. Pure, no IO.

    Returns the ordered unique keys `live_map_with_diag` will read:
    "wt:<worktree>" integration snapshots, "cand:<worktree>" candidate
    snapshots, and base keys via _base_key(checkout, branch). The revision
    66 coalescing gate in serve keys shared reads by
    frozenset(read_keys(closure(...))); the derivation lives here once and
    `live_map_with_diag` iterates it, so the key and the map can never
    drift. Keying by read keys (not batch ids) means a batch whose
    integrations/worktrees changed is a different read set and never rides
    a stale map: a missing entry stays unreadable, never matching.
    """
    keys: tuple = ()
    seen: set = set()
    for b in batch_list or []:
        if not isinstance(b, dict):
            continue
        for integ in b.get("integrations") or []:
            wt = integ.get("worktree") or ""
            key = "wt:" + wt if wt else ""
            if key and key not in seen:
                seen.add(key)
                keys += (key,)
        for p in b.get("primaries") or []:
            wt = (p.get("source") or {}).get("worktree") or ""
            key = "cand:" + wt if wt else ""
            if key and key not in seen:
                seen.add(key)
                keys += (key,)
        origin_co = (b.get("origin") or {}).get("checkout") or ""
        base_branch = (b.get("origin") or {}).get("base_branch") or ""
        key = _base_key(origin_co, base_branch)
        if origin_co and base_branch and key not in seen:
            seen.add(key)
            keys += (key,)
    return keys


def closure(batch_list: list) -> list:
    """Batch list plus every transitively inherited batch.

    Readiness of an inheriting batch needs the owned batch's live
    snapshots in the SAME map; without the closure, B/C detail views
    could never become ready even with all proofs current. Cycles are
    rejected at import; the visited set keeps this defensive.
    """
    by_id = {}
    for b in batch_list or []:
        if isinstance(b, dict) and b.get("id"):
            by_id.setdefault(b["id"], b)
    try:
        stored = {b["id"]: b for b in list_batches()}
    except Exception:
        stored = {}
    out = list(by_id.values())
    seen = set(by_id)
    queue = list(by_id.values())
    while queue:
        cur = queue.pop()
        for h in (cur.get("inherited") or []):
            ref = (h or {}).get("batch") or ""
            if not ref or ref in seen:
                continue
            seen.add(ref)
            target = by_id.get(ref) or stored.get(ref)
            if isinstance(target, dict):
                out.append(target)
                queue.append(target)
    return out


def _read_wt_cand(worktree: str, kind: str) -> tuple:
    """One worktree read for the live map: (snap_or_None, diag_or_None).

    snap matches _snapshot() exactly (including the clean flag: live
    values stay exactly {head, fingerprint, clean}); diag is present on
    failure with kind/worktree/category/op/rc plus the cleanliness
    offender path, and on success carries only kind/worktree plus the
    bounded untracked sample from the same snapshot. No source is read
    twice.
    """
    core, category, op, rc = _snapshot_status(worktree)
    if core is None:
        return None, {"kind": kind, "worktree": worktree,
                      "category": category, "op": op, "rc": rc,
                      "offender": "", "status_ok": False}
    clean, status_ok, offender = _source_clean_diag(worktree)
    snap = {"head": core["head"], "fingerprint": core["fingerprint"],
            "clean": clean}
    # The untracked sample rides the SAME already-read snapshot (names
    # only, bounded): mismatch leaves can name the next unknown hashing
    # trigger in their own frame with no second read. Live values stay
    # exactly {head, fingerprint, clean}.
    sample = core.get("untracked_sample") or []
    if clean:
        return snap, {"kind": kind, "worktree": worktree,
                      "category": None, "op": None, "rc": None,
                      "offender": "", "status_ok": True,
                      "untracked_sample": sample}
    return snap, {"kind": kind, "worktree": worktree,
                  "category": (None if status_ok
                               else "status_unreadable"),
                  "op": None, "rc": None, "offender": offender,
                  "status_ok": status_ok, "untracked_sample": sample}


def live_map_with_diag(batch_list: list) -> tuple:
    """(live, diag): live matches live_map() exactly; diag holds per-key
    read context from the same single read pass (failure diagnostics on
    failed keys; kind/worktree plus the bounded untracked sample on
    keys whose snapshot read succeeded).

    live keys: "wt:<worktree>" integration snapshots, "cand:<worktree>"
    candidate snapshots, base keys via _base_key(checkout, branch).
    Missing live entries mean unreadable, never matching.

    Iterates read_keys(batch_list): the gate key and the read set are one
    derivation, so they can never drift.
    """
    live: dict = {}
    diag: dict = {}
    for key in read_keys(batch_list):
        if key.startswith("wt:"):
            snap, err = _read_wt_cand(key[len("wt:"):], "wt")
            live[key] = snap
            if err is not None:
                diag[key] = err
        elif key.startswith("cand:"):
            snap, err = _read_wt_cand(key[len("cand:"):], "cand")
            live[key] = snap
            if err is not None:
                diag[key] = err
        else:
            _, origin_co, base_branch = key.split("\x00")
            head, ok = _git_head_diag(origin_co, base_branch)
            live[key] = {"head": head} if head else None
            if not ok:
                diag[key] = {"kind": "base", "worktree": origin_co,
                             "branch": base_branch,
                             "category": "base_unreadable",
                             "op": None, "rc": None, "offender": "",
                             "status_ok": False}
    return live, diag


def live_map(batch_list: list) -> dict:
    """Fresh source states keyed by worktree (never lane names).

    Keys: "wt:<worktree>" integration snapshots, "cand:<worktree>"
    candidate snapshots, base keys via _base_key(checkout, branch).
    Missing entries mean unreadable, never matching.
    """
    live, _ = live_map_with_diag(batch_list)
    return live


def _evidence_verified(batch_id: str, ev: dict) -> str | None:
    """None when stored file evidence re-verifies, else a reason."""
    if ev.get("kind") == "note":
        return "note-only"
    if not ev.get("stored"):
        return "referenced, not stored+reverified"
    name = Path(ev.get("stored_path") or "").name
    if not name or name in (".", ".."):
        return "bad stored path"
    spath = evidence_dir(batch_id) / name
    try:
        resolved = spath.resolve()
    except OSError:
        return "unreadable"
    try:
        resolved.relative_to(evidence_dir(batch_id).resolve())
    except (OSError, ValueError):
        return "outside evidence dir"
    if not resolved.is_file():
        return "missing artifact"
    try:
        if sha256_file(resolved) != ev.get("sha256"):
            return "hash mismatch"
    except OSError:
        return "unreadable"
    return None


def _diag_part(live_diag: dict | None, kind: str, map_key: str,
                reason_code: str, expected_head: str = "",
                expected_fp: str = "", actual_head: str = "",
                actual_fp: str = "", clean=None, vstat: dict | None = None,
                path: str = "") -> dict:
    """One structured leaf fragment for a read-related failure.

    Read-error identity (category/op/rc) and the cleanliness offender
    PATH come from the SAME single live-map read (live_diag); expected
    vs actual heads/fingerprints are short12. Only the op category, rc
    int, and repo-relative path travel outward — never stderr text,
    file bytes, or secrets. Chain/key are attached by the caller.
    """
    d = ((live_diag or {}).get(map_key) if map_key else None) or {}
    if not isinstance(d, dict):
        d = {}
    offender = path or str(d.get("offender") or "")
    part = {"reason_code": str(reason_code or "")[:80],
            "kind": str(kind or "")[:24],
            "map_key": str(map_key or "")[:_DIAG_PATH_CHARS],
            "worktree": str(d.get("worktree") or "")[:_DIAG_PATH_CHARS],
            "path": offender[:_DIAG_PATH_CHARS],
            "category": d.get("category"), "op": d.get("op"),
            "rc": d.get("rc"),
            "expected_head": str(expected_head or "")[:12],
            "expected_fp": str(expected_fp or "")[:12],
            "actual_head": str(actual_head or "")[:12],
            "actual_fp": str(actual_fp or "")[:12],
            "clean": clean}
    if vstat is not None:
        part["vstat"] = {str(k)[:24]: str(v)[:24]
                         for k, v in vstat.items()}
    if reason_code in ("candidate_fingerprint_changed",
                       "integration_mismatch", "gate_inputs_stale",
                       "run_proof_stale"):
        sample = d.get("untracked_sample") or []
        if isinstance(sample, list):
            part["untracked_sample"] = [
                str(x)[:_DIAG_PATH_CHARS] for x in sample[:5] if x]
    return part


# ---- Persisted readiness-negative record (observability only) ----
#
# One JSONL line per failing leaf, written once per TOP-LEVEL evaluation
# (never inside recursive sub-evaluations), so a flap frame survives even
# when nobody watches the UI. Additive only: readiness, reasons, gates,
# proofs, and the fingerprint algorithm are untouched.
#
# Location is a SEPARATE diagnostics dir (never batches/, whose *.json
# glob would load a side file as a batch). Writes are guarded by a
# module-level lock (ThreadingHTTPServer), use a unique temp name plus
# os.replace for rotation, and are best-effort: recording can never
# raise into evaluation.
_NEG_DIRNAME = "diagnostics"
_NEG_LINES_NAME = "readiness-negatives.jsonl"
_NEG_STATE_NAME = "readiness-negatives-state.json"
_NEG_MAX_LINES = 200
_NEG_MAX_BYTES = 256 * 1024
_NEG_STATE_MAX_KEYS = 1000
_NEG_FLUSH_S = 60
_NEG_HEARTBEAT_S = 6 * 3600
_NEG_LOCK = threading.Lock()
_NEG_FLUSHED = {}


def _neg_lock_acquire():
    """Hold an exclusive cross-process flock across one negatives
    read-modify-write, so the daemon tick (a separate process) and the
    dashboard server cannot interleave lines/state writes. Returns the
    open fh, or None (best-effort: None keeps the old in-process-only
    behavior, e.g. platforms without fcntl). Always released with
    _neg_lock_release in a finally. Ordering is fixed everywhere:
    flock first, then _NEG_LOCK — never the reverse.
    """
    try:
        import fcntl
    except ImportError:  # noqa: BLE001 - no flock here; old behavior
        return None
    try:
        lines_path, _ = _neg_files()
        # No mkdir here: creating the diagnostics dir is the writer's
        # job (_neg_write_text). A pure read (e.g. recovery with no
        # entries) must leave a missing dir missing.
        fh = open(str(lines_path.parent / ".negatives.lock"), "a+b")
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        try:
            fh.close()
        except OSError:
            pass
        return None
    return fh


def _neg_lock_release(fh) -> None:
    if fh is None:
        return
    try:
        import fcntl
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
    except Exception:  # noqa: BLE001 - unlock is best-effort
        pass


def _neg_files() -> tuple:
    d = paths.home() / _NEG_DIRNAME
    return d / _NEG_LINES_NAME, d / _NEG_STATE_NAME


def _neg_sig(batch_id: str, diag: dict) -> str:
    """Dedupe signature: stable failure identity WITHOUT actual values
    (live heads/fingerprints move every poll, which would defeat
    suppression) and without timestamps."""
    core = [batch_id, diag.get("chain"), diag.get("key"),
            diag.get("reason_code"), diag.get("map_key"),
            diag.get("category"), diag.get("op"), diag.get("rc"),
            diag.get("path")]
    return json.dumps(core, ensure_ascii=False, sort_keys=True,
                      default=str)


def _neg_write_text(path: Path, text: str) -> None:
    """Atomic bounded write via unique temp + os.replace (util.write_json
    uses a fixed .tmp name, which concurrent writers would collide on)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".%d.%d.tmp" % (os.getpid(),
                                           threading.get_ident()))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _neg_read_state(state_path: Path) -> dict:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_negative(batch_id: str, primary_key: str, diag: dict) -> None:
    """Append one record line per failure signature per key.

    An identical signature for the same key is suppressed UNTIL that key
    recovers (_record_recovery clears its memory, so fail->recover->fail
    appends again and flap frequency stays measurable): a persistently
    failing key appends once, never floods the capped file. Suppressed
    polls only bump counters with last_seen; the state file is flushed
    at most once per _NEG_FLUSH_S per key (process-local throttle —
    losing a few counts on restart is acceptable, losing lines is not).
    One heartbeat re-append per key per _NEG_HEARTBEAT_S keeps
    days-long failures visible. Best-effort.
    """
    try:
        # Write path: ensure the dir BEFORE acquiring, so the flock
        # file opens and the cross-process read-modify-write actually
        # serializes (first-ever concurrent refreshes double-appended
        # without it). Pure reads still never mkdir (see acquire).
        _lines_path, _ = _neg_files()
        _lines_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lk = _neg_lock_acquire()
    try:
        with _NEG_LOCK:
            lines_path, state_path = _neg_files()
            state = _neg_read_state(state_path)
            entries = state.get("entries")
            if not isinstance(entries, dict):
                entries = {}
            sigkey = str(batch_id or "") + "\x00" + str(primary_key or "")
            sig = _neg_sig(batch_id, diag or {})
            now = time.time()
            prev = entries.get(sigkey)
            if (isinstance(prev, dict) and prev.get("sig") == sig
                    and now - float(prev.get("at") or 0)
                    < _NEG_HEARTBEAT_S):
                prev["suppressed"] = int(prev.get("suppressed") or 0) + 1
                prev["last_seen"] = now
                if now - float(_NEG_FLUSHED.get(sigkey) or 0) \
                        >= _NEG_FLUSH_S:
                    state["entries"] = entries
                    _neg_write_text(state_path, json.dumps(
                        state, indent=2, ensure_ascii=False) + "\n")
                    _NEG_FLUSHED[sigkey] = now
                return
            try:
                old = lines_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                old = []
            record = {"at": utcnow(), "batch": batch_id, "key": primary_key,
                      "chain": (diag or {}).get("chain"),
                      "kind": (diag or {}).get("kind"),
                      "map_key": (diag or {}).get("map_key"),
                      "worktree": (diag or {}).get("worktree"),
                      "reason_code": (diag or {}).get("reason_code"),
                      "category": (diag or {}).get("category"),
                      "op": (diag or {}).get("op"),
                      "rc": (diag or {}).get("rc"),
                      "path": (diag or {}).get("path"),
                      "expected_head": (diag or {}).get("expected_head"),
                      "expected_fp": (diag or {}).get("expected_fp"),
                      "actual_head": (diag or {}).get("actual_head"),
                      "actual_fp": (diag or {}).get("actual_fp"),
                      "clean": (diag or {}).get("clean"),
                      "vstat": (diag or {}).get("vstat"),
                      "untracked_sample": (diag or {}).get(
                          "untracked_sample")}
            old.append(json.dumps(record, ensure_ascii=False,
                                  default=str))
            while len(old) > _NEG_MAX_LINES:
                old = old[-_NEG_MAX_LINES:]
            while (len(("\n".join(old) + "\n").encode("utf-8"))
                    > _NEG_MAX_BYTES and len(old) > 1):
                old = old[1:]
            _neg_write_text(lines_path, "\n".join(old) + "\n")
            entries[sigkey] = {"sig": sig, "at": now, "last_seen": now,
                               "suppressed": 0}
            if len(entries) > _NEG_STATE_MAX_KEYS:
                ordered = sorted(entries.items(),
                                 key=lambda kv: float(
                                     (kv[1] or {}).get("at") or 0))
                entries = dict(ordered[-_NEG_STATE_MAX_KEYS:])
            state["entries"] = entries
            _neg_write_text(state_path, json.dumps(state, indent=2,
                                                   ensure_ascii=False)
                            + "\n")
            _NEG_FLUSHED[sigkey] = now
    except Exception:
        pass
    finally:
        _neg_lock_release(lk)


def _record_recovery(batch_id: str, primary_key: str) -> None:
    """A recovered key clears its dedupe memory so the NEXT failure
    appends again (flap frequency stays measurable). Appends nothing
    and never edits record lines. Best-effort."""
    lk = _neg_lock_acquire()
    try:
        with _NEG_LOCK:
            _, state_path = _neg_files()
            state = _neg_read_state(state_path)
            entries = state.get("entries")
            if not isinstance(entries, dict):
                return
            sigkey = str(batch_id or "") + "\x00" + str(primary_key or "")
            if sigkey in entries:
                del entries[sigkey]
                state["entries"] = entries
                _neg_write_text(state_path, json.dumps(state, indent=2,
                                                       ensure_ascii=False)
                                + "\n")
    except Exception:
        pass
    finally:
        _neg_lock_release(lk)


def evaluate(batch: dict, live: dict | None = None,
             _chain: tuple = (), live_diag: dict | None = None,
             record: bool = False) -> dict:
    """Per-primary readiness against live source and run provenance.

    A candidate is ready only when review is accepted, every required
    gate (batch policy UNION primary extras) passes with real
    hash-reverified artifact evidence plus Root attestation wherever the
    catalog requires it (runtime gates always), each passing gate's
    inputs match the current integration source (old proof never
    borrowed), the live integration/candidate/base sources match the
    recorded bindings, the bound run still resolves, and every inherited
    reference evaluates ready. Anything else yields explicit reasons.
    """
    live = live or {}
    catalog = ((batch.get("policy") or {}).get("gates") or {})
    # M2.5b: a held batch is never READY, whatever its gates say.
    hold = batch.get("hold") if isinstance(batch.get("hold"), dict) \
        else {}
    hold_by = str((hold or {}).get("by") or "")
    hold_why = " ".join(str((hold or {}).get("reason") or "").split())
    hold_reason = (f"보류: {hold_why} — {hold_by}" if hold_by
                   and hold_why else
                   (f"보류 — {hold_by}" if hold_by else ""))
    # M2.14c: a standalone batch superseded by a done joint op is
    # never READY, even if its hold is later released — one
    # shippable home per candidate. Read-only: neither the hold
    # nor the batch file is changed here.
    sup_joint = ""
    try:
        sup_joint = str((_joint_supersede_map() or {}).get(
            str(batch.get("id") or "")) or "")
    except (Exception, SystemExit):  # noqa: BLE001 - no map, no rule
        sup_joint = ""
    results = []
    seen: set = set()
    for p in batch.get("primaries") or []:
        reasons: list = []
        causes: list = []
        parts: list = []
        child_diags: list = []
        ok = True
        if hold_reason:
            ok = False
            reasons.append(hold_reason)
        if sup_joint:
            ok = False
            reasons.append(f"함께 올림으로 대체: {sup_joint}")
        dep, target, lane = p.get("dep"), p.get("target"), p.get("lane")
        key = primary_result_key(batch.get("repo") or "",
                                 p if isinstance(p, dict) else {})
        if key is None:
            # A persisted record without a valid typed identity is
            # never skipped: skipping would hide a blocked candidate
            # and let the remaining entries read READY. Explicit
            # not-ready with an identity cause instead.
            results.append({
                "key": "", "dep": dep, "target": target, "lane": lane,
                "ready": False,
                "reasons": ["persisted primary has no valid typed "
                            "identity (blocked-unknown-primary)"],
                "attention": [], "inherited": [],
                "leaf": "persisted primary has no valid typed identity"})
            continue
        if key in seen:
            continue
        seen.add(key)
        dep = str(dep or "")
        target = str(target or "")
        verdict = (p.get("review") or {}).get("verdict")
        if verdict not in ("accept", "accept-with-notes"):
            ok = False
            reasons.append(f"independent review verdict is {verdict!r}")
        required = p.get("required_gates") or []
        if not required:
            ok = False
            reasons.append("no required-gate policy recorded")
        lane_gates = {}
        lane_worktree = ""
        for integ in batch.get("integrations") or []:
            if (integ.get("lane") or "") != lane:
                continue
            lane_worktree = integ.get("worktree") or ""
            for g in integ.get("gates") or []:
                if isinstance(g, dict):
                    lane_gates.setdefault(g.get("key"), g)
        for req in required:
            spec = catalog.get(req)
            if spec is None:
                ok = False
                reasons.append(f"required gate {req!r} has no policy entry")
                continue
            g = lane_gates.get(req)
            if g is None:
                ok = False
                reasons.append(f"required gate {req!r} not recorded")
                continue
            na = g.get("na") or {}
            if isinstance(na, dict) and str(na.get("reason") or ""):
                # M2.4b: marked N/A with a mandatory recorded reason
                # — satisfied without evidence, never silent.
                continue
            if g.get("status") != "pass":
                ok = False
                reasons.append(f"required gate {req!r} is "
                               f"{g.get('status')!r}")
                continue
            files = [e for e in g.get("evidence") or []
                     if e.get("kind") != "note"]
            if not files:
                ok = False
                reasons.append(f"required gate {req!r} has no artifact "
                               "evidence (notes only)")
                continue
            for ev in files:
                problem = _evidence_verified(batch["id"], ev)
                if problem:
                    ok = False
                    reasons.append(f"gate {req!r} evidence "
                                   f"{ev.get('label')!r}: {problem}")
            if (spec.get("root") or spec.get("runtime")) and not (
                    g.get("root_attested") or g.get("owner_attested")):
                ok = False
                reasons.append(f"runtime gate {req!r} lacks human "
                               "attestation (owner or Root)")
            inputs = g.get("inputs") or {}
            live_wt = live.get("wt:" + lane_worktree) if lane_worktree else None
            if not inputs.get("head") or not inputs.get("fingerprint"):
                ok = False
                reasons.append(f"gate {req!r} not bound to exact source")
            elif live_wt is None:
                ok = False
                reasons.append("live integration source unreadable")
                parts.append(_diag_part(
                    live_diag, "wt", "wt:" + lane_worktree
                    if lane_worktree else "", "gate_inputs_unreadable",
                    expected_head=str(inputs.get("head") or ""),
                    expected_fp=str(inputs.get("fingerprint") or "")))
            elif (inputs.get("head"), inputs.get("fingerprint")) != (
                    live_wt.get("head"), live_wt.get("fingerprint")):
                ok = False
                reasons.append(f"gate {req!r} inputs predate current "
                               "integration source (STALE)")
                parts.append(_diag_part(
                    live_diag, "wt", "wt:" + lane_worktree
                    if lane_worktree else "", "gate_inputs_stale",
                    expected_head=str(inputs.get("head") or ""),
                    expected_fp=str(inputs.get("fingerprint") or ""),
                    actual_head=str(live_wt.get("head") or ""),
                    actual_fp=str(live_wt.get("fingerprint") or ""),
                    clean=live_wt.get("clean")))
        # Integration source binding.
        live_wt = live.get("wt:" + lane_worktree) if lane_worktree else None
        recorded_pairs = {((i.get("head") or ""),
                           (i.get("fingerprint") or ""))
                          for i in (batch.get("integrations") or [])
                          if (i.get("lane") or "") == lane}
        if live_wt is None:
            ok = False
            reasons.append("live integration state unreadable")
            parts.append(_diag_part(
                live_diag, "wt", "wt:" + lane_worktree
                if lane_worktree else "", "integration_unreadable"))
        else:
            if ((live_wt.get("head"), live_wt.get("fingerprint"))
                    not in recorded_pairs):
                ok = False
                reasons.append("live integration source differs from "
                               "recorded head+fingerprint")
                exp_pair = sorted(recorded_pairs)[0] \
                    if recorded_pairs else ("", "")
                parts.append(_diag_part(
                    live_diag, "wt", "wt:" + lane_worktree
                    if lane_worktree else "", "integration_mismatch",
                    expected_head=exp_pair[0], expected_fp=exp_pair[1],
                    actual_head=str(live_wt.get("head") or ""),
                    actual_fp=str(live_wt.get("fingerprint") or ""),
                    clean=live_wt.get("clean")))
            if not live_wt.get("clean", False):
                ok = False
                reasons.append("integration worktree has uncommitted changes")
                parts.append(_diag_part(
                    live_diag, "wt", "wt:" + lane_worktree
                    if lane_worktree else "", "integration_not_clean",
                    actual_head=str(live_wt.get("head") or ""),
                    actual_fp=str(live_wt.get("fingerprint") or ""),
                    clean=False))
        # Original candidate source + target/base binding (never skipped).
        src = p.get("source") or {}
        src_wt = src.get("worktree") or ""
        if not src_wt:
            ok = False
            reasons.append("candidate source binding not recorded")
        else:
            live_src = live.get("cand:" + src_wt)
            if live_src is None:
                ok = False
                reasons.append("live candidate source unreadable")
                parts.append(_diag_part(
                    live_diag, "cand", "cand:" + src_wt,
                    "candidate_unreadable",
                    expected_head=str(src.get("head") or ""),
                    expected_fp=str(src.get("fingerprint") or "")))
            else:
                if live_src.get("head") != src.get("head"):
                    ok = False
                    reasons.append("candidate source HEAD moved after review")
                    parts.append(_diag_part(
                        live_diag, "cand", "cand:" + src_wt,
                        "candidate_head_moved",
                        expected_head=str(src.get("head") or ""),
                        actual_head=str(live_src.get("head") or ""),
                        clean=live_src.get("clean")))
                if live_src.get("fingerprint") != src.get("fingerprint"):
                    ok = False
                    reasons.append("candidate source fingerprint changed")
                    parts.append(_diag_part(
                        live_diag, "cand", "cand:" + src_wt,
                        "candidate_fingerprint_changed",
                        expected_head=str(src.get("head") or ""),
                        expected_fp=str(src.get("fingerprint") or ""),
                        actual_head=str(live_src.get("head") or ""),
                        actual_fp=str(live_src.get("fingerprint") or ""),
                        clean=live_src.get("clean")))
                if not live_src.get("clean", False):
                    ok = False
                    reasons.append("candidate source has uncommitted "
                                   "changes")
                    parts.append(_diag_part(
                        live_diag, "cand", "cand:" + src_wt,
                        "candidate_not_clean",
                        expected_head=str(src.get("head") or ""),
                        expected_fp=str(src.get("fingerprint") or ""),
                        actual_head=str(live_src.get("head") or ""),
                        actual_fp=str(live_src.get("fingerprint") or ""),
                        clean=False))
        origin_co = (batch.get("origin") or {}).get("checkout") or ""
        recorded_base = (batch.get("origin") or {}).get("base_head") or ""
        base_branch = (batch.get("origin") or {}).get("base_branch") or ""
        if not origin_co or not base_branch or not recorded_base:
            ok = False
            reasons.append("target/base binding not recorded")
        else:
            live_base = live.get(_base_key(origin_co, base_branch))
            if live_base is None:
                ok = False
                reasons.append("live target/base unreadable")
                parts.append(_diag_part(
                    live_diag, "base",
                    _base_key(origin_co, base_branch), "base_unreadable",
                    expected_head=recorded_base))
            elif live_base.get("head") != recorded_base:
                ok = False
                reasons.append("target/base moved after review")
                parts.append(_diag_part(
                    live_diag, "base",
                    _base_key(origin_co, base_branch), "base_moved",
                    expected_head=recorded_base,
                    actual_head=str(live_base.get("head") or "")))
        if p.get("run_id"):
            try:
                run = store.load(p["run_id"])
            except SystemExit:
                run = None
            if not isinstance(run, dict) or run.get("repo") != batch.get(
                    "repo"):
                ok = False
                reasons.append("bound run missing or moved repo")
                parts.append(_diag_part(
                    live_diag, "run-proof", "", "run_missing"))
            else:
                # Re-verify the original review proof live: an ACCEPT
                # whose run steps went stale (moved/dirty bytes) is not
                # ready, even when the recorded snapshot still matches.
                verdict = str((p.get("review") or {}).get("verdict")
                              or "pending")
                live_fp = (live.get("cand:" + src_wt) or {}).get(
                    "fingerprint") if src_wt else None
                live_head = (live.get("cand:" + src_wt) or {}).get(
                    "head") if src_wt else None
                vstat = store.verification_status(run, live_fp, live_head)
                if verdict in ("accept", "accept-with-notes"):
                    bad = _accept_proof_gaps(vstat)
                    if bad:
                        ok = False
                        reasons.append(
                            "bound run review proof no longer fresh: "
                            + ", ".join(f"{s}={vstat[s]}" for s in bad))
                        parts.append(_diag_part(
                            live_diag, "run-proof",
                            "cand:" + src_wt if src_wt else "",
                            "run_proof_stale",
                            expected_head=str(src.get("head") or ""),
                            expected_fp=str(src.get("fingerprint") or ""),
                            actual_head=str(live_head or ""),
                            actual_fp=str(live_fp or ""),
                            vstat=vstat))
        else:
            ok = False
            reasons.append("no run/queue provenance bound")
        # Inherited coverage counts only when the owning batch is fresh.
        inherited_state = []
        for h in batch.get("inherited") or []:
            ref = h.get("batch") or ""
            try:
                target_batch = load_batch(ref)
            except BatchError:
                ok = False
                reasons.append(f"inherited batch {ref!r} unreadable")
                parts.append(_diag_part(
                    live_diag, "inherited", "", "inherited_unreadable"))
                inherited_state.append({"batch": ref, "ready": 0,
                                        "total": 0, "stale": True})
                continue
            if ref in _chain or ref == batch.get("id"):
                ok = False
                reasons.append(f"inherited batch {ref!r} is circular")
                parts.append(_diag_part(
                    live_diag, "inherited", "", "inherited_circular"))
                inherited_state.append({"batch": ref, "ready": 0,
                                        "total": 0, "stale": True})
                continue
            sub = evaluate(target_batch, live,
                           _chain + (batch.get("id"),),
                           live_diag=live_diag)
            sub_ready = sum(1 for r in sub["results"] if r["ready"])
            sub_total = len(sub["results"])
            stale = sub_ready != sub_total
            if stale:
                ok = False
                reasons.append(f"inherited {ref!r} not fully proven "
                               f"({sub_ready}/{sub_total} ready)")
                cause = _cause_from_sub(ref, sub)
                entry = {"batch": ref, "ready": sub_ready,
                         "total": sub_total, "stale": stale}
                if cause:
                    entry["cause"] = cause
                    causes.append(cause)
                for sr in sub["results"]:
                    if (isinstance(sr, dict) and not sr.get("ready")
                            and isinstance(sr.get("diag"), dict)):
                        entry["diag"] = sr["diag"]
                        child_diags.append(sr["diag"])
                        break
                inherited_state.append(entry)
                continue
            inherited_state.append({"batch": ref, "ready": sub_ready,
                                    "total": sub_total, "stale": stale})
            continue
        if child_diags:
            # The deepest already-computed descendant leaf stays
            # authoritative; its chain already names parent->..->leaf.
            leaf_diag = child_diags[0]
        elif parts:
            leaf_diag = dict(parts[0])
            leaf_diag["chain"] = list(_chain) + [batch.get("id")]
            leaf_diag["key"] = key
        else:
            leaf_diag = None
        # M2.16d: a compiler/runtime major mismatch co-located
        # in one manifest warns on the entry — never a readiness
        # reason, so existing manual batches keep their READY.
        attentions = []
        try:
            _seen_wt = set()
            for _integ in batch.get("integrations") or []:
                _wt = str(_integ.get("worktree") or "")
                if not _wt or _wt in _seen_wt:
                    continue
                _seen_wt.add(_wt)
                _label = m2.compiler_major_mismatch_same_manifest(
                    _wt, dep)
                if _label and _label not in attentions:
                    attentions.append(_label)
        except Exception:  # noqa: BLE001 - warnings never break it
            attentions = []
        entry = {"key": key, "dep": dep, "target": target,
                 "lane": lane, "ready": ok, "reasons": reasons,
                 "attention": attentions,
                 "inherited": inherited_state,
                 **({"issue": dict(p["issue"])}
                    if isinstance(p.get("issue"), dict) else {}),
                 "leaf": _bound_text(
                     "; ".join(causes[:_LEAF_CAUSES_MAX]),
                     _LEAF_TOTAL_CHARS)}
        if leaf_diag is not None:
            entry["diag"] = leaf_diag
        results.append(entry)
    if record and not _chain:
        # Top-level only: one failing leaf is one persisted record with
        # its full chain. Only a READY result counts as recovery and
        # clears dedupe memory, so the next failure appends again. A
        # not-ready result without diag (review pending, gate fail) is
        # neither a record nor a recovery. Never inside recursion.
        for r in results:
            if isinstance(r, dict) and isinstance(r.get("diag"), dict):
                _record_negative(batch.get("id") or "",
                                 r.get("key") or "", r["diag"])
            elif isinstance(r, dict) and r.get("ready"):
                _record_recovery(batch.get("id") or "",
                                 r.get("key") or "")
    return {"batch": batch.get("id"), "evaluated_at": utcnow(),
            "results": results}


def summarize(batches: list, live_by_batch: dict,
              precomputed: dict | None = None) -> dict:
    """Per-repo counts: reviewed / validating / failed / ready + next action.

    reviewed: primaries with accepted review (deduplicated). validating:
    accepted but not ready. failed: any required gate fail/blocked or
    review request-changes. ready: evaluated ready. Without evaluation
    input a batch counts accepted primaries as validating, never ready.
    Inherited coverage never inflates unique counts.

    When precomputed is explicitly supplied (even empty), its entries are
    the only evaluations used: a batch id missing from the map counts as
    unevaluated (validating, never ready) and is never silently
    re-evaluated. Standalone callers that pass nothing keep the current
    per-batch evaluation behavior. No caching, no parallel evaluator.
    """
    use_pre = precomputed is not None
    per_repo: dict = {}
    for b in batches:
        repo = b.get("repo") or ""
        row = per_repo.setdefault(repo, {"reviewed": 0, "validating": 0,
                                         "failed": 0, "ready": 0,
                                         "unique": 0, "next_action": ""})
        if use_pre:
            ev = precomputed.get(b.get("id"))
            if not isinstance(ev, dict):
                ev = {"results": []}
        else:
            ev = evaluate(b, live_by_batch.get(b.get("id")))
        by_key = {r["key"]: r for r in (ev.get("results") or [])
                    if isinstance(r, dict) and r.get("key")}
        seen: set = set()
        for p in b.get("primaries") or []:
            if not isinstance(p, dict):
                continue
            key = primary_result_key(repo, p)
            if key is None:
                # Persisted record without valid typed identity:
                # counted, never ready (mirrors evaluate's explicit
                # not-ready; import_ rejects such records).
                row["unique"] += 1
                row["validating"] += 1
                continue
            if key in seen:
                continue
            seen.add(key)
            row["unique"] += 1
            verdict = (p.get("review") or {}).get("verdict")
            if verdict in ("accept", "accept-with-notes"):
                row["reviewed"] += 1
            r = by_key.get(key)
            if r is None or not r["ready"]:
                reasons = " ".join((r or {}).get("reasons") or [])
                if ("is 'fail'" in reasons or "is 'blocked'" in reasons
                        or verdict == "request-changes"):
                    row["failed"] += 1
                else:
                    row["validating"] += 1
            else:
                row["ready"] += 1
    for repo, row in per_repo.items():
        if row["failed"]:
            row["next_action"] = f"막힘 {row['failed']}건 원인 해소"
        elif row["validating"]:
            row["next_action"] = f"검증 중 {row['validating']}건 게이트 완료"
        elif row["ready"] and row["ready"] == row["unique"] and row["unique"]:
            row["next_action"] = "전 항목 검증 완료 · 병합 검토 가능"
        elif row["unique"]:
            row["next_action"] = "리뷰 진행"
        else:
            row["next_action"] = "스캔 실행"
    return per_repo


def uncovered_candidates(queue_items: list, batches: list,
                         repo: str = "") -> list:
    """Review-terminal dependency candidates with no batch primary.

    Only queue items with status waiting-review and a real dep surface
    here, with integration-pending state and no readiness claim. Queued,
    running, cancelled, deferred, and externally watched items are not
    new unvalidated reviews; question/draft/excluded rows never appear.
    """
    covered = set()
    for b in batches or []:
        for p in b.get("primaries") or []:
            if isinstance(p, dict):
                pkey = primary_result_key(b.get("repo") or "", p)
                if pkey is not None:
                    covered.add(pkey)
    out = []
    for it in queue_items or []:
        if not isinstance(it, dict):
            continue
        if str(it.get("status") or "") not in UNCOVERED_STATUSES:
            continue
        if it.get("watch"):
            continue
        if repo and it.get("repo") != repo:
            continue
        dep = it.get("dep") or ""
        if dep:
            key = candidate_key(it.get("repo"), dep, it.get("target"),
                                it.get("lane"))
            if key in covered:
                continue
            out.append({"key": key, "repo": it.get("repo"), "dep": dep,
                        "target": it.get("target"), "lane": it.get("lane"),
                        "status": it.get("status"),
                        "run_id": it.get("run_id"),
                        "branch": it.get("branch")})
            continue
        # Source13: typed issue-triage items surface with the same
        # distinct issue namespace when strictly identifiable;
        # anything else stays uncovered-implicit (never fabricated).
        # Discussions, PRs and unknown numeric rows are not issues —
        # only an explicit issue-triage kind qualifies.
        if str(it.get("kind") or "") != "issue-triage":
            continue
        inum = _strict_issue_number(it.get("issue"))
        ilane = it.get("lane")
        if ilane not in _KNOWN_LANES:
            ilane = it.get("base_branch")
        if not inum or ilane not in _KNOWN_LANES:
            continue
        key = issue_candidate_key(it.get("repo") or "", ilane, inum)
        if key in covered:
            continue
        out.append({"key": key, "repo": it.get("repo"), "dep": "",
                    "target": "", "lane": ilane,
                    "issue": {"issue": inum, "repo": it.get("repo"),
                              "lane": ilane},
                    "status": it.get("status"),
                    "run_id": it.get("run_id"),
                    "branch": it.get("branch")})
    out.sort(key=lambda e: str(e.get("dep") or e.get("key")))
    return out


# ---- App-owned integration preparation ------------------------------------
# A small deterministic operation (first slice, not a wizard): take one
# review-finished candidate run and one registered base batch, validate both
# sources live against their recorded identities, then create a NEW
# isolated worktree on a NEW codex/review-* branch based on the base
# integration commit and cherry-pick the already-authored candidate commit.
# A candidate range of 2.._MAX_REPLAY_COMMITS commits is replayed in
# recorded order (one cherry-pick per commit, first conflict aborts);
# the op record carries commit_list/replayed_from for it.
#
# Nothing here accepts paths, refs, shell, or branch names: the payload is
# two registry ids (run + batch) and every path/commit derives from the
# registered records. Protected refs are never touched (no push/merge/
# force/reset/clean); only the new branch and the new worktree are
# created. A conflict or drift records blocked, never success. One op per
# (run, batch): a retry returns the recorded operation, never a duplicate
# branch or an overwrite. New-source re-runs need a new reviewed run.
# All runtime gates on the new source start PENDING: base-batch proofs
# are never borrowed for the integrated bytes.

INTEGRATION_OPS_SCHEMA = 1

# Multi-commit candidate replay bound: ranges longer than this are
# refused with a split instruction (Root may raise the constant only
# with a reasoned decision; the refusal names the count).
_MAX_REPLAY_COMMITS = 5


class IntegrationError(Exception):
    """Prepare-operation failure. http_status selects the API mapping:
    404 for unknown registry ids, otherwise 409 (blocked/busy) — never
    a silent success."""

    def __init__(self, message: str, http_status: int = 409):
        super().__init__(message)
        self.http_status = http_status


def integration_ops_dir() -> Path:
    d = paths.home() / "integration_ops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _op_id(run_id: str, batch_id: str) -> str:
    for label, value in (("run", run_id), ("batch", batch_id)):
        if (not isinstance(value, str) or not value or "/" in value
                or value.startswith(".") or len(value) > 128):
            raise IntegrationError(f"unknown {label} id",
                                   http_status=404)
    return f"prep-{run_id}-{batch_id}"


def _batch_card_lanes(card: dict) -> list:
    """Integration lanes of a batch card (port of prepBatchLanes)."""
    lanes = []
    for integ in ((card or {}).get("integrations") or []):
        lane = str((integ or {}).get("lane") or "")
        if lane and lane not in lanes:
            lanes.append(lane)
    return lanes


def _closure_kin(card: dict, by_id: dict) -> int:
    """READY batches inside the inheritance closure (port of
    batchClosureReady): ancestors only, siblings never include each
    other. Cycle- and gap-safe."""
    if not isinstance(card, dict) or not card.get("id"):
        return 0
    seen = {card["id"]}
    kin = 0
    stack = [card["id"]]
    while stack:
        node = by_id.get(stack.pop()) or {}
        inh = node.get("inherited") or []
        if not isinstance(inh, list):
            continue
        for head in inh:
            pid = (head.get("batch") if isinstance(head, dict)
                   else head)
            if not pid or pid in seen:
                continue
            seen.add(pid)
            parent = by_id.get(pid)
            if (isinstance(parent, dict)
                    and (parent.get("ready") or 0) > 0):
                kin += 1
            stack.append(pid)
    return kin


def _inherits(by_id: dict, from_id: str, target_id: str) -> bool:
    seen = {from_id}
    stack = [from_id]
    while stack:
        node = by_id.get(stack.pop()) or {}
        inh = node.get("inherited") or []
        if not isinstance(inh, list):
            continue
        for head in inh:
            pid = (head.get("batch") if isinstance(head, dict)
                   else head)
            if not pid or pid in seen:
                continue
            if pid == target_id:
                return True
            seen.add(pid)
            stack.append(pid)
    return False


def _fully_ready(card: dict) -> bool:
    """Eligible common-ancestor bar (port of prepBaseFullyReady)."""
    return (isinstance(card, dict) and (card.get("ready") or 0) > 0
            and card.get("ready") == card.get("primaries")
            and not (card.get("attention") or []))


def default_base(repo: str, lane: str, cards: list = None,
                 ambiguous: str = "guess") -> dict:
    """Server-side port of the dashboard broadestReady rule (S1b).

    Picks the default prepare base for a candidate lane from the
    batches snapshot (the same observation the UI reads): a unique
    broadest READY batch wins; tied sibling leaves resolve to their
    deepest READY common ancestor; with no eligible ancestor there is
    no auto default. Returns {"id", "kin", "tie", "via_ancestor"}
    with id "" meaning "pick by hand". `cards` injects fixtures in
    tests; production reads the snapshot (cold start refreshes once,
    exactly like /api/batches). `ambiguous="empty"` (R6) refuses the
    newest-updated_at guess on information-poor ties so the M2 driver
    skips with '기준 묶음 선택 필요' instead of guessing; the UI path
    keeps the guess."""
    if cards is None:
        try:
            snap = read_json(batches_snapshot_path(), default=None)
        except Exception:  # noqa: BLE001 - cold start recomputes below
            snap = None
        cards = ((snap or {}).get("batches") or [])
        if not cards:
            try:
                snap = refresh_batches_snapshot("m2-default-base")
            except Exception:  # noqa: BLE001 - no base without state
                return {"id": "", "kin": 0, "tie": 0,
                        "via_ancestor": False}
            cards = ((snap or {}).get("batches") or [])
    listed = [c for c in (cards or [])
              if isinstance(c, dict) and c.get("id")
              and c.get("repo") in (repo, None)]
    by_id = {c["id"]: c for c in listed}
    cands = []
    for card in listed:
        lanes = _batch_card_lanes(card)
        lane_ok = (lane in lanes
                   or (lane == "next" and "main" in lanes
                       and not [c for c in listed
                                if lane in _batch_card_lanes(c)
                                and (c.get("ready") or 0) > 0]))
        if not lane_ok or (card.get("ready") or 0) <= 0:
            continue
        cands.append({"card": card,
                      "kin": _closure_kin(card, by_id)})
    # A next candidate falls back to main-lane batches only when the
    # next lane has no READY batch at all (mirrors the UI rule).
    if not cands and lane == "next":
        for card in listed:
            if ("main" in _batch_card_lanes(card)
                    and (card.get("ready") or 0) > 0):
                cands.append({"card": card,
                              "kin": _closure_kin(card, by_id)})
    if not cands:
        return {"id": "", "kin": 0, "tie": 0, "via_ancestor": False}
    top_kin = max(c["kin"] for c in cands)
    top = [c for c in cands if c["kin"] == top_kin]
    if len(top) == 1:
        return {"id": top[0]["card"]["id"], "kin": top_kin, "tie": 1,
                "via_ancestor": False}
    if not all(isinstance(c["card"].get("inherited"), list)
               for c in top):
        if ambiguous == "empty":
            return {"id": "", "kin": top_kin, "tie": len(top),
                    "via_ancestor": False}
        pick = max(top, key=lambda c: str(c["card"].get(
            "updated_at") or ""))
        return {"id": pick["card"]["id"], "kin": top_kin,
                "tie": len(top), "via_ancestor": False}
    top_ids = {c["card"]["id"] for c in top}
    best, best_kin, best_at = None, -1, ""
    for card in listed:
        if card["id"] in top_ids or not _fully_ready(card):
            continue
        if not all(_inherits(by_id, c["card"]["id"], card["id"])
                   for c in top):
            continue
        kin = _closure_kin(card, by_id)
        at = str(card.get("updated_at") or "")
        if kin > best_kin or (kin == best_kin and at > best_at):
            best, best_kin, best_at = card, kin, at
    if best is not None:
        return {"id": best["id"], "kin": best_kin, "tie": len(top),
                "via_ancestor": True}
    return {"id": "", "kin": 0, "tie": len(top), "via_ancestor": False}


def _slug(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())
            .strip("-")[:80] or "item")


def op_file(op_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,200}",
                        op_id or ""):
        raise IntegrationError("invalid operation id", http_status=404)
    return integration_ops_dir() / (op_id + ".json")


def latest_integration_op(run_id: str) -> dict | None:
    """Newest recorded prepare operation for a run (read-only).

    M2.13: when the newest candidate is a BLOCKED prepare and a
    DONE derived record (op "reresolve", derived_from == that
    prepare's op_id) exists for the same run, the derived record
    wins. The in-prepare re-derive writes both records in the
    same second and the tie used to fall to file-name order
    ("-rederive.json" sorts before ".json"), stranding the run
    under "already recorded" forever. Timestamps and file names
    never decide this override; anything else is unchanged.
    """
    best = None
    seen = []
    every = []
    try:
        files = sorted(integration_ops_dir().glob("prep-*.json"))
    except OSError:
        return None
    for path in files:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        every.append(rec)
        if rec.get("run_id") != run_id:
            continue
        # M2.14b: a blocked joint is not pipeline state — the run
        # falls back to today's answer as if it were not there.
        if (rec.get("kind") == "joint"
                and rec.get("status") != "done"):
            continue
        seen.append(rec)
        if (best is None or str(rec.get("updated_at") or "")
                >= str(best.get("updated_at") or "")):
            best = rec
    # M2.14b: a DONE joint record covering this run supersedes the
    # run's own records — the joint tree is the shippable form of
    # both halves. No timestamp or file-order reliance, as in
    # M2.13. A blocked joint falls through to today's answer.
    joint = None
    for rec in every:
        if rec.get("op") != "reresolve":
            continue
        if rec.get("kind") != "joint":
            continue
        if rec.get("status") != "done":
            continue
        runs = rec.get("runs")
        if not isinstance(runs, list) or run_id not in [
                str(r) for r in runs]:
            continue
        if not ((rec.get("result") or {}).get("head") or ""):
            continue
        if (joint is None
                or (str(rec.get("updated_at") or ""),
                    str(rec.get("op_id") or ""))
                >= (str(joint.get("updated_at") or ""),
                    str(joint.get("op_id") or ""))):
            joint = rec
    if joint is not None:
        return joint
    if (isinstance(best, dict) and best.get("op") == "prepare"
            and best.get("status") == "blocked"):
        src_id = str(best.get("op_id") or "")
        if src_id:
            derived = None
            for rec in seen:
                if rec.get("op") != "reresolve":
                    continue
                if str(rec.get("derived_from") or "") != src_id:
                    continue
                if rec.get("status") != "done":
                    continue
                if rec.get("kind") not in (None, "lockfix",
                                           "rederive"):
                    continue
                if not ((rec.get("result") or {}).get("head") or ""):
                    continue
                if (derived is None
                        or (str(rec.get("updated_at") or ""),
                            str(rec.get("op_id") or ""))
                        >= (str(derived.get("updated_at") or ""),
                            str(derived.get("op_id") or ""))):
                    derived = rec
            if derived is not None:
                return derived
    return best


# M2.7-SEC F3: no app-controlled git mutation ever runs owner
# hooks. Nitro sets core.hooksPath=.husky/_ (pre-commit runs `yarn
# typecheck` + `yarn eslint --fix`, commit-msg runs commitlint via
# npx) — executing that inside prepare/reresolve/family/followup/
# rollup would run dependency code outside the sandbox, with
# network, and --fix would mutate the tree mid-operation. Every
# call through this helper carries hooksPath=/dev/null; commit
# messages stay commitlint-shaped by construction (see the followup
# + rollup message builders).
_GIT_NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]


def _run_git(args: list, cwd: str, timeout: int = 120):
    try:
        argv = ["git"] + _GIT_NO_HOOKS + list(args)
        with git_readonly_child_env(argv, cwd=cwd) as child_env:
            return subprocess.run(argv,
                                  cwd=str(cwd),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=timeout,
                                  text=True,
                                  env=child_env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrationError(f"git {' '.join(args[:2])} failed to "
                               f"start: {exc}") from exc


def _queue_item_for_run(repo: str, run_id: str) -> dict:
    try:
        queue = sched.load(repo).get("queue", [])
    except SystemExit:
        return {}
    for item in queue or []:
        if isinstance(item, dict) and item.get("run_id") == run_id:
            return item
    return {}


def _record_blocked(opf: Path, rec: dict, error: str) -> dict:
    rec["status"] = "blocked"
    rec["error"] = error[:1000]
    rec["updated_at"] = utcnow()
    for gate in rec.get("gates") or []:
        if gate.get("key") == "source-prepared":
            gate["status"] = "blocked"
            gate["note"] = error[:300]
    write_json(opf, rec)
    return rec


def record_branch_cleanup(op_id: str, note: str) -> str:
    """Persist the D3 leftover-branch outcome once per op (E2/S15).

    The first recorded note wins and is returned; later calls return
    the stored text without touching git. Stored at
    result.branch_cleanup so the UI and later ticks reuse the exact
    same wording (no tick-to-tick drift). Never raises: an
    unreadable op reads as "" (the caller then reuses in-memory
    text only)."""
    try:
        opf = op_file(op_id)
        rec = json.loads(opf.read_text(encoding="utf-8"))
    except (OSError, ValueError, IntegrationError):
        return ""
    if not isinstance(rec, dict):
        return ""
    stored = str(((rec.get("result") or {}).get("branch_cleanup")
                  or ""))
    if stored:
        return stored
    try:
        rec.setdefault("result", {})["branch_cleanup"] = str(note)[:300]
        write_json(opf, rec)
    except (OSError, ValueError):
        return ""
    return str(note)[:300]


def remove_leftover_branch_at_base(checkout: str, branch: str,
                                   base_head: str,
                                   result_path: str = "") -> str:
    """Remove an app-created branch left by a blocked prepare (D3).

    Only when ALL hold: the branch tip is EXACTLY base_head (zero own
    commits), no checkout has it checked out (`worktree list`), and
    the removed worktree path is gone from disk. Deletion is plain
    `git branch -d` — never forced, so a branch with commits always
    survives. Returns a Korean note naming the outcome either way."""
    if not branch or not base_head or not checkout:
        return "브랜치 유지됨: 정보 부족"
    if not Path(checkout).is_dir():
        return f"브랜치 {branch} 유지됨: 기준 작업실 확인 불가"
    if result_path and Path(result_path).exists():
        return f"브랜치 {branch} 유지됨: 작업실 잔존"
    try:
        tip = _git_text(["rev-parse", "--verify", branch], checkout)
    except Exception:  # noqa: BLE001 - probe failure keeps branch
        return f"브랜치 {branch} 유지됨: 확인 실패"
    if not tip:
        return "남은 브랜치 없음"
    if tip != base_head:
        return (f"브랜치 {branch} 유지됨 "
                f"(기준과 다른 커밋 {tip[:12]})")
    try:
        listed = _run_git(["worktree", "list", "--porcelain"], checkout)
    except Exception:  # noqa: BLE001 - probe failure keeps branch
        return f"브랜치 {branch} 유지됨: 확인 실패"
    if listed.returncode == 0 and any(
            line.strip() == f"branch refs/heads/{branch}"
            for line in (listed.stdout or "").splitlines()):
        return f"브랜치 {branch} 유지됨: 아직 체크아웃됨"
    try:
        gone = _run_git(["branch", "-d", branch], checkout)
    except Exception:  # noqa: BLE001 - delete failure keeps branch
        return f"브랜치 {branch} 유지됨: 삭제 실패"
    if gone.returncode == 0:
        return (f"브랜치 {branch} 정리됨 "
                f"(기준 head, 자체 커밋 없음)")
    return f"브랜치 {branch} 유지됨: 삭제 거부됨"


def _reconcile_existing(opf: Path, rec: dict, new_path: Path) -> dict:
    """Idempotent retry: never duplicate or overwrite, only re-verify.

    A drifted done-record is projected blocked (and persisted so the stale
    done claim cannot stand); an interrupted started-record is settled to
    blocked without creating anything.
    """
    if rec.get("status") == "started":
        rec = dict(rec)
        _push_attempt(rec, dict(rec))
        rec["status"] = "blocked"
        rec["error"] = ("prior attempt recorded STARTED but never "
                        "completed (interrupted); no duplicate created, "
                        "no overwrite")
        rec["updated_at"] = utcnow()
        for gate in rec.get("gates") or []:
            if gate.get("key") == "source-prepared":
                gate["status"] = "blocked"
                gate["note"] = "interrupted before completion"
        write_json(opf, rec)
        out = dict(rec)
        out["existing"] = True
        out["reconciled"] = True
        return out
    out = dict(rec)
    out["existing"] = True
    if rec.get("status") != "done":
        return out
    live = _snapshot(str(new_path))
    want_head = ((rec.get("result") or {}).get("head") or "")
    want_fp = ((rec.get("result") or {}).get("fingerprint") or "")
    if (live is None or live.get("head") != want_head
            or live.get("fingerprint") != want_fp
            or not live.get("clean")):
        rec = dict(rec)
        _push_attempt(rec, dict(rec))
        rec["status"] = "blocked"
        rec["error"] = ("recorded integration source no longer matches "
                        "the live worktree; operator review required, "
                        "no new branch created")
        rec["updated_at"] = utcnow()
        for gate in rec.get("gates") or []:
            if gate.get("key") == "source-prepared":
                gate["status"] = "blocked"
                gate["note"] = "live source drifted after prepare"
        write_json(opf, rec)
        out = dict(rec)
        out["existing"] = True
        out["live_mismatch"] = rec["error"]
    return out


def _base_candidate_owners(batch: dict) -> dict:
    """candidate_key -> owning batch id for direct + inherited primaries.

    Read-only registry scan over the batch closure (stored records
    only, no snapshots, no mutation). Malformed entries are skipped,
    never trusted as matches. The first (nearest) owner wins so the
    refusal names the most direct provenance.
    """
    owners: dict = {}

    def _collect(b: dict) -> None:
        if not isinstance(b, dict):
            return
        bid = b.get("id") or ""
        repo = b.get("repo") or ""
        for p in b.get("primaries") or []:
            if not isinstance(p, dict):
                continue
            key = primary_result_key(repo, p)
            if key is None:
                continue
            owners.setdefault(key, bid)

    _collect(batch)
    for other in closure([batch]):
        if isinstance(other, dict) and other.get("id") != batch.get("id"):
            _collect(other)
    return owners


def _manifest_only_keys_differ(old_text: str, new_text: str,
                               keys: set) -> bool:
    """True when two manifests differ only in the given dep keys'
    declared ranges (M2.10 group purity: any other line — scripts,
    a non-range value — refuses the re-derive)."""
    try:
        old = json.loads(old_text)
        new = json.loads(new_text)
    except ValueError:
        return False
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False

    def _strip(d):
        d = dict(d)
        for sec in _DEP_SECTIONS:
            if isinstance(d.get(sec), dict):
                sec2 = {k: v for k, v in d[sec].items()
                        if k not in keys}
                d[sec] = sec2
        return d

    return _strip(old) == _strip(new)


def _rederive_eligible(base_worktree: str, base_head: str, dep: str,
                       target: str, sha: str):
    """Read-only purity + base checks for an in-prepare re-derive.

    Inspects the candidate commit itself (parent -> sha): it must
    change ONLY declared range lines in the re-derive manifests,
    plus lockfiles. M2.10: a group bump (react-native + family,
    react + react-dom) derives together — every changed dep must
    still sit on the candidate's `from` range in the current base
    (a moved line is stale/refresh work, never re-derived), and
    every changed dep gets one target across manifests. Returns
    ("ok", {"manifests": [...], "parent": ..., "group": ...})
    | ("no", reason) | ("stale", base_target_version). Never raises,
    never writes.
    """
    try:
        import json as _json
        parent = _run_git(["rev-parse", f"{sha}^"], base_worktree)
        if parent.returncode != 0 or not (parent.stdout or "").strip():
            return ("no", "candidate has no readable parent commit")
        parent_sha = parent.stdout.strip().splitlines()[0].strip()
        files = _run_git(["diff", "--name-only", parent_sha, sha],
                         base_worktree)
        if files.returncode != 0:
            return ("no", "candidate diff unreadable")
        names = [line.strip() for line in
                 (files.stdout or "").splitlines() if line.strip()]
        others = sorted(f for f in names
                        if not _is_lock_rel(f)
                        and f not in REDERIVE_MANIFESTS)
        if others:
            return ("no", f"candidate touches {', '.join(others[:5])} "
                          "besides manifests/locks")
        manifests = sorted(f for f in names if not _is_lock_rel(f))
        if not manifests:
            return ("no", "lock-only conflict — the existing lock "
                          "re-resolve path handles it")
        group = {}
        already = {}
        for path in manifests:
            ptext = _show_file_at(parent_sha, path, base_worktree)
            ctext = _show_file_at(sha, path, base_worktree)
            btext = _show_file_at(base_head, path, base_worktree)
            if ptext is None or ctext is None or btext is None:
                return ("no", f"{path} unreadable in candidate or base")
            try:
                pdoc = _json.loads(ptext)
                cdoc = _json.loads(ctext)
                bdoc = _json.loads(btext)
            except Exception:
                return ("no", f"{path} is not a JSON manifest — "
                              "re-derive unsupported")
            if not all(isinstance(d, dict) for d in (pdoc, cdoc, bdoc)):
                return ("no", f"{path} is not a JSON manifest — "
                              "re-derive unsupported")
            changed = _manifest_changed_ranges(ptext, ctext)
            if not changed:
                return ("no", f"{path} has no range change to re-apply")
            if not _manifest_only_keys_differ(ptext, ctext,
                                              set(changed)):
                return ("no", f"candidate manifest {path} changes more "
                              "than dependency ranges; refusing")
            group.setdefault(path, {})
            already.setdefault(path, {})
            for key, (old_r, new_r) in sorted(changed.items()):
                if not (isinstance(old_r, str) and old_r
                        and isinstance(new_r, str) and new_r
                        and old_r != new_r):
                    return ("no", f"candidate manifest {path} has a "
                                  f"non-range change for {key}; refusing")
                brow = _manifest_dep_ranges(btext, key)
                if brow is None:
                    return ("no", f"기준에 {key} 선언 없음 ({path}) — "
                                  "re-derive unsupported")
                if brow == new_r:
                    # The base already holds this line's target —
                    # skip it (no edit) and record it, never refuse.
                    already[path][key] = (old_r, new_r)
                    continue
                if brow != old_r:
                    return ("no", f"candidate manifest {path} moves "
                                  f"{key} with the group, but the base "
                                  f"holds neither end (base {brow}, "
                                  f"candidate {old_r}->{new_r}) — "
                                  "refresh work, not re-derive")
                group[path][key] = (old_r, new_r)
        touched = {d for per in list(group.values())
                   + list(already.values()) for d in per}
        if dep not in touched:
            return ("no", f"candidate does not touch {dep}'s own range "
                          "line; refusing")
        targets = {}
        for path in set(group) | set(already):
            for key, (_frm, to) in {**(group.get(path) or {}),
                                    **(already.get(path) or {})}.items():
                targets.setdefault(key, set()).add(to)
        split = sorted(d for d, tos in targets.items() if len(tos) > 1)
        if split:
            return ("no", f"candidate gives {split[0]} different targets "
                          "across manifests; refusing")
        ranges = set()
        for path in manifests:
            btext = _show_file_at(base_head, path, base_worktree)
            # A range string is not a set: update() would add its
            # characters one by one and the stale check below could
            # never match (M2.10 found it dead).
            rng = _manifest_dep_ranges(btext or "", dep)
            if rng:
                ranges.add(rng)
        base_holds_target = False
        if ranges:
            blocked = _show_file_at(base_head, "yarn.lock",
                                    base_worktree)
            if blocked:
                res = _declared_resolutions(blocked, dep, ranges)
                base_holds_target = bool(
                    res and all(isinstance(v, str) and v == target
                                for v in res.values()))
        if not any(group.values()):
            # Every line is already in base — a dup, never an
            # empty derive.
            if base_holds_target:
                return ("stale", target)
            return ("no", "every group line is already in base but "
                           f"the base lock does not resolve {dep} to "
                           f"{target}; refusing an empty derive")
        if base_holds_target:
            return ("stale", target)
        return ("ok", {"manifests": manifests, "parent": parent_sha,
                       "group": group, "already": already})
    except Exception as exc:  # noqa: BLE001 - fail closed, no derive
        return ("no", f"eligibility check failed "
                      f"({type(exc).__name__}); refusing")


def _attempt_rederive(opf, rec, *, op_id, repo, run_id, lane, batch_id,
                      dep, target, base_worktree, base_head, work_commit,
                      origin, retry, failed):
    """M2.9a in-prepare re-derive of a pure range bump (M2.10: a
    group bump derives together).

    Settles the original op as blocked (the pick itself failed) and,
    when eligible, derives the range edit(s) on the current base
    through the reresolve machinery (kind "rederive"). Always
    records and returns a record: the done derived record, or the
    blocked original. Single-commit candidates only; lock-only
    conflicts, non-range changes, moved group lines and stale/dup
    shapes keep today's honest block (stale ones keep the stale
    label).
    """
    verdict = _rederive_eligible(base_worktree, base_head, dep, target,
                                 work_commit)
    if verdict[0] == "stale":
        rec["stale"] = {"dep": dep, "target": target,
                        "base_version": verdict[1]}
        return _record_blocked(
            opf, rec,
            f"기준이 이미 {dep}를 {verdict[1]}로 올림 — {target} 후보를 "
            f"현재 기준에서 새로 만들어야 함; {failed}")
    if verdict[0] == "no":
        return _record_blocked(
            opf, rec, f"{failed}; re-derive refused: {verdict[1]}")
    _record_blocked(opf, rec, failed)
    derived_id = op_id + "-rederive"
    dex = read_json(op_file(derived_id), default=None)
    if isinstance(dex, dict) and dex.get("op_id") == derived_id:
        if dex.get("status") == "done" and (
                (dex.get("result") or {}).get("head") or ""):
            return dex
        if not retry:
            return _record_blocked(
                opf, rec,
                f"{failed}; already attempted as {derived_id} — "
                "explicit retry only")
    try:
        drec = _reresolve_on_base_inner(
            op_id, retry=retry, origin=origin, kind="rederive",
            rederive_group=(verdict[1] or {}).get("group"),
            rederive_already=(verdict[1] or {}).get("already"))
    except IntegrationError as exc:
        return _record_blocked(
            opf, rec, f"{failed}; re-derive refused: {str(exc)[:300]}")
    if not isinstance(drec, dict) or drec.get("status") != "done" \
            or not ((drec.get("result") or {}).get("head") or ""):
        err = drec.get("error") if isinstance(drec, dict) else "unknown"
        return _record_blocked(
            opf, rec, f"{failed}; re-derive failed: {str(err)[:300]}")
    _record_blocked(opf, rec,
                    f"{failed}; re-derived as {derived_id} "
                    "(원본 커밋과 다름)")
    # M2.13 belt and braces: the derived record must read
    # strictly newer than the source note above. utcnow() only
    # resolves to seconds and both writes land in the same
    # second, so stamp source-time + 1s (never the clock alone).
    try:
        import datetime as _datetime
        try:
            _src_dt = _datetime.datetime.fromisoformat(
                str(rec.get("updated_at") or ""))
        except ValueError:
            _src_dt = None
        if _src_dt is None:
            _new_at = utcnow()
        else:
            if _src_dt.tzinfo is None:
                _src_dt = _src_dt.replace(
                    tzinfo=_datetime.timezone.utc)
            _new_at = (_src_dt + _datetime.timedelta(
                seconds=1)).isoformat(timespec="seconds")
        drec["updated_at"] = _new_at
        write_json(op_file(derived_id), drec)
    except (OSError, ValueError, IntegrationError):
        pass
    return drec


def prepare_rederive_hint(repo: str, op_id: str) -> dict | None:
    """M2.11c glance hint (read-only except one bounded cache key).

    For a BLOCKED prepare whose error is a cherry-pick conflict,
    runs _rederive_eligible once per (op_id, current lane base
    head) against the CURRENT default base — not the op's pinned
    one. The outcome is cached on the op record as
    rederive_hint {base, verdict, note, at}; a fresh cache entry
    for the same base head is reused without recomputing. Any
    other op (or an unreadable base) yields None: no hint, and
    the existing reason stays untouched. Never raises.
    """
    try:
        return _prepare_rederive_hint_inner(repo, op_id)
    except (Exception, SystemExit):  # noqa: BLE001 - hint is advisory
        return None


def _prepare_rederive_hint_inner(repo: str, op_id: str) -> dict | None:
    rec = read_json(op_file(op_id), default=None)
    if not isinstance(rec, dict):
        return None
    if rec.get("op") != "prepare" or rec.get("status") != "blocked":
        return None
    if "cherry-pick" not in str(rec.get("error") or ""):
        return None
    lane = str(rec.get("lane") or "")
    dep = str(rec.get("dep") or "")
    target = str(rec.get("target") or "")
    if not lane or not dep or not target:
        return None
    try:
        base_id = str((default_base(repo, lane) or {}).get("id")
                      or "")
    except (Exception, SystemExit):  # noqa: BLE001 - no base, no hint
        return None
    if not base_id:
        return None
    base_batch = read_json(batch_file(base_id), default=None)
    if not isinstance(base_batch, dict):
        return None
    head = ""
    worktree = ""
    for integ in (base_batch.get("integrations") or []):
        if not isinstance(integ, dict):
            continue
        if str(integ.get("lane") or "") != lane:
            continue
        head = str(integ.get("head") or "")
        cand_path = str(integ.get("worktree") or integ.get("path")
                        or "")
        if cand_path and Path(cand_path).is_dir():
            worktree = cand_path
        break
    if not head:
        return None
    cached = rec.get("rederive_hint")
    if isinstance(cached, dict) and cached.get("base") == head \
            and cached.get("verdict") in ("ok", "no", "stale"):
        return cached
    if not worktree:
        try:
            worktree = str((repos.load(repo) or {}).get("checkout")
                           or "")
        except (Exception, SystemExit):  # noqa: BLE001 - no tree
            worktree = ""
    if not worktree or not Path(worktree).is_dir():
        return None
    cand = rec.get("candidate") or {}
    commits = cand.get("commit_list") or []
    sha = str(commits[0] if commits else (cand.get("head") or ""))
    if not sha:
        return None
    verdict, payload = _rederive_eligible(worktree, head, dep,
                                          target, sha)
    note = ""
    if verdict == "ok":
        others = [e for e in _group_entries(
            (payload or {}).get("group"))
            if not e.startswith(dep + " ")] if dep else []
        if others:
            note = f"외 {len(others)}개 같이 올림"
    elif verdict == "stale":
        note = f"이미 기준에 {payload}"
    else:
        note = str(payload or "")[:300]
    hint = {"base": head, "verdict": verdict, "note": note,
            "at": utcnow()}
    try:
        with m2.op_locked(op_id):
            fresh = read_json(op_file(op_id), default=None)
            if isinstance(fresh, dict):
                fresh["rederive_hint"] = hint
                write_json(op_file(op_id), fresh)
    except m2.OpBusy:
        # A check is running on this op: return the fresh hint for
        # this render, cache it next time. Never wait, never fail.
        pass
    return hint


@m2.op_guarded(lambda run_id, batch_id, **_: _op_id(run_id, batch_id),
                  busy=lambda op: IntegrationError(
                      f"다른 확인 작업이 {op}에서 실행 중입니다", 409))
def prepare_integration(run_id: str, batch_id: str,
                        retry: bool = False,
                        origin: str = "ui") -> dict:
    """Prepare one isolated integration worktree (app-owned, synchronous).

    Holds no lease itself: the caller (HTTP/CLI/daemon driver) acquires
    the global worker lease first so contention reports busy instead of
    overlapping. `origin` ("ui" or "m2") records who started the op.
    Raises IntegrationError on every refused path; returns the persisted
    operation record on success AND on recorded-blocked outcomes.
    With retry=True an existing BLOCKED record re-runs explicitly
    (attempt archived, leftover branch verified); otherwise the
    existing record reconciles and a blocked op stays terminal.
    """
    op_id = _op_id(run_id, batch_id)
    try:
        state = store.load(run_id)
    except SystemExit:
        raise IntegrationError(f"unknown run '{run_id}'",
                               http_status=404) from None
    try:
        batch = load_batch(batch_id)
    except BatchError:
        raise IntegrationError(f"unknown batch '{batch_id}'",
                               http_status=404) from None
    repo = state.get("repo") or ""
    if not repo or batch.get("repo") != repo:
        raise IntegrationError("run and batch belong to different repos")
    queue_item = _queue_item_for_run(repo, run_id)
    dep = str(state.get("dep") or queue_item.get("dep") or "")
    target = str(state.get("target") or queue_item.get("target") or "")
    issue_ctx = None
    if dep and target:
        lane = str(queue_item.get("lane") or state.get("lane") or "")
    else:
        # Source13: typed issue path. A partial dep/target identity or
        # a bare run.issue with sourceKind git proves nothing on its
        # own; admission requires the app-owned issue-triage queue
        # record via _resolve_issue_candidate, which raises before any
        # write on unknown/discussion/PR/mixed/contradictory identity.
        if dep or target:
            raise IntegrationError(
                "run carries a partial dep/target identity with no "
                "matched issue provenance; prepare refused")
        issue_ctx = _resolve_issue_candidate(repo, run_id)
        dep, target = "", ""
        lane = issue_ctx["lane"]
    # Candidate proof uses the same strong acceptance contract as batch
    # evaluation (factored as _candidate_proof so the M2 driver
    # pre-flights exactly this gate): review+e2e must be FRESH against
    # the live bytes (missing/stale/unknown fail closed). Status-done
    # alone never greens.
    work_commit, work_fp, proof_error = _candidate_proof(state, run_id)
    if proof_error:
        raise IntegrationError(proof_error)
    cand_worktree = str(store.run_dir(run_id) / "work")
    integrations = [i for i in (batch.get("integrations") or [])
                    if isinstance(i, dict)]
    if not integrations:
        raise IntegrationError("base batch records no integration source")
    # Lane anchoring (fail closed, before any mutation): a main-lane
    # candidate may only anchor a main-lane integration, so a breaking
    # next baseline is never labelled main-stable. A next-lane candidate
    # may anchor an accepted main integration (existing N workflow) or a
    # next one. Unknown/malformed lanes on either side refuse outright.
    if lane not in ("main", "next"):
        raise IntegrationError("run records no usable lane "
                               f"({lane!r}); refusing to anchor integration")
    if len(integrations) == 1:
        base_integ = integrations[0]
    else:
        matches = [i for i in integrations
                   if str(i.get("lane") or "") == lane]
        if len(matches) != 1:
            raise IntegrationError("base batch has several integrations "
                                   "and no single lane match")
        base_integ = matches[0]
    base_lane = str(base_integ.get("lane") or "")
    if lane == "main" and base_lane != "main":
        raise IntegrationError("main-lane candidate cannot anchor "
                               f"{base_lane or 'unknown'}-lane base "
                               f"integration of batch '{batch_id}'")
    if lane == "next" and base_lane not in ("main", "next"):
        raise IntegrationError("next-lane candidate cannot anchor "
                               f"{base_lane or 'unknown'}-lane base "
                               f"integration of batch '{batch_id}'")
    base_worktree = str(base_integ.get("worktree") or "")
    base_head = str(base_integ.get("head") or "")
    base_fp = str(base_integ.get("fingerprint") or "")
    if not base_worktree or not base_head or not base_fp:
        raise IntegrationError("base integration records no exact source")
    base_snap = _snapshot(base_worktree)
    if (base_snap is None or base_snap.get("head") != base_head
            or base_snap.get("fingerprint") != base_fp
            or not base_snap.get("clean")):
        raise IntegrationError("base integration source drifted or "
                               "unreadable")
    # The base batch itself must be READY under live evaluation (a
    # failed/pending batch can never anchor an integration server-side;
    # UI filtering alone is not a gate).
    base_live, base_diag = live_map_with_diag(closure([batch]))
    base_ev = evaluate(batch, base_live, live_diag=base_diag, record=True)
    base_results = base_ev.get("results") or []
    base_unready = [r for r in base_results if not r.get("ready")]
    if not base_results or base_unready:
        first_reasons = "; ".join(
            str(reason) for r in base_unready[:2]
            for reason in (r.get("reasons") or [])[:2]) or "no ready results"
        leafs = "; ".join(
            str(r.get("leaf") or "") for r in base_unready[:2]
            if r.get("leaf"))[:_LEAF_TOTAL_CHARS]
        suffix = (" leaf: " + leafs) if leafs else ""
        raise IntegrationError("base batch is not READY "
                               f"({first_reasons}){suffix}")
    # Scope: only a single non-merge commit range (baseCommit..workCommit)
    # is replayed in this slice. Anything else is rejected BEFORE any
    # mutation — a partial candidate is never marked applied.
    base_commit = str(state.get("baseCommit") or "")
    if not base_commit:
        raise IntegrationError("run records no base commit "
                               "(insufficient evidence)")
    listed = _run_git(["rev-list", "--reverse",
                       f"{base_commit}..{work_commit}"], cand_worktree)
    rev_list = [r.strip() for r in (listed.stdout.splitlines()
                                    if listed.returncode == 0 else [])
                if r.strip()]
    if listed.returncode != 0 or not rev_list:
        raise IntegrationError("candidate has no replayable commits in "
                               f"base {base_commit[:12]}..work "
                               f"{work_commit[:12]}; refusing before any "
                               "mutation")
    if len(rev_list) > _MAX_REPLAY_COMMITS:
        raise IntegrationError(f"candidate is a {len(rev_list)}-commit "
                               f"range; multi-commit replay supports at "
                               f"most {_MAX_REPLAY_COMMITS} — split the "
                               "candidate")
    if rev_list[-1] != work_commit:
        raise IntegrationError("candidate history moved during prepare "
                               f"(recorded {work_commit[:12]}, range tip "
                               f"{rev_list[-1][:12]}); refusing")
    merged = _run_git(["rev-list", "--merges",
                       f"{base_commit}..{work_commit}"], cand_worktree)
    merge_list = [r.strip() for r in (merged.stdout.splitlines()
                                      if merged.returncode == 0 else [])
                  if r.strip()]
    if merged.returncode == 0 and merge_list:
        raise IntegrationError("candidate range contains merge commit "
                               f"{merge_list[0][:12]}; merge replay refused "
                               "before any mutation")
    multi = len(rev_list) > 1
    commit_list = []
    if multi:
        for sha in rev_list:
            shown = _run_git(["show", "-s", "--format=%s", sha],
                             cand_worktree)
            subject = (shown.stdout.strip().splitlines() or [""])[0][:160]
            commit_list.append({"head": sha,
                                "subject": subject or sha[:12]})
    if issue_ctx is not None:
        # Source13: the ORIGINAL candidate branch stays the queue-owned
        # codex/ branch (validated, recorded as candidate.branch). The
        # integration itself gets a NEW separate app-owned branch: the
        # unchanged _git_head collision guard would reject the original
        # branch because it already exists in the shared Git store, and
        # reusing/moving it is forbidden. The digest binds repo/lane/
        # issue/run/batch so retries of the same op reconcile stably
        # while unrelated collisions still refuse.
        _ib = _strict_issue_branch(issue_ctx.get("branch"))
        if _ib is None:
            raise IntegrationError(
                "issue queue branch is not a safe owned codex/ branch; "
                "prepare refused")
        _idig = hashlib.sha256(
            "|".join((repo, lane, issue_ctx["number"], run_id,
                      batch_id)).encode("utf-8")).hexdigest()[:12]
        slug = (f"prep-{_slug(batch_id)}-issue-{issue_ctx['number']}-"
                f"{_idig}")
        new_path = paths.home() / "reviews" / slug
        branch = f"codex/review-issue-{issue_ctx['number']}-{_idig}"
    else:
        slug = (f"prep-{_slug(batch_id)}-{_slug(dep)}-{_slug(target)}")
        new_path = paths.home() / "reviews" / slug
        branch = f"codex/review-{_slug(dep)}-{_slug(target)}"
    opf = op_file(op_id)
    existing = read_json(opf, default=None)
    if isinstance(existing, dict) and existing.get("op_id") == op_id:
        if not retry:
            return _reconcile_existing(opf, existing, new_path)
        if str((existing.get("family") or {}).get("role") or "") == \
                "primary":
            # M2.6: a family primary re-prepared as a single would
            # silently drop its siblings — retry the family as one
            # through prepare_family_integration instead.
            raise IntegrationError(
                "operation is a family prepare; retry it as a family, "
                "not as a single candidate")
        _prepare_retry(opf, existing)
        # Fall through: the fresh record below archives this blocked
        # state via _push_attempt (chain-safe).
    # Duplicate guard (read-only, before any mutation): a candidate
    # already present in the requested base — as a direct primary or
    # through an inherited batch — is refused with its provenance
    # instead of preparing a redundant integration. Recorded retries of
    # the same (run, batch) operation still reconcile above; this only
    # blocks creating a NEW redundant one. No op file, worktree,
    # branch, job, or model call is created on this path.
    if issue_ctx is not None:
        cand_key = issue_candidate_key(repo, lane,
                                       issue_ctx["number"])
        cand_label = (f"issue #{issue_ctx['number']} ({lane})")
    else:
        cand_key = candidate_key(repo, dep, target, lane)
        cand_label = f"candidate {dep} {target} ({lane})"
    owner = _base_candidate_owners(batch).get(cand_key)
    if owner:
        if owner == batch_id:
            where = "as a direct primary"
        else:
            where = f"through inherited batch '{owner}'"
        raise IntegrationError(
            f"{cand_label} is already present "
            f"in base batch '{batch_id}' {where}; inspect the "
            f"registered integration instead of preparing a duplicate")
    if new_path.exists():
        raise IntegrationError("integration path already exists "
                               f"(unrelated): {new_path}")
    if _git_head(base_worktree, branch):
        raise IntegrationError("branch already exists "
                               f"(unrelated): {branch}")
    rec = {"schema": INTEGRATION_OPS_SCHEMA, "op": "prepare",
           "op_id": op_id, "repo": repo, "run_id": run_id,
           "batch_id": batch_id, "dep": dep, "target": target,
           "lane": lane, "origin": str(origin or "ui"),
           **({"issue": {
               "issue": issue_ctx["number"], "repo": repo,
               "lane": lane, "url": issue_ctx["url"],
               "branch": branch}}
               if issue_ctx is not None else {}),
           "candidate": {"head": work_commit, "fingerprint": work_fp,
                         "branch": str(state.get("branch") or queue_item.get("branch") or ""),
                         "worktree": cand_worktree},
           "base": {"batch": batch_id,
                    "lane": str(base_integ.get("lane") or ""),
                    "head": base_head, "fingerprint": base_fp,
                    "checkout": base_worktree},
           "result": {"path": str(new_path), "branch": branch,
                      "head": "", "fingerprint": "",
                      "proof_note": "base-batch proofs never apply to the "
                      "integrated bytes; all runtime gates start pending"},
           "gates": [{"key": "source-prepared", "status": "pending",
                      "note": ""},
                     {"key": "fastchecks", "status": "pending",
                      "note": "not run in this step"},
                     {"key": "native", "status": "pending",
                      "note": "Root device QA after source freeze"}],
           "status": "started", "error": "",
           "created_at": utcnow(), "updated_at": utcnow()}
    if multi:
        rec["candidate"]["commit_list"] = commit_list
        rec["result"]["replayed_from"] = list(rev_list)
    if isinstance(existing, dict):
        # P-HIST: a legacy record under this op id is never erased;
        # its evidence moves into the bounded attempts list.
        _push_attempt(rec, existing)
    write_json(opf, rec)
    try:
        added = _run_git(["worktree", "add", "-b", branch, str(new_path),
                          base_head], base_worktree)
        if added.returncode != 0:
            return _record_blocked(
                opf, rec, "worktree add failed: "
                f"{(added.stderr or added.stdout or '').strip()[-500:]}")
        pick_order = rev_list if multi else [work_commit]
        failed = ""
        for pos, sha in enumerate(pick_order, 1):
            pick = _run_git(["-c", "user.name=openmainty",
                             "-c", "user.email=openmainty@localhost",
                             "cherry-pick", sha], str(new_path))
            if pick.returncode == 0:
                continue
            # U1: record the conflicted paths BEFORE the abort erases
            # them, so the UI can label the conflict scope honestly.
            seen = _conflicted_paths(str(new_path))
            if seen:
                rec["result"]["conflicted_paths"] = sorted(seen)[:20]
            # FIX-a4: a "conflict" that is really a stale candidate
            # (the base moved the bumped dep's own line after the
            # candidate was cut) is labelled with the base version
            # right here, from read-only blobs — never guessed.
            stale_hit = _stale_label_for_pick(
                dep, target, seen, base_worktree, cand_worktree,
                base_head, sha)
            if isinstance(stale_hit, dict):
                rec["stale"] = {"dep": dep, "target": target,
                                "base_version": stale_hit[
                                    "base_version"]}
            abort = _run_git(["cherry-pick", "--abort"], str(new_path))
            removed = _run_git(["worktree", "remove", str(new_path)],
                               base_worktree)
            if removed.returncode == 0 and not new_path.exists():
                cleanup = "new worktree removed"
            else:
                cleanup = (f"conflict workspace preserved at {new_path} "
                           f"(abort rc={abort.returncode}, remove "
                           f"rc={removed.returncode})")
            detail = (pick.stderr or pick.stdout or '').strip()[-500:]
            if multi:
                subj = commit_list[pos - 1]["subject"] if pos - 1 < len(
                    commit_list) else sha[:12]
                rec["result"]["replay_failed_at"] = {
                    "pos": pos, "total": len(pick_order),
                    "head": sha, "subject": subj}
                failed = (f"cherry-pick conflicted or failed at commit "
                          f"{pos}/{len(pick_order)} {sha[:12]} ({subj}): "
                          f"{detail}; no success claimed, {cleanup}")
            else:
                failed = ("cherry-pick conflicted or failed: "
                          f"{detail}; no success claimed, {cleanup}")
            if not multi and not isinstance(stale_hit, dict):
                # M2.9a: a pure range bump conflicting on lines the
                # base moved is re-derived on the current base right
                # here (derived source, never the original commit).
                # Anything else keeps today's honest block below.
                return _attempt_rederive(
                    opf, rec, op_id=op_id, repo=repo, run_id=run_id,
                    lane=lane, batch_id=batch_id, dep=dep, target=target,
                    base_worktree=base_worktree, base_head=base_head,
                    work_commit=sha, origin=origin, retry=retry,
                    failed=failed)
            if isinstance(stale_hit, dict):
                # FIX-a4: the stale sentence leads the record's
                # error — pos/detail/cleanup notes stay behind it.
                # rec["stale"] above marks it for the driver,
                # glance and metrics.
                failed = f"{stale_hit['label']}; {failed}"
            break
        if failed:
            return _record_blocked(opf, rec, failed)
        res_snap = _snapshot(str(new_path))
        if (res_snap is None or not res_snap.get("head")
                or not res_snap.get("fingerprint")
                or not res_snap.get("clean")):
            return _record_blocked(opf, rec, "integrated source unreadable "
                                   "or dirty after cherry-pick")
    except Exception as exc:
        # A crash/timeout after the STARTED write must not leave a phantom
        # started record: settle it blocked, then report the failure.
        _record_blocked(opf, rec, f"interrupted: {type(exc).__name__}: "
                                  f"{exc}"[:1000])
        raise
    rec["result"]["head"] = res_snap["head"]
    rec["result"]["fingerprint"] = res_snap["fingerprint"]
    rec["status"] = "done"
    rec["updated_at"] = utcnow()
    for gate in rec["gates"]:
        if gate["key"] == "source-prepared":
            gate["status"] = "pass"
            gate["note"] = (f"{branch} @ {res_snap['head'][:12]} "
                            f"fp {res_snap['fingerprint'][:12]}")
    write_json(opf, rec)
    return rec


class _op_exclusion:
    """Re-entrant per-op exclusion for issue paths.

    m2.op_locked is LOCK_NB: re-acquiring an already-held op raises
    OpBusy instead of deadlocking. The register path holds the op lock
    across import_batch, whose issue binder performs the same checks;
    this wrapper makes the inner acquisition a no-op so the register
    flow never 409s itself, while a direct import still contends for
    the lock. Lock order stays global lease (serve layer) -> op.
    """

    _held = threading.local()

    def __init__(self, op_id: str):
        self._op_id = op_id
        self._cm = None
        self._mine = False

    def __enter__(self):
        held = getattr(_op_exclusion._held, "ids", None)
        if held is None:
            held = set()
            _op_exclusion._held.ids = held
        if self._op_id in held:
            return None
        self._cm = m2.op_locked(self._op_id)
        self._cm.__enter__()
        held.add(self._op_id)
        self._mine = True
        return None

    def __exit__(self, exc_type, exc, tb):
        if not self._mine:
            return False
        held = getattr(_op_exclusion._held, "ids", None)
        if held is not None:
            held.discard(self._op_id)
        try:
            return self._cm.__exit__(exc_type, exc, tb)
        finally:
            self._cm = None
            self._mine = False


def _expected_issue_sandbox(cfg: dict) -> object:
    """Recorded sandbox value the writer stores for a configured step.

    Mirrors the actual writer policy: install steps run unsandboxed,
    sandbox == "loopback" becomes "loopback-only" ONLY when the writer
    also accepts the argv (_allows_loopback: flutter test), everything
    else is "strict".
    """
    from . import fastcheck as _fc
    install = bool(cfg.get("install", False))
    if install:
        return False
    if str(cfg.get("sandbox") or "") == "loopback" \
            and _fc._allows_loopback(list(cfg.get("argv") or [])):
        return "loopback-only"
    return "strict"


def _validated_issue_receipt(op_id: str, rec: dict) -> tuple:
    """Validate the live worker-attested fastcheck receipt for an issue op.

    Reads ONLY actual shipped shapes: the registered profile's nested
    fastcheck section (repos.load(repo).get("fastcheck").steps — the same
    mapping _run_fastcheck_once uses), the receipt at
    fastcheck.op_fastcheck_path(op_id) whose per-step head/fingerprint
    the runner stamps from the op result, and the op's own fastchecks
    gate (status pass, attested_by worker, receipt_sha256) as
    _update_op_gate actually writes. Returns (receipt, configured_steps).
    Raises IntegrationError on every missing/stale/mismatched shape;
    never manufactures a receipt.
    """
    from . import fastcheck as _fc
    try:
        checked_op = m2.check_op_id(str(op_id))
    except ValueError as exc:
        raise IntegrationError(f"invalid operation id {op_id!r}",
                               http_status=404) from exc
    result = rec.get("result") if isinstance(rec, dict) else None
    if not isinstance(result, dict):
        raise IntegrationError("issue op binding has no prepared result")
    rhead = str(result.get("head") or "")
    rfp = str(result.get("fingerprint") or "")
    rpath = str(result.get("path") or "")
    if not (rhead and rfp and rpath):
        raise IntegrationError("issue op binding has no prepared source")
    repo = str(rec.get("repo") or "")
    try:
        profile = repos.load(repo)
    except (Exception, SystemExit) as exc:
        raise IntegrationError(
            f"registered profile unreadable: {exc}") from exc
    if not isinstance(profile, dict):
        raise IntegrationError("registered profile is not an object")
    fc_profile = profile.get("fastcheck")
    if not isinstance(fc_profile, dict):
        raise IntegrationError("registered profile has no fastcheck "
                               "section; register refused")
    configured = fc_profile.get("steps") or []
    if not isinstance(configured, list) or not configured:
        raise IntegrationError("registered profile has no check steps; "
                               "register refused")
    for cfg in configured:
        # The writer executes whatever is listed, but a registerable
        # contract needs complete step identities to compare against.
        if not isinstance(cfg, dict) \
                or not isinstance(cfg.get("argv") or None, list) \
                or not (cfg.get("argv") or []) \
                or not str(cfg.get("gate") or ""):
            raise IntegrationError("registered check step is incomplete; "
                                   "register refused")
    try:
        receipt = read_json(_fc.op_fastcheck_path(checked_op), default=None)
    except (Exception, SystemExit):  # noqa: BLE001 - unreadable refuses
        receipt = None
    if not isinstance(receipt, dict):
        raise IntegrationError("no fastcheck receipt for this op; "
                               "register refused")
    if str(receipt.get("op") or "") != checked_op:
        raise IntegrationError("fastcheck receipt is for another op")
    if receipt.get("ok") is not True:
        raise IntegrationError("fastcheck receipt is not passing")
    if receipt.get("worker_attested") is not True \
            or str(receipt.get("attestor") or "") != "worker":
        raise IntegrationError("fastcheck receipt is not worker-attested")
    if receipt.get("root_attested") is not False:
        raise IntegrationError("fastcheck receipt root attestation "
                               "unexpected")
    executed = receipt.get("steps")
    if not isinstance(executed, list) \
            or len(executed) < len(configured):
        raise IntegrationError("fastcheck receipt steps are missing or "
                               "partial; register refused")
    for pos, cfg in enumerate(configured):
        ex = executed[pos]
        if not isinstance(ex, dict):
            raise IntegrationError("fastcheck receipt step is not an object")
        if list(ex.get("argv") or []) != list(cfg.get("argv") or []):
            raise IntegrationError("fastcheck steps changed since the "
                                   "receipt (argv); re-run, do not reuse")
        install = bool(cfg.get("install", False))
        if bool(ex.get("install", False)) != install:
            raise IntegrationError("fastcheck steps changed since the "
                                   "receipt (install); re-run, do not reuse")
        want_timeout = cfg.get("timeout_s") \
            or (1200 if install else 600)
        if (ex.get("timeout_s") or 0) != want_timeout:
            raise IntegrationError("fastcheck steps changed since the "
                                   "receipt (timeout); re-run, do not reuse")
        if str(ex.get("gate") or "") != str(cfg.get("gate") or ""):
            raise IntegrationError("fastcheck steps changed since the "
                                   "receipt (gate); re-run, do not reuse")
        if ex.get("network_sandbox") != _expected_issue_sandbox(cfg):
            raise IntegrationError("fastcheck steps changed since the "
                                   "receipt (sandbox); re-run, do not reuse")
        if str(ex.get("head") or "") != rhead \
                or str(ex.get("fingerprint") or "") != rfp:
            raise IntegrationError("fastcheck step is not on the prepared "
                                   "bytes")
        # run_step always writes an explicit numeric rc and a boolean
        # timed_out; a missing/None/False/empty rc is not a success.
        if type(ex.get("rc")) is not int or ex.get("rc") != 0:
            raise IntegrationError("fastcheck step has no recorded "
                                   "success")
        if ex.get("timed_out") is not False:
            raise IntegrationError("fastcheck step did not pass")
        # The log origin must be the canonical per-step path the writer
        # uses — a random readable path with a matching SHA cannot
        # substitute for the op's own evidence.
        want_log = str(paths.home() / "integration_ops"
                       / f"{checked_op}.fastcheck.{pos}.log")
        log_path = str(ex.get("log") or "")
        if log_path != want_log:
            raise IntegrationError("fastcheck step log is not the op's "
                                   "recorded log; register refused")
        try:
            with open(log_path, "rb") as fh:
                log_bytes = fh.read()
        except OSError as exc:
            raise IntegrationError("fastcheck log unreadable; register "
                                   f"refused: {exc}") from exc
        if hashlib.sha256(log_bytes).hexdigest() != str(
                ex.get("log_sha256") or ""):
            raise IntegrationError("fastcheck log does not match the "
                                   "receipt; register refused")
    extras = executed[len(configured):]
    if len(extras) > 1:
        raise IntegrationError("fastcheck receipt carries unexpected "
                               "extra steps")
    cons = receipt.get("consumer")
    if extras:
        # Consumer extras use the actual writer shape: run_step fields
        # with gate "consumer" but WITHOUT head/fingerprint stamps, bound
        # to the recorded summary.consumer entry. A failing consumer
        # already flips ok False; anything else is unexpected.
        if not isinstance(cons, dict) or cons.get("status") != "pass":
            raise IntegrationError("fastcheck receipt carries an unexpected "
                                   "extra step")
        ex = extras[0]
        if not isinstance(ex, dict) \
                or str(ex.get("gate") or "") != "consumer":
            raise IntegrationError("fastcheck receipt carries an unexpected "
                                   "extra step")
        if type(ex.get("rc")) is not int or ex.get("rc") != 0:
            raise IntegrationError("fastcheck consumer step has no "
                                   "recorded success")
        if ex.get("timed_out") is not False:
            raise IntegrationError("fastcheck consumer step did not pass")
        # Bind to the actual op consumer log: the writer's canonical
        # per-op path, readable bytes matching the step's recorded
        # SHA, and the summary consumer record citing the same path
        # with its own passing fields. An arbitrary readable path or
        # an unreadable log never satisfies a configured receipt.
        want_cons_log = str(paths.home() / "integration_ops"
                            / f"{checked_op}.fastcheck.consumer.log")
        if str(ex.get("log") or "") != want_cons_log \
                or str(cons.get("log") or "") != want_cons_log:
            raise IntegrationError("fastcheck consumer step does not bind "
                                   "the op's recorded consumer log; "
                                   "register refused")
        try:
            with open(want_cons_log, "rb") as fh:
                cons_bytes = fh.read()
        except OSError as exc:
            raise IntegrationError("fastcheck consumer log unreadable; "
                                   f"register refused: {exc}") from exc
        if hashlib.sha256(cons_bytes).hexdigest() != str(
                ex.get("log_sha256") or ""):
            raise IntegrationError("fastcheck consumer log does not match "
                                   "the receipt; register refused")
        if type(cons.get("rc")) is not int or cons.get("rc") != 0 \
                or cons.get("timed_out") is not False \
                or list(ex.get("argv") or []) != list(
                    cons.get("argv") or []):
            raise IntegrationError("fastcheck consumer record does not "
                                   "describe the recorded pass; register "
                                   "refused")
    # The op's own fastchecks gate must be a passing worker gate citing
    # this exact receipt bytes, as _update_op_gate actually writes.
    gate_status = ""
    gate_by = ""
    gate_sha = ""
    for gate in rec.get("gates") or []:
        if isinstance(gate, dict) and gate.get("key") == "fastchecks":
            gate_status = str(gate.get("status") or "")
            gate_by = str(gate.get("attested_by") or "")
            gate_sha = str(gate.get("receipt_sha256") or "")
            break
    try:
        live_sha = _fc._receipt_sha(_fc.op_fastcheck_path(checked_op))
    except OSError:
        live_sha = ""
    if gate_status != "pass" or gate_by != "worker":
        raise IntegrationError("op fastchecks gate is not a passing worker "
                               "gate; re-run, do not reuse")
    if not gate_sha or gate_sha != live_sha:
        raise IntegrationError("op fastchecks gate does not cite the "
                               "current receipt bytes; re-run, do not reuse")
    want_tc = [str(d) for d in (fc_profile.get("toolchain") or []) if d]
    if list(receipt.get("toolchain") or []) != want_tc:
        raise IntegrationError("fastcheck toolchain changed since the "
                               "receipt; re-run, do not reuse")
    return receipt, configured


def _bind_issue_primaries(repo: str, origin_m2: str,
                          primaries: list,
                          integrations_raw: list) -> None:
    """Bind issue primaries to the exact app-owned prepare op.

    For every validated issue primary: the manifest must carry the
    server-known op id (origin_m2); that op is loaded through the
    existing flat registry (op_file/<op_id>.json — no invented
    directory, no first-match scan, no rebind to another op) and must
    be a done prepare whose run/repo/issue/lane match. The batch's
    lane integration must point at the op's exact result worktree, and
    at least one passing worker gate must record the result
    head/fingerprint. A current successful worker-attested fastcheck
    receipt for the op on those exact bytes is required (same shared
    check the register route uses). The fastcheck/profile/evidence
    reads are read-only; this binder writes nothing.
    """
    issue_ps = [p for p in primaries
                if isinstance(p, dict) and isinstance(p.get("issue"),
                                                      dict)]
    if not issue_ps:
        return
    if not origin_m2:
        raise BatchError("issue primaries require an app-owned prepare "
                         "op binding (origin_m2); manual manifests without "
                         "a completed app prepare refuse")
    # import_batch holds the op exclusion BEFORE the repo lock for
    # typed issue imports and retains it through the commit, so a stale
    # result, a branch switch, or a race with fastcheck/followup
    # cannot slip between validation and commit. Re-entrant: the
    # register path (and import_batch itself) already owns this op's
    # lock, where this inner acquisition is a no-op instead of a
    # self-409. Direct /api/batch/import calls also contend here.
    try:
        with _op_exclusion(str(origin_m2 or "")):
            _bind_issue_primaries_locked(repo, origin_m2, primaries,
                                         integrations_raw, issue_ps)
    except m2.OpBusy as exc:
        raise BatchError(f"issue op {origin_m2!r} is busy; import "
                         "refused") from exc
    except ValueError as exc:
        raise BatchError(
            f"invalid issue op id {origin_m2!r}") from exc


def _bind_issue_primaries_locked(repo: str, origin_m2: str,
                                 primaries: list,
                                 integrations_raw: list,
                                 issue_ps: list) -> None:
    """_bind_issue_primaries body under the op exclusion."""
    try:
        _, rec = _load_op(str(origin_m2))
    except IntegrationError as exc:
        raise BatchError(f"issue op binding unreadable: {exc}") from exc
    if str(rec.get("op") or "") != "prepare" \
            or str(rec.get("status") or "") != "done":
        raise BatchError("issue op binding is not a completed prepare")
    if str(rec.get("repo") or "") != repo:
        raise BatchError("issue op binding repo mismatch")
    rblk = rec.get("issue")
    if not isinstance(rblk, dict):
        raise BatchError("issue op binding carries no issue identity")
    # LIVE origin/result check: a direct import must refuse a
    # stale-result or branch-switched op exactly like the register
    # route does, not just compare worktree strings.
    try:
        _check_op_source(rec)
    except IntegrationError as exc:
        raise BatchError(f"issue op source moved since prepare: "
                         f"{exc}") from exc
    try:
        _validated_issue_receipt(str(origin_m2), rec)
    except IntegrationError as exc:
        raise BatchError(f"issue op binding receipt refused: {exc}") from exc
    # SAME original-candidate binding and canonical origin URL the
    # register route enforces: a changed GitHub profile or payload
    # cannot rebind an already prepared op, and a stale original
    # proof cannot bypass via a pending primary.
    run_id = str(rec.get("run_id") or "")
    try:
        _assert_issue_op_binding_current(repo, rec)
    except IntegrationError as exc:
        raise BatchError(f"issue op binding moved since prepare: "
                         f"{exc}") from exc
    for p in issue_ps:
        if str(p.get("run_id") or "") != run_id:
            raise BatchError("issue primary run does not match the "
                             "bound prepare op")
        iblk = p["issue"]
        if _strict_issue_number(iblk.get("issue")) != _strict_issue_number(
                rblk.get("issue")) or iblk.get("lane") != rblk.get("lane"):
            raise BatchError("issue primary does not match the bound "
                             "prepare op identity")
        if str(iblk.get("repo") or "") != str(rblk.get("repo") or "") \
                or str(iblk.get("url") or "") != str(
                    rblk.get("url") or ""):
            raise BatchError("issue primary origin does not match the "
                             "bound prepare op record")
    # The lane integration must pin the op's exact result worktree, and
    # every passing worker gate must record its head/fingerprint.
    if not _issue_integration_matches(rec, integrations_raw):
        raise BatchError("no lane integration pins the bound prepare "
                         "op's exact result source")
    return None


def _issue_integration_matches(rec: dict, integrations: list) -> bool:
    """True when a lane integration pins the op's exact result.

    The worktree must equal the op result path AND at least one passing
    worker gate must record the exact result head/fingerprint. Zero
    passing worker gates is vacuous, not a match.
    """
    result = (rec or {}).get("result") or {}
    rpath = str(result.get("path") or "")
    rhead = str(result.get("head") or "")
    rfp = str(result.get("fingerprint") or "")
    lane = str((rec or {}).get("lane") or "")
    if not (rpath and rhead and rfp and lane):
        return False
    for integ in integrations or []:
        if not isinstance(integ, dict):
            continue
        if str(integ.get("lane") or "") != lane:
            continue
        if str(integ.get("worktree") or "") != rpath:
            continue
        ok = True
        matched = 0
        for g in integ.get("gates") or []:
            if not isinstance(g, dict):
                continue
            if not str(g.get("key") or "").startswith("worker:"):
                continue
            if str(g.get("status") or "") != "pass":
                continue
            inputs = g.get("inputs") or {}
            if str(inputs.get("head") or "") != rhead \
                    or str(inputs.get("fingerprint") or "") != rfp:
                ok = False
                break
            matched += 1
        if ok and matched:
            return True
    return False


def register_issue_batch(op_id: str) -> dict:
    """Register a done issue prepare op as a pending-review batch.

    Server-side orchestration for the explicit UI control: every
    path/head/manifest derives from the registered op, the current
    passed fastcheck receipt and the registered profile — the id-only
    payload carries no paths. Holds the per-op lock (lease first at the
    serve layer, op lock second — the hard order), so a concurrent run
    surfaces as 409 and never as a duplicate write. Idempotent: an
    already-registered op reuses its exact batch only when the original
    primary and the prepared integration expectation still match and
    every current check passes; anything else is stale drift, not a
    reuse. Refusals raise before any write, corrupt records stay
    preserved.
    """
    try:
        with _op_exclusion(str(op_id or "")):
            return _register_issue_batch_locked(str(op_id or ""))
    except m2.OpBusy as exc:
        raise IntegrationError(f"operation {op_id!r} is busy; register "
                               "refused", http_status=409) from exc
    except ValueError as exc:
        raise IntegrationError(f"unknown integration operation {op_id!r}",
                               http_status=404) from exc


def _register_issue_batch_locked(op_id: str) -> dict:
    """register_issue_batch body under the per-op lock."""
    try:
        _, rec = _load_op(op_id)
    except IntegrationError:
        raise
    if str(rec.get("op") or "") != "prepare" \
            or str(rec.get("status") or "") != "done":
        raise IntegrationError("operation is not a completed prepare; "
                               "register refused")
    rblk = rec.get("issue")
    if not isinstance(rblk, dict) \
            or _strict_issue_number(rblk.get("issue")) is None:
        raise IntegrationError("operation is not an issue prepare; "
                               "register refused")
    repo = str(rec.get("repo") or "")
    run_id = str(rec.get("run_id") or "")
    lane = str(rec.get("lane") or "")
    if lane not in _KNOWN_LANES:
        raise IntegrationError("operation lane unknown; register refused")
    # LIVE: the prepared source must still match the record right now —
    # drifted, dirty or branch-switched bytes refuse before any write.
    _check_op_source(rec)
    receipt, _configured = _validated_issue_receipt(op_id, rec)
    result = rec.get("result") or {}
    # ORIGINAL-candidate binding, before BOTH new registration and
    # reused success: shared gate with the direct-import binder.
    ctx = _assert_issue_op_binding_current(repo, rec)
    # Idempotent-reuse comparisons below need the recorded original
    # identity in this scope (the gate above validates the same
    # values; recomputed here from the bound run, never re-read from
    # live result bytes).
    _ctx_run = ctx.get("run") or {}
    orig_head = str(_ctx_run.get("workCommit") or "")
    orig_fp = str(_ctx_run.get("workTreeFingerprint") or "")
    try:
        canon_wt = str(store.run_dir(run_id) / "work")
    except (Exception, SystemExit) as exc:
        raise IntegrationError("issue op candidate path unresolvable: "
                               f"{exc}") from exc
    try:
        bid = m2.batch_id_for_op(op_id)
    except ValueError as exc:
        raise IntegrationError(f"cannot derive batch id: {exc}") from exc
    try:
        existing = load_batch(bid)
    except (BatchError, SystemExit, ValueError, OSError) as exc:
        # A present-but-unreadable batch file is corruption: preserve it
        # byte-for-byte, never treat it as absent and overwrite it.
        try:
            present = batch_file(bid).exists()
        except (BatchError, OSError):
            present = False
        if present:
            raise IntegrationError("registered batch file is unreadable or "
                                   "corrupt; preserved, register refused"
                                   ) from exc
        existing = None
    if existing is not None and \
            (not isinstance(existing, dict) or not existing):
        # Present-but-not-a-batch-object (empty object, empty list,
        # null parsed as a non-dict): a stored record, not an absence.
        # Never treat it as missing and overwrite it.
        raise IntegrationError("registered batch file is not a batch "
                               "object; preserved, register refused")
    if isinstance(existing, dict) and existing:
        if str(existing.get("origin_m2") or "") != op_id:
            raise IntegrationError("batch id collision with an unrelated "
                                   "batch; register refused")
        if str(existing.get("repo") or "") != repo:
            raise IntegrationError("registered batch belongs to a "
                                   "different repo; stale, register "
                                   "refused")
        # Idempotent reuse ONLY for the same exact original primary and
        # prepared integration expectation, with current checks above
        # already fresh: persisted original (run/repo/issue/canonical
        # URL/path/lane/count) and the separately bound integrated
        # result must both match. Drift, corruption or an unrelated
        # record refuses explicitly, preserving bytes.
        old_ps = existing.get("primaries") or []
        if len(old_ps) != 1 or not isinstance(old_ps[0], dict):
            raise IntegrationError("registered batch primary count "
                                   "changed; stale, register refused")
        old_p = old_ps[0]
        old_src = old_p.get("source")
        if not isinstance(old_src, dict):
            raise IntegrationError("registered batch primary lost its "
                                   "source; stale, register refused")
        old_blk = old_p.get("issue")
        if not isinstance(old_blk, dict) \
                or str(old_blk.get("issue") or "") != ctx["number"] \
                or str(old_blk.get("repo") or "") != repo \
                or str(old_blk.get("url") or "") != ctx["url"]:
            raise IntegrationError("registered batch issue identity "
                                   "changed; stale, register refused")
        if str(old_p.get("run_id") or "") != run_id \
                or str(old_p.get("lane") or "") != lane \
                or str(old_src.get("worktree") or "") != canon_wt \
                or str(old_src.get("branch") or "") != ctx["branch"] \
                or str(old_src.get("head") or "") != orig_head \
                or str(old_src.get("fingerprint") or "") != orig_fp:
            raise IntegrationError("registered batch no longer matches the "
                                   "current original source; stale, "
                                   "register refused")
        old_integ = existing.get("integrations") or []
        if len(old_integ) != 1:
            raise IntegrationError("registered batch integration changed; "
                                   "stale, register refused")
        if str(old_integ[0].get("worktree") or "") \
                != str(result.get("path") or ""):
            raise IntegrationError("registered batch no longer matches the "
                                   "current prepared source; stale, "
                                   "register refused")
        if not _issue_integration_matches(rec, old_integ):
            raise IntegrationError("registered batch integration gates no "
                                   "longer pin the current prepared bytes; "
                                   "stale, register refused")
        return {"id": bid, "repo": repo, "reused": True,
                "primaries": len(existing.get("primaries") or [])}
    try:
        profile = repos.load(repo)
    except (Exception, SystemExit) as exc:
        raise IntegrationError(
            f"registered profile unreadable: {exc}") from exc
    fc_summary = {"steps": receipt.get("steps") or []}
    base_id = str(rec.get("batch_id") or "")
    if not base_id:
        raise IntegrationError("prepare record has no base batch; "
                               "register refused")
    issue_src = {"number": ctx["number"], "lane": lane,
                 "url": ctx["url"], "branch": ctx["branch"]}
    try:
        manifest = m2.build_issue_batch_manifest(repo, run_id, lane, rec,
                                                 issue_src, base_id,
                                                 fc_summary,
                                                 profile if isinstance(
                                                     profile, dict) else {})
    except ValueError as exc:
        raise IntegrationError(f"issue manifest invalid: {exc}") from exc
    batch = import_batch(manifest, allowed_roots=None)
    return {"id": batch.get("id"), "repo": batch.get("repo"),
            "reused": False,
            "primaries": len(batch.get("primaries") or [])}


# ---- Family prepare (M2.6) -------------------------------------------------
# A release-train family (same lane, target, scan and npm scope) prepares
# as ONE combined candidate: every member's declaration bump lands on one
# worktree, one lock re-resolve runs with the repo's own package manager
# (the S3 machinery and its closure guard, reused unchanged), and one
# commit freezes the tree. The driver then runs one fastcheck and
# registers one batch whose primaries are all members. The primary
# member's op record IS the pipeline record (op "prepare", the same op
# id a single prepare would use) so resume/reconcile/batch-id helpers
# keep working; member records ride along with family.role "member" and
# are never reconciled or counted alone (m2 guards on that role).


def prepare_family_integration(repo: str, lane: str, base_id: str,
                               members: list,
                               origin: str = "m2",
                               retry: bool = False) -> dict:
    """Prepare one worktree for a release-train family (M2.6).

    `members` is [{run_id, dep, target}, ...], primary first. Holds no
    lease itself — the caller acquires the worker lease first, exactly
    like prepare_integration. Returns {"ok": True, "rec", "note",
    "primary_op"} on done and {"ok": False, "error"} on a
    recorded-block; raises IntegrationError on every refused path
    (including "already present in base", which the driver maps to a
    present-skip just like the single path). With retry=True an
    existing BLOCKED primary record re-runs explicitly (attempt
    archived via _push_attempt, leftover branch verified by
    _prepare_retry); a reconciled already-done family returns
    reused True. Member records archive any prior record under
    their op id instead of overwriting it.
    """
    import hashlib as _hashlib
    if not isinstance(members, list) or len(members) < 2:
        raise IntegrationError("family prepare needs at least 2 members")
    if lane not in ("main", "next"):
        raise IntegrationError(f"family lane {lane!r} is not main/next")
    if origin not in ("ui", "m2"):
        raise IntegrationError(f"unknown family origin {origin!r}")
    primary = members[0]
    states, queues, proofs = [], [], []
    for pos, ref in enumerate(members):
        if not isinstance(ref, dict):
            raise IntegrationError(f"family member {pos} is not a record")
        run_id = str(ref.get("run_id") or "")
        dep = str(ref.get("dep") or "")
        target = str(ref.get("target") or "")
        if not (run_id and dep and target):
            raise IntegrationError(
                f"family member {pos} carries no run/dep/target identity")
        try:
            state = store.load(run_id)
        except SystemExit:
            raise IntegrationError(
                f"unknown run '{run_id}'", http_status=404) from None
        if str(state.get("repo") or "") != repo:
            raise IntegrationError(
                f"family member {run_id} belongs to another repo")
        if str(state.get("dep") or "") != dep \
                or str(state.get("target") or "") != target:
            raise IntegrationError(
                f"family member {run_id} mismatches its run record")
        queue_item = _queue_item_for_run(repo, run_id)
        if str(queue_item.get("lane") or state.get("lane") or "") != lane:
            raise IntegrationError(
                f"family member {run_id} is not on lane {lane}")
        work_commit, work_fp, proof_error = _candidate_proof(
            state, run_id)
        if proof_error:
            raise IntegrationError(
                f"family member {run_id}: {proof_error}")
        states.append(state)
        queues.append(queue_item)
        proofs.append((work_commit, work_fp))
    try:
        batch = load_batch(base_id)
    except BatchError:
        raise IntegrationError(f"unknown batch '{base_id}'",
                               http_status=404) from None
    if batch.get("repo") != repo:
        raise IntegrationError("base batch belongs to another repo")
    integrations = [i for i in (batch.get("integrations") or [])
                    if isinstance(i, dict)]
    if not integrations:
        raise IntegrationError("base batch records no integration source")
    if len(integrations) == 1:
        base_integ = integrations[0]
    else:
        matches = [i for i in integrations
                   if str(i.get("lane") or "") == lane]
        if len(matches) != 1:
            raise IntegrationError("base batch has several integrations "
                                   "and no single lane match")
        base_integ = matches[0]
    base_lane = str(base_integ.get("lane") or "")
    if lane == "main" and base_lane != "main":
        raise IntegrationError("main-lane family cannot anchor "
                               f"{base_lane or 'unknown'}-lane base "
                               f"integration of batch '{base_id}'")
    if lane == "next" and base_lane not in ("main", "next"):
        raise IntegrationError("next-lane family cannot anchor "
                               f"{base_lane or 'unknown'}-lane base "
                               f"integration of batch '{base_id}'")
    base_worktree = str(base_integ.get("worktree") or "")
    base_head = str(base_integ.get("head") or "")
    base_fp = str(base_integ.get("fingerprint") or "")
    if not base_worktree or not base_head or not base_fp:
        raise IntegrationError("base integration records no exact source")
    base_snap = _snapshot(base_worktree)
    if (base_snap is None or base_snap.get("head") != base_head
            or base_snap.get("fingerprint") != base_fp
            or not base_snap.get("clean")):
        raise IntegrationError("base integration source drifted or "
                               "unreadable")
    base_live, base_diag = live_map_with_diag(closure([batch]))
    base_ev = evaluate(batch, base_live, live_diag=base_diag, record=True)
    base_results = base_ev.get("results") or []
    base_unready = [r for r in base_results if not r.get("ready")]
    if not base_results or base_unready:
        first_reasons = "; ".join(
            str(reason) for r in base_unready[:2]
            for reason in (r.get("reasons") or [])[:2]) or "no ready results"
        raise IntegrationError("base batch is not READY "
                               f"({first_reasons})")
    # Per-member replayable ranges (S2 checks each, before mutation).
    ranges = []
    total_picks = 0
    for (state, queue_item, (work_commit, work_fp),
            ref) in zip(states, queues, proofs, members):
        run_id = str(ref.get("run_id") or "")
        cand_worktree = str(store.run_dir(run_id) / "work")
        base_commit = str(state.get("baseCommit") or "")
        if not base_commit:
            raise IntegrationError(
                f"family member {run_id} records no base commit "
                "(insufficient evidence)")
        listed = _run_git(["rev-list", "--reverse",
                           f"{base_commit}..{work_commit}"],
                          cand_worktree)
        rev_list = [r.strip() for r in (listed.stdout.splitlines()
                                        if listed.returncode == 0
                                        else []) if r.strip()]
        if listed.returncode != 0 or not rev_list:
            raise IntegrationError(
                f"family member {run_id} has no replayable commits; "
                "refusing before any mutation")
        if rev_list[-1] != work_commit:
            raise IntegrationError(
                f"family member {run_id} history moved during prepare; "
                "refusing")
        merged = _run_git(["rev-list", "--merges",
                           f"{base_commit}..{work_commit}"],
                          cand_worktree)
        merge_list = [r.strip() for r in (merged.stdout.splitlines()
                                          if merged.returncode == 0
                                          else []) if r.strip()]
        if merged.returncode == 0 and merge_list:
            raise IntegrationError(
                f"family member {run_id} range contains merge commit "
                f"{merge_list[0][:12]}; merge replay refused before any "
                "mutation")
        total_picks += len(rev_list)
        changed = _run_git(["diff", "--name-only",
                            f"{base_commit}", work_commit],
                           cand_worktree)
        if changed.returncode != 0:
            raise IntegrationError(
                f"family member {run_id} diff unreadable; refusing "
                "before any mutation")
        cand_files = [line.strip() for line in
                      (changed.stdout or "").splitlines()
                      if line.strip()]
        ranges.append({"ref": ref, "run_id": run_id,
                       "dep": str(ref.get("dep") or ""),
                       "target": str(ref.get("target") or ""),
                       "cand_worktree": cand_worktree,
                       "base_commit": base_commit,
                       "work_commit": work_commit,
                       "work_fp": work_fp,
                       "rev_list": rev_list,
                       "manifest": sorted(f for f in cand_files
                                          if not _is_lock_rel(f))})
    if total_picks > _MAX_REPLAY_COMMITS:
        raise IntegrationError(
            f"family replays {total_picks} commits; at most "
            f"{_MAX_REPLAY_COMMITS} supported — split the family")
    # Duplicate guard per member (read-only, before any mutation).
    owners = _base_candidate_owners(batch)
    for entry in ranges:
        owner = owners.get(candidate_key(
            repo, entry["dep"], entry["target"], lane))
        if owner:
            where = ("as a direct primary" if owner == base_id
                     else f"through inherited batch '{owner}'")
            raise IntegrationError(
                f"candidate {entry['dep']} {entry['target']} ({lane}) "
                f"is already present in base batch '{base_id}' {where}; "
                "inspect the registered integration instead of "
                "preparing a duplicate")
    fam_tag = _hashlib.sha1(
        "|".join(sorted(r["run_id"] for r in ranges)).encode(
            "utf-8")).hexdigest()[:8]
    target = str(primary.get("target") or "")
    slug = (f"family-{_slug(base_id)}-{_slug(str(primary.get('dep') or ''))}"
            f"-{_slug(target)}-{fam_tag}")
    new_path = paths.home() / "reviews" / slug
    branch = (f"codex/family-{_slug(str(primary.get('dep') or ''))}"
              f"-{_slug(target)}-{fam_tag}")
    op_id = _op_id(str(primary.get("run_id") or ""), base_id)
    opf = op_file(op_id)
    existing = read_json(opf, default=None)
    if isinstance(existing, dict) and existing.get("op_id") == op_id:
        if not retry or str(existing.get("status") or "") == "done":
            # An explicit retry of an already-done family reuses it
            # (re-verified by reconcile); only a blocked record
            # re-runs below.
            reconciled = _reconcile_existing(opf, existing, new_path)
            ok = isinstance(reconciled, dict) \
                and str(reconciled.get("status") or "") == "done"
            return {"ok": ok, "rec": reconciled, "reused": True,
                    "note": str((reconciled.get("family") or {})
                                .get("note") or "") if isinstance(
                                    reconciled, dict) else "",
                    "primary_op": op_id}
        _prepare_retry(opf, existing)
        # Fall through: the fresh record below archives this blocked
        # state via _push_attempt (chain-safe).
    if new_path.exists():
        raise IntegrationError("integration path already exists "
                               f"(unrelated): {new_path}")
    if _git_head(base_worktree, branch):
        raise IntegrationError("branch already exists "
                               f"(unrelated): {branch}")
    member_ids = [r["run_id"] for r in ranges]
    note = (f"함께 준비: "
            f"{', '.join(r['dep'] for r in ranges)} {target}")
    rec = {"schema": INTEGRATION_OPS_SCHEMA, "op": "prepare",
           "op_id": op_id, "repo": repo,
           "run_id": str(primary.get("run_id") or ""),
           "batch_id": base_id,
           "dep": str(primary.get("dep") or ""), "target": target,
           "lane": lane, "origin": str(origin or "m2"),
           "candidate": {"head": ranges[0]["work_commit"],
                         "fingerprint": ranges[0]["work_fp"],
                         "branch": str(states[0].get("branch") or ""),
                         "worktree": ranges[0]["cand_worktree"]},
           "base": {"batch": base_id,
                    "lane": str(base_integ.get("lane") or ""),
                    "head": base_head, "fingerprint": base_fp,
                    "checkout": base_worktree},
           "family": {"role": "primary", "members": member_ids,
                      "note": note},
           "result": {"path": str(new_path), "branch": branch,
                      "head": "", "fingerprint": "",
                      "proof_note": "base-batch proofs never apply to the "
                      "integrated bytes; all runtime gates start pending"},
           "gates": [{"key": "source-prepared", "status": "pending",
                      "note": ""},
                     {"key": "fastchecks", "status": "pending",
                      "note": "not run in this step"},
                     {"key": "native", "status": "pending",
                      "note": "Root device QA after source freeze"}],
           "status": "started", "error": "",
           "created_at": utcnow(), "updated_at": utcnow()}
    if retry and isinstance(existing, dict):
        # Explicit retry: the blocked primary archives (chain-safe)
        # instead of being erased.
        _push_attempt(rec, existing)
    write_json(opf, rec)

    def _fail(message: str) -> dict:
        return {"ok": False,
                "error": str((_record_blocked(opf, rec, message) or {})
                             .get("error") or message)[:1000],
                "primary_op": op_id, "rec": rec}

    try:
        added = _run_git(["worktree", "add", "-b", branch,
                          str(new_path), base_head], base_worktree)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout or "").strip()[-500:]
            return _fail(f"worktree add failed: {detail}")
        manifest_reapplied: list = []
        rebuilt_manifests = _snapshot_manifest_texts(str(new_path))
        union_manifest = sorted({f for r in ranges for f in r["manifest"]})
        for entry in ranges:
            for pos, sha in enumerate(entry["rev_list"], 1):
                pick = _run_git(["-c", "user.name=openmainty",
                                 "-c", "user.email=openmainty@localhost",
                                 "cherry-pick", "-n", sha],
                                str(new_path))
                if pick.returncode == 0:
                    rebuilt_manifests.update(
                        _snapshot_manifest_texts(str(new_path)))
                    continue
                conflicted = _conflicted_paths(str(new_path))
                _run_git(["cherry-pick", "--quit"], str(new_path))
                if conflicted is None:
                    cleanup = _remove_derived_tree(
                        base_worktree, new_path)
                    return _fail(
                        f"family member {entry['dep']} conflict paths "
                        f"unreadable at commit {pos}/"
                        f"{len(entry['rev_list'])} {sha[:12]}; no success "
                        f"claimed, {cleanup}")
                outsiders = sorted(p for p in conflicted
                                   if not _is_lock_rel(p))
                manifest_paths = sorted(
                    p for p in outsiders if p in _RERESOLVE_MANIFESTS)
                others = sorted(
                    p for p in outsiders
                    if p not in _RERESOLVE_MANIFESTS)
                if others:
                    cleanup = _remove_derived_tree(
                        base_worktree, new_path)
                    return _fail(
                        f"family member {entry['dep']} non-lock conflict "
                        f"at commit {pos}/{len(entry['rev_list'])} "
                        f"{sha[:12]}, paths: "
                        f"{', '.join(others[:8])}; no success claimed, "
                        f"{cleanup}")
                if manifest_paths:
                    try:
                        manifest_reapplied.extend(
                            _reapply_manifest_ranges(
                                worktree=str(new_path),
                                cand_worktree=entry["cand_worktree"],
                                base_head=base_head,
                                base_commit=entry["base_commit"],
                                rev_list=entry["rev_list"], pos=pos,
                                dep=entry["dep"],
                                manifest_paths=manifest_paths,
                                prior_texts=rebuilt_manifests,
                                sibling_deps=tuple(
                                    r["dep"] for r in ranges
                                    if r is not entry)))
                    except IntegrationError as d1err:
                        _run_git(["cherry-pick", "--abort"],
                                 str(new_path))
                        cleanup = _remove_derived_tree(
                            base_worktree, new_path)
                        return _fail(f"{d1err}; {cleanup}")
                for p in conflicted:
                    if not _is_lock_rel(p):
                        continue
                    if p.rsplit("/", 1)[-1] != "yarn.lock" \
                            or "/" in p:
                        cleanup = _remove_derived_tree(
                            base_worktree, new_path)
                        return _fail(
                            f"{p} 충돌은 이 단계에서 재생성하지 않음 "
                            f"(yarn berry 전용); {cleanup}")
                _restore_lockfiles_to_head(str(new_path))
                # The conflicted pick's cleanly-applied manifest hunks
                # survive --quit in the worktree: refresh the baseline
                # a later D1-d reapply checks against, or it would
                # clobber an earlier member's staged bump.
                rebuilt_manifests.update(
                    _snapshot_manifest_texts(str(new_path)))
        _restore_lockfiles_to_head(str(new_path))
        live_files = _worktree_changed_files(str(new_path))
        if live_files is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"family diff unreadable; {cleanup}")
        live_manifest = sorted(f for f in live_files
                               if not _is_lock_rel(f))
        if live_manifest != union_manifest:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("family manifest set differs from members "
                         f"({', '.join(live_manifest[:8])} vs "
                         f"{', '.join(union_manifest[:8])}); {cleanup}")
        manager = _detect_manager(str(new_path))
        if manager is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("no known lockfile in family tree; "
                         f"{cleanup}")
        try:
            lock_text = Path(new_path, "yarn.lock").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            lock_text = ""
        flavor = _yarn_flavor(lock_text) if manager == "yarn" else ""
        package_manager = _package_manager_field(str(new_path))
        manifest_files = ("package.json", "example/package.json")
        bumped = [entry["dep"] for entry in ranges
                  if any(_manifest_declared_range(str(new_path), name,
                                                  entry["dep"])
                         != _manifest_declared_range(base_worktree, name,
                                                     entry["dep"])
                         for name in manifest_files)]
        argv, regen_origin, refusal = _regen_plan(
            manager, flavor, bool(bumped))
        if not argv:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{refusal}; {cleanup}")
        for dep in [entry["dep"] for entry in ranges
                    if entry["dep"] not in bumped]:
            dropped = _drop_dep_stanzas(lock_text, dep)
            try:
                Path(new_path, "yarn.lock").write_text(
                    dropped, encoding="utf-8")
            except OSError:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail("cannot stage lockfile for refresh; "
                             f"{cleanup}")
            lock_text = dropped
        tool_version = _tool_version(argv[0], str(new_path))
        registry_host = _registry_host(argv[0], str(new_path))
        regen_env = _regen_env()
        disk_ok, disk_note = _regen_disk_ok()
        if not disk_ok:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{disk_note}; {cleanup}")
        try:
            regen = subprocess.run(
                argv, cwd=str(new_path), env=regen_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_RERESOLVE_TIMEOUT, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"lock tool failed to start ({argv[0]}): "
                         f"{exc}; {cleanup}")
        if regen.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(regen.stderr or regen.stdout or "")
            tail = tail.strip()[-500:]
            return _fail(f"lock tool {argv[0]} rc={regen.returncode}: "
                         f"{tail}; {cleanup}")
        present_locks = [name for name in _LOCK_FILENAMES
                         if Path(new_path, name).is_file()]
        staged = _run_git(["add", "--"] + present_locks,
                          str(new_path))
        if staged.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("cannot stage regenerated lockfiles; "
                         f"{cleanup}")
        post_files = _worktree_changed_files(str(new_path))
        if post_files is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("family diff unreadable after regenerate; "
                         f"{cleanup}")
        post_manifest = sorted(f for f in post_files
                               if not _is_lock_rel(f))
        if post_manifest != union_manifest:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("regenerate moved non-lock files "
                         f"({', '.join(post_manifest[:8])}); {cleanup}, "
                         "nothing committed")
        base_locks = {}
        for name in _LOCK_FILENAMES:
            shown = _run_git(["show", f"HEAD:{name}"], str(new_path))
            if shown.returncode == 0:
                base_locks[name] = shown.stdout or ""
        unrelated = None
        for entry in ranges:
            bad_here = []
            for name in _LOCK_FILENAMES:
                cur_path = Path(new_path) / name
                if not cur_path.is_file():
                    continue
                new_text = cur_path.read_text(encoding="utf-8",
                                              errors="replace")
                old_text = base_locks.get(name)
                if old_text is None or old_text == new_text:
                    continue
                bad_here.extend(_lock_delta_guard(manager, old_text,
                                                  new_text,
                                                  entry["dep"]))
            bad_here = set(bad_here)
            unrelated = bad_here if unrelated is None \
                else (unrelated & bad_here)
        unrelated = sorted(unrelated or [])[:10]
        if unrelated:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("관련 없는 잠금 이동: "
                         f"{', '.join(unrelated)}; nothing committed, "
                         f"{cleanup}")
        regen_lock = Path(new_path, "yarn.lock").read_text(
            encoding="utf-8", errors="replace")
        base_lock = base_locks.get("yarn.lock") or ""
        for entry in ranges:
            declared_ranges = set()
            for mf in manifest_files:
                rng = _manifest_declared_range(str(new_path), mf,
                                               entry["dep"])
                if rng:
                    declared_ranges.add(rng)
            if not declared_ranges:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"family tree declares no range for "
                             f"{entry['dep']}; nothing committed, "
                             f"{cleanup}")
            ok, seen = _check_declared_resolution(
                regen_lock, base_lock, entry["dep"], declared_ranges,
                entry["target"])
            if not ok:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"잠금이 {seen}로 해석됨 (후보 목표 "
                             f"{entry['target']}); nothing committed, "
                             f"{cleanup}")
        staged_all = _run_git(["add", "-A"], str(new_path))
        if staged_all.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("cannot stage family tree; "
                         f"{cleanup}")
        msg = (f"chore(deps): "
               f"{', '.join(r['dep'] for r in ranges)} -> {target} "
               f"(family prepare, {len(ranges)} members; lock "
               f"re-resolved on base {base_head[:12]} with "
               f"{' '.join(argv)} (origin {regen_origin}"
               f"{', tool ' + tool_version if tool_version else ''}"
               f"{', registry ' + registry_host if registry_host else ''}"
               f"{', pm ' + package_manager if package_manager else ''}"
               ").\")\n\n"
               f"Members: "
               f"{', '.join(r['run_id'] for r in ranges)}.")
        committed = _run_git(["-c", "user.name=openmainty",
                              "-c", "user.email=openmainty@localhost",
                              "commit", "-m", msg], str(new_path))
        if committed.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(committed.stderr or committed.stdout
                               or "").strip()[-300:]
            return _fail(f"family commit failed: {tail}; {cleanup}")
        res_snap = _snapshot(str(new_path))
        if (res_snap is None or not res_snap.get("head")
                or not res_snap.get("fingerprint")
                or not res_snap.get("clean")):
            return _fail("family source unreadable or dirty after "
                         "commit")
        if manifest_reapplied:
            regen_origin = "manifest-adjacent"
        rec["regenerated_by"] = {"tool": argv[0], "argv": argv,
                                 "rc": 0,
                                 "tool_version": tool_version,
                                 "registry_host": registry_host,
                                 "package_manager": package_manager,
                                 "mode": "update-lockfile",
                                 "env_flags": list(RERESOLVE_ENV_FLAGS),
                                 "resolved_version": target,
                                 "manifest_reapplied": manifest_reapplied,
                                 "command_origin": regen_origin,
                                 "members": member_ids}
        rec["regen_receipt"] = dict(
            _write_regen_log(op_id, regen.stdout or ""),
            argv=argv, rc=0, registry_host=registry_host,
            tool_version=tool_version,
            env_keys=sorted(regen_env),
            scripts_off=(regen_env.get("YARN_ENABLE_SCRIPTS")
                         == "0"))
        rec["result"]["head"] = res_snap["head"]
        rec["result"]["fingerprint"] = res_snap["fingerprint"]
        rec["status"] = "done"
        rec["updated_at"] = utcnow()
        for gate in rec["gates"]:
            if gate["key"] == "source-prepared":
                gate["status"] = "pass"
                gate["note"] = (f"가족: {branch} @ "
                                f"{res_snap['head'][:12]} fp "
                                f"{res_snap['fingerprint'][:12]} "
                                f"({len(ranges)}건 함께 준비)")
        write_json(opf, rec)
        # Member records ride the same tree: done, role member, never
        # reconciled or counted alone (the driver guards on the role).
        for entry in ranges[1:]:
            mopf = op_file(_op_id(entry["run_id"], base_id))
            mprev = read_json(mopf, default=None)
            mrec = {"schema": INTEGRATION_OPS_SCHEMA, "op": "prepare",
                    "op_id": _op_id(entry["run_id"], base_id),
                    "repo": repo, "run_id": entry["run_id"],
                    "batch_id": base_id, "dep": entry["dep"],
                    "target": entry["target"], "lane": lane,
                    "origin": str(origin or "m2"),
                    "candidate": {"head": entry["work_commit"],
                                  "fingerprint": entry["work_fp"],
                                  "branch": "",
                                  "worktree": entry["cand_worktree"]},
                    "base": dict(rec["base"]),
                    "family": {"role": "member", "primary": op_id,
                               "members": member_ids, "note": note},
                    "result": {"path": str(new_path), "branch": branch,
                               "head": res_snap["head"],
                               "fingerprint": res_snap["fingerprint"],
                               "proof_note": "member of family " + op_id},
                    "gates": [{"key": "source-prepared",
                               "status": "pass",
                               "note": f"가족 대표 {branch}"}],
                    "status": "done", "error": "",
                    "created_at": utcnow(), "updated_at": utcnow()}
            if isinstance(mprev, dict) \
                    and mprev.get("op_id") == mrec["op_id"]:
                # P-HIST: a member's prior record (e.g. its blocked
                # single) archives instead of being overwritten; a
                # blocked one is also marked superseded by the
                # family, exactly like the marking pass below.
                _push_attempt(mrec, mprev)
                if str(mprev.get("status") or "") == "blocked":
                    mrec["superseded_by"] = op_id
            write_json(mopf, mrec)
        return {"ok": True, "rec": rec, "note": note,
                "primary_op": op_id}
    except Exception as exc:
        _record_blocked(opf, rec,
                        f"interrupted: {type(exc).__name__}: {exc}"
                        [:1000])
        raise


# ---- Family retry (M2.6b) --------------------------------------------------
# An explicit owner/root action for a blocked release train: the M2.6
# driver never auto-retries recorded work, so a trio that blocked as
# singles stays blocked although prepare_family_integration would now
# handle it. POST /api/integration/family-retry {repo, run_id, by,
# confirm} runs the whole train ONCE on the current default base.
# Never automatic, never from the driver tick.


def family_train(repo: str, run_id: str) -> list:
    """Read-only release-train lookup for one run (M2.6b).

    Returns the member refs [{run_id, dep, target}, ...], primary
    (oldest) first: waiting-review dep-update items sharing lane,
    target, scan (created_at) and npm scope — the same bucket the
    M2.6 driver prepares as one. Raises IntegrationError 409 when
    the run is not queued or rides no train (fewer than 2 members).
    Verification is NOT checked here; the caller decides (display
    shows the button, execute refuses unreviewed trains).
    """
    try:
        queue = sched.load(repo).get("queue", [])
    except SystemExit:
        raise IntegrationError(f"unknown repo '{repo}'",
                               http_status=404) from None
    mine = None
    for item in queue or []:
        if isinstance(item, dict) \
                and str(item.get("run_id") or "") == run_id:
            mine = item
            break
    if mine is None:
        raise IntegrationError(f"run '{run_id}' is not queued; no "
                               "family to retry", http_status=409)
    groups = m2._family_groups(queue)
    key = (str(mine.get("lane") or ""),
           str(mine.get("target") or ""),
           str(mine.get("created_at") or ""),
           m2._family_key(str(mine.get("dep") or "")))
    slot = groups.get(key)
    members = []
    if slot is not None:
        ordered = sorted(slot["verified"] + slot["unverified"],
                         key=lambda i: (str(i.get("created_at") or ""),
                                        str(i.get("run_id") or "")))
        members = [{"run_id": str(i.get("run_id") or ""),
                    "dep": str(i.get("dep") or ""),
                    "target": str(i.get("target") or "")}
                   for i in ordered]
    if len(members) < 2:
        raise IntegrationError(f"run '{run_id}' rides no release "
                               "train (no same lane+target+scan+scope "
                               "siblings); nothing to retry as a "
                               "family", http_status=409)
    return members


def mark_train_superseded(member_ids, family_op: str,
                          base_id: str = "") -> list:
    """Mark still-blocked member records superseded_by (M2.6b).

    Field-only, never deleted. Used by the explicit family_retry
    and by the driver when it reunites a train whose member left a
    blocked single behind. Returns the marked op ids.
    """
    ids = {str(i) for i in (member_ids or [])}
    order = [str(i) for i in (member_ids or [])]
    marked = []
    try:
        ops_dir = integration_ops_dir()
    except Exception:  # noqa: BLE001 - unlistable means unmarked
        ops_dir = None
    if ops_dir is not None:
        try:
            files = sorted(ops_dir.glob("*.json"))
        except OSError:
            files = []
        for path in files:
            try:
                rec = read_json(path, default=None)
            except Exception:  # noqa: BLE001 - unreadable never acts
                continue
            if not isinstance(rec, dict):
                continue
            if str(rec.get("run_id") or "") not in ids:
                continue
            if str(rec.get("status") or "") != "blocked":
                continue
            if str(rec.get("op_id") or "") == family_op:
                continue
            rec["superseded_by"] = family_op
            rec["updated_at"] = utcnow()
            try:
                write_json(path, rec)
            except Exception:  # noqa: BLE001 - unmarkable stays
                continue
            marked.append(str(rec.get("op_id") or path.name))
    # Member records overwritten by the fresh family run carry the
    # same mark (set at write time); list them too so the caller
    # sees every old record the family superseded.
    if base_id:
        for rid in order:
            try:
                mrec = read_json(op_file(_op_id(rid, base_id)),
                                 default=None)
            except Exception:  # noqa: BLE001 - unreadable never acts
                continue
            if isinstance(mrec, dict) and \
                    str(mrec.get("superseded_by") or "") == family_op:
                marked.append(str(mrec.get("op_id") or ""))
    return marked


def family_retry(repo: str, run_id: str, by: str) -> dict:
    """Run one release train as a family, explicitly (M2.6b).

    `by` is "owner" or "root" — a worker can never retry (403).
    Resolves the train via family_train, refuses unreviewed trains
    (409 naming the pending members), resolves the current default
    base (409 when ambiguous/missing — the owner picks), then runs
    prepare_family_integration ONCE with retry=True so the members'
    old blocked single records archive (P-HIST) instead of blocking
    the run. On done, every still-blocked op of a member run is
    marked superseded_by (field only, never deleted) and one
    timeline event records who retried what. A reconciled already-
    done family returns reused (no duplicate event). The normal
    fastcheck/batch resume picks the done family up next tick.
    """
    name = str(by or "").strip()
    if name not in ("owner", "root"):
        raise IntegrationError("family retry needs by owner or root "
                               "(a worker can never retry)",
                               http_status=403)
    try:
        state = store.load(run_id)
    except SystemExit:
        raise IntegrationError(f"unknown run '{run_id}'",
                               http_status=404) from None
    if str(state.get("repo") or "") != repo:
        raise IntegrationError(f"run '{run_id}' belongs to another "
                               "repo", http_status=404)
    members = family_train(repo, run_id)
    try:
        queue = sched.load(repo).get("queue", [])
    except SystemExit:
        queue = []
    by_run = {str(i.get("run_id") or ""): i for i in (queue or [])
              if isinstance(i, dict)}
    pending = [m["run_id"] for m in members
               if by_run.get(m["run_id"], {}).get("verified") is not True]
    if pending:
        raise IntegrationError("가족 준비 대기: 리뷰 남음 "
                               f"{len(pending)}건 "
                               f"({', '.join(pending[:4])}); the whole "
                               "train waits, never partially",
                               http_status=409)
    lane = str(by_run.get(run_id, {}).get("lane")
               or state.get("lane") or "")
    base = default_base(repo, lane, ambiguous="empty")
    base_id = (base or {}).get("id") or ""
    if not base_id:
        reason = ("기준 묶음 선택 필요"
                  if (base or {}).get("tie", 0) > 1
                  else "기준 묶음 없음")
        raise IntegrationError(f"{reason}; pick the base first",
                               http_status=409)
    out = prepare_family_integration(repo, lane, base_id, members,
                                     origin="m2", retry=True)
    if not out.get("ok"):
        return out
    if out.get("reused"):
        out["members"] = members
        return out
    family_op = str(out.get("primary_op") or "")
    member_ids = {m["run_id"] for m in members}
    marked = mark_train_superseded(member_ids, family_op, base_id)
    label = (f"{', '.join(m['dep'] for m in members)} "
             f"{members[0]['target']}")
    events.append(repo, "m2-family-retry", family_op,
                  f"가족으로 다시 시도: {label} — {name}",
                  f"구성원 {len(members)}건 · 기준 {base_id} · "
                  f"대체된 막힘 기록 {len(marked)}건",
                  actor=name)
    out["members"] = members
    out["marked_superseded"] = sorted(marked)
    return out


# ---- Derived lock re-resolve (S3/D1) ---------------------------------------
# A blocked prepare op whose cherry-pick conflict is lockfile-only can be
# re-resolved on the base with the repo's own package manager, producing a
# NEW derived operation (op id "<prepare op_id>-lockfix"). The original
# blocked record is never rewritten: re-preparing under an existing op id
# archives the prior evidence in a bounded `attempts` list (P-HIST) instead
# of erasing it. The regenerate command derives only from structured
# facts (tracked lockfile + packageManager, replayed manifest hunks,
# op dep/target — D1-a); free log text never becomes argv and a blanket
# install is never accepted. yarn berry resolves with
# install --mode=update-lockfile, scripts off, no link step. The
# regenerated lock delta may move only the candidate package's own
# dependency closure over the base+new union graph (D1-b); anything else
# blocks with a bounded list. The new lock must resolve dep to exactly
# the op target. Network/tool failure is explicit and never logs tokens
# or auth.

_LOCK_FILENAMES = ("yarn.lock", "package-lock.json", "pnpm-lock.yaml",
                   "Podfile.lock", "pubspec.lock")
_PUB_FILES = ("pubspec.lock", "pubspec.yaml")


def _has_pub_conflict(paths) -> bool:
    """True when any conflicted path is a Dart pub file (M3.3a).

    The regen below is yarn-only, so pub conflicts stay
    prepare-only: a pubspec.lock conflict must NOT read as JS
    "lock-only", and pubspec.yaml is not a re-resolvable manifest.
    """
    return any(str(p).split("/")[-1] in _PUB_FILES
               for p in (paths or []))
_MAX_ATTEMPTS = 10
_RERESOLVE_TIMEOUT = 300


def _is_lock_rel(path: str) -> bool:
    return (path or "").rsplit("/", 1)[-1] in _LOCK_FILENAMES


def _attempt_snapshot(prior: dict) -> dict:
    cand = prior.get("candidate") or {}
    res = prior.get("result") or {}
    return {
        "status": str(prior.get("status") or ""),
        "error": str(prior.get("error") or "")[:300],
        "at": str(prior.get("updated_at") or ""),
        "commit_list": [c.get("head") for c in
                        (cand.get("commit_list") or [])
                        if isinstance(c, dict) and c.get("head")],
        "replayed_from": [h for h in (res.get("replayed_from") or [])
                          if isinstance(h, str)],
        "result_head": str(res.get("head") or ""),
        "gates": [{"key": g.get("key"), "status": g.get("status"),
                   "note": str(g.get("note") or "")[:120]}
                  for g in (prior.get("gates") or [])
                  if isinstance(g, dict)],
    }


def _push_attempt(rec: dict, prior) -> None:
    """Archive one prior record state; chain-safe and bounded.

    The prior chain carries FIRST, then the prior snapshot, so attempt
    1 stays visible at attempt 3. Exact-duplicate entries collapse (a
    record archiving itself, as reconcile does, must not double its
    own chain). Only the newest _MAX_ATTEMPTS entries survive.
    """
    if not isinstance(prior, dict):
        return
    snap = _attempt_snapshot(prior)
    if not snap["at"] and not snap["error"] and not snap["status"]:
        return
    prior_chain = [a for a in (prior.get("attempts") or [])
                   if isinstance(a, dict)]
    # Structural dedup only: when a record archives itself (reconcile),
    # its own chain is already the prior chain. Content-equal but
    # distinct attempts (same-second retries) are never collapsed.
    own = [a for a in (rec.get("attempts") or [])
           if isinstance(a, dict) and a not in prior_chain]
    rec["attempts"] = (prior_chain + own + [snap])[-_MAX_ATTEMPTS:]


def _conflicted_paths(worktree: str):
    proc = _run_git(["diff", "--name-only", "--diff-filter=U"],
                    worktree)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip()]


def _restore_lockfiles_to_head(worktree: str) -> list:
    """Reset every lockfile present back to HEAD (the base); keeps the
    staged manifest hunks. Returns the lockfiles reset."""
    reset = []
    for name in _LOCK_FILENAMES:
        proc = _run_git(["ls-files", "--error-unmatch", name],
                        worktree)
        if proc.returncode != 0:
            continue
        back = _run_git(["restore", "--source=HEAD", "--staged",
                         "--worktree", "--", name], worktree)
        if back.returncode != 0:
            detail = (back.stderr or back.stdout or "").strip()[-300:]
            raise IntegrationError("cannot reset lockfile "
                                   f"{name} to base: {detail}")
        reset.append(name)
    return reset


def _run_tool(argv: list, cwd: str, timeout: int = 60):
    """Run a package-manager binary directly (never through git)."""
    try:
        return subprocess.run(list(argv), cwd=str(cwd),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout,
                              text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrationError(f"tool {argv[0] if argv else '?'} "
                               f"failed to start: {exc}") from exc


def _remove_derived_tree(base_worktree: str, new_path) -> str:
    """Force-remove an app-owned derived worktree.

    --force is required because the tree is routinely dirty here
    (conflict markers left by cherry-pick --quit, staged regen
    output). It is safe: the path was created by this operation
    moments ago — a pre-existing path refuses before any mutation —
    and residue would break idempotent retry.
    """
    removed = _run_git(["worktree", "remove", "--force",
                        str(new_path)], base_worktree)
    if removed.returncode == 0 and not Path(new_path).exists():
        return "new worktree removed"
    return (f"conflict workspace preserved at {new_path} "
            f"(remove rc={removed.returncode})")


def _check_leftover_branch(base_worktree: str, branch: str,
                            base_head: str,
                            result_path: str) -> str:
    """Return "" when a leftover retry branch may be deleted, else the
    specific refusal reason. Never deletes here. All four must hold:
    (a) the name equals the record's result.branch, (b) it points
    EXACTLY at the record's base.head (zero own commits), (c) no
    worktree uses it, (d) result.path does not exist on disk."""
    if not branch:
        return ("record names no branch to retry on; operator review "
                "required, nothing deleted")
    if not result_path or Path(result_path).exists():
        return (f"result path still exists ({result_path or 'unrecorded'}); "
                "operator review required, nothing deleted")
    tip = _git_text(["rev-parse", branch], base_worktree)
    if tip:
        if tip != base_head:
            return (f"leftover branch {branch} has own commits "
                    f"(at {tip[:12]}, base {base_head[:12]}); operator "
                    "review required, nothing deleted and never forced")
        listed = _run_git(["worktree", "list", "--porcelain"],
                          base_worktree)
        if listed.returncode == 0:
            here = f"branch refs/heads/{branch}"
            if any(line.strip() == here
                   for line in (listed.stdout or "").splitlines()):
                return (f"leftover branch {branch} is still checked out "
                        "in a worktree; operator review required, "
                        "nothing deleted")
    return ""


def _joint_branch_tidy(base_worktree: str, branch: str,
                       base_head: str, drec: dict) -> None:
    """Remove a commit-less joint branch in the same cleanup.

    S4.2-b rule: the joint tree committed nothing, so a branch still
    pointing at the base head owns no work — delete it with safe
    `-d` (which refuses a checked-out or unmerged branch). Anything
    else (missing branch, own commits) is left alone. The outcome is
    recorded at result.branch_cleanup. Never raises.
    """
    if not branch or not isinstance(drec, dict):
        return
    try:
        tip = _git_text(["rev-parse", "--verify", branch],
                        base_worktree)
        if not tip or tip != base_head:
            return
        gone = _run_git(["branch", "-d", branch], base_worktree)
        if gone.returncode == 0:
            note = ("leftover branch removed (at base head, "
                    "no own commits)")
        else:
            detail = (gone.stderr or gone.stdout or "").strip()[-200:]
            note = f"leftover branch kept ({detail})"
        drec.setdefault("result", {})["branch_cleanup"] = note[:300]
    except (Exception, SystemExit):  # noqa: BLE001 - tidy never blocks
        return


def _delete_leftover_branch(base_worktree: str, branch: str) -> None:
    gone = _run_git(["branch", "-d", branch], base_worktree)
    if gone.returncode != 0:
        raise IntegrationError(
            f"leftover branch {branch} refused safe delete "
            f"({(gone.stderr or gone.stdout or '').strip()[-200:]}); "
            "operator review required, no retry")


def _prepare_retry(opf: Path, existing: dict) -> None:
    """Validate an explicit retry of a BLOCKED op. No mutation on any
    refusal path; on success the leftover branch is deleted and the
    caller proceeds to a fresh record (which archives this state via
    _push_attempt). Total attempts cap at _MAX_ATTEMPTS."""
    op_id = str(existing.get("op_id") or "?")
    if existing.get("status") != "blocked":
        raise IntegrationError(
            f"operation {op_id!r} is not blocked "
            f"({existing.get('status') or 'unknown'}); only a blocked "
            "op retries, nothing started")
    chain = [a for a in (existing.get("attempts") or [])
             if isinstance(a, dict)]
    if len(chain) >= _MAX_ATTEMPTS:
        raise IntegrationError(
            f"operation {op_id!r} already attempted {len(chain)} times "
            f"(cap {_MAX_ATTEMPTS}); operator review required, no retry")
    res = existing.get("result") or {}
    base = existing.get("base") or {}
    base_worktree = str(base.get("checkout") or "")
    base_head = str(base.get("head") or "")
    if not base_worktree or not base_head:
        raise IntegrationError("blocked op records no exact base "
                               "source; cannot verify the leftover "
                               "branch, no retry")
    reason = _check_leftover_branch(
        base_worktree, str(res.get("branch") or ""), base_head,
        str(res.get("path") or ""))
    if reason:
        raise IntegrationError(reason)
    branch = str(res.get("branch") or "")
    if branch and _git_text(["rev-parse", "--verify", branch],
                            base_worktree):
        _delete_leftover_branch(base_worktree, branch)
    _ = opf


def retry_integration_op(op_id: str) -> dict:
    """Explicit retry of one BLOCKED prepare, derived, or rollup op.

    Dispatches on the recorded op kind; the record keeps its op id and
    archives the blocked state into attempts. Raises IntegrationError
    on every refusal (unknown, non-blocked, attempt cap, leftover
    branch conditions).
    """
    _, rec = _load_op(op_id)
    if rec.get("op") == "prepare":
        return prepare_integration(str(rec.get("run_id") or ""),
                                   str(rec.get("batch_id") or ""),
                                   retry=True)
    if rec.get("op") == "reresolve":
        origin = str(rec.get("derived_from") or "")
        if not origin:
            raise IntegrationError(
                f"operation {op_id!r} records no source prepare op; "
                "no retry")
        return reresolve_on_base(origin, retry=True)
    if rec.get("op") == "rollup":
        req = rec.get("request") or {}
        return rollup_leaves(str(rec.get("repo") or ""),
                             str(rec.get("lane") or ""),
                             str(req.get("base_batch") or ""),
                             [str(x) for x in (req.get("leaves") or [])
                              if str(x)],
                             retry=True)
    raise IntegrationError(f"operation {op_id!r} is not a retryable "
                           "integration op")


def prepare_retry(run_id: str, batch_id: str, by: str) -> dict:
    """M2.11a: one explicit human retry of a BLOCKED prepare.

    Only owner/root may retry (a worker never retries); the M2
    driver never calls this. Refuses anything but a blocked
    prepare op with 409 before starting anything. Runs
    prepare_integration(retry=True, origin="ui") exactly once —
    the attempt is archived on the record as today — and records
    "준비 다시 시도 (by …)" with the op id and the result status.
    The result may be the re-derived record, a new honest block,
    or done. Never loops.
    """
    name = str(by or "").strip()
    if name not in ("owner", "root"):
        raise IntegrationError(
            "prepare retry needs by owner or root (a worker can "
            "never retry)", http_status=400)
    try:
        state = store.load(run_id)
    except SystemExit as exc:
        raise IntegrationError(f"unknown run {run_id!r}; "
                               "nothing started",
                               http_status=404) from exc
    repo = str((state or {}).get("repo") or "")
    op_id = _op_id(run_id, batch_id)
    _, rec = _load_op(op_id)
    if rec.get("op") != "prepare" or rec.get("status") != "blocked":
        raise IntegrationError(
            f"operation {op_id} is not a blocked prepare "
            f"(op {rec.get('op') or '?'} status "
            f"{rec.get('status') or 'missing'}) — only a blocked "
            "prepare retries; nothing started", http_status=409)
    out = prepare_integration(run_id, batch_id, retry=True,
                              origin="ui")
    status = str((out or {}).get("status") or "?")
    derived = str((out or {}).get("op_id") or "")
    detail = f"{op_id}: {status}"
    if derived and derived != op_id:
        detail += f" (파생 {derived})"
    events.append(repo, "m2-prepare-retry", op_id,
                  f"준비 다시 시도 (by {name})", detail, actor=name)
    return out


def _merge_joint_groups(x_group: dict, y_group: dict) -> dict:
    """Union two eligibility groups for a joint derive.

    Same manifest line to the same (from, to) merges silently;
    the same line to different values refuses (the pair no
    longer describes one tree). Raises IntegrationError.
    """
    merged = {}
    for path in sorted(set(x_group or {}) | set(y_group or {})):
        per = {}
        for src in (x_group or {}, y_group or {}):
            for d, ft in ((src.get(path) or {}).items()):
                try:
                    ft = (ft[0], ft[1])
                except (IndexError, TypeError):
                    raise IntegrationError(
                        f"joint group has a malformed entry for "
                        f"{d} ({path}); refusing") from None
                if d in per and per[d] != ft:
                    raise IntegrationError(
                        f"joint candidates edit {path} {d} to "
                        f"different values ({per[d][1]} vs {ft[1]}); "
                        "refusing")
                per[d] = ft
        if per:
            merged[path] = per
    if not merged:
        raise IntegrationError("joint group is empty; refusing")
    return merged


def _joint_lock_ids(x_op_id: str, joint_id: str) -> list:
    return sorted({str(x_op_id or ""), str(joint_id or "")})


@m2.op_guarded(lambda run_id, partner_run_id, batch_id, **_:
               _joint_lock_ids(_op_id(run_id, batch_id),
                               _op_id(run_id, batch_id) + "-joint"),
               busy=lambda op: IntegrationError(
                   f"다른 확인 작업이 {op}에서 실행 중입니다", 409))
def joint_retry(run_id: str, partner_run_id: str, batch_id: str,
                by: str) -> dict:
    """M2.14b: one explicit human joint retry of two candidates that
    only ship together (live: RN 0.88 + expo 58).

    Only owner/root (a worker never retries); the M2 driver never
    calls this. X (run_id) must be a blocked prepare; Y
    (partner_run_id) must be a blocked prepare or a done
    reresolve. The pair must be a CURRENT joint hint — fresh
    detection must name Y's current pipeline op, else 409. One
    derive per call; never a loop. The M2 driver picks the done
    joint record up through the normal fastcheck -> batch stages.
    """
    name = str(by or "").strip()
    if name not in ("owner", "root"):
        raise IntegrationError(
            "joint retry needs by owner or root (a worker can "
            "never retry)", http_status=400)
    if not str(partner_run_id or "").strip() \
            or partner_run_id == run_id:
        raise IntegrationError("a joint retry needs two different "
                               "runs; nothing started", http_status=400)
    try:
        x_state = store.load(run_id)
        y_state = store.load(partner_run_id)
    except SystemExit as exc:
        raise IntegrationError(f"unknown run; nothing started",
                               http_status=404) from exc
    x_repo = str((x_state or {}).get("repo") or "")
    y_repo = str((y_state or {}).get("repo") or "")
    if not x_repo or x_repo != y_repo:
        raise IntegrationError("joint runs belong to different repos; "
                               "nothing started", http_status=400)
    x_op_id = _op_id(run_id, batch_id)
    _, x_rec = _load_op(x_op_id)
    if x_rec.get("op") != "prepare" \
            or x_rec.get("status") != "blocked":
        raise IntegrationError(
            f"operation {x_op_id} is not a blocked prepare — only a "
            "blocked prepare anchors a joint retry; nothing started",
            http_status=409)
    lane = str(x_rec.get("lane") or "")
    if lane not in ("main", "next"):
        raise IntegrationError("blocked op carries no usable lane; "
                               "nothing started", http_status=409)
    hint = m2.detect_joint_partner(x_repo, lane, x_rec)
    if not isinstance(hint, dict) or not hint.get("partner_run"):
        raise IntegrationError(
            "no current joint partner for this candidate — the pair "
            "changed or is gone; nothing started", http_status=409)
    if str((x_rec.get("base") or {}).get("head") or "") != str(
            hint.get("base") or ""):
        raise IntegrationError(
            "the lane base moved since this pair was found — refresh "
            "the hint by re-running detection; nothing started",
            http_status=409)
    if str(hint.get("partner_run") or "") != str(partner_run_id):
        raise IntegrationError(
            f"current joint partner is {hint.get('partner_run')} — "
            "not the requested run; nothing started", http_status=409)
    y_op_id = str(hint.get("partner_op") or "")
    if not y_op_id:
        raise IntegrationError("joint hint names no partner op; "
                               "nothing started", http_status=409)
    try:
        with m2.op_locked(y_op_id):
            out = _joint_reresolve_inner(
                x_op_id, y_op_id, partner_run_id, by=name)
    except m2.OpBusy as exc:
        raise IntegrationError(
            f"다른 확인 작업이 {exc.op_id}에서 실행 중입니다",
            http_status=409) from exc
    status = str((out or {}).get("status") or "?")
    x_dep = str(x_rec.get("dep") or "")
    y_dep = str(hint.get("partner_dep") or "")
    events.append(x_repo, "m2-joint-retry", _op_id(run_id, batch_id)
                  + "-joint", f"함께 다시 시도 (by {name}): "
                  f"{x_dep} + {y_dep}".strip(" +"), status, actor=name)
    return out


def _joint_commit_flow(plan: dict) -> dict:
    """M2.14/M2.17 shared joint tree-commit flow (read the plan,
    never the caller's op records).

    `plan` carries the validated derive inputs — x/y {pay, op_id,
    run_id, dep, target, commit}, x {fp, branch, worktree},
    base {worktree, head, fp, lane, batch_id}, ids {joint_id,
    slug, branch, origin}, plus repo/lane. From the union merge
    on, every step is identical for the human retry and the
    driver refresh: joint-file reconcile, collision checks,
    candidate lock reads, group guards, drec framing, worktree
    add, range re-apply, lock regen, unrelated-change guards,
    RN-mix preflight, commit, done marking. Refusals raise
    IntegrationError or return the recorded block via _fail —
    never a partial tree (worktree removed on every failure).
    Never pushes, never deletes beyond its own derived tree.
    """

    x_pay = plan["x_pay"]
    y_pay = plan["y_pay"]
    x_op_id = plan["x_op_id"]
    y_op_id = plan["y_op_id"]
    partner_run_id = plan["partner_run_id"]
    x_run_id = plan["x_run_id"]
    y_run_id = plan["y_run_id"]
    x_dep = plan["x_dep"]
    x_target = plan["x_target"]
    x_commit = plan["x_commit"]
    y_dep = plan["y_dep"]
    y_target = plan["y_target"]
    y_commit = plan["y_commit"]
    base_worktree = plan["base_worktree"]
    base_head = plan["base_head"]
    base_fp = plan["base_fp"]
    base_lane = plan["base_lane"]
    repo = plan["repo"]
    lane = plan["lane"]
    rec = plan["rec"]
    rec_run_id = rec["run_id"]
    rec_batch_id = rec["batch_id"]
    rec_dep = rec["dep"]
    rec_target = rec["target"]
    rec_joint_with = rec["joint_with"]
    rec_runs = rec["runs"]
    rec_candidate = rec["candidate"]
    rec_derived_from = rec["derived_from"]
    origin = rec["origin"]
    joint_id = plan["joint_id"]
    slug = plan["slug"]
    branch = plan["branch"]
    union = _merge_joint_groups((x_pay or {}).get("group"),
                                (y_pay or {}).get("group"))
    union_already = _merge_joint_groups_soft(
        (x_pay or {}).get("already"), (y_pay or {}).get("already"))
    joint_file = op_file(joint_id)
    new_path = paths.home() / "reviews" / slug
    jexisting = read_json(joint_file, default=None)
    if isinstance(jexisting, dict) \
            and jexisting.get("op_id") == joint_id:
        return _reconcile_existing(joint_file, jexisting, new_path)
    if new_path.exists():
        raise IntegrationError("integration path already exists "
                               f"(unrelated): {new_path}")
    if _git_head(base_worktree, branch):
        raise IntegrationError("branch already exists "
                               f"(unrelated): {branch}")
    try:
        x_lock = _show_file_at(x_commit, "yarn.lock", base_worktree)
        y_lock = _show_file_at(y_commit, "yarn.lock", base_worktree)
    except (Exception, SystemExit) as exc:
        raise IntegrationError("candidate locks unreadable; cannot "
                               "pin siblings") from exc
    if not x_lock or not y_lock:
        raise IntegrationError("candidate lock unreadable; cannot pin "
                               "siblings; nothing committed")
    # --- end of _joint_commit_flow part 1 (continued below) ---
    group_deps = sorted(
        {d for per in union.values() for d in (per or {})}
        | {x_dep})
    guard_deps = sorted(
        set(group_deps)
        | {d for per in (union_already or {}).values()
           for d in (per or {})})
    side_of = {}
    for d in group_deps:
        in_x = any(d in (per or {}) for per in (x_pay or {}).get(
            "group", {}).values())
        side_of[d] = "x" if in_x else "y"
    x_ft = _dep_from_to((x_pay or {}).get("group"), x_dep) \
        or _dep_from_to((x_pay or {}).get("already"), x_dep)
    y_ft = _dep_from_to((y_pay or {}).get("group"), y_dep) \
        or _dep_from_to((y_pay or {}).get("already"), y_dep)
    if x_ft is None or y_ft is None:
        raise IntegrationError("joint group names no range for its "
                               "own dep; refusing")
    drec = {"schema": INTEGRATION_OPS_SCHEMA, "op": "reresolve",
            "op_id": joint_id, "derived_from": rec_derived_from,
            "joint_with": rec_joint_with,
            "runs": rec_runs,
            "kind": "joint",
            "repo": repo, "run_id": rec_run_id,
            "batch_id": rec_batch_id,
            "dep": rec_dep, "target": rec_target, "lane": lane,
            "candidate": {"head": rec_candidate.get("head") or "",
                          "fingerprint": rec_candidate.get(
                              "fingerprint") or "",
                          "branch": rec_candidate.get("branch") or "",
                          "worktree": rec_candidate.get("worktree") or ""},
            "joint_candidates": [
                {"run": x_run_id, "dep": x_dep,
                 "target": x_target, "head": x_commit},
                {"run": y_run_id, "dep": y_dep,
                 "target": y_target, "head": y_commit}],
            "base": {"batch": rec_batch_id,
                     "lane": base_lane,
                     "head": base_head, "fingerprint": base_fp,
                     "checkout": base_worktree},
            "result": {"path": str(new_path), "branch": branch,
                       "head": "", "fingerprint": "",
                       "proof_note": (
                           f"DERIVED SOURCE: joint range re-apply on "
                           f"base {base_head[:12]} from candidates "
                           f"{x_commit[:12]} + {y_commit[:12]}; never "
                           "equals either original commit.")},
            "group": _group_entries(union),
            "gates": [{"key": "source-prepared", "status": "pending",
                       "note": ""},
                      {"key": "fastchecks", "status": "pending",
                       "note": "not run in this step"},
                      {"key": "native", "status": "pending",
                       "note": "Root device QA after source freeze"}],
            "status": "started", "error": "",
            "created_at": utcnow(), "updated_at": utcnow(),
            "origin": origin}
    if union_already and any((union_already or {}).values()):
        seen_already = {}
        for _per in (union_already or {}).values():
            for _d, _ft in (_per or {}).items():
                try:
                    seen_already[_d] = _ft[1]
                except (IndexError, TypeError):
                    continue
        drec["already"] = sorted(
            f"{_d} {seen_already[_d]}" for _d in seen_already)
    write_json(joint_file, drec)

    def _fail(message: str) -> dict:
        _joint_branch_tidy(base_worktree, branch, base_head, drec)
        return _record_blocked(joint_file, drec, message)

    try:
        added = _run_git(["worktree", "add", "-b", branch,
                          str(new_path), base_head], base_worktree)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout or "").strip()[-500:]
            return _fail(f"worktree add failed: {detail}")
        manifest_reapplied = _reapply_group_ranges(
            worktree=str(new_path), group=union,
            manifest_paths=sorted(union),
            prior_texts={}, base_head=base_head)
        manifest_reapplied.extend(
            _verify_group_manifests(
                worktree=str(new_path), group=union,
                manifest_reapplied=manifest_reapplied,
                already=union_already))
        manager = _detect_manager(str(new_path))
        if manager is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("no known lockfile in derived tree; "
                         f"{cleanup}")
        try:
            lock_text = Path(new_path, "yarn.lock").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            lock_text = ""
        flavor = _yarn_flavor(lock_text) if manager == "yarn" else ""
        package_manager = _package_manager_field(str(new_path))
        manifest_files = ("package.json", "example/package.json")
        manifest_bump = any(
            _manifest_declared_range(str(new_path), name, d)
            != _manifest_declared_range(base_worktree, name, d)
            for name in manifest_files for d in group_deps)
        argv, regen_origin, refusal = _regen_plan(
            manager, flavor, bool(manifest_bump))
        if not argv:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{refusal}; {cleanup}")
        if regen_origin == "lock-refresh":
            dropped = lock_text
            for d in group_deps:
                dropped = _drop_dep_stanzas(dropped, d)
            try:
                Path(new_path, "yarn.lock").write_text(
                    dropped, encoding="utf-8")
            except OSError:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail("cannot stage lockfile for refresh; "
                             f"{cleanup}")
        tool_version = _tool_version(argv[0], str(new_path))
        registry_host = _registry_host(argv[0], str(new_path))
        regen_env = _regen_env()
        disk_ok, disk_note = _regen_disk_ok()
        if not disk_ok:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{disk_note}; {cleanup}")
        try:
            regen = subprocess.run(
                argv, cwd=str(new_path), env=regen_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_RERESOLVE_TIMEOUT, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"lock tool failed to start ({argv[0]}): "
                         f"{exc}; {cleanup}")
        if regen.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(regen.stderr or regen.stdout or "")
            tail = tail.strip()[-500:]
            return _fail(f"lock tool {argv[0]} rc={regen.returncode}: "
                         f"{tail}; {cleanup}")
        present_locks = [name for name in _LOCK_FILENAMES
                         if Path(new_path, name).is_file()]
        staged = _run_git(["add", "--"] + present_locks,
                          str(new_path))
        if staged.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("cannot stage regenerated lockfiles; "
                         f"{cleanup}")
        post_files = _worktree_changed_files(str(new_path))
        if post_files is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"derived diff unreadable; {cleanup}")
        post_manifest = sorted(f for f in post_files
                               if not _is_lock_rel(f))
        if post_manifest != sorted(union):
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("derived manifest set differs from joint "
                         f"group ({', '.join(post_manifest[:8])}); "
                         f"{cleanup}, nothing committed")
        base_locks = {}
        for name in _LOCK_FILENAMES:
            shown = _run_git(["show", f"HEAD:{name}"], str(new_path))
            if shown.returncode == 0:
                base_locks[name] = shown.stdout or ""
        unrelated = []
        for name in _LOCK_FILENAMES:
            cur_path = Path(new_path) / name
            if not cur_path.is_file():
                continue
            new_text = cur_path.read_text(encoding="utf-8",
                                          errors="replace")
            old_text = base_locks.get(name)
            if old_text is None or old_text == new_text:
                continue
            bad = _lock_delta_guard(manager, old_text, new_text,
                                    x_dep,
                                    extra_deps=tuple(
                                        d for d in guard_deps
                                        if d != x_dep))
            unrelated.extend(bad)
        unrelated = unrelated[:10]
        if unrelated:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("관련 없는 잠금 이동: "
                         f"{', '.join(unrelated)}"
                         f"{' 외' if len(unrelated) == 10 else ''}; "
                         f"nothing committed, {cleanup}")
        regen_lock = Path(new_path, "yarn.lock").read_text(
            encoding="utf-8", errors="replace")
        declared_ranges = set()
        for mf in manifest_files:
            rng = _manifest_declared_range(str(new_path), mf, x_dep)
            if rng:
                declared_ranges.add(rng)
        if not declared_ranges:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"derived tree declares no range for {x_dep}; "
                         f"nothing committed, {cleanup}")
        base_lock = base_locks.get("yarn.lock") or ""
        ok, seen = _check_declared_resolution(regen_lock, base_lock,
                                              x_dep, declared_ranges,
                                              x_target)
        if not ok:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"잠금이 {seen}로 해석됨 (후보 목표 {x_target}); "
                         f"nothing committed, {cleanup}")
        pin_locks = {"x": x_lock, "y": y_lock}
        for sdep in group_deps:
            if sdep == x_dep:
                continue
            side = side_of.get(sdep, "x")
            new_ranges = set()
            for _sg in (side, "y" if side == "x" else "x"):
                side_group = (x_pay or {}).get("group") \
                    if _sg == "x" else (y_pay or {}).get("group")
                for _per in (side_group or {}).values():
                    if sdep in (_per or {}):
                        try:
                            new_ranges.add(_per[sdep][1])
                        except (IndexError, TypeError):
                            continue
            if not new_ranges:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"derived group names no range for "
                             f"sibling {sdep}; nothing committed, "
                             f"{cleanup}")
            want = {}
            for _sg in (side, "y" if side == "x" else "x"):
                cand_lock = pin_locks.get(_sg) or ""
                if not cand_lock:
                    continue
                for _rng, _ver in (
                        _declared_resolutions(
                            cand_lock, sdep, new_ranges) or {}).items():
                    # M2.12: berry "npm:" and bare forms of one range
                    # are the same pin — compare normalized, or a
                    # same-dep conflict in different forms slips
                    # through.
                    _norm = (_rng[4:] if _rng.startswith("npm:")
                             else _rng)
                    if _norm in want and want[_norm] != _ver:
                        cleanup = _remove_derived_tree(
                            base_worktree, new_path)
                        return _fail(
                            f"joint candidates pin {sdep} {_norm} "
                            f"differently ({want[_norm]} vs {_ver}); "
                            f"nothing committed, {cleanup}")
                    want.setdefault(_norm, _ver)
            pinned = {}
            for rng in new_ranges:
                # M2.12: berry "npm:" descriptor form or bare.
                ver = want.get(rng)
                if ver is None:
                    ver = want.get("npm:" + rng)
                if not isinstance(ver, str) or not ver:
                    cleanup = _remove_derived_tree(
                        base_worktree, new_path)
                    return _fail(
                        f"candidate lock pins no single version for "
                        f"sibling {sdep} {rng}; nothing committed, "
                        f"{cleanup}")
                pinned[rng] = ver
            for rng, ver in sorted(pinned.items()):
                ok, seen = _check_declared_resolution(
                    regen_lock, base_lock, sdep, {rng}, ver)
                if not ok:
                    cleanup = _remove_derived_tree(
                        base_worktree, new_path)
                    return _fail(
                        f"sibling {sdep} resolves {seen}, candidate "
                        f"locked {ver}; nothing committed, "
                        f"{cleanup}")
        try:
            dupes = lock_duplicate_versions(base_worktree,
                                            str(new_path))
            mixed = m2.rn_group_mixed(dupes)
        except (Exception, SystemExit):  # noqa: BLE001 - fail closed
            mixed = "RN 혼재 확인 실패"
        if mixed:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"함께 올려도 RN 혼재 남음: {mixed}; "
                         f"nothing committed, {cleanup}")
        others = [e for e in _group_entries(union)
                  if not e.startswith(x_dep + " ")]
        body_group = ("joint re-applied together: "
                      + ", ".join(others) + "\n\n") if others else ""
        msg = (f"chore(deps): {x_dep} {x_ft[0]} -> {x_ft[1]} + "
               f"{y_dep} {y_ft[0]} -> {y_ft[1]}\n\n"
               f"{body_group}"
               f"re-derived from {x_commit[:12]} + {y_commit[:12]} "
               f"on {base_head}\n\n"
               f"DERIVED SOURCE: joint range re-apply on base with "
               f"{' '.join(argv)} (origin {origin}"
               f"{', tool ' + tool_version if tool_version else ''}"
               f"{', registry ' + registry_host if registry_host else ''}"
               f"{', pm ' + package_manager if package_manager else ''}"
               f"). Never equals candidates {x_commit[:12]}, "
               f"{y_commit[:12]}.")
        committed = _run_git(["-c", "user.name=openmainty",
                              "-c", "user.email=openmainty@localhost",
                              "commit", "-m", msg], str(new_path))
        if committed.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(committed.stderr or committed.stdout
                               or "").strip()[-300:]
            return _fail(f"derived commit failed: {tail}; {cleanup}")
        res_snap = _snapshot(str(new_path))
        if (res_snap is None or not res_snap.get("head")
                or not res_snap.get("fingerprint")
                or not res_snap.get("clean")):
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("integrated source unreadable or dirty "
                         f"after commit; {cleanup}")
        drec["result"]["head"] = res_snap["head"]
        drec["result"]["fingerprint"] = res_snap["fingerprint"]
        drec["status"] = "done"
        drec["error"] = ""
        drec["updated_at"] = utcnow()
        for gate in drec.get("gates") or []:
            if gate.get("key") == "source-prepared":
                gate["status"] = "pass"
                gate["note"] = "joint range re-applied on base"
        write_json(joint_file, drec)
        return drec
    except IntegrationError as exc:
        cleanup = _remove_derived_tree(base_worktree, new_path)
        return _fail(f"{exc}; {cleanup}")


def derive_joint_refresh(repo: str, lane: str, base_id: str,
                         x_op_id: str, y_run_id: str,
                         by: str = "m2") -> dict:
    """M2.17 B: driver-side refreshed joint derive.

    The M2 driver calls this (never joint_retry) when a newer
    target of a done joint's member appears: it re-derives the
    pair's union on the CURRENT base through the shared joint
    commit flow — same range re-apply, lock regen, RN-mix
    preflight and record shape as the human retry. X reuses its
    solo op's candidate commit; Y uses its run's verified work
    commit. The record runs with the FRESH run as primary
    (run_id = Y, joint_with = [X op]) so the next tick resumes
    it instead of re-deriving. Refusals (unreadable inputs,
    moved base, unmergeable or mixed union) raise
    IntegrationError and commit nothing — the worktree is
    removed on every failure path. Never pushes, never deletes
    beyond its own derived tree."""

    try:
        from . import store as _store
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - no store
        raise IntegrationError(f"run store unreadable; refusing: {exc}") from exc
    x_opf, x_rec = _load_op(x_op_id)
    if not isinstance(x_rec, dict) or not x_rec:
        raise IntegrationError(f"partner op {x_op_id!r} unreadable; "
                               "joint refresh refused")
    if str(x_rec.get("repo") or "") != repo \
            or str(x_rec.get("lane") or "") != lane:
        raise IntegrationError("partner op is another repo/lane; "
                               "joint refresh refused")
    x_run = str(x_rec.get("run_id") or "")
    x_dep = str(x_rec.get("dep") or "")
    x_target = str(x_rec.get("target") or "")
    x_commit = str((x_rec.get("candidate") or {}).get("head") or "")
    if not x_run or not x_dep or not x_commit:
        raise IntegrationError("partner op lacks a candidate; "
                               "joint refresh refused")
    try:
        y_state = _store.load(y_run_id)
    except (Exception, SystemExit) as exc:
        raise IntegrationError(f"fresh run {y_run_id!r} unreadable; "
                               f"joint refresh refused: {exc}") from exc
    y_dep = str(y_state.get("dep") or "")
    y_target = str(y_state.get("target") or "")
    y_commit = str(y_state.get("workCommit") or "")
    y_fp = str(y_state.get("workTreeFingerprint") or "")
    if str(y_state.get("repo") or "") != repo \
            or str(y_state.get("lane") or "") != lane:
        raise IntegrationError("fresh run is another repo/lane; "
                               "joint refresh refused")
    if not y_dep or not y_target or not y_commit or not y_fp:
        raise IntegrationError("fresh run lacks a verified candidate; "
                               "joint refresh refused")
    if y_dep == x_dep:
        raise IntegrationError("joint members must differ; "
                               "joint refresh refused")
    y_branch = ""
    try:
        from . import sched as _sched
        st = _sched.load(repo)
        for entry in st.get("queue") or []:
            if isinstance(entry, dict) \
                    and str(entry.get("run_id") or "") == y_run_id:
                y_branch = str(entry.get("branch") or "")
                break
    except (Exception, SystemExit):  # noqa: BLE001 - branch optional
        y_branch = ""
    base = load_batch(base_id)
    if not isinstance(base, dict) or not base:
        raise IntegrationError("base batch unreadable; "
                               "joint refresh refused")
    if str(base.get("repo") or "") != repo:
        raise IntegrationError("base is another repo; "
                               "joint refresh refused")
    integs = [i for i in (base.get("integrations") or [])
              if isinstance(i, dict)]
    if len(integs) == 1:
        lane_integ = integs[0]
    else:
        lane_integ = next(
            (i for i in integs if str(i.get("lane") or "") == lane),
            {})
    base_worktree = str((lane_integ or {}).get("worktree") or "")
    base_head = str((lane_integ or {}).get("head") or "")
    base_fp = str((lane_integ or {}).get("fingerprint") or "")
    if not base_worktree or not base_head or not base_fp:
        raise IntegrationError("base integration source unrecorded; "
                               "joint refresh refused")
    base_snap = _snapshot(base_worktree)
    if (base_snap is None or base_snap.get("head") != base_head
            or base_snap.get("fingerprint") != base_fp
            or not base_snap.get("clean")):
        raise IntegrationError("base integration source moved or "
                               "dirty; joint refresh refused")
    x_verdict, x_pay = _rederive_eligible(
        base_worktree, base_head, x_dep, x_target, x_commit)
    if x_verdict != "ok":
        raise IntegrationError(
            f"joint candidate {x_dep} not re-deriveable now "
            f"({(x_pay or '')[:200]}); refusing")
    y_verdict, y_pay = _rederive_eligible(
        base_worktree, base_head, y_dep, y_target, y_commit)
    if y_verdict != "ok":
        raise IntegrationError(
            f"joint candidate {y_dep} not re-deriveable now "
            f"({(y_pay or '')[:200]}); refusing")
    yslug = f"{_slug(y_dep)}-{_slug(y_target)}"
    joint_id = f"{x_op_id}-joint-{yslug}"
    slug = (f"prep-{_slug(base_id)}-{_slug(x_dep)}-{_slug(x_target)}-"
            f"{yslug}-joint")
    branch = (f"codex/review-{_slug(x_dep)}-{_slug(x_target)}-"
              f"{yslug}-joint")
    plan = {
        "x_pay": x_pay, "y_pay": y_pay,
        "x_op_id": x_op_id, "y_op_id": None,
        "partner_run_id": y_run_id,
        "x_run_id": x_run, "y_run_id": y_run_id,
        "x_dep": x_dep, "x_target": x_target, "x_commit": x_commit,
        "y_dep": y_dep, "y_target": y_target, "y_commit": y_commit,
        "base_worktree": base_worktree, "base_head": base_head,
        "base_fp": base_fp, "base_lane": lane,
        "repo": repo, "lane": lane,
        "rec": {
            "run_id": y_run_id,
            "batch_id": base_id,
            "dep": y_dep, "target": y_target,
            "joint_with": [x_op_id],
            "runs": [y_run_id, x_run],
            "candidate": {
                "head": y_commit, "fingerprint": y_fp,
                "branch": y_branch, "worktree": "",
            },
            "derived_from": x_op_id,
            "origin": by,
        },
        "joint_id": joint_id, "slug": slug, "branch": branch,
    }
    return _joint_commit_flow(plan)


def _joint_reresolve_inner(x_op_id: str, y_op_id: str,
                           partner_run_id: str, by: str) -> dict:
    """The joint derive work (one caller: joint_retry, which holds
    the X + joint op locks; Y's op lock is taken here).

    Unions both candidates' eligibility groups on X's pinned base,
    regenerates the lock once, pins every sibling from its own
    candidate lock (the M2.12 npm: rule), and preflights the RN
    mix before committing — a still-mixed tree is an honest
    block, never a commit. Record kind "joint".
    """
    x_opf, x_rec = _load_op(x_op_id)
    if x_rec.get("op") != "prepare" \
            or x_rec.get("status") != "blocked":
        raise IntegrationError(f"operation {x_op_id!r} is not a "
                               "blocked prepare; joint refused")
    try:
        y_opf, y_rec = _load_op(y_op_id)
    except (OSError, ValueError, IntegrationError) as exc:
        raise IntegrationError(f"partner op {y_op_id!r} unreadable; "
                               "joint refused") from exc
    if y_rec.get("op") == "prepare":
        if y_rec.get("status") != "blocked":
            raise IntegrationError(
                f"partner op {y_op_id!r} is not blocked; joint refused")
    elif y_rec.get("op") == "reresolve":
        if y_rec.get("status") != "done" \
                or y_rec.get("kind") not in (None, "lockfix",
                                             "rederive"):
            raise IntegrationError(
                f"partner op {y_op_id!r} is not a done derived "
                "record; joint refused")
    else:
        raise IntegrationError(f"partner op {y_op_id!r} is not a "
                               "prepare or derived record; refusing")
    if str(y_rec.get("run_id") or "") != str(partner_run_id):
        raise IntegrationError("partner op belongs to another run; "
                               "joint refused")
    repo = str(x_rec.get("repo") or "")
    lane = str(x_rec.get("lane") or "")
    if str(y_rec.get("repo") or "") != repo \
            or str(y_rec.get("lane") or "") != lane:
        raise IntegrationError("joint runs belong to different "
                               "repo/lane records; refusing")
    x_dep = str(x_rec.get("dep") or "")
    x_target = str(x_rec.get("target") or "")
    y_dep = str(y_rec.get("dep") or "")
    y_target = str(y_rec.get("target") or "")
    if not x_dep or not x_target or not y_dep or not y_target:
        raise IntegrationError("joint ops carry no usable dep/target "
                               "identity; refusing")
    if y_dep == x_dep:
        raise IntegrationError("joint needs two different deps; "
                               "refusing")
    x_commit = str((x_rec.get("candidate") or {}).get("head") or "")
    y_commit = str((y_rec.get("candidate") or {}).get("head") or "")
    if not x_commit or not y_commit:
        raise IntegrationError("joint ops record no candidate source "
                               "identity; refusing")
    base = x_rec.get("base") or {}
    base_worktree = str(base.get("checkout") or "")
    base_head = str(base.get("head") or "")
    base_fp = str(base.get("fingerprint") or "")
    if not base_worktree or not base_head or not base_fp:
        raise IntegrationError("blocked op records no exact base "
                               "source; refusing")
    base_snap = _snapshot(base_worktree)
    if (base_snap is None or base_snap.get("head") != base_head
            or base_snap.get("fingerprint") != base_fp
            or not base_snap.get("clean")):
        raise IntegrationError("base integration source drifted or "
                               "unreadable; refusing")
    x_batch = str(x_rec.get("batch_id") or "")
    x_verdict, x_pay = _rederive_eligible(
        base_worktree, base_head, x_dep, x_target, x_commit)
    if x_verdict != "ok":
        raise IntegrationError(
            f"joint candidate {x_dep} not re-deriveable now "
            f"({(x_pay or '')[:200]}); refusing")
    y_verdict, y_pay = _rederive_eligible(
        base_worktree, base_head, y_dep, y_target, y_commit)
    if y_verdict != "ok":
        raise IntegrationError(
            f"joint candidate {y_dep} not re-deriveable now "
            f"({(y_pay or '')[:200]}); refusing")
    plan = {
        "x_pay": x_pay, "y_pay": y_pay,
        "x_op_id": x_op_id, "y_op_id": y_op_id,
        "partner_run_id": partner_run_id,
        "x_run_id": str(x_rec.get("run_id") or ""),
        "y_run_id": str(partner_run_id or ""),
        "x_dep": x_dep, "x_target": x_target, "x_commit": x_commit,
        "y_dep": y_dep, "y_target": y_target, "y_commit": y_commit,
        "base_worktree": base_worktree, "base_head": base_head,
        "base_fp": base_fp,
        "base_lane": str(base.get("lane") or ""),
        "repo": repo, "lane": lane,
        "rec": {
            "run_id": str(x_rec.get("run_id") or ""),
            "batch_id": x_batch,
            "dep": x_dep, "target": x_target,
            "joint_with": [y_op_id],
            "runs": [str(x_rec.get("run_id") or ""), partner_run_id],
            "candidate": {
                "head": x_commit,
                "fingerprint": str(
                    (x_rec.get("candidate") or {})
                    .get("fingerprint") or ""),
                "branch": str((x_rec.get("candidate") or {})
                              .get("branch") or ""),
                "worktree": str((x_rec.get("candidate") or {})
                                .get("worktree") or ""),
            },
            "derived_from": x_op_id,
            "origin": "ui",
        },
        "joint_id": x_op_id + "-joint",
        "slug": (f"prep-{_slug(x_batch)}-{_slug(x_dep)}-"
                 f"{_slug(x_target)}-joint"),
        "branch": (f"codex/review-{_slug(x_dep)}-"
                   f"{_slug(x_target)}-joint"),
    }
    return _joint_commit_flow(plan)


def _dep_from_to(group: dict, dep: str):
    """(from, to) for dep in an eligibility group (None when absent)."""
    for _per in (group or {}).values():
        if dep in (_per or {}):
            try:
                return ((_per[dep][0], _per[dep][1]))
            except (IndexError, TypeError):
                continue
    return None


def _merge_joint_groups_soft(first: dict, second: dict) -> dict:
    """Union two already-in-base maps (same shape as the group merge,
    but a missing side is simply absent — never a refusal)."""
    merged = {}
    for path in sorted(set(first or {}) | set(second or {})):
        per = {}
        for src in (first or {}, second or {}):
            for d, ft in ((src.get(path) or {}).items()):
                try:
                    per.setdefault(d, (ft[0], ft[1]))
                except (IndexError, TypeError):
                    continue
        if per:
            merged[path] = per
    return merged


def _worktree_changed_files(worktree: str) -> list | None:
    proc = _run_git(["diff", "--name-only", "HEAD"], worktree)
    if proc.returncode != 0:
        return None
    out = [line.strip() for line in (proc.stdout or "").splitlines()
           if line.strip()]
    unk = _run_git(["ls-files", "--others", "--exclude-standard"],
                   worktree)
    if unk.returncode != 0:
        return None
    out.extend(line.strip() for line in (unk.stdout or "").splitlines()
               if line.strip())
    return sorted(set(out))


def _detect_manager(worktree: str) -> str | None:
    for name, tool in (("yarn.lock", "yarn"),
                       ("package-lock.json", "npm"),
                       ("pnpm-lock.yaml", "pnpm")):
        proc = _run_git(["ls-files", "--error-unmatch", name],
                        worktree)
        if proc.returncode == 0:
            return tool
    return None


def _package_manager_field(worktree: str) -> str:
    try:
        data = json.loads(Path(worktree, "package.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    pm = data.get("packageManager") if isinstance(data, dict) else None
    return pm if isinstance(pm, str) else ""


RERESOLVE_ENV_FLAGS = ("YARN_ENABLE_SCRIPTS", "YARN_ENABLE_TELEMETRY",
                       "CI", "HUSKY")


def _regen_env() -> dict:
    """M1: registry-tool env built on fastcheck.scrub_env (lazy import,
    no module cycle). Secrets never reach the child: every *_TOKEN /
    *_KEY / *_SECRET, SSH_AUTH_SOCK, GH_TOKEN/GITHUB_TOKEN and stray
    NPM_CONFIG_* are dropped. PATH, HOME and TMPDIR stay inherited —
    the daemon has no profile toolchain here, so the tool must
    resolve exactly as the operator configured it. YARN_CACHE_FOLDER
    / COREPACK_HOME point at the app-owned regenerable cache.
    YARN_ENABLE_SCRIPTS stays "0" (the S3-tested value). Only key
    NAMES are ever recorded, never values."""
    from . import fastcheck as _fc
    base = paths.home()
    cache = base / "cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    home = os.environ.get("HOME") or str(base)
    env = _fc.scrub_env([], home, str(cache))
    env["PATH"] = os.environ.get("PATH") or env["PATH"]
    env["HOME"] = home
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    env["YARN_ENABLE_SCRIPTS"] = "0"
    return env


REGEN_MIN_DISK_GB = 30


def _regen_disk_ok(_avail_bytes=None):
    """N2: refuse the regen when the app home holds less than 30 GB.

    Same bar and measure as the fastcheck disk guard (statvfs
    available-to-user bytes, conservative). `_avail_bytes` injects
    the reading in tests. Returns (ok, korean_detail) with the GB.
    """
    try:
        avail = (_avail_bytes if _avail_bytes is not None
                 else os.statvfs(str(paths.home())).f_bavail
                 * os.statvfs(str(paths.home())).f_frsize)
    except OSError as exc:
        return (False, f"디스크 여유를 읽을 수 없음: {exc}")
    gb = avail / 1024 ** 3
    if gb < REGEN_MIN_DISK_GB:
        return (False, f"디스크 여유 {gb:.1f}GB — 잠금 재생성에 "
                       f"{REGEN_MIN_DISK_GB}GB 필요 (캐시 "
                       "재다운로드 대비)")
    return (True, f"디스크 여유 {gb:.1f}GB")


def _write_regen_log(op_id: str, text: str) -> dict:
    """Save registry-tool output as a sha-pinned log (M1 step receipt
    backing). Returns {"log", "log_sha256"}; on write failure returns
    empty strings and the caller still blocks loudly with the tail."""
    try:
        log_path = (integration_ops_dir()
                    / f"{op_id}.regen.log")
        log_path.write_text(text or "", encoding="utf-8")
        return {"log": str(log_path),
                "log_sha256": sha256_file(log_path)}
    except OSError:
        return {"log": "", "log_sha256": ""}


def _regen_plan(manager: str, flavor: str, manifest_bump: bool):
    """D1-a (structured): the command derives only from the tracked
    lockfile (+packageManager), the replayed manifest hunks, and the
    op's dep/target. Free log text never becomes argv. yarn berry
    resolves via --mode=update-lockfile (no link step, scripts off);
    a lock-only refresh first drops dep's own stanzas so only its
    descriptors re-resolve. Anything else refuses."""
    if manager != "yarn" or flavor != "berry":
        label = manager or "unknown"
        return (None, "",
                f"{label} lockfile은 이 단계에서 재생성하지 않음 "
                "(yarn berry 전용)")
    argv = ["yarn", "install", "--mode=update-lockfile"]
    if manifest_bump:
        return argv, "manifest-bump", ""
    return argv, "lock-refresh", ""


def _tool_version(tool: str, cwd: str) -> str:
    try:
        proc = _run_tool([tool, "--version"], cwd)
    except IntegrationError:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().splitlines()[0][:40] \
        if (proc.stdout or "").strip() else ""


def _registry_host(tool: str, cwd: str) -> str:
    if tool == "yarn":
        args = [tool, "config", "get", "npmRegistryServer"]
    elif tool == "npm":
        args = [tool, "config", "get", "registry"]
    else:
        return ""
    try:
        proc = _run_tool(args, cwd)
    except IntegrationError:
        return ""
    if proc.returncode != 0:
        return ""
    url = (proc.stdout or "").strip().splitlines()
    url = url[0].strip() if url else ""
    host = url.split("://", 1)[-1].split("/", 1)[0]
    host = host.rsplit("@", 1)[-1]
    return host[:120]


def _scrub_auth(text: str) -> str:
    return re.sub(r"(https?://)[^/\s]*:[^@/\s]*@",
                  r"\1<redacted>@", text or "")


def _parse_yarn_chunk(chunk: str):
    """Parse one blank-line-separated chunk into (descriptors, body).

    Returns None for header comments and non-stanza text (callers keep
    such chunks byte-identical when rewriting). A stanza header is one
    or more non-indented lines ending in ":" — this covers v1
    single-line headers and berry multi-line bodies alike.
    """
    lines = [line for line in (chunk or "").splitlines() if line.strip()]
    if not lines or lines[0][:1] in (" ", "\t", "#"):
        return None
    head = []
    body: list = []
    for i, line in enumerate(lines):
        head.append(line)
        if line.rstrip().endswith(":"):
            body = lines[i + 1:]
            break
    else:
        return None
    descs = []
    for hline in head:
        hline = hline.strip()
        if hline.endswith(":"):
            hline = hline[:-1]
        for part in hline.split(","):
            part = part.strip().strip('"')
            if part:
                descs.append(part)
    if not descs:
        return None
    return (descs, "\n".join(body))


def _split_yarn_stanzas(text: str) -> list:
    """Parse a yarn v1/berry lockfile into (descriptors, body) stanzas."""
    stanzas = []
    for chunk in (text or "").split("\n\n"):
        parsed = _parse_yarn_chunk(chunk)
        if parsed is not None:
            stanzas.append(parsed)
    return stanzas


# Stanza-level fields sit at <=2-space indent; dependency entries
# (which may name a dep "version") sit deeper and must not match.
_VERSION_LINE_RE = re.compile(r"^ {0,2}version\s*:?\s*\"?([^\"\s]+)\"?\s*$")


def _yarn_lock_versions(text: str) -> dict:
    """package -> sorted locked versions from one yarn.lock text.

    Groups stanza `version:` values by descriptor package name
    (reusing the L1 alias rule). Workspace stanzas are skipped —
    they pin the repo's own version, which legitimately moves.
    """
    out: dict = {}
    for descs, body in _split_yarn_stanzas(text):
        if any("@workspace:" in d for d in descs):
            continue
        version = None
        for line in (body or "").splitlines():
            m = _VERSION_LINE_RE.match(line)
            if m:
                version = m.group(1)
                break
        if version is None:
            continue
        for desc in descs:
            name = _yarn_pkg_name(desc)
            if name and name != "__metadata":
                out.setdefault(name, set()).add(version)
    return {k: sorted(v) for k, v in out.items()}


def lock_duplicate_versions(base_wt: str, new_wt: str,
                            top: int = 10) -> dict:
    """Packages gaining a second locked version (M2.5c, read-only).

    Compares the root yarn.lock in two worktrees; a package counts
    when the new lock holds 2+ versions including one the base
    lacked (a pure replace stays silent). Returns {"items":
    ["metro 0.84.4 + 0.87.1", ...top N], "total": N}. Missing
    locks mean no report. Never raises, never blocks."""
    try:
        base_text = Path(base_wt, "yarn.lock").read_text(
            encoding="utf-8", errors="ignore")
        new_text = Path(new_wt, "yarn.lock").read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return {"items": [], "total": 0}
    try:
        base = _yarn_lock_versions(base_text)
        new = _yarn_lock_versions(new_text)
    except Exception:  # noqa: BLE001 - unparseable means no report
        return {"items": [], "total": 0}
    dupes = []
    for name in sorted(new):
        vers = new[name]
        if len(vers) > 1 and set(vers) - set(base.get(name) or []):
            dupes.append(f"{name} {' + '.join(vers)}")
    return {"items": dupes[:max(1, top)], "total": len(dupes)}


def _drop_dep_stanzas(text: str, dep: str) -> str:
    """Remove exactly dep's own stanzas (exact package-name match on
    parsed descriptors); every other chunk stays byte-identical."""
    kept = []
    for chunk in (text or "").split("\n\n"):
        parsed = _parse_yarn_chunk(chunk)
        if parsed is not None and any(
                _yarn_pkg_name(d) == dep for d in parsed[0]):
            continue
        kept.append(chunk)
    return "\n\n".join(kept)


def _yarn_flavor(text: str) -> str:
    """berry when a __metadata stanza parses, v1 on its header line."""
    for chunk in (text or "").split("\n\n"):
        parsed = _parse_yarn_chunk(chunk)
        if parsed is not None and parsed[0] == ["__metadata"]:
            return "berry"
    head = (text or "")[:500]
    if "# yarn lockfile v1" in head:
        return "v1"
    return ""


def _descriptor_range(desc: str, name: str) -> str:
    if not desc.startswith(name):
        return ""
    rest = desc[len(name):]
    return rest[1:] if rest.startswith("@") else rest


def _range_matches_declared(desc_range: str, declared: str) -> bool:
    return desc_range == declared or desc_range == "npm:" + declared


def _body_version(body: str) -> str:
    match = re.search(r'^\s*version:?\s*"?([^"\s]+)"?\s*$',
                      body or "", re.M)
    return match.group(1) if match else ""


def _check_declared_resolution(new_text: str, base_text: str, dep: str,
                               declared: set, target: str):
    """R7: stanzas whose descriptors match the workspace's declared
    range(s) for dep must resolve to exactly target. Any OTHER dep
    version may remain only byte-identical to the base (e.g. a retained
    older node for a transitive descriptor). Returns (ok, seen)."""
    def _index(text):
        out = {}
        for descs, body in _split_yarn_stanzas(text):
            key = tuple(sorted(descs))
            if set(_yarn_pkg_name(d) for d in key) == {"__metadata"}:
                continue
            out[key] = body
        return out
    base = _index(base_text)
    new = _index(new_text)
    for key, body in new.items():
        names = {_yarn_pkg_name(d) for d in key}
        if dep not in names:
            continue
        ranges = {_descriptor_range(d, dep) for d in key}
        if any(_range_matches_declared(r, x) for r in ranges
               for x in declared):
            if _body_version(body) != target:
                return False, _body_version(body) or "없음"
        elif base.get(key) != body:
            return False, _body_version(body) or "없음"
    return True, target


def _declared_resolutions(text: str, dep: str, declared: set) -> dict:
    """Map each declared range of dep to the version its matching lock
    stanza(s) resolve to (rollup per-leaf check). Ranges with no
    matching stanza are absent; a range resolving to several versions
    maps to the sorted version list."""
    out = {}
    for descs, body in _split_yarn_stanzas(text):
        names = {_yarn_pkg_name(d) for d in descs}
        if "__metadata" in names or dep not in names:
            continue
        ver = _body_version(body) or ""
        for d in descs:
            rng = _descriptor_range(d, dep)
            if any(_range_matches_declared(rng, x) for x in declared):
                out.setdefault(rng, set()).add(ver)
    return {r: (sorted(v)[0] if len(v) == 1 else sorted(v))
            for r, v in out.items()}


def _yarn_resolved_versions(text: str, dep: str) -> set:
    """All locked versions for dep's own stanzas (v1 and berry)."""
    vers = set()
    for descs, body in _split_yarn_stanzas(text):
        if any(_yarn_pkg_name(d) == dep for d in descs):
            match = re.search(r'^\s*version:?\s*"?([^"\s]+)"?\s*$',
                              body or "", re.M)
            if match:
                vers.add(match.group(1))
    return vers


def _yarn_pkg_name(descriptor: str) -> str:
    """Package name of one lock descriptor: everything before the
    FIRST "@" after an optional leading scope. An npm alias
    ("string-width-cjs@npm:string-width@^4.2.0") belongs to the alias
    package ("string-width-cjs") — rsplit once named it
    "alias@npm:target" and falsely tripped the closure guard (L1)."""
    desc = (descriptor or "").strip()
    if desc.startswith("@"):
        return "@" + desc[1:].split("@", 1)[0]
    return desc.split("@", 1)[0]


def _yarn_entry_name(line: str):
    """Exact dependency name of one lock body entry line (v1 `name
    "range"` or berry `name: "protocol:range"`); None for other lines."""
    stripped = (line or "").strip()
    m = re.match(r'^"?([^"\s:]+)"?\s*:', stripped)
    if m:
        return m.group(1)
    m = re.match(r'^"?([^"\s]+)"?\s', stripped)
    if m:
        return m.group(1)
    return None


def _yarn_stanza_deps(body: str) -> list:
    """Dependency names of one lock stanza body (v1 and berry).

    Section headers sit at 2-space indent, entries deeper: v1 uses
    `name "range"`, berry uses `name: "protocol:range"`. Only
    dependencies: and optionalDependencies: expand the closure —
    peerDependencies: are supplied by the consumer, not resolved here,
    so they stay out (or the closure drags in whole peer trees).
    """
    deps = []
    in_deps = False
    for line in (body or "").splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent <= 2:
            in_deps = stripped in ("dependencies:",
                                   "optionalDependencies:")
            continue
        if in_deps:
            name = _yarn_entry_name(stripped)
            if name:
                deps.append(name)
    return deps


def _yarn_range_of(value: str) -> str:
    """Range of one lock entry value (`npm:^7.25.2` -> `^7.25.2`).

    Strips one leading `protocol:` prefix (npm, patch, ...); a
    bare range or anything unparsable passes through unchanged
    (the range check then fails open, never blocks)."""
    value = (value or "").strip()
    if ":" in value:
        head, rest = value.split(":", 1)
        if rest and re.fullmatch(r"[A-Za-z][A-Za-z0-9+.]*",
                                 head or ""):
            return rest
    return value


def _yarn_hard_dep_ranges(lock_text: str, dep: str) -> list:
    """[(pkg, version, range)] — non-workspace stanzas whose hard
    `dependencies:` names dep (yarn v1 + berry).

    version comes from the stanza `version:` line ("" when
    absent); the range is the entry value with any `protocol:`
    prefix stripped. `peerDependencies:` (including `*`) never
    counts — only hard `dependencies:` decide. Unreadable text
    gives []. Never raises."""
    out = []
    try:
        stanzas = _split_yarn_stanzas(lock_text)
    except Exception:  # noqa: BLE001 - unreadable lock
        return out
    for descs, body in stanzas:
        try:
            if any("@workspace:" in d for d in descs):
                continue
            names = sorted(
                {_yarn_pkg_name(d) for d in descs}
                - {"__metadata"})
            if not names:
                continue
            ver = ""
            for line in (body or "").splitlines():
                m = _VERSION_LINE_RE.match(line)
                if m:
                    ver = m.group(1)
                    break
            in_hard = False
            for line in (body or "").splitlines():
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip(" "))
                stripped = line.strip()
                if indent <= 2:
                    in_hard = (stripped == "dependencies:")
                    continue
                if not in_hard:
                    continue
                m = re.match(r'^"?([^"\s:]+)"?\s*:\s*"([^"]*)"',
                             stripped)
                if m is None:
                    m = re.match(r'^"?([^"\s]+)"?\s+"([^"]*)"',
                                 stripped)
                if m is None:
                    continue
                if m.group(1) == dep:
                    out.append((names[0], ver, _yarn_range_of(
                        m.group(2))))
        except Exception:  # noqa: BLE001 - one bad stanza
            continue
    return out


def _yarn_hard_dependent_counts(lock_text: str) -> dict:
    """{dep: distinct packages with a hard `dependencies:` on it}.

    Counts distinct stanza packages (not stanzas), skipping
    workspace and metadata stanzas. Unreadable text gives {}.
    Never raises."""

    counts = {}
    try:
        stanzas = _split_yarn_stanzas(lock_text)
    except Exception:  # noqa: BLE001 - unreadable lock
        return counts
    for descs, body in stanzas:
        try:
            if any("@workspace:" in d for d in descs):
                continue
            names = sorted(
                {_yarn_pkg_name(d) for d in descs}
                - {"__metadata"})
            if not names:
                continue
            in_hard = False
            seen = set()
            for line in (body or "").splitlines():
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip(" "))
                stripped = line.strip()
                if indent <= 2:
                    in_hard = (stripped == "dependencies:")
                    continue
                if in_hard:
                    name = _yarn_entry_name(stripped)
                    if name:
                        seen.add(name)
            for dep in seen:
                counts.setdefault(dep, set()).add(names[0])
        except Exception:  # noqa: BLE001 - one bad stanza
            continue
    return {k: len(v) for k, v in counts.items()}


def _yarn_closure(stanzas: list, dep: str) -> set:
    by_name: dict = {}
    for descs, body in stanzas:
        for desc in descs:
            by_name.setdefault(_yarn_pkg_name(desc),
                               []).append(body)
    seen = {dep}
    stack = [dep]
    while stack:
        name = stack.pop()
        for body in by_name.get(name, []):
            for sub in _yarn_stanza_deps(body):
                if sub not in seen:
                    seen.add(sub)
                    stack.append(sub)
    return seen


def _is_workspace_key(key: tuple) -> bool:
    return any("@workspace:" in d for d in key)


def _lock_delta_guard(manager: str, base_text: str, new_text: str,
                      dep: str, extra_deps=()) -> list:
    """D1-b: every added/removed/changed lock entry must belong to the
    candidate package's own dependency closure. The closure runs over
    the UNION of the base and new graphs (a new version may add new
    transitive names); __metadata and header comments are ignored. A
    `@workspace:` stanza may change only on lines naming dep itself.
    extra_deps (rollup) widens the allowed set to the union of every
    listed dep's closure; the workspace line rule then accepts a line
    naming any of them. Returns the bounded unrelated-name list
    (empty = clean)."""
    if manager != "yarn":
        raise IntegrationError(f"{manager} lockfile delta is not "
                               "auditable by this slice; refusing")
    base_st = _split_yarn_stanzas(base_text)
    new_st = _split_yarn_stanzas(new_text)
    base = {}
    for descs, body in base_st:
        key = tuple(sorted(descs))
        if set(_yarn_pkg_name(d) for d in key) == {"__metadata"}:
            continue
        base[key] = body
    new = {}
    for descs, body in new_st:
        key = tuple(sorted(descs))
        if set(_yarn_pkg_name(d) for d in key) == {"__metadata"}:
            continue
        new[key] = body
    dep_set = {dep} | {d for d in (extra_deps or ()) if d}
    allowed = set()
    for name in dep_set:
        allowed |= _yarn_closure(base_st + new_st, name)
    allowed |= dep_set
    bad = []
    for key in set(base) | set(new):
        old, cur = base.get(key), new.get(key)
        if old == cur:
            continue
        names = {_yarn_pkg_name(d) for d in key} - {"__metadata"}
        if not names:
            continue
        if _is_workspace_key(key):
            # Exact entry-name match: a substring test would let
            # "@types/react" cover a "@types/react-dom" line.
            changed = set((old or "").splitlines()) ^ set(
                (cur or "").splitlines())
            if any(_yarn_entry_name(line) not in dep_set
                   for line in changed):
                first = sorted(names)[0]
                if first not in bad:
                    bad.append(first)
            continue
        for name in sorted(names):
            if name and name not in allowed and name not in bad:
                bad.append(name)
    return bad[:10]


_DEP_SECTIONS = ("dependencies", "devDependencies",
                 "peerDependencies", "optionalDependencies")
_RERESOLVE_MANIFESTS = ("package.json", "example/package.json")


# M2.9a: manifests whose range lines may be re-applied on the
# current base when a pure bump conflicts on lines the base moved.
REDERIVE_MANIFESTS = _RERESOLVE_MANIFESTS + ("pubspec.yaml",)


def _manifest_dep_ranges(text: str, dep: str):
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for section in _DEP_SECTIONS:
        sec = data.get(section)
        if isinstance(sec, dict) and dep in sec:
            return sec[dep]
    return None


def _manifest_declared_range(worktree: str, filename: str,
                             dep: str):
    try:
        text = Path(worktree, filename).read_text(encoding="utf-8")
    except OSError:
        return None
    return _manifest_dep_ranges(text, dep)


def _snapshot_manifest_texts(worktree: str) -> dict:
    """Current bytes of the re-resolvable manifests in a worktree.

    A later D1-d conflict re-checks against what earlier replay steps
    left (clean applies included), never against the stale original.
    Missing files are simply absent.
    """
    out = {}
    for name in _RERESOLVE_MANIFESTS:
        try:
            out[name] = Path(worktree, name).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def _show_file_at(rev: str, path: str, cwd: str):
    proc = _run_git(["show", f"{rev}:{path}"], cwd)
    if proc.returncode != 0:
        return None
    return proc.stdout if proc.stdout is not None else None


def _manifest_changed_ranges(old_text: str, new_text: str):
    """Every changed declaration between two manifests.

    Returns {dep: (old_range, new_range)} across the dependency
    sections (a missing side reads None). Returns {} when nothing
    changed or when either side is not a manifest dict at all.
    """
    try:
        old = json.loads(old_text)
        new = json.loads(new_text)
    except ValueError:
        return {}
    if not isinstance(old, dict) or not isinstance(new, dict):
        return {}
    out = {}
    for sec in _DEP_SECTIONS:
        o = old.get(sec)
        n = new.get(sec)
        if not isinstance(o, dict) or not isinstance(n, dict):
            continue
        for key in set(o) | set(n):
            if o.get(key) != n.get(key):
                out[key] = (o.get(key), n.get(key))
    return out


def _manifest_only_dep_differs(old_text: str, new_text: str, dep: str):
    """True when two manifests differ only in dep's declared range.

    Returns (ok, old_range, new_range). Any second changed line, any
    non-manifest shape change, or any non-string range refuses.
    """
    try:
        old = json.loads(old_text)
        new = json.loads(new_text)
    except ValueError:
        return False, "", ""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False, "", ""

    def _strip(d):
        d = dict(d)
        for sec in _DEP_SECTIONS:
            if isinstance(d.get(sec), dict) and dep in d[sec]:
                sec2 = dict(d[sec])
                del sec2[dep]
                d[sec] = sec2
        return d

    if _strip(old) != _strip(new):
        return False, "", ""
    found = []
    for sec in _DEP_SECTIONS:
        o = old.get(sec)
        n = new.get(sec)
        if not isinstance(o, dict) or not isinstance(n, dict):
            continue
        for key in set(o) | set(n):
            if o.get(key) != n.get(key) and key == dep:
                found.append((o.get(key), n.get(key)))
    if len(found) != 1:
        return False, "", ""
    old_r, new_r = found[0]
    if (not isinstance(old_r, str) or not isinstance(new_r, str)
            or not old_r or not new_r or old_r == new_r):
        return False, "", ""
    return True, old_r, new_r


def _replace_dep_range_line(base_text: str, dep: str,
                            old_range: str, new_range: str):
    """Rebuild base bytes with exactly dep's range line replaced.

    Returns the new text, or None unless exactly one `"dep":`
    declaration line carrying the old range exists. A dep
    declared in two sections (live shape: devDependencies
    "19.2.8" plus peerDependencies "*") still replaces cleanly
    when only one line carries the old range; two lines with the
    same old range refuse (fail closed — the sections cannot be
    told apart textually).
    """
    if not old_range or not new_range:
        return None
    lines = (base_text or "").splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines)
            if re.search(r'"' + re.escape(dep) + r'"\s*:', line)]
    old_quoted = f'"{old_range}"'
    with_old = [i for i in hits if lines[i].count(old_quoted) == 1]
    if len(with_old) != 1:
        return None
    i = with_old[0]
    lines[i] = lines[i].replace(old_quoted, f'"{new_range}"', 1)
    return "".join(lines)


def classify_conflict_scope(conflicted_paths, dep, texts) -> str:
    """Pure scope classifier over RECORDED conflict data (no git, no
    mutation) — the same rule the reresolve enforcer applies, so the
    driver, the UI label and the fixture-free tests share one verdict.

    conflicted_paths: the recorded rec.result.conflicted_paths.
    dep: the candidate dependency name.
    texts: {manifest_path: {"parent": str|None, "candidate": str|None,
    "base": str|None}} for the re-resolvable manifests.
    Returns "lock-only" | "adjacent" | "same-line" | "other" |
    "unknown". An empty or missing conflict list is "unknown" (no
    evidence to act on — the display may guess, the driver never
    does). Range equality uses the same helpers as the D1-d
    enforcer, so the two cannot disagree on the same inputs.
    """
    if not conflicted_paths:
        return "unknown"
    if _has_pub_conflict(conflicted_paths):
        # M3.3a prepare-only: pubspec conflicts never enter the
        # yarn-only regen — not even pubspec.lock, although it sits
        # in _LOCK_FILENAMES.
        return "other"
    non_lock = [p for p in (conflicted_paths or [])
                if str(p).split("/")[-1] not in _LOCK_FILENAMES]
    if not non_lock:
        return "lock-only"
    if any(p not in _RERESOLVE_MANIFESTS for p in non_lock):
        return "other"
    texts = texts or {}
    verdict = "adjacent"
    for path in non_lock:
        blobs = texts.get(path) or {}
        parent_text = blobs.get("parent")
        cand_text = blobs.get("candidate")
        base_text = blobs.get("base")
        if parent_text is None or cand_text is None \
                or base_text is None:
            return "unknown"
        ok, parent_range, _to_range = _manifest_only_dep_differs(
            parent_text, cand_text, dep)
        if not ok:
            verdict = "other"
            continue
        if _manifest_dep_ranges(base_text, dep) != parent_range:
            if verdict != "other":
                verdict = "same-line"
    return verdict


def _stale_label_for_pick(dep, target, conflicted_paths,
                            base_worktree, cand_worktree, base_head,
                            sha):
    """FIX-a4: read-only blob texts for one conflicted pick.

    Parent/candidate manifests come from the candidate repo, the
    base manifest from the base repo. Anything unreadable returns
    None (fail closed to a true conflict) — never raises into the
    conflict cleanup path.
    """
    try:
        paths = [str(p) for p in (conflicted_paths or [])
                 if str(p).split("/")[-1] not in _LOCK_FILENAMES
                 and p in _RERESOLVE_MANIFESTS]
        if not paths or not sha or not base_head:
            return None
        parent = _git_text(["rev-parse", f"{sha}^"], cand_worktree)
        if not parent:
            return None
        texts = {}
        for path in paths:
            texts[path] = {
                "parent": _show_file_at(parent, path, cand_worktree),
                "candidate": _show_file_at(sha, path, cand_worktree),
                "base": _show_file_at(base_head, path, base_worktree)}
        return stale_candidate_label(dep, target, conflicted_paths,
                                     texts)
    except Exception:
        return None


def stale_candidate_label(dep, target, conflicted_paths,
                          texts) -> dict | None:
    """FIX-a4: a "conflict" that is really a stale candidate.

    Pure (no git, no mutation): when every conflicted manifest hunk
    touches only the bumped dep's own version line(s) and the base
    holds a DIFFERENT version of that dep, the candidate was cut
    from an older base — it must be remade, never force-applied.
    Returns {"label", "base_version"} or None. Lockfile-only
    conflicts return None (the re-resolve path owns them); any
    other dep's line, any missing blob, or disagreeing base
    versions across manifests also return None (a true conflict).
    """
    dep = str(dep or "")
    target = str(target or "")
    if not dep or not conflicted_paths:
        return None
    non_lock = [str(p) for p in (conflicted_paths or [])
                if str(p).split("/")[-1] not in _LOCK_FILENAMES]
    if not non_lock:
        return None
    if any(p not in _RERESOLVE_MANIFESTS for p in non_lock):
        return None
    texts = texts or {}
    versions = set()
    for path in non_lock:
        blobs = texts.get(path) or {}
        parent_text = blobs.get("parent")
        cand_text = blobs.get("candidate")
        base_text = blobs.get("base")
        if parent_text is None or cand_text is None \
                or base_text is None:
            return None
        ok, parent_range, to_range = _manifest_only_dep_differs(
            parent_text, cand_text, dep)
        if not ok:
            return None
        base_range = _manifest_dep_ranges(base_text, dep)
        if not isinstance(base_range, str) or not base_range:
            return None
        if base_range == parent_range:
            # The base did not move this line — a true conflict.
            return None
        if base_range == to_range:
            # The base already holds the candidate's intent.
            return None
        versions.add(base_range)
    if len(versions) != 1:
        return None
    base_version = next(iter(versions))
    return {"label": f"기준이 이미 {dep}를 {base_version}로 올림 — "
                     f"{target} 후보를 현재 기준에서 다시 만들어야 함",
            "base_version": base_version}


def prepare_reresolve_scope(rec) -> dict:
    """Read-only driver gate for one reresolve attempt (never mutates).

    Resolves the single-commit range and the three blob texts per
    manifest via read-only git in the recorded base checkout, then
    runs the pure classifier. Returns {eligible, scope, reason} —
    eligible only for lock-only/adjacent single-commit ranges with
    complete evidence. Multi-commit ranges, missing checkouts and
    unreadable blobs all refuse (the manual UI path still handles
    multi-commit ranges). Never raises: unreadable reads as
    ineligible with the reason.
    """
    try:
        return _prepare_reresolve_scope(rec)
    except Exception as exc:  # noqa: BLE001 - unreadable never acts
        return {"eligible": False, "scope": "unknown",
                "reason": f"확인 불가: {type(exc).__name__}"}


def _prepare_reresolve_scope(rec) -> dict:
    rec = rec or {}
    if str(rec.get("op") or "") != "prepare" \
            or str(rec.get("status") or "") != "blocked":
        return {"eligible": False, "scope": "unknown",
                "reason": "blocked prepare 기록이 아님"}
    if "cherry-pick" not in str(rec.get("error") or ""):
        return {"eligible": False, "scope": "unknown",
                "reason": "cherry-pick 충돌 기록 없음"}
    dep = str(rec.get("dep") or "")
    if not dep:
        return {"eligible": False, "scope": "unknown",
                "reason": "dep 없음"}
    result = rec.get("result") or {}
    conflicted = result.get("conflicted_paths") or []
    if not conflicted:
        return {"eligible": False, "scope": "unknown",
                "reason": "충돌 범위 미기록"}
    if _has_pub_conflict(conflicted):
        # M3.3a prepare-only: pubspec conflicts never enter the
        # yarn-only regen (see classify_conflict_scope).
        return {"eligible": False, "scope": "other",
                "reason": "pubspec 재해결은 이 단계에서 미지원 — "
                          "충돌 시 사람이 확인"}
    non_lock = [p for p in conflicted
                if str(p).split("/")[-1] not in _LOCK_FILENAMES]
    if not non_lock:
        # Lock-only needs no blob reads: the regen re-resolves the
        # recorded range, and the enforcer re-verifies the range
        # itself before mutating.
        return {"eligible": True, "scope": "lock-only",
                "reason": "lock-only 범위 — 자동 재해결 시도"}
    if any(p not in _RERESOLVE_MANIFESTS for p in non_lock):
        return {"eligible": False, "scope": "other",
                "reason": "잠금·선언 범위를 벗어난 충돌 — 사람이 확인"}
    base = rec.get("base") or {}
    checkout = str(base.get("checkout") or "")
    base_head = str(base.get("head") or "")
    work = str((rec.get("candidate") or {}).get("head") or "")
    if not (checkout and base_head and work):
        return {"eligible": False, "scope": "unknown",
                "reason": "기준/후보 정보 부족"}
    if not Path(checkout).is_dir():
        return {"eligible": False, "scope": "unknown",
                "reason": "기준 작업실 확인 불가"}
    listed = _run_git(["rev-list", "--reverse",
                       f"{base_head}..{work}"], checkout)
    revs = [r.strip() for r in (listed.stdout or "").splitlines()
            if r.strip()] if listed.returncode == 0 else []
    if len(revs) != 1 or revs[0] != work:
        return {"eligible": False, "scope": "unknown",
                "reason": f"단일 커밋 범위 아님({len(revs)}건) — "
                          "수동 재해결로 처리"}
    parent = _git_text(["rev-parse", f"{work}^"], checkout)
    if not parent:
        return {"eligible": False, "scope": "unknown",
                "reason": "부모 커밋 확인 불가"}
    texts = {}
    for path in _RERESOLVE_MANIFESTS:
        if path not in conflicted:
            continue
        texts[path] = {
            "parent": _show_file_at(parent, path, checkout),
            "candidate": _show_file_at(work, path, checkout),
            "base": _show_file_at(base_head, path, checkout)}
    scope = classify_conflict_scope(conflicted, dep, texts)
    if scope in ("lock-only", "adjacent"):
        return {"eligible": True, "scope": scope,
                "reason": f"{scope} 범위 — 자동 재해결 시도"}
    if scope == "same-line":
        return {"eligible": False, "scope": scope,
                "reason": "기준이 같은 의존성 선언을 이미 변경함 — "
                          "새 기준 실행이 필요"}
    if scope == "other":
        return {"eligible": False, "scope": scope,
                "reason": "잠금·선언 범위를 벗어난 충돌 — 사람이 확인"}
    return {"eligible": False, "scope": "unknown",
            "reason": "범위를 확정할 수 없음 — 사람이 확인"}


def _reapply_manifest_ranges(*, worktree: str, cand_worktree: str,
                             base_head: str, base_commit: str,
                             rev_list: list, pos: int,
                             dep: str, manifest_paths: list,
                             prior_texts: dict,
                             blob_worktrees: dict = None,
                             conflict_sha: str = "",
                             parent_sha: str = "",
                             sibling_deps: tuple = ()) -> list:
    """D1-d: rebuild base manifests with exactly dep's range reapplied.

    Compares the CONFLICTING commit (rev_list[pos-1]) against its own
    parent (rev_list[pos-2], or base_commit at pos 1) — never the range
    tip, whose later commits may touch the manifest again. For each
    manifest: that commit's diff may touch ONLY dep's own declared-range
    line, and the current base line must equal the conflicting commit's
    parent line (else this is a true same-line conflict — the base
    already moved it — and the candidate is stale). The current base
    line is the original base blob at pos 1, or the accumulated rebuild
    from earlier positions (prior_texts, updated in place) — a later
    conflict re-checks against what earlier reapplications left, never
    against the stale original. Returns manifest_reapplied entries.
    Raises IntegrationError with the specific refusal; the caller aborts
    and removes the worktree.

    M2.6b: sibling_deps names fellow train members' deps (family
    path only). A changed line belonging to a sibling is never
    dep's to apply: when the running tree already holds the
    candidate intent it is recorded satisfied; otherwise it is left
    untouched for the sibling's own pick (recorded deferred). Any
    other extra line still refuses, exactly like the single path.
    """
    reapplied = []
    # Rollup passes both shas explicitly: at a leaf boundary
    # rev_list[pos-2] is the previous leaf's tip, not the conflicting
    # commit's own parent. The default derivation is unchanged for the
    # single-range caller.
    if not conflict_sha:
        conflict_sha = rev_list[pos - 1]
    if not parent_sha:
        parent_sha = rev_list[pos - 2] if pos > 1 else base_commit
    blobs = blob_worktrees or {}

    def _blob(sha):
        return _show_file_at(sha, path, blobs.get(sha) or cand_worktree)

    for path in manifest_paths:
        cand_text = _blob(conflict_sha)
        parent_text = _blob(parent_sha)
        base_text = prior_texts.get(path)
        if base_text is None:
            base_text = _show_file_at(base_head, path, worktree)
        if cand_text is None or parent_text is None \
                or base_text is None:
            raise IntegrationError(
                f"manifest {path} unreadable in candidate, parent or "
                "base; refusing")
        # M2.6b: per-line intent reconciliation. A candidate may
        # legitimately touch a sibling's line when trains overlap
        # (the ts-eslint pair: one commit bumps both lines) — every
        # changed line must either reapply cleanly (own dep, base
        # still on the parent range) or already hold the candidate's
        # intent in the running tree (an earlier member put it
        # there). The own-dep staleness check runs FIRST, so a stale
        # base reports "기준이 같은 의존성 선언을 이미 변경함" instead
        # of hiding behind the multi-line message.
        changes = _manifest_changed_ranges(parent_text, cand_text)
        if not changes:
            raise IntegrationError(
                f"manifest {path} shows no range change; refusing")
        satisfied = []
        deferred = []
        own_change = None
        sibs = set(sibling_deps or ())
        for key, (old_r, new_r) in changes.items():
            tree_r = _manifest_dep_ranges(base_text, key)
            if isinstance(new_r, str) and new_r \
                    and tree_r == new_r:
                if key == dep or key in sibs:
                    satisfied.append(key)
                    continue
                # A stranger's line already at the candidate intent
                # is coincidence, not consent — still out of scope.
                raise IntegrationError(
                    f"candidate manifest {path} touches more than "
                    f"{dep}'s own range line (also {key}); refusing")
            if key != dep:
                if key in sibs:
                    # M2.6b: a fellow member's line is never ours
                    # to apply — its own pick handles it later.
                    deferred.append(key)
                    continue
                raise IntegrationError(
                    f"candidate manifest {path} touches more than "
                    f"{dep}'s own range line (also {key}); refusing")
            if (not isinstance(old_r, str) or not old_r
                    or not isinstance(new_r, str) or not new_r
                    or old_r == new_r):
                raise IntegrationError(
                    f"candidate manifest {path} touches more than "
                    f"{dep}'s own range line; refusing")
            if tree_r != old_r:
                raise IntegrationError(
                    "기준이 같은 의존성 선언을 이미 변경함"
                    f"({tree_r or '없음'}) — 후보를 현재 기준에서 새로 "
                    "만들어야 함")
            own_change = (old_r, new_r)
        new_bytes = base_text
        if own_change is not None:
            new_bytes = _replace_dep_range_line(
                base_text, dep, own_change[0], own_change[1])
            if new_bytes is None:
                raise IntegrationError(
                    f"manifest {path} has no single {dep} declaration "
                    "line; refusing")
            ok, _, _ = _manifest_only_dep_differs(base_text,
                                                  new_bytes, dep)
            if not ok:
                raise IntegrationError(
                    f"rebuilt manifest {path} differs beyond {dep}; "
                    "refusing")
        # Always rewrite: the conflicted pick left merge markers in
        # the worktree file, even when the resolved bytes equal the
        # running tree (a fully-satisfied overlapping pick).
        try:
            Path(worktree, path).write_text(new_bytes,
                                            encoding="utf-8")
        except OSError as exc:
            raise IntegrationError(
                f"cannot write rebuilt manifest "
                f"{path}: {exc}; refusing") from exc
        staged = _run_git(["add", "--", path], worktree)
        if staged.returncode != 0:
            raise IntegrationError("cannot stage rebuilt manifest "
                                   f"{path}; refusing")
        prior_texts[path] = new_bytes
        entry = {"file": path, "dep": dep,
                 "from": own_change[0] if own_change
                 else _manifest_dep_ranges(base_text, dep),
                 "to": own_change[1] if own_change
                 else _manifest_dep_ranges(base_text, dep)}
        if satisfied:
            # Sibling lines the running tree already holds at the
            # candidate's intent (overlapping trains).
            entry["already_applied"] = sorted(satisfied)
        if deferred:
            # Fellow members' lines left for their own picks.
            entry["deferred_to_sibling"] = sorted(deferred)
        reapplied.append(entry)
    return reapplied


def _group_entries(group: dict) -> list:
    """Flatten {path: {dep: (from, to)}} to sorted "dep from->to"
    strings (M2.10 derived record, commit body, glance detail)."""
    seen = {}
    for per in (group or {}).values():
        for d, ft in (per or {}).items():
            try:
                seen.setdefault(d, set()).add((ft[0], ft[1]))
            except (IndexError, TypeError):
                continue
    return sorted(f"{d} {f}->{t}" for d, fts in seen.items()
                  for f, t in sorted(fts))


def _group_targets(group: dict) -> dict:
    """{dep: target} for a group map (eligibility already refuses a
    dep with different targets across manifests; sorted-first is
    deterministic)."""
    out = {}
    for per in (group or {}).values():
        for d, ft in (per or {}).items():
            try:
                out.setdefault(d, set()).add(ft[1])
            except (IndexError, TypeError):
                continue
    return {d: sorted(t)[0] for d, t in out.items()}


def _verify_group_manifests(*, worktree: str, group: dict,
                             manifest_reapplied: list,
                             already: dict = None) -> list:
    """M2.10: manifests the pick applied cleanly already hold the
    group intent — verify (never assume) and record them, so the
    message, the guard and the checks see the whole group, not
    just the conflicted files. Already-in-base lines must still
    sit at their target (the base held them; nothing may have
    moved them). Returns synthesized entries in the same {"file",
    "dep", "from", "to"} shape. Raises IntegrationError; the
    caller aborts and removes the worktree."""
    have = {e.get("file") for e in (manifest_reapplied or [])
            if isinstance(e, dict)}
    extra = []
    for path in sorted(set(group or {}) | set(already or {})):
        entries = (group or {}).get(path) or {}
        skipped = (already or {}).get(path) or {}
        try:
            text = Path(worktree, path).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            raise IntegrationError(
                f"derived manifest {path} unreadable; "
                "refusing") from None
        for gdep in sorted(entries):
            _frm, to = entries[gdep]
            tree_r = _manifest_dep_ranges(text, gdep)
            if tree_r != to:
                raise IntegrationError(
                    f"derived manifest {path} holds {gdep} at "
                    f"{tree_r or '없음'}, not the group target {to}; "
                    "refusing")
        for gdep in sorted(skipped):
            try:
                to = skipped[gdep][1]
            except (IndexError, TypeError):
                raise IntegrationError(
                    f"derived manifest {path} has a malformed "
                    f"already-entry for {gdep}; refusing") from None
            tree_r = _manifest_dep_ranges(text, gdep)
            if tree_r != to:
                raise IntegrationError(
                    f"derived manifest {path} moved {gdep} off its "
                    f"base value (now {tree_r or '없음'}, base held "
                    f"{to}); refusing")
        if path not in have:
            for gdep in sorted(entries):
                frm, to = entries[gdep]
                extra.append({"file": path, "dep": gdep,
                              "from": frm, "to": to})
    return extra


def _reapply_group_ranges(*, worktree: str, group: dict,
                          manifest_paths: list, prior_texts: dict,
                          base_head: str) -> list:
    """M2.10: rebuild base manifests with a whole group re-applied.

    group is {path: {dep: (from, to)}} from _rederive_eligible (the
    base still holds every `from` there — verified read-only before
    any mutation). For each conflicted manifest: the running tree
    must still hold `from` for every listed dep (a drift mid-replay
    refuses instead of guessing), then every line is replaced and
    the rebuilt file must equal the base except those keys' values
    (any other change refuses). Returns manifest_reapplied entries
    in the same {"file", "dep", "from", "to"} shape as the single
    path, one per dep. Raises IntegrationError; the caller aborts
    and removes the worktree.
    """
    reapplied = []
    for path in manifest_paths:
        entries = (group or {}).get(path) or {}
        if not entries:
            raise IntegrationError(
                f"manifest {path} conflicted but carries no group "
                "entry; refusing")
        base_text = prior_texts.get(path)
        if base_text is None:
            base_text = _show_file_at(base_head, path, worktree)
        if base_text is None:
            raise IntegrationError(
                f"manifest {path} unreadable in base; refusing")
        new_bytes = base_text
        for gdep in sorted(entries):
            frm, to = entries[gdep]
            tree_r = _manifest_dep_ranges(new_bytes, gdep)
            if tree_r != frm:
                raise IntegrationError(
                    f"manifest {path} no longer holds {gdep} at "
                    f"{frm} (now {tree_r or '없음'}); refusing")
            new_bytes = _replace_dep_range_line(new_bytes, gdep,
                                                frm, to)
            if new_bytes is None:
                raise IntegrationError(
                    f"manifest {path} has no single {gdep} "
                    "declaration line; refusing")
        if not _manifest_only_keys_differ(base_text, new_bytes,
                                          set(entries)):
            raise IntegrationError(
                f"rebuilt manifest {path} differs beyond the group; "
                "refusing")
        try:
            Path(worktree, path).write_text(new_bytes,
                                            encoding="utf-8")
        except OSError as exc:
            raise IntegrationError(
                f"cannot write rebuilt manifest "
                f"{path}: {exc}; refusing") from exc
        staged = _run_git(["add", "--", path], worktree)
        if staged.returncode != 0:
            raise IntegrationError("cannot stage rebuilt manifest "
                                   f"{path}; refusing")
        prior_texts[path] = new_bytes
        for gdep in sorted(entries):
            frm, to = entries[gdep]
            reapplied.append({"file": path, "dep": gdep,
                              "from": frm, "to": to})
    return reapplied


def _reresolve_lock_ids(op_id: str) -> list:
    """Op ids whose worktrees reresolve touches: the source record it
    reads and the '<op>-lockfix' tree it writes (sorted). A retry posts
    the derived id itself, which already ends in -lockfix (or
    -rederive for an M2.9a re-derive, which never posts here but is
    recognized all the same)."""
    op = str(op_id or "")
    if op.endswith(("-lockfix", "-rederive")):
        derived = op
    else:
        derived = op + "-lockfix"
    return sorted({op, derived})


@m2.op_guarded(lambda op_id, **_: _reresolve_lock_ids(op_id),
                  busy=lambda op: IntegrationError(
                      f"다른 확인 작업이 {op}에서 실행 중입니다", 409))
def reresolve_on_base(op_id: str, retry: bool = False,
                      origin=None, kind=None) -> dict:
    """Re-resolve a lockfile-only prepare conflict on the base.

    Explicit only: the UI posts {op, confirm: true}; no scheduler,
    daemon, or CLI-auto caller exists. Produces a NEW derived op
    ("<op_id>-lockfix") and never rewrites the original blocked record.
    With retry=True an existing BLOCKED derived record re-runs
    explicitly (attempt archived, leftover branch verified).
    `origin` records who started it: "m2" for driver-initiated
    (S15a auto-reresolve); UI calls pass nothing and keep today's
    shape (no origin key). Never root_attested (J1) — a derived op
    is worker-built until Root QA lands.

    M2.9a: kind="rederive" produces a "<op_id>-rederive" record for
    a pure range bump re-applied on the current base (called from
    inside prepare_integration on an adjacent conflict — never by
    the UI). Same guards, same lease/op-lock order; the commit
    message names the original candidate and the record carries
    kind "rederive" so metrics and the glance tell it apart.
    """
    return _reresolve_on_base_inner(op_id, retry=retry, origin=origin,
                                    kind=kind)


def _reresolve_on_base_inner(op_id: str, retry: bool = False,
                             origin=None, kind=None,
                             rederive_group=None,
                             rederive_already=None) -> dict:
    """The reresolve work without the op-guard decorator.

    External callers use reresolve_on_base (locks {source, derived}).
    prepare_integration's M2.9a re-derive calls this directly: it
    already holds the source op lock, and the derived id is fresh
    (or serialized by that same source lock on retry), so taking
    the guard again would self-refuse.

    M2.10: rederive_group ({path: {dep: (from, to)}} from
    _rederive_eligible) re-applies a whole group bump together.
    Without it a kind="rederive" call keeps the single-dep path.
    rederive_already (same shape) lists group lines the base
    already holds at the target: never edited, only recorded.
    """
    if kind not in (None, "rederive"):
        raise IntegrationError("unknown derive kind", http_status=400)
    if origin not in (None, "ui", "m2"):
        raise IntegrationError(f"unknown reresolve origin {origin!r}")
    opf, rec = _load_op(op_id)
    if rec.get("op") != "prepare":
        raise IntegrationError(f"operation {op_id!r} is not a prepare "
                               "op; lock re-resolve refused")
    if rec.get("status") != "blocked":
        raise IntegrationError(f"operation {op_id!r} is not blocked "
                               f"({rec.get('status') or 'unknown'}); "
                               "lock re-resolve refused")
    if "cherry-pick" not in str(rec.get("error") or ""):
        raise IntegrationError(f"operation {op_id!r} records no "
                               "cherry-pick conflict; lock re-resolve "
                               "refused")
    run_id = str(rec.get("run_id") or "")
    batch_id = str(rec.get("batch_id") or "")
    try:
        state = store.load(run_id)
    except SystemExit:
        raise IntegrationError(f"unknown run '{run_id}'",
                               http_status=404) from None
    try:
        batch = load_batch(batch_id)
    except BatchError:
        raise IntegrationError(f"unknown batch '{batch_id}'",
                               http_status=404) from None
    repo = str(rec.get("repo") or "")
    if not repo or batch.get("repo") != repo:
        raise IntegrationError("run and batch belong to different repos")
    dep = str(rec.get("dep") or "")
    target = str(rec.get("target") or "")
    lane = str(rec.get("lane") or "")
    if not dep or not target or lane not in ("main", "next"):
        raise IntegrationError("blocked op carries no usable dep/target/"
                               "lane identity")
    work_commit = str((rec.get("candidate") or {}).get("head") or "")
    work_fp = str((rec.get("candidate") or {}).get("fingerprint") or "")
    if not work_commit or not work_fp:
        raise IntegrationError("blocked op records no candidate source "
                               "identity")
    cand_worktree = str((rec.get("candidate") or {}).get("worktree")
                        or "")
    cand_snap = _snapshot(cand_worktree) if cand_worktree else None
    live_head = cand_snap.get("head") if cand_snap else None
    live_fp = cand_snap.get("fingerprint") if cand_snap else None
    vstat = store.verification_status(state, live_fp, live_head)
    gaps = _accept_proof_gaps(vstat)
    if gaps:
        detail = ", ".join(f"{s}={vstat.get(s)}" for s in gaps)
        raise IntegrationError("candidate review proof not fresh "
                               f"({detail}); re-verify, do not derive")
    if (cand_snap is None or live_head != work_commit
            or live_fp != work_fp or not cand_snap.get("clean")):
        raise IntegrationError("candidate source drifted or unreadable; "
                               "re-verify, do not derive")
    base = rec.get("base") or {}
    base_worktree = str(base.get("checkout") or "")
    base_head = str(base.get("head") or "")
    base_fp = str(base.get("fingerprint") or "")
    if not base_worktree or not base_head or not base_fp:
        raise IntegrationError("blocked op records no exact base source")
    base_snap = _snapshot(base_worktree)
    if (base_snap is None or base_snap.get("head") != base_head
            or base_snap.get("fingerprint") != base_fp
            or not base_snap.get("clean")):
        raise IntegrationError("base integration source drifted or "
                               "unreadable")
    base_commit = str(state.get("baseCommit") or "")
    if not base_commit:
        raise IntegrationError("run records no base commit "
                               "(insufficient evidence)")
    listed = _run_git(["rev-list", "--reverse",
                       f"{base_commit}..{work_commit}"], cand_worktree)
    rev_list = [r.strip() for r in (listed.stdout.splitlines()
                                    if listed.returncode == 0 else [])
                if r.strip()]
    if listed.returncode != 0 or not rev_list:
        raise IntegrationError("candidate has no replayable commits; "
                               "refusing before any mutation")
    if len(rev_list) > _MAX_REPLAY_COMMITS:
        raise IntegrationError(f"candidate is a {len(rev_list)}-commit "
                               f"range; at most {_MAX_REPLAY_COMMITS} "
                               "supported")
    if rev_list[-1] != work_commit:
        raise IntegrationError("candidate history moved during "
                               "re-resolve; refusing")
    multi = len(rev_list) > 1
    commit_list = []
    for sha in rev_list:
        shown = _run_git(["show", "-s", "--format=%s", sha],
                         cand_worktree)
        subject = (shown.stdout.strip().splitlines() or [""])[0][:160]
        commit_list.append({"head": sha,
                            "subject": subject or sha[:12]})
    changed = _run_git(["diff", "--name-only",
                        f"{base_commit}", work_commit], cand_worktree)
    if changed.returncode != 0:
        raise IntegrationError("candidate diff unreadable; refusing "
                               "before any mutation")
    cand_files = [line.strip() for line in
                  (changed.stdout or "").splitlines() if line.strip()]
    cand_manifest = sorted(f for f in cand_files
                           if not _is_lock_rel(f))
    # An empty manifest set is valid: a lock-only candidate (declared
    # range already admits the target) derives purely by regenerating
    # the lock on the base.
    # M2.9a: a re-derived range bump rides the same machinery with
    # its own identity (never "-lockfix", never the original).
    suffix = "-rederive" if kind == "rederive" else "-lockfix"
    derived_id = op_id + suffix
    derived_file = op_file(derived_id)
    slug = (f"prep-{_slug(batch_id)}-{_slug(dep)}-{_slug(target)}"
            f"{suffix}")
    new_path = paths.home() / "reviews" / slug
    branch = (f"codex/review-{_slug(dep)}-{_slug(target)}"
              f"{suffix}")
    dexisting = read_json(derived_file, default=None)
    if isinstance(dexisting, dict) \
            and dexisting.get("op_id") == derived_id:
        if not retry:
            return _reconcile_existing(derived_file, dexisting, new_path)
        _prepare_retry(derived_file, dexisting)
        # Fall through: the fresh derived record below archives this
        # blocked state via _push_attempt (chain-safe).
    if new_path.exists():
        raise IntegrationError("integration path already exists "
                               f"(unrelated): {new_path}")
    if _git_head(base_worktree, branch):
        raise IntegrationError("branch already exists "
                               f"(unrelated): {branch}")
    drec = {"schema": INTEGRATION_OPS_SCHEMA, "op": "reresolve",
            "op_id": derived_id, "derived_from": op_id,
            "repo": repo, "run_id": run_id, "batch_id": batch_id,
            "dep": dep, "target": target, "lane": lane,
            "candidate": {"head": work_commit, "fingerprint": work_fp,
                          "branch": str((rec.get("candidate") or {})
                                        .get("branch") or ""),
                          "worktree": cand_worktree},
            "base": {"batch": batch_id,
                     "lane": str(base.get("lane") or ""),
                     "head": base_head, "fingerprint": base_fp,
                     "checkout": base_worktree},
            "result": {"path": str(new_path), "branch": branch,
                       "head": "", "fingerprint": "",
                       # M2.9a: the re-derive proof names the original
                       # candidate and the base it was re-applied on.
                       "proof_note": (
                           f"DERIVED SOURCE: range re-applied on base "
                           f"{base_head[:12]} from candidate "
                           f"{work_commit[:12]}; never equals the "
                           "original commit."
                           if kind == "rederive" else
                           "DERIVED SOURCE: lockfile regenerated on "
                           "the base; never equals the candidate "
                           "commits")},
            "gates": [{"key": "source-prepared", "status": "pending",
                       "note": ""},
                      {"key": "fastchecks", "status": "pending",
                       "note": "not run in this step"},
                      {"key": "native", "status": "pending",
                       "note": "Root device QA after source freeze"}],
            "status": "started", "error": "",
            "created_at": utcnow(), "updated_at": utcnow()}
    if origin is not None:
        # S17: who started it ("m2" for the driver). UI calls pass
        # nothing, so their records keep today's shape (no key).
        drec["origin"] = origin
    if kind == "rederive":
        # M2.9a: the record tells re-derives apart from lockfixes
        # for metrics and the glance.
        drec["kind"] = "rederive"
        if rederive_group:
            # M2.10: the whole re-applied group ("dep from->to"),
            # for the glance title, the commit body and audits.
            drec["group"] = _group_entries(rederive_group)
        if rederive_already and any((rederive_already or {}).values()):
            # Lines the base already held at the target: skipped,
            # never edited — recorded as "dep to".
            seen_already = {}
            for _per in (rederive_already or {}).values():
                for _d, _ft in (_per or {}).items():
                    try:
                        seen_already[_d] = _ft[1]
                    except (IndexError, TypeError):
                        continue
            drec["already"] = sorted(
                f"{_d} {seen_already[_d]}" for _d in seen_already)
    if multi:
        drec["candidate"]["commit_list"] = commit_list
        drec["result"]["replayed_from"] = list(rev_list)
    if isinstance(dexisting, dict):
        # Retry (or legacy) chain: the prior blocked state archives
        # into attempts instead of being erased.
        _push_attempt(drec, dexisting)
    write_json(derived_file, drec)

    def _fail(message: str) -> dict:
        return _record_blocked(derived_file, drec, message)

    try:
        added = _run_git(["worktree", "add", "-b", branch,
                          str(new_path), base_head], base_worktree)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout or "").strip()[-500:]
            return _fail(f"worktree add failed: {detail}")
        lockfix = False
        manifest_reapplied: list = []
        rebuilt_manifests: dict = {}
        remaining = list(rev_list)
        while remaining:
            sha = remaining[0]
            pick = _run_git(["-c", "user.name=openmainty",
                             "-c", "user.email=openmainty@localhost",
                             "cherry-pick", "-n", sha], str(new_path))
            if pick.returncode == 0:
                # A later conflict re-checks manifests against this
                # applied state, not the stale original base.
                rebuilt_manifests.update(
                    _snapshot_manifest_texts(str(new_path)))
                remaining = remaining[1:]
                continue
            pos = len(rev_list) - len(remaining) + 1
            conflicted = _conflicted_paths(str(new_path))
            abort = _run_git(["cherry-pick", "--quit"], str(new_path))
            _ = abort
            if conflicted is None:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail("conflict paths unreadable at commit "
                             f"{pos}/{len(rev_list)} {sha[:12]}; "
                             f"no success claimed, {cleanup}")
            outsiders = sorted(p for p in conflicted
                               if not _is_lock_rel(p))
            manifest_paths = sorted(
                p for p in outsiders if p in _RERESOLVE_MANIFESTS)
            others = sorted(
                p for p in outsiders if p not in _RERESOLVE_MANIFESTS)
            if others:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                subj = next((c["subject"] for c in commit_list
                             if c["head"] == sha), sha[:12])
                return _fail("non-lock conflict at commit "
                             f"{pos}/{len(rev_list)} {sha[:12]} "
                             f"({subj}), paths: "
                             f"{', '.join(others[:8])}; no success "
                             f"claimed, {cleanup}")
            if manifest_paths:
                try:
                    if kind == "rederive" and rederive_group:
                        # M2.10: the whole group goes on together —
                        # never a partial derive of one member.
                        manifest_reapplied.extend(
                            _reapply_group_ranges(
                                worktree=str(new_path),
                                group=rederive_group,
                                manifest_paths=manifest_paths,
                                prior_texts=rebuilt_manifests,
                                base_head=base_head))
                    else:
                        manifest_reapplied.extend(
                            _reapply_manifest_ranges(
                                worktree=str(new_path),
                                cand_worktree=cand_worktree,
                                base_head=base_head,
                                base_commit=base_commit,
                                rev_list=rev_list, pos=pos, dep=dep,
                                manifest_paths=manifest_paths,
                                prior_texts=rebuilt_manifests))
                except IntegrationError as d1err:
                    _run_git(["cherry-pick", "--abort"], str(new_path))
                    cleanup = _remove_derived_tree(base_worktree,
                                                   new_path)
                    return _fail(f"{d1err}; {cleanup}")
            for p in conflicted:
                if not _is_lock_rel(p):
                    continue
                if p.rsplit("/", 1)[-1] != "yarn.lock" or "/" in p:
                    cleanup = _remove_derived_tree(base_worktree,
                                                   new_path)
                    return _fail(f"{p} 충돌은 이 단계에서 재생성하지 "
                                 f"않음 (yarn berry 전용); {cleanup}")
            _restore_lockfiles_to_head(str(new_path))
            # Same freshness rule as the family loop: a conflicted
            # pick's cleanly-applied manifest hunks survive --quit, so
            # a later position must re-check against them, not the
            # stale original base.
            rebuilt_manifests.update(
                _snapshot_manifest_texts(str(new_path)))
            lockfix = True
            remaining = remaining[1:]
        if kind == "rederive" and rederive_group:
            # M2.10: manifests the pick applied cleanly already hold
            # the group intent — verify and record them.
            try:
                manifest_reapplied.extend(
                    _verify_group_manifests(
                        worktree=str(new_path),
                        group=rederive_group,
                        manifest_reapplied=manifest_reapplied,
                        already=rederive_already))
            except IntegrationError as gerr:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"{gerr}; {cleanup}")
        if not lockfix:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("replay applied cleanly with no lockfile "
                         "conflict; no lock re-resolve needed, "
                         f"{cleanup}")
        _restore_lockfiles_to_head(str(new_path))
        live_files = _worktree_changed_files(str(new_path))
        if live_files is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"derived diff unreadable; {cleanup}")
        live_manifest = sorted(f for f in live_files
                               if not _is_lock_rel(f))
        if live_manifest != cand_manifest:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("derived manifest set differs from candidate "
                         f"({', '.join(live_manifest[:8])} vs "
                         f"{', '.join(cand_manifest[:8])}); {cleanup}")
        manager = _detect_manager(str(new_path))
        if manager is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("no known lockfile in derived tree; "
                         f"{cleanup}")
        try:
            lock_text = Path(new_path, "yarn.lock").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            lock_text = ""
        flavor = _yarn_flavor(lock_text) if manager == "yarn" else ""
        package_manager = _package_manager_field(str(new_path))
        manifest_files = ("package.json", "example/package.json")
        if kind == "rederive" and rederive_group:
            # M2.10: every group member's declaration counts for
            # the bump plan and the refresh drop alike.
            group_deps = sorted(
                {d for per in rederive_group.values()
                 for d in (per or {})} | {dep})
            # Already-in-base lines are never re-applied and never
            # pinned, but their lock stanzas belong to the same
            # closure, so the guard allows them too.
            guard_deps = sorted(
                set(group_deps)
                | {d for per in (rederive_already or {}).values()
                   for d in (per or {})})
        else:
            group_deps = [dep]
            guard_deps = [dep]
        manifest_bump = any(
            _manifest_declared_range(str(new_path), name, d)
            != _manifest_declared_range(base_worktree, name, d)
            for name in manifest_files for d in group_deps)
        argv, origin, refusal = _regen_plan(manager, flavor,
                                            bool(manifest_bump))
        if not argv:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{refusal}; {cleanup}")
        if origin == "lock-refresh":
            dropped = lock_text
            for d in group_deps:
                dropped = _drop_dep_stanzas(dropped, d)
            try:
                Path(new_path, "yarn.lock").write_text(
                    dropped, encoding="utf-8")
            except OSError:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail("cannot stage lockfile for refresh; "
                             f"{cleanup}")
        tool_version = _tool_version(argv[0], str(new_path))
        registry_host = _registry_host(argv[0], str(new_path))
        # M1: secrets never reach the registry tool.
        regen_env = _regen_env()
        # N2: the first regen re-downloads into the app cache —
        # refuse early when the disk cannot take it.
        disk_ok, disk_note = _regen_disk_ok()
        if not disk_ok:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{disk_note}; {cleanup}")
        try:
            regen = subprocess.run(
                argv, cwd=str(new_path), env=regen_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_RERESOLVE_TIMEOUT, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"lock tool failed to start ({argv[0]}): "
                         f"{exc}; {cleanup}")
        if regen.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(regen.stderr or regen.stdout or "")
            tail = tail.strip()[-500:]
            return _fail(f"lock tool {argv[0]} rc={regen.returncode}: "
                         f"{tail}; {cleanup}")
        present_locks = [name for name in _LOCK_FILENAMES
                         if Path(new_path, name).is_file()]
        staged = _run_git(["add", "--"] + present_locks,
                          str(new_path))
        if staged.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("cannot stage regenerated lockfiles; "
                         f"{cleanup}")
        post_files = _worktree_changed_files(str(new_path))
        if post_files is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("derived diff unreadable after regenerate; "
                         f"{cleanup}")
        post_manifest = sorted(f for f in post_files
                               if not _is_lock_rel(f))
        if post_manifest != cand_manifest:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("regenerate moved non-lock files "
                         f"({', '.join(post_manifest[:8])}); {cleanup}, "
                         "nothing committed")
        base_locks = {}
        for name in _LOCK_FILENAMES:
            shown = _run_git(["show", f"HEAD:{name}"], str(new_path))
            if shown.returncode == 0:
                base_locks[name] = shown.stdout or ""
        unrelated = []
        for name in _LOCK_FILENAMES:
            cur_path = Path(new_path) / name
            if not cur_path.is_file():
                continue
            new_text = cur_path.read_text(encoding="utf-8",
                                          errors="replace")
            old_text = base_locks.get(name)
            if old_text is None or old_text == new_text:
                continue
            bad = _lock_delta_guard(manager, old_text, new_text,
                                    dep,
                                    extra_deps=tuple(
                                        d for d in guard_deps
                                        if d != dep))
            unrelated.extend(bad)
        unrelated = unrelated[:10]
        if unrelated:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("관련 없는 잠금 이동: "
                         f"{', '.join(unrelated)}"
                         f"{' 외' if len(unrelated) == 10 else ''}; "
                         f"nothing committed, {cleanup}")
        regen_lock = Path(new_path, "yarn.lock").read_text(
            encoding="utf-8", errors="replace")
        declared_ranges = set()
        for mf in manifest_files:
            rng = _manifest_declared_range(str(new_path), mf, dep)
            if rng:
                declared_ranges.add(rng)
        if not declared_ranges:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"derived tree declares no range for {dep}; "
                         f"nothing committed, {cleanup}")
        base_lock = base_locks.get("yarn.lock") or ""
        ok, seen = _check_declared_resolution(regen_lock, base_lock,
                                              dep, declared_ranges,
                                              target)
        if not ok:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"잠금이 {seen}로 해석됨 (후보 목표 {target}); "
                         f"nothing committed, {cleanup}")
        for sdep in group_deps:
            # M2.10: every sibling's NEW range pins to the version
            # the CANDIDATE's own lock resolved — the derive must
            # reproduce the reviewed change, not a newer publish.
            # Every other sibling stanza (old ranges, transitive
            # nodes) rides the same byte-identical-to-base rule as
            # the primary via the shared checker.
            if sdep == dep:
                continue
            new_ranges = set()
            for _per in (rederive_group or {}).values():
                if sdep in (_per or {}):
                    try:
                        new_ranges.add(_per[sdep][1])
                    except (IndexError, TypeError):
                        continue
            if not new_ranges:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"derived group names no range for "
                             f"sibling {sdep}; nothing committed, "
                             f"{cleanup}")
            cand_lock = (_show_file_at(work_commit, "yarn.lock",
                                       cand_worktree) or "")
            if not cand_lock:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"candidate lock unreadable; cannot pin "
                             f"sibling {sdep}; nothing committed, "
                             f"{cleanup}")
            want = _declared_resolutions(cand_lock, sdep, new_ranges)
            pinned = {}
            for rng in new_ranges:
                # M2.12: _declared_resolutions keys carry the lock
                # descriptor's own range form — yarn berry writes an
                # "npm:" protocol prefix ("@scope/dep@npm:0.88.0")
                # while the manifest declares it bare. Accept both
                # forms (the bare form first); anything else is a
                # genuinely unpinned sibling.
                ver = want.get(rng)
                if ver is None:
                    ver = want.get("npm:" + rng)
                if not isinstance(ver, str) or not ver:
                    cleanup = _remove_derived_tree(
                        base_worktree, new_path)
                    return _fail(
                        f"candidate lock pins no single version for "
                        f"sibling {sdep} {rng}; nothing committed, "
                        f"{cleanup}")
                pinned[rng] = ver
            for rng, ver in sorted(pinned.items()):
                ok, seen = _check_declared_resolution(
                    regen_lock, base_lock, sdep, {rng}, ver)
                if not ok:
                    cleanup = _remove_derived_tree(
                        base_worktree, new_path)
                    return _fail(
                        f"sibling {sdep} resolves {seen}, candidate "
                        f"locked {ver}; nothing committed, "
                        f"{cleanup}")
        if kind == "rederive" and (manifest_reapplied or
                                   rederive_group):
            # M2.9a: the message names the original candidate's own
            # range edit (from the re-applied manifest entries).
            # M2.10: the first line names the primary dep; the body
            # lists the whole re-applied group. A single-dep derive
            # reads exactly like the M2.9 message.
            pft = None
            for _p in sorted(rederive_group or {}):
                _per = (rederive_group or {}).get(_p) or {}
                if dep in _per:
                    try:
                        pft = (_per[dep][0], _per[dep][1])
                    except (IndexError, TypeError):
                        pft = None
                    break
            if pft is None:
                _first = manifest_reapplied[0]
                pft = (_first.get("from"), _first.get("to"))
            others = [e for e in _group_entries(rederive_group)
                      if not e.startswith(dep + " ")]
            body_group = ("group re-applied together: "
                          + ", ".join(others) + "\n\n") if others else ""
            already_lines = []
            for _per in (rederive_already or {}).values():
                for _d, _ft in (_per or {}).items():
                    try:
                        already_lines.append(f"{_d} {_ft[1]}")
                    except (IndexError, TypeError):
                        continue
            body_already = ("already in base: "
                            + ", ".join(sorted(set(already_lines)))
                            + "\n\n") if already_lines else ""
            msg = (f"chore(deps): {dep} {pft[0]} -> {pft[1]}\n\n"
                   f"{body_group}"
                   f"{body_already}"
                   f"re-derived from {work_commit} on {base_head}\n\n"
                   f"DERIVED SOURCE: range re-applied on base with "
                   f"{' '.join(argv)} (origin {origin}"
                   f"{', tool ' + tool_version if tool_version else ''}"
                   f"{', registry ' + registry_host if registry_host else ''}"
                   f"{', pm ' + package_manager if package_manager else ''}"
                   "). Never equals candidate "
                   f"{work_commit[:12]}.")
        else:
            msg = (f"chore(deps): {dep} -> {target} "
                   f"(DERIVED lock re-resolve of {op_id})\n\n"
                   f"DERIVED SOURCE: lockfile regenerated on base "
                   f"{base_head[:12]} with {' '.join(argv)} "
                   f"(origin {origin}"
                   f"{', tool ' + tool_version if tool_version else ''}"
                   f"{', registry ' + registry_host if registry_host else ''}"
                   f"{', pm ' + package_manager if package_manager else ''}"
                   "). Never equals candidate "
                   f"{work_commit[:12]}.")
        committed = _run_git(["-c", "user.name=openmainty",
                              "-c", "user.email=openmainty@localhost",
                              "commit", "-m", msg], str(new_path))
        if committed.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(committed.stderr or committed.stdout
                               or "").strip()[-300:]
            return _fail(f"derived commit failed: {tail}; {cleanup}")
        res_snap = _snapshot(str(new_path))
        if (res_snap is None or not res_snap.get("head")
                or not res_snap.get("fingerprint")
                or not res_snap.get("clean")):
            return _fail("derived source unreadable or dirty after "
                         "commit")
        if manifest_reapplied and kind != "rederive":
            origin = "manifest-adjacent"
        if kind == "rederive":
            # M2.9a: the derivation is a re-applied range bump, not
            # a lock refresh — recorded under its own origin.
            origin = "rederive"
        drec["regenerated_by"] = {"tool": argv[0], "argv": argv,
                                  "rc": 0,
                                  "tool_version": tool_version,
                                  "registry_host": registry_host,
                                  "package_manager": package_manager,
                                  "mode": "update-lockfile",
                                  "env_flags": list(RERESOLVE_ENV_FLAGS),
                                  "resolved_version": target,
                                  "manifest_reapplied": manifest_reapplied,
                                  "command_origin": origin}
        # M1 step receipt: what ran, its pinned log, and the fact
        # the env was scrubbed (key names only, never values).
        drec["regen_receipt"] = dict(
            _write_regen_log(op_id, regen.stdout or ""),
            argv=argv, rc=0, registry_host=registry_host,
            tool_version=tool_version,
            env_keys=sorted(regen_env),
            scripts_off=(regen_env.get("YARN_ENABLE_SCRIPTS")
                         == "0"))
        drec["result"]["head"] = res_snap["head"]
        drec["result"]["fingerprint"] = res_snap["fingerprint"]
        if kind == "rederive":
            drec["result"]["proof_note"] = (
                "DERIVED SOURCE: range re-applied on base "
                f"{base_head[:12]} from candidate {work_commit[:12]}; "
                "never equals the original commit.")
        else:
            drec["result"]["proof_note"] = ("DERIVED SOURCE: lockfile "
                                            "regenerated on base "
                                            f"{base_head[:12]}; never "
                                            "equals candidate "
                                            f"{work_commit[:12]}")
        drec["status"] = "done"
        drec["updated_at"] = utcnow()
        for gate in drec["gates"]:
            if gate["key"] == "source-prepared":
                gate["status"] = "pass"
                gate["note"] = (f"파생: {branch} @ "
                                f"{res_snap['head'][:12]} fp "
                                f"{res_snap['fingerprint'][:12]} "
                                f"(원본 {op_id}에서 범위 재적용)"
                                if kind == "rederive" else
                                f"파생: {branch} @ "
                                f"{res_snap['head'][:12]} fp "
                                f"{res_snap['fingerprint'][:12]} "
                                f"(원본 {op_id}에서 잠금 재생성)")
        write_json(derived_file, drec)
        return drec
    except Exception as exc:
        _record_blocked(derived_file, drec,
                        f"interrupted: {type(exc).__name__}: {exc}"
                        [:1000])
        raise


def _archive_fastcheck_receipt(op_id: str, existing: dict) -> str:
    """Rename the receipt to *.fastcheck.attempt-N.json (kept, never
    overwritten) and list it in the op record's attempts. Returns
    the archived file name. L2: owner/Root re-run of a RED receipt
    through {op, confirm:true, retry:true}."""
    from . import fastcheck as _fc
    from .util import read_json as _read_json, write_json as _write_json
    home = paths.home()
    stem = _fc.op_fastcheck_path(op_id).stem
    taken = set()
    try:
        for path in (home / "integration_ops").glob(stem + ".attempt-*.json"):
            taken.add(path.name)
    except OSError:
        pass
    num = 1
    while (stem + f".attempt-{num}.json") in taken:
        num += 1
    archived = stem + f".attempt-{num}.json"
    try:
        opf = op_file(op_id)
        rec = _read_json(opf, default=None) or {}
    except Exception as exc:  # noqa: BLE001 - lost op never fails retry
        raise IntegrationError(
            f"operation {op_id!r}의 재시도 기록 실패: {exc}") from exc
    if not isinstance(rec, dict):
        raise IntegrationError(
            f"operation {op_id!r}의 작업 기록 없음 — 재시도 거부")
    chain = list(rec.get("attempts") or [])
    if len(chain) >= _MAX_ATTEMPTS:
        raise IntegrationError(
            f"operation {op_id!r}의 재시도 횟수 초과 "
            f"({_MAX_ATTEMPTS}건) — 사람이 확인해야 함")
    try:
        (home / "integration_ops" / (stem + ".json")).rename(
            home / "integration_ops" / archived)
    except OSError as exc:
        raise IntegrationError(
            f"operation {op_id!r}의 기존 검증 기록을 보관할 수 없음: "
            f"{exc}") from exc
    try:
        chain.append({"kind": "fastcheck-retry", "at": utcnow(),
                      "prev_receipt": archived,
                      "prev_ok": bool((existing or {}).get("ok"))})
        rec["attempts"] = chain[-_MAX_ATTEMPTS:]
        rec["updated_at"] = utcnow()
        _write_json(opf, rec)
    except IntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001 - lost op never fails retry
        raise IntegrationError(
            f"operation {op_id!r}의 재시도 기록 실패: {exc}") from exc
    return archived


def stamp_counted_block(op_id: str, lane: str, epoch: int = 0) -> bool:
    """Stamp one lane-block contribution on the op record (L2).

    The driver calls this exactly when a fastcheck red counts a
    lane block. One contribution per op max: an existing stamp
    stays. A later passing re-run clears exactly this via
    clear_counted_block(); the count stays honest, no silent
    erasure. The sched block epoch rides along so release can tell
    a still-current contribution from one consumed by a reset."""
    from .util import read_json as _read_json, write_json as _write_json
    try:
        opf = op_file(op_id)
        rec = _read_json(opf, default=None) or {}
    except Exception:  # noqa: BLE001 - lost op never fails a tick
        return False
    if not isinstance(rec, dict) or rec.get("counted_block"):
        return False
    rec["counted_block"] = {"lane": str(lane), "at": utcnow(),
                            "epoch": int(epoch or 0)}
    rec["updated_at"] = utcnow()
    try:
        _write_json(opf, rec)
    except Exception:  # noqa: BLE001 - lost op never fails a tick
        return False
    return True


def clear_counted_block(op_id: str):
    """Drop the stamp and return it (or None). The caller (m2
    release) owns the sched-state arithmetic."""
    from .util import read_json as _read_json, write_json as _write_json
    try:
        opf = op_file(op_id)
        rec = _read_json(opf, default=None) or {}
    except Exception:  # noqa: BLE001 - unreadable never acts
        return None
    if not isinstance(rec, dict) or not rec.get("counted_block"):
        return None
    stamp = rec.pop("counted_block")
    rec["updated_at"] = utcnow()
    try:
        _write_json(opf, rec)
    except Exception:  # noqa: BLE001 - unreadable never acts
        return None
    return stamp if isinstance(stamp, dict) else {}


def run_fastcheck_for_op(op_id: str, origin: str = "ui", retry: bool = False,
                         _runner=None) -> dict:
    """On-demand fastcheck on a done prepare op (explicit only).

    The UI posts {op, confirm: true} from the "검증 실행" button
    shown on a done op with no receipt. Takes no lease itself (the
    serve layer holds it, like reresolve); the runner takes the op
    lock non-blocking, so contention surfaces as OpBusy → 409 and
    nothing starts. Uses the same fastcheck.run as the driver. The
    receipt records origin "ui" and stays worker-attested, never
    root_attested — only Root QA attests. A red result returns
    ok False without rewriting the op status; the receipt existing
    afterwards keeps auto-resume from re-running it.

    L2: {op, confirm:true, retry:true} re-runs a RED receipt (a
    green one refuses — nothing to fix). The old receipt is
    archived as *.fastcheck.attempt-N.json and listed in attempts;
    a passing re-run clears that op's lane-block contribution via
    m2.release_counted_block (count stays honest). Automatic resume
    still never re-runs red receipts.
    """
    if origin != "ui":
        raise IntegrationError(f"unknown fastcheck origin {origin!r}")
    _opf, rec = _load_op(op_id)
    if rec.get("op") == "reresolve":
        # FIX-b3: a done reresolve derived from the same run's
        # prepare runs the same check (with autonomy off, the
        # owner cannot run the advertised check otherwise).
        # Anything failing the pipeline rule stays refused below.
        if m2._pipeline_prepare_rec(rec) is None:
            raise IntegrationError(
                f"operation {op_id!r}은 준비 작업이 "
                "아님 — 검증 실행 거부")
    elif rec.get("op") != "prepare":
        raise IntegrationError(f"operation {op_id!r}은 준비 작업이 "
                               "아님 — 검증 실행 거부")
    if rec.get("status") != "done":
        raise IntegrationError(f"operation {op_id!r}은 완료 상태가 아님 "
                               f"({rec.get('status') or 'unknown'}) — "
                               "검증 실행 거부")
    from . import fastcheck as _fc
    try:
        existing = read_json(_fc.op_fastcheck_path(op_id), default=None)
    except Exception:  # noqa: BLE001 - unreadable never acts
        existing = None
    archived = ""
    if isinstance(existing, dict):
        if not retry:
            raise IntegrationError(
                f"operation {op_id!r}에 이미 검증 기록 "
                "있음 — 다시 실행하지 않습니다")
        if existing.get("ok"):
            raise IntegrationError(
                f"operation {op_id!r}의 검증은 이미 통과함 — "
                "재실행할 이유가 없습니다")
        archived = _archive_fastcheck_receipt(op_id, existing)
    once = _run_fastcheck_once(op_id, rec, archived, _fc,
                                 origin, _runner)
    summary = once["summary"]
    block_released = once["block_released"]
    return {"op": op_id, "ok": bool(summary.get("ok")),
            "summary": summary, "retried": bool(archived),
            "archived_receipt": archived,
            "block_released": block_released}


def _run_fastcheck_once(op_id: str, rec: dict, archived, _fc,
                        origin: str, _runner=None) -> dict:
    """The shared once-run tail of manual fastcheck and rerun.

    Same path both ways: the op's recorded worktree runs the
    profile steps once (no lease here — the serve layer holds
    it); the op exclusion lock inside _fc.run turns contention
    into OpBusy → 409 and nothing starts. A passing run with an
    archived predecessor clears exactly that op's counted-block
    contribution. Returns {"summary", "block_released", "repo"}.
    Raises IntegrationError on every refused path; never loops.
    """
    run_id = str(rec.get("run_id") or "")
    try:
        state = store.load(run_id)
    except SystemExit:
        raise IntegrationError(f"unknown run {run_id!r}",
                               http_status=404) from None
    repo = state.get("repo") or ""
    try:
        profile = repos.load(repo)
    except SystemExit as exc:
        raise IntegrationError(f"repo profile unreadable: {exc}") from exc
    fc_profile = profile.get("fastcheck") or {}
    if not (fc_profile.get("steps") or []):
        raise IntegrationError("fastcheck_steps 없음")
    result = rec.get("result") or {}
    worktree = str(result.get("path") or "")
    if not worktree or not os.path.isdir(worktree):
        raise IntegrationError(f"operation {op_id!r}의 작업실이 없음 — "
                               "검증 실행 거부")
    head = str(result.get("head") or "")
    fp = str(result.get("fingerprint") or "")
    run = _runner if _runner is not None else _fc.run
    try:
        summary = run(op_id, worktree, fc_profile, head, fp,
                      origin=origin)
    except m2.OpBusy as exc:
        raise IntegrationError(
            f"다른 확인 작업이 {exc.op_id}에서 실행 중입니다",
            409) from exc
    except _fc.FastcheckSkip as exc:
        raise IntegrationError(f"{exc} — 시작하지 않았으니 나중에 "
                               "다시 시도하세요", 409) from exc
    except _fc.FastcheckBlocked as exc:
        raise IntegrationError(str(exc)[:300]) from exc
    block_released = False
    if archived and summary.get("ok"):
        # L2: the passing re-run clears exactly this op's
        # contribution (the stamp the red counted). A still-red
        # re-run keeps the count — never silent erasure.
        try:
            block_released = bool(m2.release_counted_block(
                repo, op_id))
        except Exception:  # noqa: BLE001 - bookkeeping never fails rerun
            block_released = False
    return {"summary": summary, "block_released": block_released,
            "repo": repo}


def _archive_fastcheck_prev(op_id: str, _stamp: str = None) -> tuple:
    """Archive a done op's receipt + step logs under a UTC stamp.

    Renames `<op>.fastcheck.json` to
    `<op>.fastcheck.prev-<stamp>.json` and each
    `<op>.fastcheck.*.log` to
    `<op>.fastcheck.prev-<stamp>.*.log` — kept, never deleted.
    No receipt means nothing to archive ("", []). A name from an
    earlier archive (rest starting with `prev-`) is skipped, so
    a second rerun never re-renames the first archive's logs
    away from their receipt. An already-existing destination
    refuses (POSIX rename would overwrite silently) — nothing
    is renamed and nothing runs. `_stamp` overrides the clock
    (tests only). Never loops.
    """
    from datetime import datetime, timezone
    ops = paths.home() / "integration_ops"
    stamp = _stamp or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")
    receipt = ops / (op_id + ".fastcheck.json")
    if not receipt.is_file():
        return "", []
    archived = f"{op_id}.fastcheck.prev-{stamp}.json"
    if (ops / archived).exists():
        raise IntegrationError(
            f"operation {op_id!r}의 보관 이름 {archived} 이미 있음 — "
            "덮어쓰지 않으니 잠시 뒤 다시 실행하세요")
    try:
        receipt.rename(ops / archived)
    except OSError as exc:
        raise IntegrationError(
            f"operation {op_id!r}의 기존 검증 기록을 보관할 수 없음: "
            f"{exc}") from exc
    logs = []
    try:
        cands = sorted(ops.glob(op_id + ".fastcheck.*.log"))
    except OSError:
        cands = []
    for cand in cands:
        rest = cand.name[len(op_id + ".fastcheck."):]
        if rest.startswith("prev-"):
            continue
        dest = f"{op_id}.fastcheck.prev-{stamp}.{rest}"
        if (ops / dest).exists():
            raise IntegrationError(
                f"operation {op_id!r}의 보관 이름 {dest} 이미 있음 — "
                "덮어쓰지 않으니 잠시 뒤 다시 실행하세요")
        try:
            cand.rename(ops / dest)
        except OSError as exc:
            raise IntegrationError(
                f"operation {op_id!r}의 검증 로그를 보관할 수 없음: "
                f"{exc}") from exc
        logs.append(dest)
    return archived, logs


def rerun_fastcheck_for_op(op_id: str, by: str, _runner=None,
                         _stamp: str = None) -> dict:
    """M2.16c: one explicit human re-verification of a done prepare
    op whose receipt predates a new gate (the plain manual
    fastcheck refuses while any receipt exists — a receipt that
    predates a new gate could otherwise never be re-checked).

    Only owner/root (a worker never re-runs); the M2 driver never
    calls this. Unknown ops and non-done (or non-prepare) ops
    refuse with 409 before anything starts. The existing receipt
    — and its step logs — is archived under a UTC prev-stamp
    name (never deleted, never overwritten; an existing
    destination refuses before anything moves); without a
    receipt there is simply nothing to archive. Then fastcheck
    runs exactly once through the same path as the manual run,
    and "검증 다시 실행 (by …)" is recorded with the result.
    One run per call; never a loop. `_stamp` overrides the
    archive clock (tests only).
    """
    name = str(by or "").strip()
    if name not in ("owner", "root"):
        raise IntegrationError(
            "rerun needs by owner or root (a worker can never "
            "re-run)", http_status=400)
    try:
        _opf, rec = _load_op(op_id)
    except IntegrationError as exc:
        raise IntegrationError(f"unknown operation {op_id!r}; "
                               "nothing started",
                               http_status=409) from exc
    if rec.get("op") == "reresolve":
        # Same pipeline allowance as the manual run: a done
        # reresolve derived from the same run's prepare re-runs
        # the same check.
        if m2._pipeline_prepare_rec(rec) is None:
            raise IntegrationError(
                f"operation {op_id!r}은 준비 작업이 "
                "아님 — 검증 다시 실행 거부")
    elif rec.get("op") != "prepare":
        raise IntegrationError(f"operation {op_id!r}은 준비 작업이 "
                               "아님 — 검증 다시 실행 거부")
    if rec.get("status") != "done":
        raise IntegrationError(f"operation {op_id!r}은 완료 상태가 아님 "
                               f"({rec.get('status') or 'unknown'}) — "
                               "검증 다시 실행 거부")
    from . import fastcheck as _fc
    try:
        has_receipt = _fc.op_fastcheck_path(op_id).is_file()
    except OSError:  # noqa: BLE001 - unreadable means no archive
        has_receipt = False
    archived, logs = ("", [])
    if has_receipt:
        # M2.16b-A2: ANY existing receipt file archives first —
        # a corrupt or non-dict receipt must move aside under a
        # prev name, never be overwritten by the new run.
        archived, logs = _archive_fastcheck_prev(op_id, _stamp)
    once = _run_fastcheck_once(op_id, rec, "", _fc, "ui", _runner)
    summary = once["summary"]
    detail = (f"{op_id}: "
              f"{'통과' if summary.get('ok') else '실패'}"
              + (f" (이전 기록 {archived} 보관)" if archived else ""))
    events.append(once["repo"], "m2-fastcheck-rerun", op_id,
                  f"검증 다시 실행 (by {name})", detail, actor=name)
    return {"op": op_id, "ok": bool(summary.get("ok")),
            "summary": summary, "archived_receipt": archived,
            "archived_logs": logs}


def _qa_lock_desc_versions(text: str) -> dict:
    """{descriptor: version} from one yarn.lock text (read-only)."""
    out: dict = {}
    try:
        for descs, body in _split_yarn_stanzas(text or ""):
            version = None
            for line in (body or "").splitlines():
                m = _VERSION_LINE_RE.match(line)
                if m:
                    version = m.group(1)
                    break
            if version is None:
                continue
            for desc in descs:
                if _yarn_pkg_name(desc) == "__metadata":
                    continue
                out[str(desc)] = str(version)
    except (Exception, SystemExit):  # noqa: BLE001 - no map
        pass
    return out


_DEP_EDGE_RE = re.compile(r"^ {4}([^:\s][^:]*):\s*\"?([^\"\s]+)\"?\s*$")


def _qa_lock_reachable(text: str, roots: list) -> set:
    """Package names reachable from root dep names in one lock.

    BFS over stanza dependency edges (read-only, pure). Unknown
    or unparseable locks give the roots only — never raises.
    """
    reached = set(str(r) for r in roots or [] if r)
    try:
        stanzas = []
        for descs, body in _split_yarn_stanzas(text or ""):
            version = None
            deps = []
            for line in (body or "").splitlines():
                if version is None:
                    m = _VERSION_LINE_RE.match(line)
                    if m:
                        version = m.group(1)
                        continue
                m = _DEP_EDGE_RE.match(line)
                if m:
                    # Edge names may be quoted in the lock
                    # ("@scope/name": "npm:^x"); descriptors never
                    # are — strip so the lookup hits.
                    deps.append((m.group(1).strip().strip('"'),
                                 m.group(2)))
            if version is None:
                continue
            for desc in descs:
                name = _yarn_pkg_name(desc)
                if name and name != "__metadata":
                    stanzas.append(
                        (name, _descriptor_range(desc, name),
                         version, deps))
        by_desc = {}
        for name, rng, version, deps in stanzas:
            by_desc.setdefault((name, rng), []).append(
                (version, deps))
            by_desc.setdefault(
                (name, "npm:" + rng), []).append((version, deps))
        queue = []
        seen_vers: dict = {}
        for name, rng, version, deps in stanzas:
            if name in reached:
                queue.append((name, version, deps))
                seen_vers.setdefault(name, set()).add(version)
        while queue:
            _name, _ver, deps = queue.pop()
            for dname, drange in deps:
                for version, sub in by_desc.get(
                        (dname, drange), []):
                    if version in seen_vers.setdefault(
                            dname, set()):
                        continue
                    seen_vers[dname].add(version)
                    reached.add(dname)
                    queue.append((dname, version, sub))
    except (Exception, SystemExit):  # noqa: BLE001 - roots only
        pass
    return reached


def _qa_git(args: list, cwd: str, timeout: int = 120):
    """Read-only git call (never raises; None on failure)."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks",
             "-c", "core.hooksPath=/dev/null"] + list(args),
            cwd=str(cwd), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


def _qa_base_lock_text(worktree: str, base_head: str) -> str:
    """The base tree's yarn.lock via git show (read-only)."""
    if not worktree or not base_head:
        return ""
    return _qa_git(["show", f"{base_head}:yarn.lock"], worktree) or ""


def _qa_diffcheck(batch: dict, base: dict,
                  base_lock: str = "") -> dict:
    """Changed files + lock entries base..head (read-only)."""
    out: dict = {"status": "unavailable", "note": "",
                 "stat": "", "files": [], "lock_entries": []}
    try:
        integs = [i for i in (batch.get("integrations") or [])
                  if isinstance(i, dict)]
        if not integs:
            out["note"] = "묶음 작업실 기록 없음 — 직접 확인"
            return out
        integ = integs[0]
        worktree = str(integ.get("worktree") or "")
        head = str(integ.get("head") or "")
        bintegs = [i for i in ((base or {}).get("integrations") or [])
                   if isinstance(i, dict)]
        bhead = str((bintegs[0].get("head") or "") if bintegs else "")
        if not worktree or not head or not bhead:
            out["note"] = "기준/결과 기록 없음 — 직접 확인"
            return out
        if not base_lock:
            base_lock = _qa_base_lock_text(worktree, bhead)
        stat = _qa_git(["diff", "--stat", bhead, head], worktree)
        names = _qa_git(["diff", "--name-only", bhead, head],
                        worktree)
        if stat is None or names is None:
            out["note"] = "git diff 읽기 실패 — 직접 확인"
            return out
        files = [l for l in names.splitlines() if l.strip()]
        try:
            head_lock = Path(worktree, "yarn.lock").read_text(
                encoding="utf-8", errors="ignore")
        except OSError:
            head_lock = ""
        old_map = _qa_lock_desc_versions(base_lock)
        new_map = _qa_lock_desc_versions(head_lock)
        primaries = [p for p in (batch.get("primaries") or [])
                     if isinstance(p, dict) and p.get("dep")]
        roots = [str(p.get("dep")) for p in primaries]
        reached = _qa_lock_reachable(head_lock, roots)
        entries = []
        for desc in sorted(set(old_map) | set(new_map)):
            before = old_map.get(desc)
            after = new_map.get(desc)
            if before == after:
                continue
            name = _yarn_pkg_name(desc) or ""
            entries.append(
                {"desc": desc, "name": name,
                 "range": _descriptor_range(desc, name),
                 "before": before, "after": after,
                 "in_dep_tree": bool(name) and name in reached})
            if len(entries) >= 50:
                break
        out.update({"status": "ok", "stat": stat[-4000:],
                    "files": files[:200],
                    "lock_entries": entries,
                    "base_head": bhead, "head": head})
    except (Exception, SystemExit):  # noqa: BLE001 - note, no raise
        out = {"status": "unavailable",
               "note": "증거 수집 실패 — 직접 확인",
               "stat": "", "files": [], "lock_entries": []}
    return out


def qa_dir(batch_id: str) -> Path:
    """Product QA evidence home: ~/.openmainty/qa/<batch>/."""
    _check_id(batch_id)
    return paths.home() / "qa" / batch_id


def _qa_peer_scratch(batch_id: str, stamp: str, which: str) -> Path:
    """Peer scratch dir under the app tmp dir (left in place)."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", batch_id)[:80]
    return paths.home() / "tmp" / f"qa-peer-{safe}-{stamp}-{which}"


def _qa_archive(worktree: str, sha: str, dest: Path):
    """git archive <sha> | tar -x into dest (read-only source)."""
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "scratch 디렉터리 준비 실패 — 직접 확인"
    try:
        arc = subprocess.run(
            ["git", "--no-optional-locks",
             "-c", "core.hooksPath=/dev/null",
             "archive", sha],
            cwd=str(worktree), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git archive 실패 ({exc}) — 직접 확인"
    if arc.returncode != 0 or not arc.stdout:
        return "git archive 실패 — 직접 확인"
    try:
        untar = subprocess.run(
            ["tar", "-x", "-C", str(dest)],
            input=arc.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"압축 해제 실패 ({exc}) — 직접 확인"
    if untar.returncode != 0:
        return "압축 해제 실패 — 직접 확인"
    return ""


def _qa_run_yarn_explain(scratch: Path, timeout: int = 120) -> dict:
    """yarn explain peer-requirements in a scratch copy.

    Uses the repo's own .yarn/releases bundle with a scrubbed
    env (fastcheck's), a timeout, and no install. Never raises:
    failures read as notes.
    """
    out: dict = {"status": "unavailable", "note": "",
                 "incorrectly_met": [], "output": ""}
    try:
        import shutil as _shutil
        cands = sorted(scratch.glob(".yarn/releases/*.cjs"))
        if not cands:
            out["note"] = "번들 yarn 없음 — 직접 확인"
            return out
        node = _shutil.which("node") or ""
        if not node:
            out["note"] = "node 없음 — 직접 확인"
            return out
        from . import fastcheck as _fc
        home = paths.home()
        env = _fc.scrub_env(
            [os.path.dirname(node)], str(home / "tmp" / "qa-env"),
            str(home / "cache"))
        proc = subprocess.run(
            [node, str(cands[0]), "explain", "peer-requirements"],
            cwd=str(scratch), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, text=True,
            env=env)
    except subprocess.TimeoutExpired:
        out["status"] = "timeout"
        out["note"] = "시간 초과 — 직접 확인"
        return out
    except (OSError, subprocess.SubprocessError) as exc:
        out["note"] = f"실행 실패 ({exc}) — 직접 확인"
        return out
    if proc.returncode != 0:
        tail = ((proc.stderr or "").strip().splitlines() or [""])[
            -1].strip()
        out["note"] = f"yarn explain 실패 ({tail[:120]}) — 직접 확인"
        return out
    text = proc.stdout or ""
    bad = sorted({l.strip() for l in text.splitlines()
                  if "✘" in l})
    out.update({"status": "ok", "incorrectly_met": bad[:200],
                "output": text[-20000:]})
    return out


def _qa_peer(batch: dict, base_head: str, head: str,
             worktree: str, stamp: str) -> dict:
    """Peer-explain on git-archive copies of base and head."""
    out: dict = {"status": "unavailable", "note": "",
                 "base": {}, "head": {},
                 "added": [], "removed": []}
    try:
        if not worktree or not base_head or not head:
            out["note"] = "기준/결과 기록 없음 — 직접 확인"
            return out
        results = {}
        for which, sha in (("base", base_head), ("head", head)):
            scratch = _qa_peer_scratch(
                str(batch.get("id") or "batch"), stamp, which)
            err = _qa_archive(worktree, sha, scratch)
            if err:
                results[which] = {"status": "unavailable",
                                  "note": err,
                                  "incorrectly_met": [],
                                  "output": ""}
                continue
            results[which] = _qa_run_yarn_explain(scratch)
        out["base"] = {k: results["base"].get(k)
                       for k in ("status", "note",
                                 "incorrectly_met")}
        out["head"] = {k: results["head"].get(k)
                       for k in ("status", "note",
                                 "incorrectly_met")}
        if results["base"].get("status") == "ok" \
                and results["head"].get("status") == "ok":
            bset = set(results["base"]["incorrectly_met"] or [])
            hset = set(results["head"]["incorrectly_met"] or [])
            out.update({"status": "ok",
                        "added": sorted(hset - bset)[:200],
                        "removed": sorted(bset - hset)[:200]})
        else:
            out["note"] = "한쪽 읽기 실패 — 직접 확인"
    except (Exception, SystemExit):  # noqa: BLE001 - note, no raise
        out = {"status": "unavailable",
               "note": "증거 수집 실패 — 직접 확인",
               "base": {}, "head": {}, "added": [], "removed": []}
    return out


def _qa_tool(name: str) -> str:
    """Absolute tool path, preferring the node sibling (read-only).

    PATH shims (a bun npm, an nvm stub) can fail under the
    scrubbed env; the binary next to node is the real one.
    Returns "" when nothing usable is found.
    """
    try:
        import shutil as _shutil
        node = _shutil.which("node") or ""
        if node:
            sib = str(Path(os.path.dirname(node), name))
            if os.path.isfile(sib) and os.access(sib, os.X_OK):
                return sib
        return _shutil.which(name) or ""
    except (Exception, SystemExit):  # noqa: BLE001 - no tool
        return ""


def _qa_registry_repo(pkg: str, timeout: int = 60) -> str:
    """owner/name from the registry metadata (read-only)."""
    try:
        npm = _qa_tool("npm") or "npm"
        from . import fastcheck as _fc
        home = paths.home()
        # The npm launcher resolves node through PATH (shebang),
        # so its own dir rides along in the scrubbed toolchain.
        env = _fc.scrub_env(
            [os.path.dirname(npm)] if os.path.dirname(npm) else [],
            str(home / "tmp" / "qa-env"),
            str(home / "cache"))
        proc = subprocess.run(
            [npm, "view", str(pkg), "repository.url", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    try:
        url = json.loads(proc.stdout or '""')
    except ValueError:
        return ""
    m = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?\s*$",
                  str(url or ""))
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def _qa_gh(args: list, timeout: int = 60):
    """Read-only gh call (None on any failure)."""
    try:
        import shutil as _shutil
        if not _shutil.which("gh"):
            return None
        proc = subprocess.run(
            ["gh"] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout or ""


_FLAG_RE = re.compile(
    r"\bremov(?:e|ed|es|al|ing)\b"
    r"|\bdeprecat(?:e|ed|es|ion|ing)\b"
    # M2.20d: "non-breaking" is not breaking; a "drop-in" is
    # not a drop.
    r"|(?<!non-)\bbreaking\b"
    r"|\bno longer\b"
    r"|\bdrop(?:s|ped|ping)?(?!-in\b)\b",
    re.IGNORECASE)


def _qa_source(batch: dict, base_lock: str = "") -> dict:
    """Release notes between base-pin and target (read-only)."""
    out: dict = {"status": "unavailable", "note": "",
                 "releases": []}
    try:
        primaries = [p for p in (batch.get("primaries") or [])
                     if isinstance(p, dict) and p.get("dep")
                     and p.get("target")]
        if not primaries:
            out["note"] = "후보 기록 없음 — 직접 확인"
            return out
        base_map = _qa_lock_desc_versions(base_lock)
        releases = []
        ok_any = False
        for p in primaries[:4]:
            dep = str(p.get("dep"))
            target = str(p.get("target"))
            from_vers = sorted(
                {v for d, v in base_map.items()
                 if _yarn_pkg_name(d) == dep})
            repo = _qa_registry_repo(dep)
            if not repo:
                releases.append(
                    {"dep": dep, "target": target, "repo": "",
                     "note": "릴리스 노트 읽기 실패 — 직접 확인",
                     "notes": []})
                continue
            versions_out = _qa_gh(
                ["api", f"repos/{repo}/tags?per_page=100",
                 "--jq", ".[].name"])
            tags = []
            if versions_out:
                for line in versions_out.splitlines():
                    tag = line.strip()
                    ver = tag[1:] if tag.startswith("v") else tag
                    # M2.18b: the window is (base pin, target]
                    # INCLUDING the target — its notes are the
                    # most relevant; the later filter caps at it.
                    if ver:
                        tags.append((tag, ver))
            # Keep tags between the base pin and the target,
            # newest 10.
            try:
                from . import versions as _v
                tags = [t for t in tags
                        if _v.compare(t[1], target) <= 0
                        and (not from_vers or any(
                            _v.compare(t[1], f) > 0
                            for f in from_vers))]
                tags.sort(key=lambda t: [
                    int(x) if x.isdigit() else x
                    for x in re.split(r"(\d+)",
                                      t[1])][:8])
                tags = tags[-10:]
            except (Exception, SystemExit):  # noqa: BLE001
                tags = tags[-10:]
            notes = []
            for tag, _ver in tags:
                body = _qa_gh(["release", "view", tag,
                               "--repo", repo]) or ""
                # M2.18c: one flag per stem — lowercase before
                # the set so "Removed" and "removed" count once.
                flags = sorted(
                    {m.lower() for m in _FLAG_RE.findall(body)})
                notes.append({"tag": tag,
                              "flags": flags,
                              "body_head": body[:2000]})
            releases.append({"dep": dep, "target": target,
                             "repo": repo, "notes": notes,
                             "note": ""})
            ok_any = True
        out["releases"] = releases
        if ok_any:
            out["status"] = "ok"
        else:
            out["note"] = "릴리스 노트 읽기 실패 — 직접 확인"
    except (Exception, SystemExit):  # noqa: BLE001 - note, no raise
        out = {"status": "unavailable",
               "note": "증거 수집 실패 — 직접 확인", "releases": []}
    return out


def _qa_consumer(batch: dict) -> dict:
    """Fastcheck receipt's consumer + steps (copied, read-only)."""
    out: dict = {"status": "unavailable", "note": "",
                 "consumer": {}, "steps": [], "ok": None}
    try:
        op_id = str(batch.get("origin_m2") or "")
        if not op_id:
            out["note"] = "연결된 검증 기록 없음 — 직접 확인"
            return out
        from . import fastcheck as _fc
        summary = read_json(_fc.op_fastcheck_path(op_id),
                            default=None)
        if not isinstance(summary, dict):
            out["note"] = "검증 기록 읽기 실패 — 직접 확인"
            return out
        cons = summary.get("consumer")
        if isinstance(cons, dict):
            out["consumer"] = {
                k: cons.get(k) for k in
                ("status", "script", "name", "rc", "label",
                 "reason", "log_tail") if k in cons}
        steps = []
        for s in (summary.get("steps") or [])[:32]:
            if isinstance(s, dict):
                steps.append({k: s.get(k) for k in
                              ("name", "gate", "rc") if k in s})
        out.update({"status": "ok", "steps": steps,
                    "ok": summary.get("ok")})
    except (Exception, SystemExit):  # noqa: BLE001 - note, no raise
        out = {"status": "unavailable",
               "note": "증거 수집 실패 — 직접 확인",
               "consumer": {}, "steps": [], "ok": None}
    return out


def _qa_note(text: str) -> str:
    """One-line suggestion, capped at 200 chars."""
    return " ".join(str(text or "").split())[:200]


def _qa_suggested_notes(batch: dict, diff: dict, peer: dict,
                        source: dict, consumer: dict) -> dict:
    """One ≤200-char suggestion per root gate, from the facts."""
    notes: dict = {}
    try:
        required = (batch.get("policy") or {}).get(
            "required_gates") or []
        gates = [str(g) for g in required if g]
        for known in ("install", "prepare", "typecheck", "lint",
                      "unit", "loaded-config", "peer-explain",
                      "diffcheck", "source-review", "root-qa"):
            if known not in gates:
                gates.append(known)
        steps = {str(s.get("gate") or ""): s
                 for s in (consumer.get("steps") or [])
                 if isinstance(s, dict)}
        files = diff.get("files") or []
        entries = diff.get("lock_entries") or []
        outside = [e for e in entries
                   if isinstance(e, dict)
                   and not e.get("in_dep_tree")]
        incorrectly = (peer.get("head") or {}).get(
            "incorrectly_met") or []
        added = peer.get("added") or []
        removed = peer.get("removed") or []
        rels = []
        for r in (source.get("releases") or []):
            if isinstance(r, dict):
                rels.extend(r.get("notes") or [])
        flagged = [n for n in rels
                   if isinstance(n, dict) and n.get("flags")]
        facts = {
            "install": ("설치 단계 rc=%s — 영수증 참조"
                        % steps.get("install", {}).get("rc", "?")),
            "prepare": ("준비 단계 rc=%s — 영수증 참조"
                        % steps.get("prepare", {}).get("rc", "?")),
            "typecheck": ("타입 검사 rc=%s — 영수증 참조"
                          % steps.get("typecheck", {}).get(
                              "rc", "?")),
            "lint": ("린트 rc=%s — 영수증 참조"
                     % steps.get("lint", {}).get("rc", "?")),
            "unit": ("단위 테스트 rc=%s — 영수증 참조"
                     % steps.get("unit", {}).get("rc", "?")),
            "loaded-config": (
                "소비 빌드 %s — 영수증 참조"
                % ((consumer.get("consumer") or {}).get("label")
                   or "?")),
            "peer-explain": (
                "incorrectly-met %d건, 추가 %d건/제거 %d건"
                % (len(incorrectly), len(added), len(removed))),
            "diffcheck": (
                "변경 %d건, 잠금 %d건%s"
                % (len(files), len(entries),
                   (" (트리 밖 %d건 주의)" % len(outside))
                   if outside else "")),
            "source-review": (
                "릴리스 %d건, 표시 %d건"
                % (len(rels), len(flagged))),
            "root-qa": "위 증거 확인 후 직접 기록",
        }
        for gate in gates:
            notes[gate] = _qa_note(facts.get(
                gate, "영수증과 위 증거 참조 후 직접 기록"))
    except (Exception, SystemExit):  # noqa: BLE001 - no notes
        pass
    return notes


def build_qa_pack(batch_id: str, by: str = "") -> dict:
    """Build the human QA evidence pack for one batch (M2.18).

    READ-ONLY toward the owner repo and the app's review
    worktrees: git diff/show/archive reads, the repo's own yarn
    bundle on archive copies, registry/gh reads. Writes only its
    own qa/<batch>/pack-<UTC>.json — never overwriting (a taken
    stamp gets a -N suffix). Nothing attests: suggested_notes
    are ≤200-char suggestions, one per root gate. Never raises a
    bare error — BatchError only for unknown batch or unwritable
    home.
    """
    batch = load_batch(batch_id)
    by = str(by or "")
    inherited = [h for h in (batch.get("inherited") or [])
                 if isinstance(h, dict) and h.get("batch")]
    base = None
    if inherited:
        try:
            base = load_batch(str(inherited[0]["batch"]))
        except BatchError:
            base = None
    bintegs = [i for i in ((base or {}).get("integrations") or [])
               if isinstance(i, dict)]
    base_head = str((bintegs[0].get("head") or "") if bintegs
                    else "")
    integs = [i for i in (batch.get("integrations") or [])
              if isinstance(i, dict)]
    head = str((integs[0].get("head") or "") if integs else "")
    worktree = str((integs[0].get("worktree") or "") if integs
                   else "")
    stamp = re.sub(r"\D", "", utcnow())[:14]
    base_lock = _qa_base_lock_text(worktree, base_head)
    diff = _qa_diffcheck(batch, base or {}, base_lock)
    peer = _qa_peer(batch, base_head, head, worktree, stamp)
    source = _qa_source(batch, base_lock)
    consumer = _qa_consumer(batch)
    pack = {
        "batch": batch_id, "at": utcnow(), "by": by,
        "base_head": base_head, "head": head,
        "diffcheck": diff, "peer": peer, "source": source,
        "consumer": consumer,
        "suggested_notes": _qa_suggested_notes(
            batch, diff, peer, source, consumer),
    }
    try:
        qdir = qa_dir(batch_id)
        qdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BatchError(f"qa dir not writable: {exc}") from exc
    idx = 0
    while True:
        suffix = "" if idx == 0 else f"-{idx}"
        path = qdir / f"pack-{stamp}{suffix}.json"
        if not path.exists():
            break
        idx += 1
        if idx > 99:
            raise BatchError("qa pack stamp collision")
    write_json(path, pack)
    pack["path"] = str(path)
    return pack


def newest_qa_pack(batch_id: str):
    """Newest qa/<batch>/pack-*.json, or None (read-only).

    An invalid batch id raises BatchError; a missing pack reads
    as None.
    """
    _check_id(batch_id)
    try:
        qdir = qa_dir(batch_id)
        if not qdir.is_dir():
            return None
        names = sorted(p.name for p in qdir.glob("pack-*.json")
                       if p.is_file())
        if not names:
            return None
        return read_json(qdir / names[-1], default=None)
    except (Exception, SystemExit):  # noqa: BLE001 - no pack
        return None


def attest_gate(batch_id: str, gate_key: str, attestor: str, note: str,
                mode: str, receipt_sha: str = "", log_text: str = "",
                na_reason: str = "", refresh: bool = True) -> dict:
    """Human attestation of one batch gate (M2.4b, product feature).

    The owner uses the dashboard review screen or `openmainty
    attest`; Root uses the same path. attestor is "owner", "root",
    or a free-text name — never empty, never "worker" (a worker can
    never attest). Evidence lands under qa/<batch>/<gate>.json (+
    .log); the batch carries a stored copy so evaluate re-verifies
    it. Modes:
    - receipt: review the worker receipt — receipt_sha must match a
      stored worker file already on the gate, with inputs bound to
      the integration head+fingerprint. The sha alone never passes:
      this call IS the human action.
    - direct: log_text is stored as the evidence (capped).
    - na: the gate does not apply — na_reason is mandatory and is
      recorded and shown, never silent.
    Gate names come from the batch's own policy (lane-agnostic).
    owner_attested vs root_attested stay distinct; readiness
    accepts either human attestor. Raises BatchError (nothing
    written) on any mismatch.
    """
    _check_id(batch_id)
    gate_key = str(gate_key or "")
    if not gate_key or len(gate_key) > 80:
        raise BatchError("gate is required (<=80 chars)")
    name = str(attestor or "").strip()
    if not name or len(name) > 80:
        raise BatchError("attestor is required (a name, <=80 chars)")
    if name == "worker":
        raise BatchError("a worker can never attest a gate")
    one_line = " ".join(str(note or "").split())
    if not one_line:
        raise BatchError("note is required (one line: what was checked)")
    if len(one_line) > 200:
        raise BatchError("note must fit one line (<=200 chars)")
    if mode not in ("receipt", "direct", "na"):
        raise BatchError("mode must be receipt, direct, or na")
    try:
        batch = load_batch(batch_id)
    except BatchError as exc:
        raise BatchError(f"unknown batch: {exc}") from exc
    catalog = (batch.get("policy") or {}).get("gates") or {}
    if gate_key not in catalog:
        raise BatchError(f"gate {gate_key!r} is not in this batch's "
                         "policy")
    repo = str(batch.get("repo") or "")
    is_root = bool(name == "root")
    now = utcnow()
    with sched.locked(repo):
        batch = load_batch(batch_id)
        touched = 0
        for integ in batch.get("integrations") or []:
            if not isinstance(integ, dict):
                continue
            for gate in integ.get("gates") or []:
                if not isinstance(gate, dict):
                    continue
                if str(gate.get("key") or "") != gate_key:
                    continue
                _attest_one_gate(batch, batch_id, integ, gate,
                                 gate_key, name, is_root, one_line,
                                 mode, receipt_sha, log_text, na_reason,
                                 now)
                touched += 1
        if not touched:
            raise BatchError(f"gate {gate_key!r} is not recorded on "
                             "any integration of this batch")
        batch["updated_at"] = now
        write_json(batch_file(batch_id), batch)
        _write_index()
    # M24b-F1: every attestation is a timeline event, so the owner
    # sees who approved what (N/A carries its reason).
    detail = f"{batch_id} · {one_line}"
    if mode == "na":
        detail += (" · 해당 없음: "
                   + " ".join(str(na_reason or "").split()))
    events.append(repo, "attest", gate_key,
                  f"확인 기록: {gate_key} — {name} ({mode})",
                  detail, actor=name)
    # M2.16c: the API path passes refresh=False and queues a
    # background refresh itself (same freshness as hold) — 10
    # serial 15-30 s attest calls must not each block on a sync
    # snapshot. The CLI default keeps the sync refresh.
    if refresh:
        try:
            refresh_all_snapshots("attest")
        except Exception:  # noqa: BLE001 - attest won; snapshots retry
            pass
    return {"batch": batch_id, "gate": gate_key, "attestor": name,
            "mode": mode, "at": now}


def set_review(batch_id: str, run_id: str, verdict: str, by: str,
               findings: list, _snap_fn=None) -> dict:
    """M2.16c: one human verdict on one batch primary.

    Auto-registered batches wait on an independent review
    ("자동 등록 — 독립 검토 대기") and nothing could ever record
    one. Only owner/root (a worker and the M2 driver never set a
    verdict). One call sets exactly one primary.

    Identity first: the primary's source worktree is re-shot and
    compared with its recorded head+fingerprint — moved, dirty
    or unreadable refuses ("후보 소스가 검토 이후 바뀜 — 다시
    검토") and nothing is written, never a rebind. accept and
    accept-with-notes then run the same _accept_proof_gaps check
    the import path uses (stale proof refuses); request-changes
    is proof-independent. findings are one-line strings (<=200
    chars each, at most 20); notes and request-changes need at
    least one.

    Writes under sched.locked(repo): the primary's review
    becomes {verdict, at, by, findings, previous} — the old
    verdict dict is kept under "previous", never dropped. Then
    _write_index and one timeline event ("검토 판정: <verdict> —
    <by>"). No synchronous snapshot refresh here: the request
    path queues refresh_all_soon instead (serve layer). Never
    raises SystemExit; every refusal is a BatchError.
    """
    name = str(by or "").strip()
    if name not in ("owner", "root"):
        raise ReviewValidationError(
            "review needs by owner or root (a worker and the M2 "
            "driver never set a verdict)")
    if verdict not in REVIEW_VERDICTS or verdict == "pending":
        raise ReviewValidationError(f"unknown verdict {verdict!r}")
    lines = []
    for finding in findings or []:
        if not isinstance(finding, str):
            raise ReviewValidationError(
                "findings must be one-line strings")
        one = " ".join(finding.split())
        if not one:
            continue
        if len(one) > 200:
            raise ReviewValidationError(
                "each finding must fit one line (<=200 chars)")
        lines.append(one)
    if len(lines) > 20:
        raise ReviewValidationError("at most 20 findings")
    if verdict in ("accept-with-notes", "request-changes") \
            and not lines:
        raise ReviewValidationError(
            f"{verdict} needs at least one finding")
    try:
        batch = load_batch(batch_id)
    except BatchError:
        raise BatchError(f"unknown batch {batch_id!r}; "
                         "nothing written") from None
    prim = None
    for cand in batch.get("primaries") or []:
        rb = cand.get("run_binding") or {}
        if cand.get("run_id") == run_id \
                or rb.get("run_id") == run_id:
            prim = cand
            break
    if prim is None:
        raise BatchError(f"unknown run {run_id!r} for batch "
                         f"{batch_id!r}; nothing written")
    src = prim.get("source") or {}
    src_wt = str(src.get("worktree") or "")
    snap_fn = _snap_fn or _snapshot
    try:
        snap = snap_fn(src_wt) if src_wt else None
    except Exception:  # noqa: BLE001 - unreadable means moved
        snap = None
    if snap is None or snap.get("head") != src.get("head") \
            or snap.get("fingerprint") != src.get("fingerprint") \
            or not snap.get("clean", False):
        raise BatchError("후보 소스가 검토 이후 바뀜 — 다시 검토 "
                         "(nothing written)")
    repo = batch.get("repo") or ""
    if verdict in ("accept", "accept-with-notes"):
        try:
            state = store.load(run_id)
        except SystemExit:
            raise BatchError(f"bound run {run_id!r} missing; "
                             "nothing written") from None
        gaps = _accept_proof_gaps(store.verification_status(
            state, snap.get("fingerprint"), snap.get("head")))
        if gaps:
            raise BatchError("증명 부족: "
                             + "; ".join(gaps)[:300])
    old = prim.get("review")
    if not isinstance(old, dict):
        old = {}
    now = utcnow()
    new = {"verdict": verdict, "at": now, "by": name,
           "findings": lines, "previous": old}
    with sched.locked(repo):
        live = load_batch(batch_id)
        for cand in live.get("primaries") or []:
            rb = cand.get("run_binding") or {}
            if cand.get("run_id") == run_id \
                    or rb.get("run_id") == run_id:
                cand["review"] = new
                break
        else:
            raise BatchError(f"unknown run {run_id!r} for batch "
                             f"{batch_id!r}; nothing written")
        live["updated_at"] = now
        write_json(batch_file(batch_id), live)
        _write_index()
    detail = f"{batch_id} · {run_id} · {verdict}"
    if lines:
        detail += f" · 소견 {len(lines)}건"
    events.append(repo, "review", batch_id,
                  f"검토 판정: {verdict} — {name}", detail,
                  actor=name)
    return {"batch": batch_id, "run": run_id, "verdict": verdict,
            "by": name, "findings": lines, "at": now,
            "previous": old}


def hold_batch(batch_id: str, hold: bool, attestor: str,
               reason: str = "", refresh: bool = True) -> dict:
    """Owner/root hold ("보류") on a batch (M2.5b, product feature).

    A held batch is never READY — evaluate reports "보류:
    <reason> — <who>" — until released. The gates themselves are
    untouched, so releasing restores them byte-for-byte. attestor
    is "owner" or "root", never empty, never "worker" (same
    identity rule as attest: agents must never hold). Holding
    needs a reason; releasing needs none. Recorded on the
    timeline; snapshots refresh (refresh=False lets the API queue
    a background refresh instead — M2.5c hold latency). Raises
    BatchError (nothing written) on any mismatch.
    """
    _check_id(batch_id)
    name = str(attestor or "").strip()
    if name not in ("owner", "root"):
        raise BatchError("hold needs attestor owner or root "
                         "(a worker can never hold)")
    hold = bool(hold)
    one_line = " ".join(str(reason or "").split())
    if hold and not one_line:
        raise BatchError("hold needs a reason (one line: why it waits)")
    if len(one_line) > 200:
        raise BatchError("reason must fit one line (<=200 chars)")
    try:
        batch = load_batch(batch_id)
    except BatchError as exc:
        raise BatchError(f"unknown batch: {exc}") from exc
    repo = str(batch.get("repo") or "")
    now = utcnow()
    with sched.locked(repo):
        batch = load_batch(batch_id)
        if hold:
            batch["hold"] = {"by": name, "reason": one_line,
                             "at": now}
        else:
            batch.pop("hold", None)
        batch["updated_at"] = now
        write_json(batch_file(batch_id), batch)
        _write_index()
    if hold:
        events.append(repo, "hold", batch_id, f"보류 설정 — {name}",
                      f"{batch_id} · {one_line}", actor=name)
    else:
        events.append(repo, "hold", batch_id, f"보류 해제 — {name}",
                      batch_id, actor=name)
    if refresh:
        try:
            refresh_all_snapshots("hold")
        except Exception:  # noqa: BLE001 - hold won; snapshots retry
            pass
    return {"batch": batch_id, "hold": hold, "by": name,
            "reason": one_line, "at": now}


def _attest_one_gate(batch: dict, batch_id: str, integ: dict,
                     gate: dict, gate_key: str, name: str,
                     is_root: bool, note: str, mode: str,
                     receipt_sha: str, log_text: str, na_reason: str,
                     now: str) -> None:
    """Apply one attestation to a gate dict (mutates)."""
    if mode == "na":
        reason = " ".join(str(na_reason or "").split())
        if not reason:
            raise BatchError(f"gate {gate_key!r}: 해당 없음에는 사유가 "
                             "필수입니다")
        if len(reason) > 200:
            raise BatchError(f"gate {gate_key!r}: 사유는 200자 이내")
        gate["na"] = {"reason": reason, "by": name, "at": now}
        gate["note"] = f"해당 없음({name}): {reason}"[:2000]
        return
    head = str(integ.get("head") or "")
    fp = str(integ.get("fingerprint") or "")
    if not (head and fp):
        raise BatchError(f"gate {gate_key!r}: integration source "
                         "unreadable — attest 거부")
    if mode == "receipt":
        if not receipt_sha:
            raise BatchError(f"gate {gate_key!r}: receipt_sha is "
                             "required in receipt mode")
        # The worker receipt lives on the worker companion gate
        # (e.g. worker:unit), not on the pending root gate being
        # reviewed — search the whole batch's stored file evidence.
        match = None
        owner_inputs = None
        worker_mark = False
        for other_integ in batch.get("integrations") or []:
            if not isinstance(other_integ, dict):
                continue
            for other in other_integ.get("gates") or []:
                if not isinstance(other, dict):
                    continue
                for ev in other.get("evidence") or []:
                    if (isinstance(ev, dict) and ev.get("kind")
                            == "file"
                            and str(ev.get("sha256") or "")
                            == receipt_sha):
                        match = ev
                        owner_inputs = other.get("inputs") or {}
                        worker_mark = (
                            str(other.get("key") or "")
                            .startswith("worker:")
                            or other.get("attestor") == "worker")
                        break
                if match is not None:
                    break
            if match is not None:
                break
        if match is None:
            raise BatchError(
                f"gate {gate_key!r}: receipt {receipt_sha[:12]}… is "
                "not stored evidence in this batch")
        if not worker_mark:
            raise BatchError(
                f"gate {gate_key!r}: receipt {receipt_sha[:12]}… is "
                "not a worker artifact — receipt review needs the "
                "worker's own record")
        problem = _evidence_verified(batch_id, match)
        if problem:
            raise BatchError(f"gate {gate_key!r}: stored receipt "
                             f"re-verify failed ({problem})")
        if ((str((owner_inputs or {}).get("head") or ""),
             str((owner_inputs or {}).get("fingerprint") or ""))
                != (head, fp)):
            raise BatchError(
                f"gate {gate_key!r}: receipt inputs differ from the "
                "integration head+fingerprint")
        try:
            reviewed_size = (evidence_dir(batch_id)
                             / Path(str(match.get("stored_path") or "")
                                    ).name).stat().st_size
        except OSError:
            reviewed_size = 0
        gate.setdefault("evidence", []).append(
            {"label": "검토한 영수증", "kind": "file",
             "stored": True,
             "stored_path": str(match.get("stored_path") or ""),
             "sha256": receipt_sha, "bytes": reviewed_size})
        gate["evidence"].append(
            {"label": "확인 기록", "kind": "note",
             "text": f"{name}: {note}"[:2000]})
    else:  # direct
        if not str(log_text or "").strip():
            raise BatchError(f"gate {gate_key!r}: log_text is required "
                             "in direct mode")
        raw = str(log_text).encode("utf-8")[:MAX_EVIDENCE_BYTES]
        digest = hashlib.sha256(raw).hexdigest()
        qdir = qa_dir(batch_id)
        qdir.mkdir(parents=True, exist_ok=True)
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", gate_key)[:60]
        (qdir / (safe_key + ".log")).write_bytes(raw)
        (qdir / (safe_key + ".json")).write_text(json.dumps(
            {"batch": batch_id, "gate": gate_key, "sha256": digest,
             "bytes": len(raw), "attestor": name, "at": now,
             "inputs": {"head": head, "fingerprint": fp},
             "note": note}, ensure_ascii=False), encoding="utf-8")
        dest = evidence_dir(batch_id)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / (digest + "-" + _safe_basename(safe_key
                                                       + ".log"))
        if not target.exists():
            target.write_bytes(raw)
            if sha256_file(target) != digest:
                target.unlink()
                raise BatchError(
                    f"gate {gate_key!r}: evidence changed during copy")
        gate.setdefault("evidence", []).append(
            {"label": "직접 확인 기록", "kind": "file",
             "origin": str(qdir / (safe_key + ".log")),
             "sha256": digest, "bytes": len(raw), "stored": True,
             "stored_path": "evidence/" + target.name})
    gate["status"] = "pass"
    gate["attestor"] = name
    gate["owner_attested"] = not is_root
    gate["root_attested"] = is_root
    gate["inputs"] = {"head": head, "fingerprint": fp}
    gate["note"] = f"{name} 확인: {note}"[:2000]
    gate.pop("na", None)


# ---- Lane rollup ------------------------------------------------------------
# A rollup merges the READY leaf batches of one lane — siblings that do
# not include each other — onto their READY common ancestor, so one
# branch carries the lane's verified work for a single owner PR.
# Explicit UI POST only ({repo, lane, base_batch, leaves, confirm:true})
# with the worker lease held throughout. OpenMainty NEVER pushes, opens
# PRs, or comments: the PR draft and owner commands are recorded and
# shown copyable-only.

_ROLLUP_MAX_LEAVES = 16
_ROLLUP_MAX_COMMITS = 32


def _rollup_op_id(lane: str, base_batch: str, leaf_ids: list) -> str:
    digest = hashlib.sha1(
        ",".join(sorted(leaf_ids)).encode("utf-8")).hexdigest()[:12]
    return f"rollup-{lane}-{_slug(base_batch)}-{digest}"


def _rollup_lane_integration(batch: dict, lane: str) -> dict:
    found = [i for i in (batch.get("integrations") or [])
             if isinstance(i, dict) and (i.get("lane") or "") == lane]
    if len(found) != 1:
        raise IntegrationError(
            f"batch '{batch.get('id')}' records {len(found)} '{lane}' "
            "integrations; rollup needs exactly one per batch")
    return found[0]


def _rollup_ancestor_ids(batch: dict) -> set:
    """All batch ids inherited (transitively) by batch. Cycle-safe."""
    out, seen, frontier = set(), set(), [
        h.get("batch") for h in (batch.get("inherited") or [])
        if isinstance(h, dict) and h.get("batch")]
    while frontier:
        bid = frontier.pop()
        if not bid or bid in seen:
            continue
        seen.add(bid)
        out.add(bid)
        try:
            parent = load_batch(bid)
        except BatchError:
            continue
        for h in (parent.get("inherited") or []):
            if isinstance(h, dict) and h.get("batch"):
                frontier.append(h.get("batch"))
    return out


def _rollup_ready(batch: dict, live: dict, diag) -> dict:
    """Refuse unless the batch evaluates fully READY (pure read).

    Returns the evaluation so the completion path can reuse it for
    display-only leaf caveats without evaluating twice.
    """
    ev = evaluate(batch, live, live_diag=diag, record=False)
    results = ev.get("results") or []
    bad = [r for r in results
           if not (isinstance(r, dict) and r.get("ready"))]
    if not results or bad:
        reasons = []
        for r in results:
            # evaluate() stores reasons at the top level of each
            # result (r["reasons"]); r["diag"] holds per-key live-read
            # diagnostics, never the verdict reasons.
            reasons.extend(r.get("reasons") or [])
        reason_txt = "; ".join(str(x)[:120] for x in reasons[:3])
        if not reason_txt:
            reason_txt = "no ready primaries"
        raise IntegrationError(
            f"batch '{batch.get('id')}' is not READY "
            f"({reason_txt}); rollup takes READY batches only")
    return ev


def _rollup_leaf_commits(leaf_worktree: str, parent_head: str,
                         leaf_head: str, batch_id: str) -> list:
    """A leaf's own integration commits: parent batch head..leaf head."""
    rev = _run_git(["rev-list", "--reverse",
                    f"{parent_head}..{leaf_head}"], leaf_worktree)
    if rev.returncode != 0:
        raise IntegrationError(
            f"leaf '{batch_id}' history unreadable from its worktree "
            f"({(rev.stderr or rev.stdout or '').strip()[-200:]}); refusing")
    commits = [r.strip() for r in (rev.stdout or "").splitlines()
               if r.strip()]
    if not commits:
        raise IntegrationError(
            f"leaf '{batch_id}' records no own commits above its "
            "parent batch head; nothing to roll up")
    if commits[-1] != leaf_head:
        raise IntegrationError(
            f"leaf '{batch_id}' history moved during rollup (recorded "
            f"{leaf_head[:12]}, range tip {commits[-1][:12]}); refusing")
    merged = _run_git(["rev-list", "--merges",
                       f"{parent_head}..{leaf_head}"], leaf_worktree)
    merge_list = [r.strip() for r in (merged.stdout.splitlines()
                                      if merged.returncode == 0 else [])
                  if r.strip()]
    if merged.returncode == 0 and merge_list:
        raise IntegrationError(
            f"leaf '{batch_id}' range contains merge commit "
            f"{merge_list[0][:12]}; merge replay refused before any "
            "mutation")
    out = []
    for sha in commits:
        shown = _run_git(["show", "-s", "--format=%s", sha],
                         leaf_worktree)
        subject = (shown.stdout.strip().splitlines() or [""])[0][:160]
        out.append({"head": sha, "subject": subject or sha[:12]})
    return out


def _rollup_ensure_present(repo_wt: str, sha: str, origin_wt: str,
                           what: str) -> None:
    """Make a leaf commit object available in the rollup repo.

    Tonight every leaf shares its base batch's repo, so this is
    usually a no-op; exotic layouts fetch the single object from the
    leaf's own worktree repo instead of failing later mid-replay.
    """
    probe = _run_git(["cat-file", "-e", sha], repo_wt)
    if probe.returncode == 0:
        return
    fetched = _run_git(["fetch", origin_wt, sha], repo_wt)
    probe = _run_git(["cat-file", "-e", sha], repo_wt)
    if fetched.returncode != 0 or probe.returncode != 0:
        raise IntegrationError(
            f"{what} {sha[:12]} unavailable in the rollup repo "
            f"({(fetched.stderr or fetched.stdout or '').strip()[-200:]}); "
            "refusing before any mutation")


_PR_LANG_DEFAULT = "en"


def _pr_language(repo: str) -> tuple:
    """Effective PR-draft language for a repo: its profile's
    `pr_language` ("en"|"ko"), else the default "en".

    Returns (lang, source) with source "repo profile", "default",
    or "default (invalid ... in profile)". The draft header prints
    both so the owner sees where the language came from. Never
    raises: an unreadable profile means the default.
    """
    try:
        prof = read_json(paths.repos_dir() / f"{repo}.json",
                         default=None)
    except Exception:  # noqa: BLE001 - unreadable profile → default
        prof = None
    if isinstance(prof, dict) and "pr_language" in prof:
        val = prof["pr_language"]
        if val in ("en", "ko"):
            return val, "repo profile"
        return _PR_LANG_DEFAULT, \
            "default (invalid %r in profile)" % (val,)
    return _PR_LANG_DEFAULT, "default"


_FIXFEAT_RE = re.compile(r"^(fix|feat)(\([^)]*\))?(!)?:")


def _pr1_seg_labels(unreleased: list, groups: dict = None) -> list:
    """Short truthful per-batch segments for the English PR1 title.

    One segment per unreleased chain batch (oldest first), derived
    only from its primaries: a shared target becomes "<common dep
    prefix> <target>"; a single primary becomes "<dep> <target>";
    mixed primaries become "<first dep> +N". When the group's own
    commits (`groups`, mid -> [(sha, subject)]) carry conventional
    fix/feat commits, the segment says so (" (with fixes)"). Never
    invents names.
    """
    segs = []
    for mid in unreleased:
        try:
            grp = load_batch(mid)
        except BatchError:
            grp = None
        pairs = []
        if isinstance(grp, dict):
            for p in grp.get("primaries") or []:
                if isinstance(p, dict) and p.get("dep"):
                    pairs.append((str(p["dep"]),
                                  str(p.get("target") or "")))
        kinds = set()
        for _, subject in ((groups or {}).get(mid) or []):
            m = _FIXFEAT_RE.match(str(subject or ""))
            if m:
                kinds.add(m.group(1))
        if kinds == {"fix"}:
            suffix = " (with fixes)"
        elif kinds == {"feat"}:
            suffix = " (with features)"
        elif kinds:
            suffix = " (with fixes + features)"
        else:
            suffix = ""
        if not pairs:
            segs.append(str(mid) + suffix)
            continue
        targets = {t for _, t in pairs if t}
        if len(targets) == 1 and len(pairs) > 1:
            ver = next(iter(targets))
            pref = os.path.commonprefix([d for d, _ in pairs])
            pref = pref.strip("@").strip("/-") or pairs[0][0]
            segs.append(f"{pref} {ver}".strip() + suffix)
        elif len(pairs) == 1:
            segs.append(
                f"{pairs[0][0]} {pairs[0][1]}".strip() + suffix)
        else:
            segs.append(f"{pairs[0][0]} +{len(pairs) - 1}" + suffix)
    return segs


def _pr1_title_en(segs: list, commits: int) -> str:
    """Short English PR1 title from the segments. Falls back to the
    commit count only when there is nothing to name."""
    if not segs:
        return f"chore: ship base chain to main ({commits} commits)"
    joined = segs[0] if len(segs) == 1 else \
        ", ".join(segs[:-1]) + " and " + segs[-1]
    title = f"chore: ship {joined} to main"
    if len(title) > 120:
        title = title[:119].rsplit(" ", 1)[0] + "…"
    return title


# Config-only lockstep members carry no runtime code, so they may
# ride any react-native minor — with a WARNING line in the draft
# body instead of a refusal.
_LOCKSTEP_CONFIG_ONLY = ("@react-native/typescript-config",
                         "@react-native/eslint-config")


def _rollup_lane_rn(tree: str):
    """Declared react-native (range, minor) in a result tree.

    Root package.json first, then example. ("", None) when neither
    declares it — the preflight then stays silent, with nothing to
    compare against. Read-only."""
    for rel in ("", "example"):
        try:
            pkg = read_json(Path(tree, rel, "package.json"),
                            default=None)
        except (Exception, SystemExit):  # noqa: BLE001 - unreadable
            continue
        if not isinstance(pkg, dict):
            continue
        for key in ("dependencies", "devDependencies",
                    "optionalDependencies"):
            sect = pkg.get(key)
            if isinstance(sect, dict) and "react-native" in sect:
                rng = str(sect["react-native"])
                return rng, m2._version_minor(rng)
    return "", None


def _rollup_lockstep_check(rn_range: str, rn_minor, leaves: list,
                           lang: str = "en") -> list:
    """M2.5d rollup preflight over one lane result's react-native.

    A leaf bumping an RN_LOCKSTEP_FAMILY member to another minor
    than the lane result's react-native must not ship in that
    draft: refuse with the plain sentence. Config-only members
    (typescript-config, eslint-config) are allowed with a WARNING
    line instead. Returns the warning lines (possibly empty).
    Raises IntegrationError on refusal. Pure read over `leaves`."""
    if rn_minor is None:
        return []
    warnings = []
    for leaf in leaves or []:
        if not isinstance(leaf, dict):
            continue
        dep = str(leaf.get("dep") or "")
        target = str(leaf.get("target") or "")
        if dep not in m2.RN_LOCKSTEP_FAMILY:
            continue
        if m2._version_minor(target) == rn_minor:
            continue
        if dep in _LOCKSTEP_CONFIG_ONLY:
            if lang == "ko":
                warnings.append(
                    f"주의: {dep} {target}은 설정 전용이라 "
                    f"react-native {rn_range}와 달라도 포함함")
            else:
                warnings.append(
                    f"Note: {dep} {target} is config-only, kept "
                    f"although react-native is {rn_range}.")
            continue
        want = m2._version_minor(target)
        want_txt = (f"{want[0]}.{want[1]}" if want else target)
        if lang == "ko":
            raise IntegrationError(
                f"{dep} {target}은 react-native {want_txt}와 함께 "
                f"출시해야 함 (현재 {rn_range}) — 빼거나 RN 올림과 함께")
        raise IntegrationError(
            f"{dep} {target} must ship with react-native "
            f"{want_txt} (lane has {rn_range}) — drop it or ride "
            "the RN bump")
    return warnings


def _rollup_workspace_maps(tree: str) -> dict:
    """FIX-b1: per-workspace react-native + declared deps.

    Returns {display: {"rn_range", "rn_minor", "deps"}} for the
    root ("root") and example ("example") manifests that exist.
    Read-only; unreadable manifests are skipped, never guessed.
    """
    spaces = {}
    for rel, disp in (("", "root"), ("example", "example")):
        try:
            pkg = read_json(Path(tree, rel, "package.json"),
                            default=None)
        except (Exception, SystemExit):  # noqa: BLE001 - skip
            continue
        if not isinstance(pkg, dict):
            continue
        deps = {}
        for key in ("dependencies", "devDependencies",
                    "optionalDependencies"):
            sect = pkg.get(key)
            if isinstance(sect, dict):
                for name, rng in sect.items():
                    deps[str(name)] = rng
        rn_range = str(deps.get("react-native") or "")
        spaces[disp] = {"rn_range": rn_range,
                        "rn_minor": m2._version_minor(rn_range)
                        if rn_range else None,
                        "deps": deps}
    return spaces


def _rn_minor_txt(rn_range: str, rn_minor) -> str:
    if rn_minor:
        return f"{rn_minor[0]}.{rn_minor[1]}.x"
    return rn_range or "?"


def _rollup_lockstep_check_ws(workspaces: dict, leaves: list,
                              lang: str = "en") -> list:
    """FIX-b1: lockstep preflight per workspace.

    A leaf bumping an RN_LOCKSTEP_FAMILY member is validated
    against the react-native of every workspace that declares
    that dep — a workspace without react-native is never judged
    by another workspace's version. The refusal names the
    workspace. Config-only members warn once per leaf, as before.
    `workspaces` is _rollup_workspace_maps output. Pure.
    """
    warnings = []
    for leaf in leaves or []:
        if not isinstance(leaf, dict):
            continue
        dep = str(leaf.get("dep") or "")
        target = str(leaf.get("target") or "")
        if dep not in m2.RN_LOCKSTEP_FAMILY:
            continue
        if dep in _LOCKSTEP_CONFIG_ONLY:
            # Warn once per workspace holding react-native (a
            # single workspace reads exactly like before).
            for _space in (workspaces or {}).values():
                _rn = str((_space or {}).get("rn_range") or "")
                if not _rn:
                    continue
                if lang == "ko":
                    warnings.append(
                        f"주의: {dep} {target}은 설정 전용이라 "
                        f"react-native {_rn}와 달라도 포함함")
                else:
                    warnings.append(
                        f"Note: {dep} {target} is config-only, kept "
                        f"although react-native is {_rn}.")
            continue
        want = m2._version_minor(target)
        want_txt = (f"{want[0]}.{want[1]}" if want else target)
        for disp, space in (workspaces or {}).items():
            if dep not in (space.get("deps") or {}):
                continue
            rn_minor = space.get("rn_minor")
            if rn_minor is None:
                # No react-native here — not judged by the root.
                continue
            if want == rn_minor:
                continue
            rn_txt = _rn_minor_txt(space.get("rn_range") or "",
                                   rn_minor)
            if lang == "ko":
                raise IntegrationError(
                    f"{disp}: {dep} {target}은 react-native "
                    f"{want_txt}와 함께 출시해야 함 (해당 작업실 "
                    f"{rn_txt}) — 빼거나 RN 올림과 함께")
            raise IntegrationError(
                f"{disp}: react-native {rn_txt} vs {dep} "
                f"{target} — drop it or ride the RN bump")
    return warnings


def _rollup_lockstep_preflight(tree: str, leaves: list,
                               lang: str = "en") -> list:
    """Draft-time preflight reading the result tree's react-native.

    Called by the single draft builder (completion + redraft), so a
    lane draft can never silently carry a lockstep violation. An
    RN-bump leaf moves the result, so the check runs against the
    merged tree, not the base. FIX-b1: validated per workspace."""
    return _rollup_lockstep_check_ws(_rollup_workspace_maps(tree),
                                     leaves, lang)


def _rollup_stack(new_path: str, lane: str, base_id: str,
                  base_head: str, head: str,
                  lang: str = "en") -> dict:
    """Every commit in <lane>..<head>, grouped by the batch it came from.

    The base chain (oldest first) then the rollup itself. Returns
    {"target_head", "groups": [(batch_id, [(sha, subject)], summary)],
    "diffstat", "rollup_diffstat", "unreleased": [batch ids]}.
    Anything unattributable is listed under its own group — never
    dropped. All git reads. `lang` renders the display summaries;
    the group ids stay internal ("rollup", "기준 묶음").
    """
    out = {"target_head": "", "groups": [], "diffstat": "",
           "rollup_diffstat": "", "unreleased": []}
    if not new_path or not base_head or not head:
        return out
    probe = _run_git(["rev-parse", lane], new_path)
    if probe.returncode != 0:
        return out
    target_head = (probe.stdout or "").strip()
    out["target_head"] = target_head
    try:
        base = load_batch(base_id)
    except BatchError:
        base = None
    chain = []
    if isinstance(base, dict):
        for bid in sorted(_rollup_ancestor_ids(base)):
            try:
                anc = load_batch(bid)
            except BatchError:
                continue
            if isinstance(anc, dict):
                chain.append(anc)
        chain.sort(key=lambda b: str(b.get("updated_at") or ""))
        chain.append(base)
    listed = set()

    def _commits(rev_range: str) -> list:
        proc = _run_git(
            ["rev-list", "--reverse", "--format=%H%x01%s", rev_range],
            new_path)
        if proc.returncode != 0:
            return []
        rows = []
        for line in (proc.stdout or "").splitlines():
            if line.startswith("commit ") or not line.strip():
                continue
            sha, _, subject = line.partition("\x01")
            if sha and sha not in listed:
                listed.add(sha)
                rows.append((sha, subject.strip()))
        return rows

    def _is_anc(a: str, b: str) -> bool:
        if not a or not b:
            return False
        return _run_git(["merge-base", "--is-ancestor", a, b],
                        new_path).returncode == 0
    prev = target_head
    for member in chain:
        integ = (member.get("integrations") or [{}])[0]
        h = str(integ.get("head") or "")
        lineal = bool(h) and _is_anc(prev, h) and _is_anc(h, base_head)
        if lineal:
            rows = _commits(f"{prev}..{h}")
            prev = h
        else:
            rows = []
        mid = str(member.get("id") or "")
        if rows:
            summary = f"{mid}: {len(rows)} commits"
        elif lineal:
            summary = f"{mid}: 0 commits (" + (
                "새 커밋 없음" if lang == "ko"
                else "no new commits") + ")"
        else:
            summary = (f"{mid}: 이력 범위 밖 — 아래 기준 합계에 "
                       "포함" if lang == "ko" else
                       f"{mid}: outside history range — counted "
                       "in the base total below")
        out["groups"].append((mid, rows, summary))
    rest = _commits(f"{target_head}..{base_head}")
    if rest:
        out["groups"].append(
            ("기준 묶음", rest,
             f"기준 묶음: {len(rest)} commits (개별 묶음 범위 밖)"
             if lang == "ko" else
             f"base batches: {len(rest)} commits (outside "
             "individual batch ranges)"))
    out["groups"].append(
        ("rollup", _commits(f"{base_head}..{head}"),
         "합치기" if lang == "ko" else "rollup"))
    diff = _run_git(["diff", "--shortstat", target_head, head],
                    new_path)
    if diff.returncode == 0:
        out["diffstat"] = (diff.stdout or "").strip()
    # S4.5-R1: the rollup commit's OWN diffstat (base_head..head).
    # PR2 ships only this range; the whole-stack stat vs the lane
    # target would overstate it (it rides the unreleased base chain).
    out["rollup_diffstat"] = ""
    roll = _run_git(["diff", "--shortstat", base_head, head],
                    new_path)
    if roll.returncode == 0:
        out["rollup_diffstat"] = (roll.stdout or "").strip()
    # Unreleased = a base-chain batch with commits the target tip does
    # not contain (attributed above). No date comparison: a batch
    # updated yesterday is still unreleased when the target is older.
    for mid, rows, _summary in out["groups"]:
        if rows and mid not in ("rollup", "기준 묶음"):
            out["unreleased"].append(mid)
    return out


def _rollup_mismatch_notices(leaves: list) -> list:
    """M2.16d: per-leaf compiler/runtime co-location warnings for
    the redraft's 확인 필요 section.

    Reads each leaf batch's integration trees (read-only) through
    the same-manifest check; a hit becomes "dep (batch): label".
    Unreadable batches or trees yield nothing — warnings are
    bonus, never a block, never a raise.
    """
    notes = []
    try:
        for leaf in leaves or []:
            if not isinstance(leaf, dict):
                continue
            dep = str(leaf.get("dep") or "")
            bid = str(leaf.get("batch") or "")
            if not dep or not bid:
                continue
            try:
                batch = load_batch(bid)
            except BatchError:
                continue
            seen = set()
            for integ in batch.get("integrations") or []:
                wt = str(integ.get("worktree") or "")
                if not wt or wt in seen:
                    continue
                seen.add(wt)
                try:
                    label = m2.compiler_major_mismatch_same_manifest(
                        wt, dep)
                except Exception:  # noqa: BLE001 - warnings bonus
                    continue
                if label:
                    note = f"{dep} ({bid}): {label}"
                    if note not in notes:
                        notes.append(note)
    except Exception:  # noqa: BLE001 - warnings bonus
        pass
    return notes


def _rollup_leaf_state_from_eval(repo: str, batch: dict, ev: dict,
                                   prim_idx: list,
                                   ready_of: dict) -> dict:
    """M2.24: normalized leaf display state from an already-computed
    evaluation. Never evaluates — callers pass the precomputed
    evaluation the glance already uses. Never raises."""
    state = {"compiler_notes": [], "newer_ready": [], "hold": None,
             "ready": 0, "total": 0, "primaries": 0, "pod_label": None}
    try:
        if not isinstance(batch, dict):
            return state
        if not isinstance(ev, dict):
            ev = {"results": []}
        results = [r for r in (ev.get("results") or [])
                   if isinstance(r, dict)]
        state["ready"] = sum(1 for r in results if r.get("ready"))
        state["total"] = len(results)
        state["primaries"] = sum(
            1 for p in (batch.get("primaries") or [])
            if isinstance(p, dict))
        for _r in results:
            for _a in _r.get("attention") or []:
                if _a and _a not in state["compiler_notes"]:
                    state["compiler_notes"].append(_a)
        try:
            hints = _newer_ready_hints(
                str(batch.get("id") or ""), str(repo or ""),
                batch.get("primaries") or [],
                batch.get("inherited") or [],
                prim_idx, ready_of)
        except Exception:  # noqa: BLE001 - hints are bonus
            hints = []
        state["newer_ready"] = [h for h in (hints or [])
                                if isinstance(h, dict)]
        hold = batch.get("hold")
        state["hold"] = hold if isinstance(hold, dict) and hold \
            else None
        try:
            drift = _batch_pod_drift(batch)
        except Exception:  # noqa: BLE001 - label is bonus
            drift = None
        if isinstance(drift, dict) and drift.get("label"):
            state["pod_label"] = str(drift.get("label"))
    except Exception:  # noqa: BLE001 - states never raise
        pass
    return state


def _rollup_leaf_state_from_card(card: dict) -> dict:
    """M2.24: normalized leaf display state from a batches-snapshot
    card — the same precomputed evaluation the glance uses. Never
    evaluates, never reads a worktree. Never raises."""
    state = {"compiler_notes": [], "newer_ready": [], "hold": None,
             "ready": 0, "total": 0, "primaries": 0, "pod_label": None}
    try:
        if not isinstance(card, dict):
            return state
        state["compiler_notes"] = [
            n for n in (card.get("compiler_notes") or [])
            if isinstance(n, str) and n]
        state["newer_ready"] = [
            h for h in (card.get("newer_ready") or [])
            if isinstance(h, dict)]
        hold = card.get("hold")
        state["hold"] = hold if isinstance(hold, dict) and hold \
            else None
        try:
            state["ready"] = int(card.get("ready") or 0)
        except (TypeError, ValueError):  # noqa: BLE001 - bonus
            state["ready"] = 0
        diag = card.get("evaluation")
        if isinstance(diag, dict):
            try:
                state["total"] = int(diag.get("total") or 0)
            except (TypeError, ValueError):  # noqa: BLE001 - bonus
                state["total"] = 0
        try:
            state["primaries"] = int(card.get("primaries") or 0)
        except (TypeError, ValueError):  # noqa: BLE001 - bonus
            state["primaries"] = 0
        drift = card.get("pod_drift")
        if isinstance(drift, dict) and drift.get("label"):
            state["pod_label"] = str(drift.get("label"))
    except Exception:  # noqa: BLE001 - states never raise
        pass
    return state


def _rollup_eval_states(repo: str, batches_by_id: dict,
                        evals_by_id: dict,
                        only_ids: set | None = None) -> dict:
    """M2.24: normalized states for eval-path callers (the summary
    row's own precomputed evaluations, the completion's readiness
    evaluations). Builds the newer-ready index once. The evaluations
    are reused, never recomputed. When only_ids is given, states
    are built only for those batch ids (M2.25: the status row pays
    _batch_pod_drift worktree reads only for rollup leaves, while
    the hint index still spans every batch). Never raises."""
    states = {}
    try:
        by_id = batches_by_id if isinstance(batches_by_id, dict) \
            else {}
        evs = evals_by_id if isinstance(evals_by_id, dict) else {}
        prim_idx = []
        ready_of = {}
        for _b in by_id.values():
            if not isinstance(_b, dict) or not _b.get("id"):
                continue
            _ev = evs.get(_b.get("id"))
            if not isinstance(_ev, dict):
                _ev = {"results": []}
            _rs = [r for r in (_ev.get("results") or [])
                   if isinstance(r, dict)]
            _ready = sum(1 for r in _rs if r.get("ready"))
            ready_of[_b.get("id")] = (_ready, len(_rs))
            for _p in _b.get("primaries") or []:
                if isinstance(_p, dict) and _p.get("dep"):
                    _inh = set()
                    for _h in _b.get("inherited") or []:
                        if isinstance(_h, dict) and _h.get("batch"):
                            _inh.add(str(_h.get("batch")))
                    prim_idx.append(
                        (_b.get("id"), str(_b.get("repo") or ""),
                         str(_p.get("lane") or ""),
                         str(_p.get("dep") or ""),
                         str(_p.get("target") or ""), _inh))
        for _bid, _b in by_id.items():
            if not isinstance(_b, dict):
                continue
            if only_ids is not None and _bid not in only_ids:
                continue
            _ev = evs.get(_bid)
            if not isinstance(_ev, dict):
                _ev = {"results": []}
            states[_bid] = _rollup_leaf_state_from_eval(
                repo, _b, _ev, prim_idx, ready_of)
    except Exception:  # noqa: BLE001 - states never raise
        pass
    return states


def _rollup_leaf_caveats(repo: str, leaves: list,
                         states_by_id: dict) -> list:
    """M2.24: read-only per-leaf caveats for a rollup op's leaves.

    Each caveat is {leaf, kind, text}. Kinds, in order: a compiler
    note (evaluation attention), a newer READY leaf, a hold placed
    after the rollup, a leaf no longer READY under the current
    evaluation, and an iOS Podfile.lock drift label. States come
    from the same precomputed evaluation the glance uses (snapshot
    cards or the caller's own evaluations) — this helper never
    evaluates and never reads a worktree. Unreadable leaves yield
    nothing — caveats are display-only, never a raise, never a
    re-roll. The stored op file is never rewritten by display;
    callers attach the list to the returned dict only.
    """
    caveats = []
    try:
        states = states_by_id if isinstance(states_by_id, dict) \
            else {}
        for leaf in leaves or []:
            if not isinstance(leaf, dict):
                continue
            dep = str(leaf.get("dep") or "")
            bid = str(leaf.get("batch") or "")
            if not dep or not bid:
                continue
            try:
                state = states.get(bid)
                if not isinstance(state, dict):
                    continue
                for _n in state.get("compiler_notes") or []:
                    if not isinstance(_n, str) or not _n:
                        continue
                    caveats.append({"leaf": bid, "kind": "compiler",
                                    "text": f"{dep} ({bid}): {_n}"})
                for _h in state.get("newer_ready") or []:
                    if not isinstance(_h, dict):
                        continue
                    caveats.append({
                        "leaf": bid, "kind": "newer_ready",
                        "text": (f"새 버전 준비됨: {_h.get('dep')} "
                                 f"{_h.get('from')} → {_h.get('to')} "
                                 f"({_h.get('batch')}) — 다시 합치기 가능")})
                hold = state.get("hold")
                if isinstance(hold, dict) and hold:
                    reason = " ".join(
                        str(hold.get("reason") or "").split())
                    text = f"{dep} ({bid}): 보류 중"
                    if reason:
                        text += f" — {reason}"
                    caveats.append({"leaf": bid, "kind": "held",
                                    "text": text})
                try:
                    fully = state.get("primaries") == 1 \
                        and int(state.get("total") or 0) > 0 \
                        and int(state.get("ready") or 0) == \
                        int(state.get("total") or 0)
                except (TypeError, ValueError):  # noqa: BLE001
                    fully = False
                if not fully:
                    caveats.append({
                        "leaf": bid, "kind": "not_ready",
                        "text": (f"{dep} ({bid}): 현재 평가에서 "
                                 "준비 완료가 아님")})
                pod_label = state.get("pod_label")
                if isinstance(pod_label, str) and pod_label:
                    caveats.append({
                        "leaf": bid, "kind": "pod_drift",
                        "text": f"{dep} ({bid}): {pod_label}"})
            except Exception:  # noqa: BLE001 - one leaf never kills it
                continue
    except Exception:  # noqa: BLE001 - caveats never raise
        pass
    return caveats


def _rollup_pr_draft(repo: str, lane: str, base_id: str, leaves: list,
                     changes: dict, branch: str, head: str,
                     op_id: str, new_path: str = "", base_head: str = "",
                     gates: list = None, lang: str = "en",
                     lang_src: str = "default",
                     notices: list = None, caveats: list = None) -> tuple:
    """(title, body, owner_commands): shown and copyable only.

    The body follows the repo's PR language (`lang`); the first line
    is a header comment printing the effective language and where it
    came from (profile vs default). Group ids stay internal.
    `notices` (redraft only) appends a 확인 필요 section; the
    automatic rollup draft never carries one.
    """
    title = (f"chore(deps): {lane} lane rollup "
             f"({len(leaves)} leaves onto {base_id})")
    lines = [f"<!-- openmainty: pr_language={lang} "
             f"(source: {lang_src}) -->",
             f"OpenMainty lane rollup of {len(leaves)} READY leaf "
             f"batches onto `{base_id}` ({lane}).",
             "", "Leaves (oldest first):"]
    for leaf in leaves:
        moves_map = changes.get(leaf["dep"]) or {}
        if not moves_map:
            moves_map = {"lockfile-only":
                         (leaf["target"], leaf["target"])}
        moves = "; ".join(
            f"{mf} ({frm} → {to})" if frm != to
            else f"{mf} ({frm}, lock refresh)"
            for mf, (frm, to) in sorted(moves_map.items()))
        lines.append(f"- {leaf['dep']}: {moves} "
                     f"[{leaf['batch']} {leaf['head'][:12]}]")
    stack = _rollup_stack(new_path, lane, base_id, base_head, head,
                          lang=lang)
    total_commits = sum(len(rows) for _, rows, _ in stack["groups"])
    if stack["target_head"]:
        if lang == "ko":
            lines += ["",
                      f"구성: 대상 `{lane}` "
                      f"({stack['target_head'][:12]}) 대비 "
                      f"{total_commits} commits"
                      + (f", {stack['diffstat']}"
                         if stack["diffstat"] else "")]
        else:
            lines += ["",
                      f"Contents: {total_commits} commits vs `{lane}` "
                      f"({stack['target_head'][:12]})"
                      + (f", {stack['diffstat']}"
                         if stack["diffstat"] else "")]
        for gid, rows, summary in stack["groups"]:
            lines.append(f"### {summary}")
            for sha, subject in rows:
                lines.append(f"- {sha[:12]} {subject}")
        if stack["unreleased"]:
            ids = ", ".join(f"`{b}`" for b in stack["unreleased"])
            if lang == "ko":
                lines += ["",
                          "이 PR은 아직 출시되지 않은 기준 묶음 "
                          f"{len(stack['unreleased'])}개를 함께 포함합니다: "
                          + ids]
            else:
                lines += ["",
                          "This PR also includes "
                          f"{len(stack['unreleased'])} not-yet-released "
                          f"base batches: " + ids]
        main_groups = [g for g in stack["groups"]
                       if g[0] not in ("rollup", "기준 묶음")
                       and _rollup_group_is_main_lane(g[0])]
        main_n = sum(len(rows) for _, rows, _ in main_groups)
        if lang == "ko":
            lines += ["",
                      "출시 순서 선택 (소유자가 결정 — OpenMainty는 "
                      "푸시·PR하지 않음):",
                      f"(a) main 체인 먼저: main 묶음 "
                      f"({main_n} commits)을 main에 PR → 병합 후 이 합치기를 "
                      f"`{lane}`에 PR",
                      f"(b) `{lane}`에 한 번에: 아래 {total_commits} commits를 "
                      f"`{lane}`에 PR 한 번으로"]
        else:
            lines += ["",
                      "Ship order, owner's choice — OpenMainty never "
                      "pushes or opens PRs:",
                      f"(a) main chain first: PR the main batches "
                      f"({main_n} commits) to main, then PR this rollup "
                      f"to `{lane}` after merge",
                      f"(b) all at once to `{lane}`: PR the "
                      f"{total_commits} commits below to `{lane}` in "
                      "one go"]
    elif lang == "ko":
        lines += ["",
                  f"구성: 대상 `{lane}` 브랜치를 합치기 저장소에서 찾지 "
                  "못해 커밋 목록을 적지 못함 — 가져오기 전 확인 필요"]
    else:
        lines += ["",
                  f"Contents: the `{lane}` branch was not found in the "
                  "rollup checkout, so no commit list — verify "
                  "before import"]
    fast = {(g.get("key") or ""): (g.get("status") or "")
            for g in (gates or []) if isinstance(g, dict)}
    fc = fast.get("fastchecks", "pending")
    if lang == "ko":
        fc_txt = {"pass": "통과", "blocked": "막힘"}.get(
            fc, "대기 중 (최종 확인 전)")
        lines += ["",
                  f"Rollup source: `{branch}` @ {head[:12]} "
                  f"(op `{op_id}`, repo `{repo}`).",
                  f"빠른 검사: {fc_txt}; native/device QA는 별도 단계. "
                  "Root QA 영수증은 가져오기 때 묶음에 포함."]
    else:
        fc_txt = {"pass": "pass", "blocked": "blocked"}.get(
            fc, "pending (before final review)")
        lines += ["",
                  f"Rollup source: `{branch}` @ {head[:12]} "
                  f"(op `{op_id}`, repo `{repo}`).",
                  f"Fastchecks: {fc_txt}; native/device QA is a "
                  "separate step. Root QA receipts are attached "
                  "at import."]
    if notices:
        if lang == "ko":
            lines += ["", "확인 필요"] + [f"- {n}" for n in notices]
        else:
            lines += ["", "Needs attention"] + [
                f"- {n}" for n in notices]
    # M2.24: leaves that changed since the draft — one line per
    # caveat, only when there is at least one. A rollup with clean
    # leaves keeps a byte-identical body.
    clean = [c for c in (caveats or []) if isinstance(c, dict)
             and str(c.get("text") or "")]
    if clean:
        if lang == "ko":
            lines += ["", "초안 이후 확인할 점"] + [
                f"- {c['text']}" for c in clean]
        else:
            lines += ["", "Caveats"] + [
                f"- {c['text']}" for c in clean]
    body = "\n".join(lines)
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:60]
    safe_title = title.replace("'", "'\\''")
    commands = [
        f"git push -u origin {branch}",
        f"gh pr create --base {lane} --title '{safe_title}' "
        f"--body-file {slug}-PR-body.md  "
        f"# save the draft body above first"]
    # M2.16e2: when the rollup tree really drifts on RN
    # prebuilt pins (declared RN vs locked prebuilt), the owner
    # must sync the lock before pushing — name the exact command
    # first (copyable text only; OpenMainty never runs pod). An
    # in-sync lock gets nothing, even though it pins the same
    # pods. Read-only detection; unreadable trees change
    # nothing.
    try:
        from . import fastcheck as _fc
        _pod_cmd = _fc.rollup_pod_command(new_path) \
            if new_path else ""
    except (Exception, SystemExit):  # noqa: BLE001 - bonus
        # command, never fatal
        _pod_cmd = ""
    if _pod_cmd:
        commands = [_pod_cmd] + commands
    return title, body, commands


_SHIP_ORDER_DEFAULT = "chain-first"


def _ship_order(repo: str) -> str:
    """Stored, visible ship policy for a repo: its profile's
    `ship_order`, else the owner default chain-first (base chain to
    main first, then the lane rollup stacked on top). Drafts print the
    effective value; code never silently assumes it."""
    try:
        prof = read_json(paths.repos_dir() / f"{repo}.json",
                         default=None)
        if isinstance(prof, dict) and prof.get("ship_order"):
            return str(prof["ship_order"])
    except Exception:  # noqa: BLE001 - unreadable profile → default
        pass
    return _SHIP_ORDER_DEFAULT


def _rollup_leaf_lines(leaves: list, changes: dict) -> list:
    out = []
    for leaf in leaves:
        moves_map = changes.get(leaf["dep"]) or {}
        if not moves_map:
            moves_map = {"lockfile-only":
                         (leaf["target"], leaf["target"])}
        moves = "; ".join(
            f"{mf} ({frm} → {to})" if frm != to
            else f"{mf} ({frm}, lock refresh)"
            for mf, (frm, to) in sorted(moves_map.items()))
        out.append(f"- {leaf['dep']}: {moves} "
                   f"[{leaf['batch']} {leaf['head'][:12]}]")
    return out


def _rollup_fastchecks_line(gates: list, lang: str = "en") -> str:
    fast = {(g.get("key") or ""): (g.get("status") or "")
            for g in (gates or []) if isinstance(g, dict)}
    fc = fast.get("fastchecks", "pending")
    if lang == "ko":
        fc_txt = {"pass": "통과", "blocked": "막힘"}.get(
            fc, "대기 중 (최종 확인 전)")
        return (f"빠른 검사: {fc_txt}; native/device QA는 별도 단계. "
                "Root QA 영수증은 가져오기 때 묶음에 포함.")
    fc_txt = {"pass": "pass", "blocked": "blocked"}.get(
        fc, "pending (before final review)")
    return (f"Fastchecks: {fc_txt}; native/device QA is a separate "
            "step. Root QA receipts are attached at import.")


def _rollup_stacked_drafts(*, repo: str, lane: str, base_id: str,
                           base_head: str, head: str, leaves: list,
                           changes: dict, branch: str, op_id: str,
                           new_path: str, gates: list,
                           qa: dict = None, lang: str = "en",
                           lang_src: str = "default",
                           supersedes: dict = None) -> dict:
    """TWO copy-only drafts implementing ship_order=chain-first.

    PR1 ships the base chain to main; PR2 stacks the rollup on top.
    `qa` carries receipt-derived lines ({"pr1": ..., "pr2": ...});
    without them each draft says plainly that its QA is pending.
    Bodies follow the repo's PR language (`lang`, "en"|"ko",
    rendered from the same data — never hardcoded per language);
    each body opens with a header comment printing the effective
    language and its source. `summary_ko` is a short Korean owner
    summary for the OpenMainty panel only. Never pushes, branches,
    or opens anything.
    """
    qa = qa or {}
    # M2.5d draft-time preflight: a lane draft carrying a lockstep
    # family member without its react-native bump is refused here
    # (both the completion and the redraft paths build through this
    # function). Config-only members pass with a WARNING line.
    lock_warnings = _rollup_lockstep_preflight(new_path, leaves, lang)
    order = _ship_order(repo)
    ship_day = utcnow()[:10].replace("-", "")
    ship_branch = f"codex/ship-main-{ship_day}"
    header = (f"<!-- openmainty: pr_language={lang} "
              f"(source: {lang_src}) -->")
    stack1 = _rollup_stack(new_path, "main", base_id, base_head,
                           base_head, lang=lang)
    commits1 = sum(len(rows) for _, rows, _ in stack1["groups"])
    stack2 = _rollup_stack(new_path, lane, base_id, base_head, head,
                           lang=lang)
    commits2 = sum(
        len(rows) for gid, rows, _ in stack2["groups"]
        if gid in ("rollup", "기준 묶음"))
    next_lanes = sorted({
        gid for gid, rows, _ in stack1["groups"]
        if rows and gid not in ("rollup", "기준 묶음")
        and not _rollup_group_is_main_lane(gid)})
    labels = []
    for gid, rows, _ in stack1["groups"]:
        if not rows or gid in ("rollup", "기준 묶음"):
            continue
        try:
            grp = load_batch(gid)
        except BatchError:
            grp = None
        # The chain carries fixes, not only dep bumps, so the title
        # names the batches' own purposes instead of "N commits".
        labels.append(str((grp or {}).get("purpose") or gid)[:30])
    if lang == "ko":
        # The chain carries fixes, not only dep bumps, so the title
        # names the batches' own purposes instead of "N commits".
        labels = []
        for gid, rows, _ in stack1["groups"]:
            if not rows or gid in ("rollup", "기준 묶음"):
                continue
            try:
                grp = load_batch(gid)
            except BatchError:
                grp = None
            labels.append(str((grp or {}).get("purpose") or gid)[:30])
        title1 = ("chore: ship base chain to main ("
                  + (", ".join(labels) if labels else
                     f"{commits1} commits") + ")")
        lines1 = [header,
                  f"PR1 — main 체인 출시 (출시 순서 정책: {order}, "
                  "소유자 결정).",
                  f"구성: 대상 `main` "
                  f"({stack1['target_head'][:12]}) 대비 {commits1} commits"
                  + (f", {stack1['diffstat']}" if stack1["diffstat"]
                     else "")]
    else:
        # Short and true: one data-derived segment per unreleased
        # chain batch (fixes ride along, so no "N commits" title).
        title1 = _pr1_title_en(
            _pr1_seg_labels(
                stack1["unreleased"],
                {gid: rows for gid, rows, _ in stack1["groups"]}),
            commits1)
        lines1 = [header,
                  f"PR1 — ship the main chain (ship order policy: "
                  f"{order}, owner's decision).",
                  f"Contents: {commits1} commits vs `main` "
                  f"({stack1['target_head'][:12]})"
                  + (f", {stack1['diffstat']}" if stack1["diffstat"]
                     else "")]
    for _gid, rows, summary in stack1["groups"]:
        if not rows and _gid == "rollup":
            continue
        lines1.append(f"### {summary}")
        for sha, subject in rows:
            lines1.append(f"- {sha[:12]} {subject}")
    if stack1["unreleased"]:
        ids = ", ".join(f"`{b}`" for b in stack1["unreleased"])
        if lang == "ko":
            lines1 += ["",
                       "이 PR은 아직 출시되지 않은 기준 묶음 "
                       f"{len(stack1['unreleased'])}개를 함께 포함합니다: "
                       + ids]
        else:
            lines1 += ["",
                       "This PR also includes "
                       f"{len(stack1['unreleased'])} not-yet-released "
                       f"base batches: " + ids]
    if next_lanes:
        if lang == "ko":
            lines1 += ["",
                       "참고: "
                       + ", ".join(f"`{b}`" for b in next_lanes)
                       + " 묶음은 next 레인으로 분류됐으나, 소유자 결정으로 "
                       "main과 함께 출시합니다."]
        else:
            names = ", ".join(f"`{b}`" for b in next_lanes)
            if len(next_lanes) == 1:
                lines1 += ["",
                           "Note: " + names + " is classified as a "
                           "next-lane batch but ships with main by "
                           "owner decision."]
            else:
                lines1 += ["",
                           "Note: " + names + " are classified as "
                           "next-lane batches but ship with main by "
                           "owner decision."]
    if lang == "ko":
        lines1 += ["",
                   "빠른 검사(PR1): " + (qa.get("pr1") or
                    "Root QA 진행 중 (claude66/qa/m0-ship-main-*)")]
    else:
        lines1 += ["",
                   "Fastchecks (PR1): " + (qa.get("pr1") or
                    "Root QA in progress (claude66/qa/m0-ship-main-*)")]
    slug1 = re.sub(r"[^a-z0-9]+", "-", ship_branch.lower()
                   ).strip("-")[:60]
    safe1 = title1.replace("'", "'\\''")
    commands1 = [
        f"git branch {ship_branch} {base_head}",
        f"git push -u origin {ship_branch}",
        f"gh pr create --base main --head {ship_branch} "
        f"--title '{safe1}' --body-file {slug1}-PR-body.md  "
        f"# save the draft body above first"]
    title2 = (f"chore(deps): {lane} lane rollup "
              f"({len(leaves)} leaves onto {base_id})")
    if lang == "ko":
        lines2 = [header,
                  f"PR2 — {lane} 합치기, PR1 위에 쌓음 (출시 순서 정책: "
                  f"{order}, 소유자 결정).",
                  "스태킹: PR1이 열려 있는 동안에는 "
                  f"`--base {ship_branch}`로 염. PR1이 병합되고 `next`가 "
                  "`main`까지 올라오면 `--base next`로 retarget "
                  f"(`gh pr edit <번호> --base next`). 참고: `next`는 현재 "
                  f"`main`과 같음({stack2['target_head'][:12]})."
                  if stack2["target_head"] else
                  "스태킹: 대상 브랜치 확인 필요 — 가져오기 전 확인.",
                  "",
                  f"구성: {base_head[:12]}..{head[:12]}, "
                  f"{commits2} commits"
                  + (f", {stack2['rollup_diffstat']}"
                     if stack2["rollup_diffstat"]
                     else "")]
    else:
        lines2 = [header,
                  f"PR2 — {lane} rollup, stacked on PR1 (ship order "
                  f"policy: {order}, owner's decision).",
                  "Stacking: while PR1 is open, use "
                  f"`--base {ship_branch}`. After PR1 merges and `next` "
                  "catches up with `main`, retarget with `--base next` "
                  "(`gh pr edit <number> --base next`). Note: `next` "
                  f"currently equals `main` "
                  f"({stack2['target_head'][:12]})."
                  if stack2["target_head"] else
                  "Stacking: the target branch needs verification — "
                  "check before import.",
                  "",
                  f"Contents: {base_head[:12]}..{head[:12]}, "
                  f"{commits2} commits"
                  + (f", {stack2['rollup_diffstat']}"
                     if stack2["rollup_diffstat"]
                     else "")]
    for gid, rows, _summary in stack2["groups"]:
        if gid not in ("rollup", "기준 묶음"):
            continue
        for sha, subject in rows:
            lines2.append(f"- {sha[:12]} {subject}")
    # M2: a strictly larger READY set supersedes an older done rollup
    # without touching it — the new drafts name the old op and list
    # only what is new since then.
    if isinstance(supersedes, dict) and supersedes.get("op"):
        new_ids = set(supersedes.get("new") or [])
        new_deps = sorted({str(leaf.get("dep") or "")
                           for leaf in leaves
                           if isinstance(leaf, dict)
                           and leaf.get("batch") in new_ids
                           and leaf.get("dep")})
        new_txt = ", ".join(new_deps) if new_deps else "(none listed)"
        if lang == "ko":
            lines2 += ["",
                       f"대체: 이 초안은 `{supersedes['op']}`를 대체합니다. "
                       "이전 초안·브랜치는 그대로 둡니다. "
                       f"새로 들어간 묶음: {new_txt}."]
        else:
            lines2 += ["",
                       f"Supersedes: this draft supersedes "
                       f"`{supersedes['op']}`; the older drafts and "
                       "branch are left untouched. "
                       f"New since then: {new_txt}."]
    lines2 += ["", f"{len(leaves)} leaves (oldest first):"]
    lines2 += _rollup_leaf_lines(leaves, changes)
    # M2.5d: config-only lockstep members ride with a WARNING line.
    lines2 += [""] + lock_warnings if lock_warnings else []
    if lang == "ko":
        lines2 += ["",
                   "빠른 검사(PR2): "
                   + (qa.get("pr2") or _rollup_fastchecks_line(
                       gates, lang="ko"))]
    else:
        lines2 += ["",
                   "Fastchecks (PR2): "
                   + (qa.get("pr2") or _rollup_fastchecks_line(gates))]
    slug2 = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:60]
    safe2 = title2.replace("'", "'\\''")
    # Every gh command names --base AND --head explicitly (gh would
    # otherwise use whatever branch the owner has checked out), and
    # every --head branch is pushed first.
    if lang == "ko":
        commands2 = [
            f"git push -u origin {branch}",
            f"gh pr create --base {ship_branch} --head {branch} "
            f"--title '{safe2}' "
            f"--body-file {slug2}-PR-body.md  "
            f"# save the draft body above first; PR1 병합 후 "
            f"`gh pr edit <번호> --base next`로 retarget"]
    else:
        commands2 = [
            f"git push -u origin {branch}",
            f"gh pr create --base {ship_branch} --head {branch} "
            f"--title '{safe2}' "
            f"--body-file {slug2}-PR-body.md  "
            f"# save the draft body above first; after PR1 merges, "
            f"retarget with `gh pr edit <number> --base next`"]
    summary_ko = (f"소유자 요약: PR1 — main에 기준 체인 {commits1} commits "
                  f"선출시 → PR2 — {lane} 합치기 {len(leaves)} leaves "
                  "(PR1 위에 쌓음)")
    return {
        "ship_order": order,
        "summary_ko": summary_ko,
        "supersedes": (supersedes.get("op")
                       if isinstance(supersedes, dict) else None),
        "pr1": {"title": title1, "body": "\n".join(lines1),
                "commands": commands1, "branch": ship_branch,
                "base": "main", "head": base_head,
                "commits": commits1},
        "pr2": {"title": title2, "body": "\n".join(lines2),
                "commands": commands2, "branch": branch,
                "base": ship_branch, "commits": commits2,
                "leaves": len(leaves)},
    }


def _rollup_manifest_moves(new_path: str, base_checkout: str,
                           leaves: list) -> dict:
    """Per-dep manifest range moves between the base and rollup trees
    (package.json + example/package.json). Shared by the rollup done
    path and the redraft path."""
    moves = {}
    for leaf in leaves:
        dep = (leaf.get("dep") or "") if isinstance(leaf, dict) \
            else ""
        if not dep:
            continue
        per = {}
        for mf in ("package.json", "example/package.json"):
            old_rng = _manifest_declared_range(base_checkout, mf, dep)
            new_rng = _manifest_declared_range(str(new_path), mf, dep)
            if old_rng != new_rng and (old_rng or new_rng):
                per[mf] = (old_rng or "없음", new_rng or "없음")
        moves[dep] = per
    return moves


def rollup_qa_path(op_id: str) -> Path:
    return integration_ops_dir() / (op_id + ".qa.json")


def regenerate_rollup_drafts(op_id: str, qa: dict = None) -> dict:
    """Rebuild the drafts of a DONE rollup op (explicit redraft path).

    Verifies the recorded branch tip and worktree fingerprint are
    unchanged (both recomputed read-only); writes ONLY the draft
    fields (the old single draft is archived) plus an optional
    persisted qa note; refreshes the glance. Never touches the branch
    or any worktree. confirm:true is enforced by the serve layer.
    """
    opf, rec = _load_op(op_id)
    if rec.get("op") != "rollup":
        raise IntegrationError(
            f"operation {op_id!r} is not a rollup", http_status=404)
    if rec.get("status") != "done":
        raise IntegrationError(
            "only a done rollup can be redrafted (blocked ops use "
            "retry)", http_status=409)
    res = rec.get("result") or {}
    path = str(res.get("path") or "")
    branch = str(res.get("branch") or "")
    head = str(res.get("head") or "")
    fp = str(res.get("fingerprint") or "")
    if not path or not branch or not head or not fp:
        raise IntegrationError("record has no rollup source to "
                               "verify; redraft refused")
    if not Path(path).is_dir():
        raise IntegrationError("rollup worktree is gone; cannot "
                               "verify — redraft refused")
    tip = _git_text(["rev-parse", branch], path)
    if tip != head:
        raise IntegrationError(
            f"rollup branch moved ({(tip or '?')[:12]} vs "
            f"{head[:12]}); redraft refused")
    try:
        snap = runner.source_snapshot(path)
    except runner.SourceUnreadable as exc:
        # An unverifiable worktree refuses the redraft explicitly
        # (unknown, never a 500, never a green redraft).
        raise IntegrationError(
            f"rollup worktree unreadable ({exc.op}); redraft refused")
    if snap.get("head") != head or \
            snap.get("fingerprint") != fp:
        raise IntegrationError("rollup worktree changed since the "
                               "run; redraft refused")
    qaf = rollup_qa_path(op_id)
    if isinstance(qa, dict) and qa:
        clean = {k: str(v)[:2000] for k, v in qa.items()
                 if k in ("pr1", "pr2") and isinstance(v, str)
                 and v.strip()}
        if clean:
            write_json(qaf, {"at": utcnow(), "qa": clean})
    noted = read_json(qaf, default=None)
    lines = {}
    if isinstance(noted, dict) and isinstance(noted.get("qa"), dict):
        lines = {k: str(v) for k, v in noted["qa"].items()
                 if k in ("pr1", "pr2")}
    base = rec.get("base") or {}
    leaves = []
    for leaf in rec.get("leaves") or []:
        if not isinstance(leaf, dict):
            continue
        leaves.append({"dep": str(leaf.get("dep") or ""),
                       "batch": str(leaf.get("batch") or ""),
                       "head": str(leaf.get("head") or ""),
                       "target": str(leaf.get("target") or "")})
    base_checkout = str(base.get("checkout") or "")
    if base_checkout and Path(base_checkout).is_dir():
        changes = _rollup_manifest_moves(path, base_checkout, leaves)
    else:
        changes = {}
    lane = str(rec.get("lane") or "")
    repo = str(rec.get("repo") or "")
    base_id = str(base.get("batch") or "")
    base_head = str(base.get("head") or "")
    lang, lang_src = _pr_language(repo)
    # M2.16d: redraft-only 확인 필요 — the automatic rollup draft
    # never carries it.
    # M2.24: the redraft also names what changed in the leaves
    # since the draft — reading the same batches snapshot the
    # glance uses, never a fresh evaluate on this path.
    redraft_caveats = []
    try:
        _snap = read_json(batches_snapshot_path(), default=None)
        _cards = {}
        for _c in ((_snap or {}).get("batches") or []):
            if isinstance(_c, dict) and _c.get("id"):
                _cards[_c["id"]] = _c
        _states = {}
        for _leaf in leaves:
            if not isinstance(_leaf, dict):
                continue
            _lbid = str(_leaf.get("batch") or "")
            if not _lbid or _lbid in _states:
                continue
            if _lbid in _cards:
                _states[_lbid] = _rollup_leaf_state_from_card(
                    _cards[_lbid])
        redraft_caveats = _rollup_leaf_caveats(repo, leaves, _states)
    except (Exception, SystemExit):  # noqa: BLE001 - redraft wins
        redraft_caveats = []
    title, body, commands = _rollup_pr_draft(
        repo, lane, base_id, leaves, changes, branch, head, op_id,
        new_path=path, base_head=base_head, gates=rec.get("gates"),
        lang=lang, lang_src=lang_src,
        notices=_rollup_mismatch_notices(leaves),
        caveats=redraft_caveats)
    # M2: a redraft keeps the recorded supersedes line (recomputed
    # against the old op file, never edited).
    sup = None
    if rec.get("supersedes"):
        old_id = str(rec["supersedes"])
        try:
            old_rec = read_json(integration_ops_dir() / (old_id + ".json"),
                                default=None)
            old_leaves = {str(x.get("batch") or "")
                          for x in ((old_rec or {}).get("leaves") or [])
                          if isinstance(x, dict)} \
                if isinstance(old_rec, dict) else set()
        except Exception:  # noqa: BLE001 - unreadable old op
            old_leaves = set()
        cur_ids = {str(x.get("batch") or "") for x in leaves}
        sup = {"op": old_id, "new": sorted(cur_ids - old_leaves)}
    stacked = _rollup_stacked_drafts(
        repo=repo, lane=lane, base_id=base_id, base_head=base_head,
        head=head, leaves=leaves, changes=changes, branch=branch,
        op_id=op_id, new_path=path, gates=rec.get("gates"), qa=lines,
        lang=lang, lang_src=lang_src, supersedes=sup)
    rec["pr_draft_previous"] = rec.get("pr_draft")
    rec["stacked_drafts_previous"] = rec.get("stacked_drafts")
    rec["pr_draft"] = {"title": title, "body": body}
    rec["stacked_drafts"] = stacked
    rec["ship_order"] = stacked["ship_order"]
    rec["owner_commands"] = commands
    rec["updated_at"] = utcnow()
    write_json(opf, rec)
    try:
        refresh_all_snapshots("rollup-redraft")
    except Exception:  # noqa: BLE001 - glance refresh never masks
        pass
    # M2.24: the result view carries caveats; the stored file
    # stays as written above (display never rewrites the op).
    rec["caveats"] = redraft_caveats
    return rec


def _rollup_group_is_main_lane(batch_id: str) -> bool:
    """True when the named batch targets main (for the (a)/(b) choice)."""
    try:
        grp = load_batch(batch_id)
    except BatchError:
        return False
    return isinstance(grp, dict) and \
        (grp.get("target_branch") or "") == "main"


def _rollup_lock_only_deps(leaves: list, base_worktree: str,
                           manifest_files: tuple) -> list:
    """Deps whose replayed manifest range equals the base range.

    A lock-only leaf bumps the resolution inside an UNCHANGED range,
    so every manifest it touches (if any) still declares the base
    range. Unreadable manifests never qualify — the guards decide.
    """
    out = []
    for leaf in leaves:
        dep = leaf.get("dep") or ""
        wt = str(leaf.get("worktree") or "")
        if not dep or not wt:
            continue
        try:
            same = all(
                _manifest_declared_range(wt, mf, dep)
                == _manifest_declared_range(base_worktree, mf, dep)
                for mf in manifest_files)
        except Exception:  # noqa: BLE001 - unreadable is not lock-only
            continue
        if same:
            out.append(dep)
    return out


@m2.op_guarded(lambda repo, lane, base_batch, leaf_ids, **_:
                   _rollup_op_id(lane, base_batch, leaf_ids),
                  busy=lambda op: IntegrationError(
                      f"다른 확인 작업이 {op}에서 실행 중입니다", 409))
def rollup_leaves(repo: str, lane: str, base_batch: str, leaf_ids: list,
                  retry: bool = False, supersedes: dict = None) -> dict:
    """Merge READY same-lane leaf batches onto their READY common base.

    Deterministic op id from (lane, base, sorted leaves): re-POST
    reconciles, retry archives attempts. Per leaf (oldest updated_at
    first) cherry-picks that leaf's own integration commits (leaf head
    minus its parent batch head), never anything else. Lockfile-only
    conflicts reset the lock and regenerate ONCE at the end (union
    closure over every leaf dep); manifest adjacency uses D1-d per
    position; anything else blocks naming leaf+commit+paths with
    cleanup. No readiness is claimed: gates stay pending for Root QA.
    """
    if not repo or lane not in ("main", "next"):
        raise IntegrationError("repo and lane(main|next) are required",
                               http_status=404 if not repo else 400)
    if not base_batch or not isinstance(leaf_ids, list) or not leaf_ids:
        raise IntegrationError("base_batch and a non-empty leaves list "
                               "are required")
    leaves_in = []
    for bid in leaf_ids:
        if not isinstance(bid, str) or not bid or bid in leaves_in:
            raise IntegrationError("leaves must be distinct non-empty "
                                   "batch ids")
        leaves_in.append(bid)
    if len(leaves_in) > _ROLLUP_MAX_LEAVES:
        raise IntegrationError(
            f"{len(leaves_in)} leaves; rollup supports at most "
            f"{_ROLLUP_MAX_LEAVES} — split the lane")
    try:
        base = load_batch(base_batch)
    except BatchError:
        raise IntegrationError(f"unknown batch '{base_batch}'",
                               http_status=404) from None
    if (base.get("repo") or "") != repo:
        raise IntegrationError(
            f"base batch '{base_batch}' belongs to a different repo")
    if (base.get("target_branch") or "") != lane:
        raise IntegrationError(
            f"base batch '{base_batch}' targets "
            f"'{base.get('target_branch')}', not '{lane}'")
    leaf_batches = []
    for bid in leaves_in:
        try:
            leaf = load_batch(bid)
        except BatchError:
            raise IntegrationError(f"unknown batch '{bid}'",
                                   http_status=404) from None
        if (leaf.get("repo") or "") != repo:
            raise IntegrationError(
                f"leaf batch '{bid}' belongs to a different repo")
        if (leaf.get("target_branch") or "") != lane:
            raise IntegrationError(
                f"leaf batch '{bid}' targets "
                f"'{leaf.get('target_branch')}', not '{lane}'")
        primaries = [p for p in (leaf.get("primaries") or [])
                     if isinstance(p, dict)]
        if len(primaries) != 1:
            raise IntegrationError(
                f"leaf batch '{bid}' records {len(primaries)} primaries; "
                "rollup needs exactly one per leaf")
        leaf_batches.append(leaf)
    deps = [str((leaf.get("primaries") or [{}])[0].get("dep") or "")
            for leaf in leaf_batches]
    if any(not d for d in deps):
        raise IntegrationError("a leaf records no dep; refusing")
    if len(set(deps)) != len(deps):
        raise IntegrationError(
            "two leaves bump the same dep; rollup refuses ambiguous "
            "merges — split them")
    for leaf in leaf_batches:
        if base_batch not in _rollup_ancestor_ids(leaf):
            raise IntegrationError(
                f"leaf batch '{leaf.get('id')}' does not inherit base "
                f"'{base_batch}'; refusing")
    # The live map must cover the TRANSITIVE closure (grandparents
    # included): readiness of an inheriting batch needs every ancestor's
    # live snapshots in the same map. closure() is the one shared
    # helper every evaluate caller uses — never a direct-parents list.
    involved = closure([base] + leaf_batches)
    live, diag = live_map_with_diag(involved)
    leaf_evals = {}
    for b in [base] + leaf_batches:
        leaf_evals[b.get("id")] = _rollup_ready(b, live, diag)
    base_integ = _rollup_lane_integration(base, lane)
    base_head = str(base_integ.get("head") or "")
    base_fp = str(base_integ.get("fingerprint") or "")
    base_worktree = str(base_integ.get("worktree") or "")
    if not base_head:
        raise IntegrationError("base integration records no exact source")
    base_snap = _snapshot(base_worktree)
    if (base_snap is None or base_snap.get("head") != base_head
            or base_snap.get("fingerprint") != base_fp
            or not base_snap.get("clean")):
        raise IntegrationError("base integration source drifted or "
                               "unreadable")
    order = sorted(leaf_batches,
                   key=lambda b: (str(b.get("updated_at") or ""),
                                  str(b.get("id") or "")))
    leaves = []
    total_commits = 0
    for leaf in order:
        bid = str(leaf.get("id") or "")
        primary = (leaf.get("primaries") or [{}])[0]
        dep = str(primary.get("dep") or "")
        target = str(primary.get("target") or "")
        integ = _rollup_lane_integration(leaf, lane)
        leaf_head = str(integ.get("head") or "")
        leaf_fp = str(integ.get("fingerprint") or "")
        leaf_worktree = str(integ.get("worktree") or "")
        if not leaf_head:
            raise IntegrationError(
                f"leaf '{bid}' integration records no exact source")
        snap = _snapshot(leaf_worktree)
        if (snap is None or snap.get("head") != leaf_head
                or snap.get("fingerprint") != leaf_fp
                or not snap.get("clean")):
            raise IntegrationError(
                f"leaf '{bid}' integration source drifted or unreadable; "
                "refusing")
        parent_id = next((str(h.get("batch") or "")
                          for h in (leaf.get("inherited") or [])
                          if isinstance(h, dict) and h.get("batch")), "")
        try:
            parent = load_batch(parent_id) if parent_id else None
        except BatchError:
            parent = None
        if parent is None:
            raise IntegrationError(
                f"leaf '{bid}' names unknown parent batch "
                f"'{parent_id}'; refusing")
        parent_integ = _rollup_lane_integration(parent, lane)
        parent_head = str(parent_integ.get("head") or "")
        if not parent_head:
            raise IntegrationError(
                f"parent batch '{parent_id}' records no '{lane}' head; "
                "refusing")
        commits = _rollup_leaf_commits(leaf_worktree, parent_head,
                                       leaf_head, bid)
        total_commits += len(commits)
        leaves.append({"batch": bid, "dep": dep, "target": target,
                       "head": leaf_head, "fingerprint": leaf_fp,
                       "worktree": leaf_worktree,
                       "parent_batch": parent_id,
                       "parent_head": parent_head,
                       "commits": commits})
    if total_commits > _ROLLUP_MAX_COMMITS:
        raise IntegrationError(
            f"rollup replays {total_commits} commits; at most "
            f"{_ROLLUP_MAX_COMMITS} — split the lane")
    # M2.5d early lockstep preflight: refuse before creating any
    # worktree or branch. FIX-b1: validated per workspace against
    # the base tree's declarations. A leaf bumping react-native
    # overrides every workspace that declares it (fail-fast only —
    # the merged-tree draft preflight is authoritative).
    _early_spaces = _rollup_workspace_maps(base_worktree)
    for leaf in leaves:
        if leaf.get("dep") == "react-native" and leaf.get("target"):
            _early_rn = str(leaf["target"])
            for _space in _early_spaces.values():
                if _space.get("rn_minor") is not None:
                    _space["rn_range"] = _early_rn
                    _space["rn_minor"] = m2._version_minor(_early_rn)
    _early_lang = _pr_language(repo)[0]
    _rollup_lockstep_check_ws(_early_spaces, leaves,
                              lang=_early_lang)
    rev_list = [c["head"] for leaf in leaves for c in leaf["commits"]]
    op_id = _rollup_op_id(lane, base_batch,
                          [leaf["batch"] for leaf in leaves])
    opf = op_file(op_id)
    new_path = paths.home() / "reviews" / f"rollup-{lane}-{_slug(base_batch)}-{op_id[-12:]}"
    branch = (f"codex/rollup-{lane}-"
              f"{utcnow()[:10].replace('-', '')}")
    if _git_head(base_worktree, branch):
        # M2: a superseding rollup must never touch the old branch —
        # but it also must not refuse on it. Only when the taken
        # branch is exactly the superseded op's own recorded branch,
        # suffix -2/-3…; any other owner branch still refuses.
        old_branch = None
        if isinstance(supersedes, dict) and supersedes.get("op"):
            try:
                old_rec = read_json(
                    op_file(str(supersedes["op"])), default=None)
                old_branch = ((old_rec or {}).get("result") or {}).get(
                    "branch")
            except Exception:  # noqa: BLE001 - unreadable old op
                old_branch = None
        if old_branch and old_branch == branch:
            n = 2
            while _git_head(base_worktree, f"{branch}-{n}"):
                n += 1
            branch = f"{branch}-{n}"
    existing = read_json(opf, default=None)
    if isinstance(existing, dict) and existing.get("op_id") == op_id:
        if not retry:
            return _reconcile_existing(opf, existing, new_path)
        _prepare_retry(opf, existing)
    elif retry:
        raise IntegrationError(
            f"no rollup op {op_id!r} to retry; nothing started")
    if new_path.exists():
        raise IntegrationError("integration path already exists "
                               f"(unrelated): {new_path}")
    if _git_head(base_worktree, branch):
        raise IntegrationError("branch already exists "
                               f"(unrelated): {branch}")
    dep_list = [leaf["dep"] for leaf in leaves]
    rec = {"schema": INTEGRATION_OPS_SCHEMA, "op": "rollup",
           "op_id": op_id, "repo": repo, "lane": lane,
           "request": {"base_batch": base_batch,
                       "leaves": [leaf["batch"] for leaf in leaves]},
           "base": {"batch": base_batch,
                    "head": base_head, "fingerprint": base_fp,
                    "checkout": base_worktree},
           "leaves": [({k: leaf[k] for k in ("batch", "dep", "target",
                                            "head", "fingerprint",
                                            "parent_batch", "parent_head")}
                       | {"commits": list(leaf["commits"])})
                       for leaf in leaves],
           "result": {"path": str(new_path), "branch": branch,
                      "head": "", "fingerprint": "",
                      "proof_note": "rollup bytes are a fresh merge; no "
                      "leaf proof applies to them, all runtime gates "
                      "start pending"},
           "gates": [{"key": "source-prepared", "status": "pending",
                      "note": ""},
                     {"key": "fastchecks", "status": "pending",
                      "note": "not run in this step"},
                     {"key": "native", "status": "pending",
                      "note": "Root device QA after source freeze"}],
           "status": "started", "error": "",
           "created_at": utcnow(), "updated_at": utcnow()}
    if isinstance(existing, dict):
        _push_attempt(rec, existing)
    write_json(opf, rec)

    def _rollup_branch_cleanup() -> str:
        """Remove the rollup branch when it is safe, and say so.

        Only when the branch points EXACTLY at the base head (zero own
        commits) and no worktree has it checked out. `git branch -d`
        refuses a branch with commits, and it is never forced — any
        other shape is kept with the specific reason, for retry or
        operator review to decide.
        """
        try:
            tip = _git_text(["rev-parse", branch], base_worktree)
        except Exception:  # noqa: BLE001 - probe failure keeps branch
            return "branch probe failed; leftover branch kept"
        if not tip:
            return "no leftover branch"
        if tip != base_head:
            return (f"leftover branch kept (own commits at "
                    f"{tip[:12]})")
        try:
            listed = _run_git(["worktree", "list", "--porcelain"],
                              base_worktree)
        except Exception:  # noqa: BLE001 - probe failure keeps branch
            return "worktree probe failed; leftover branch kept"
        if listed.returncode == 0 and any(
                line.strip() == f"branch refs/heads/{branch}"
                for line in (listed.stdout or "").splitlines()):
            return "leftover branch kept (still checked out)"
        try:
            gone = _run_git(["branch", "-d", branch], base_worktree)
        except Exception:  # noqa: BLE001 - delete failure keeps branch
            return "leftover branch kept (delete failed)"
        if gone.returncode == 0:
            return ("leftover branch removed "
                    "(at base head, no commits)")
        return "leftover branch kept (delete refused)"

    def _fail(message: str) -> dict:
        rec["result"]["branch_cleanup"] = _rollup_branch_cleanup()
        out = _record_blocked(opf, rec, message)
        try:
            refresh_all_snapshots("rollup")
        except Exception:  # noqa: BLE001 - glance refresh never masks
            pass
        return out

    try:
        added = _run_git(["worktree", "add", "-b", branch,
                          str(new_path), base_head], base_worktree)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout or "").strip()[-500:]
            return _fail(f"worktree add failed: {detail}")
        for leaf in leaves:
            for c in leaf["commits"]:
                _rollup_ensure_present(
                    str(new_path), c["head"], leaf["worktree"],
                    f"leaf '{leaf['batch']}' commit")
        current_texts: dict = {}
        manifest_reapplied: list = []
        remaining = [(leaf, c) for leaf in leaves
                     for c in leaf["commits"]]
        pos = 0
        blob_worktrees = {base_head: base_worktree}
        for leaf in leaves:
            for c in leaf["commits"]:
                blob_worktrees[c["head"]] = leaf["worktree"]
        while remaining:
            leaf, commit = remaining[0]
            sha = commit["head"]
            pos += 1
            disp = (f"{leaf['batch']} {pos}/{len(rev_list)} "
                    f"{sha[:12]} ({commit['subject']})")
            pick = _run_git(["-c", "user.name=openmainty",
                             "-c", "user.email=openmainty@localhost",
                             "cherry-pick", "-n", sha], str(new_path))
            if pick.returncode == 0:
                commit["disposition"] = "applied"
                current_texts.update(
                    _snapshot_manifest_texts(str(new_path)))
                remaining = remaining[1:]
                continue
            conflicted = _conflicted_paths(str(new_path))
            abort = _run_git(["cherry-pick", "--quit"], str(new_path))
            _ = abort
            if conflicted is None:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"conflict paths unreadable at {disp}; "
                             f"no success claimed, {cleanup}")
            outsiders = sorted(p for p in conflicted
                               if not _is_lock_rel(p))
            manifest_paths = sorted(
                p for p in outsiders if p in _RERESOLVE_MANIFESTS)
            others = sorted(
                p for p in outsiders if p not in _RERESOLVE_MANIFESTS)
            if others:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"non-lock conflict at {disp}, paths: "
                             f"{', '.join(others[:8])}; no success "
                             f"claimed, {cleanup}")
            if manifest_paths:
                try:
                    par = _run_git(["rev-parse", f"{sha}^"],
                                   str(new_path))
                    real_parent = (par.stdout or "").strip()
                    if par.returncode != 0 or not real_parent:
                        raise IntegrationError(
                            f"conflicting commit {sha[:12]} names no "
                            "readable parent; refusing")
                    # D1-d must compare the CONFLICTING commit against
                    # its own git parent. rev_list[pos-2] is wrong at a
                    # leaf boundary (the previous leaf's tip), so pass
                    # both shas explicitly.
                    manifest_reapplied.extend(
                        _reapply_manifest_ranges(
                            worktree=str(new_path),
                            cand_worktree=str(new_path),
                            base_head=base_head,
                            base_commit=base_head,
                            rev_list=rev_list, pos=pos,
                            dep=leaf["dep"],
                            manifest_paths=manifest_paths,
                            prior_texts=current_texts,
                            blob_worktrees=blob_worktrees,
                            conflict_sha=sha,
                            parent_sha=real_parent))
                    commit["disposition"] = "manifest-rebuilt"
                except IntegrationError as d1err:
                    _run_git(["cherry-pick", "--abort"], str(new_path))
                    cleanup = _remove_derived_tree(base_worktree,
                                                   new_path)
                    return _fail(f"{d1err} (at {disp}); {cleanup}")
            for p in conflicted:
                if not _is_lock_rel(p):
                    continue
                if p.rsplit("/", 1)[-1] != "yarn.lock" or "/" in p:
                    cleanup = _remove_derived_tree(base_worktree,
                                                   new_path)
                    return _fail(f"{p} 충돌은 이 단계에서 재생성하지 "
                                 f"않음 (yarn berry 전용, at {disp}); "
                                 f"{cleanup}")
            _restore_lockfiles_to_head(str(new_path))
            if commit.get("disposition") is None:
                commit["disposition"] = "lock-resolved"
            else:
                commit["disposition"] += "+lock-resolved"
            remaining = remaining[1:]
        manager = _detect_manager(str(new_path))
        if manager is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("no known lockfile in rollup tree; "
                         f"{cleanup}")
        try:
            lock_text = Path(new_path, "yarn.lock").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            lock_text = ""
        flavor = _yarn_flavor(lock_text) if manager == "yarn" else ""
        manifest_files = ("package.json", "example/package.json")
        manifest_bump = any(
            _manifest_declared_range(str(new_path), name, dep)
            != _manifest_declared_range(base_worktree, name, dep)
            for dep in dep_list for name in manifest_files)
        pre_files = _worktree_changed_files(str(new_path))
        pre_manifest = sorted(f for f in (pre_files or [])
                              if not _is_lock_rel(f))
        argv, origin, refusal = _regen_plan(manager, flavor,
                                            bool(manifest_bump))
        if not argv:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{refusal}; {cleanup}")
        # A lock-only leaf (its replayed range equals the base range
        # in every manifest) must keep its own resolution: the union
        # regen would otherwise keep the base's in-range pin and the
        # leaf would silently revert. Dropping just its stanzas forces
        # the same single regen to resolve its range fresh — the exact
        # lock-refresh machinery, scoped to that dep. Chosen over
        # `yarn set resolution` (which would write a package.json
        # `resolutions` entry the leaves never made) and over
        # hand-merging lock text (no tool would verify the bytes).
        # The union closure guard and the per-leaf declared-resolution
        # guard below stay unchanged and verify the outcome.
        lock_only = _rollup_lock_only_deps(leaves, base_worktree,
                                           manifest_files)
        drop_deps = list(dep_list) if origin == "lock-refresh" \
            else list(lock_only)
        if drop_deps:
            dropped = lock_text
            for dep in drop_deps:
                dropped = _drop_dep_stanzas(dropped, dep)
            try:
                Path(new_path, "yarn.lock").write_text(
                    dropped, encoding="utf-8")
            except OSError:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail("cannot stage lockfile for refresh; "
                             f"{cleanup}")
        tool_version = _tool_version(argv[0], str(new_path))
        registry_host = _registry_host(argv[0], str(new_path))
        # M1: secrets never reach the registry tool.
        regen_env = _regen_env()
        # N2: the first regen re-downloads into the app cache —
        # refuse early when the disk cannot take it.
        disk_ok, disk_note = _regen_disk_ok()
        if not disk_ok:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"{disk_note}; {cleanup}")
        try:
            regen = subprocess.run(
                argv, cwd=str(new_path), env=regen_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=_RERESOLVE_TIMEOUT, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail(f"lock tool failed to start ({argv[0]}): "
                         f"{exc}; {cleanup}")
        if regen.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(regen.stderr or regen.stdout or "")
            tail = tail.strip()[-500:]
            return _fail(f"lock tool {argv[0]} rc={regen.returncode}: "
                         f"{tail}; {cleanup}")
        present_locks = [name for name in _LOCK_FILENAMES
                         if Path(new_path, name).is_file()]
        staged = _run_git(["add", "--"] + present_locks,
                          str(new_path))
        if staged.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("cannot stage regenerated lockfiles; "
                         f"{cleanup}")
        post_files = _worktree_changed_files(str(new_path))
        if post_files is None:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("rollup diff unreadable after regenerate; "
                         f"{cleanup}")
        post_manifest = sorted(f for f in post_files
                               if not _is_lock_rel(f))
        if post_manifest != pre_manifest:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("regenerate moved non-lock files "
                         f"({', '.join(post_manifest[:8])}); {cleanup}, "
                         "nothing committed")
        try:
            new_lock = Path(new_path, "yarn.lock").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            new_lock = ""
        base_locks = {}
        for name in _LOCK_FILENAMES:
            shown = _run_git(["show", f"HEAD:{name}"], str(new_path))
            if shown.returncode == 0:
                base_locks[name] = shown.stdout or ""
        unrelated = []
        for name in _LOCK_FILENAMES:
            cur_path = Path(new_path) / name
            if not cur_path.is_file():
                continue
            text = cur_path.read_text(encoding="utf-8",
                                      errors="replace")
            old_text = base_locks.get(name)
            if old_text is None or old_text == text:
                continue
            bad = _lock_delta_guard(manager, old_text, text,
                                    dep_list[0],
                                    extra_deps=dep_list[1:])
            unrelated.extend(bad)
        unrelated = unrelated[:10]
        if unrelated:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            return _fail("관련 없는 잠금 이동: "
                         f"{', '.join(unrelated)}"
                         f"{' 외' if len(unrelated) == 10 else ''}; "
                         f"nothing committed, {cleanup}")
        resolutions = {}
        for leaf in leaves:
            dep = leaf["dep"]
            declared = set()
            for mf in manifest_files:
                rng = _manifest_declared_range(str(new_path), mf, dep)
                if rng:
                    declared.add(rng)
            if not declared:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"rollup tree declares no range for "
                             f"{dep}; nothing committed, {cleanup}")
            leaf_lock = _show_file_at(leaf["head"], "yarn.lock",
                                      leaf["worktree"])
            want = _declared_resolutions(leaf_lock, dep, declared)
            if not want:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"leaf '{leaf['batch']}' lock resolves "
                             f"nothing for {dep} {sorted(declared)}; "
                             f"nothing committed, {cleanup}")
            seen = _declared_resolutions(new_lock, dep, declared)
            if seen != want:
                cleanup = _remove_derived_tree(base_worktree,
                                               new_path)
                return _fail(f"leaf '{leaf['batch']}' {dep} resolves "
                             f"{seen} in rollup, expected {want} "
                             f"(its own lock); nothing committed, "
                             f"{cleanup}")
            resolutions[dep] = want
        changes = _rollup_manifest_moves(str(new_path), base_worktree,
                                         leaves)
        leaf_lines = []
        for leaf in leaves:
            dep = leaf["dep"]
            moves = changes.get(dep) or {}
            if moves:
                moves_s = "; ".join(
                    f"{mf} {frm} → {to}"
                    for mf, (frm, to) in sorted(moves.items()))
            else:
                moves_s = f"{leaf['target']} (lock refresh)"
            leaf_lines.append((leaf, moves_s))
        msg_lines = [f"chore(deps): {lane} lane rollup "
                     f"({len(leaves)} leaves onto {base_batch})", "",
                     "ROLLUP SOURCE: verified leaf batches replayed "
                     f"onto base {base_head[:12]}; lockfile regenerated "
                     f"once with {' '.join(argv)}.",
                     "Never equals any single candidate."]
        for leaf, moves_s in leaf_lines:
            msg_lines.append(f"- {leaf['dep']}: {moves_s} "
                             f"[{leaf['batch']} {leaf['head'][:12]}]")
        committed = _run_git(["-c", "user.name=openmainty",
                              "-c", "user.email=openmainty@localhost",
                              "commit", "-m", "\n".join(msg_lines)],
                             str(new_path))
        if committed.returncode != 0:
            cleanup = _remove_derived_tree(base_worktree, new_path)
            tail = _scrub_auth(committed.stderr or committed.stdout
                               or "").strip()[-300:]
            return _fail(f"rollup commit failed: {tail}; {cleanup}")
        res_snap = _snapshot(str(new_path))
        if (res_snap is None or not res_snap.get("head")
                or not res_snap.get("fingerprint")
                or not res_snap.get("clean")):
            return _fail("rollup source unreadable or dirty after "
                         "commit")
        lang, lang_src = _pr_language(repo)
        # M2.24: leaves that drifted between the readiness check
        # and the merge (usually none — they just passed). Reuses
        # the readiness evaluations, never a fresh evaluate.
        completion_caveats = _rollup_leaf_caveats(
            repo, leaves,
            _rollup_eval_states(
                repo,
                {b.get("id"): b for b in [base] + leaf_batches},
                leaf_evals))
        title, body, commands = _rollup_pr_draft(
            repo, lane, base_batch, leaves, changes, branch,
            res_snap["head"], op_id, new_path=new_path,
            base_head=base_head, gates=rec["gates"], lang=lang,
            lang_src=lang_src, caveats=completion_caveats)
        stack = _rollup_stack(new_path, lane, base_batch, base_head,
                              res_snap["head"])
        total_commits = sum(len(rows) for _, rows, _
                            in stack["groups"])
        n_unrel = len(stack["unreleased"])
        rec["regenerated_by"] = {"tool": argv[0], "argv": argv,
                                 "rc": 0,
                                 "tool_version": tool_version,
                                 "registry_host": registry_host,
                                 "mode": "update-lockfile",
                                 "env_flags": list(RERESOLVE_ENV_FLAGS),
                                 "command_origin": f"rollup-{origin}",
                                 "resolutions": resolutions,
                                 "lock_only_dropped": lock_only,
                                 "manifest_reapplied": manifest_reapplied}
        # M1 step receipt: what ran, its pinned log, and the fact
        # the env was scrubbed (key names only, never values).
        rec["regen_receipt"] = dict(
            _write_regen_log(op_id, regen.stdout or ""),
            argv=argv, rc=0, registry_host=registry_host,
            tool_version=tool_version,
            env_keys=sorted(regen_env),
            scripts_off=(regen_env.get("YARN_ENABLE_SCRIPTS")
                         == "0"))
        rec["result"]["head"] = res_snap["head"]
        rec["result"]["fingerprint"] = res_snap["fingerprint"]
        rec["result"]["proof_note"] = ("ROLLUP SOURCE: fresh merge of "
                                       f"{len(leaves)} leaf batches; "
                                       "no leaf proof applies")
        rec["pr_draft"] = {"title": title, "body": body,
                           "stack_line":
                               f"기준 묶음 {n_unrel}개 포함 · 전체 "
                               f"{total_commits} commits · 선택: main 체인 "
                               "먼저 / next 한 번에" if stack["target_head"]
                               else ""}
        stacked = _rollup_stacked_drafts(
            repo=repo, lane=lane, base_id=base_batch,
            base_head=base_head, head=res_snap["head"], leaves=leaves,
            changes=changes, branch=branch, op_id=op_id,
            new_path=new_path, gates=rec["gates"], lang=lang,
            lang_src=lang_src, supersedes=supersedes)
        if isinstance(supersedes, dict) and supersedes.get("op"):
            rec["supersedes"] = supersedes.get("op")
        rec["ship_order"] = stacked["ship_order"]
        rec["stacked_drafts"] = stacked
        rec["owner_commands"] = commands
        rec["status"] = "done"
        rec["error"] = ""
        rec["updated_at"] = utcnow()
        for gate in rec["gates"]:
            if gate["key"] == "source-prepared":
                gate["status"] = "pass"
                gate["note"] = (f"합침: {branch} @ "
                                f"{res_snap['head'][:12]} fp "
                                f"{res_snap['fingerprint'][:12]} "
                                f"({len(leaves)}개 묶음 합침)")
        write_json(opf, rec)
        try:
            refresh_all_snapshots("rollup")
        except Exception:  # noqa: BLE001 - glance refresh never masks
            pass
        # M2.24: the result view carries caveats; the stored file
        # stays as written above (display never rewrites the op).
        rec["caveats"] = completion_caveats
        return rec
    except Exception as exc:
        _record_blocked(opf, rec,
                        f"interrupted: {type(exc).__name__}: {exc}"
                        [:1000])
        raise


# ---- Source-bound followup corrections ------------------------------------
# A followup applies an operator's correction request to a PREPARED
# integration worktree (never the original candidate, never a base batch,
# never a user checkout). The endpoint accepts only a registered operation
# id plus the request text: workspace, branch, and expected source all
# derive from the recorded operation, and the live source must still match
# it (head + full fingerprint + source-aware clean) or the request is
# refused before anything runs.
#
# Execution reuses the existing subscription worker (muse.exec_prompt,
# contributor, same model family) with a source-only prompt: read/search/
# edit inside the worktree, lockfile regeneration through the repo's own
# tooling, and fastchecks (typecheck/lint/test) are allowed; native
# builds, device install/record/playback, publishing, and anything outside
# the worktree are forbidden. The model outcome is committed locally on
# the operation's own branch (hooks on, exact provenance in the message)
# only when it changed tracked source; failures and cancellations record
# blocked/cancelled with the partial state preserved for diagnosis. One
# followup runs per operation at a time; the global worker lease (held by
# the caller across the whole job) keeps contention truthful.

FOLLOWUP_MAX_CHARS = 2000
FOLLOWUP_LOG_CAP = 20000


def _load_op(op_id: str) -> tuple:
    opf = op_file(op_id)
    rec = read_json(opf, default=None)
    if not isinstance(rec, dict) or rec.get("op_id") != op_id:
        raise IntegrationError(f"unknown integration operation {op_id!r}",
                               http_status=404)
    return opf, rec


def _assert_issue_op_binding_current(repo: str, rec: dict) -> dict:
    """Shared register/binder gate for a prepared typed-issue op.

    The op's recorded original-candidate binding and canonical issue
    origin URL must still match the CURRENT queue row, run, profile
    and live candidate bytes. A changed GitHub profile or payload
    cannot rebind an already prepared op, and a stale original proof
    cannot bypass via a pending primary. Read-only; raises
    IntegrationError on any drift. Callers under the op lock translate
    to BatchError for import paths.
    """
    run_id = str(rec.get("run_id") or "")
    ctx = _resolve_issue_candidate(repo, run_id)
    rblk = rec.get("issue")
    if not isinstance(rblk, dict) or not rblk:
        raise IntegrationError("prepared op has no typed issue binding")
    if ctx["number"] != _strict_issue_number(rblk.get("issue")) \
            or ctx["lane"] != str(rec.get("lane") or ""):
        raise IntegrationError("issue op identity moved since prepare: "
                               "number/lane mismatch")
    if str(rblk.get("repo") or "") != repo:
        raise IntegrationError("issue op binding repo mismatch")
    cand = rec.get("candidate")
    if not isinstance(cand, dict) or not cand:
        raise IntegrationError("issue op binding has no original candidate")
    try:
        run = store.load(run_id)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - unreadable refuses
        raise IntegrationError(f"issue op run unreadable: {exc}") from exc
    if not isinstance(run, dict) or not run:
        raise IntegrationError("issue op run is not a stored run")
    orig_head = str(run.get("workCommit") or "")
    orig_fp = str(run.get("workTreeFingerprint") or "")
    try:
        canon_wt = str(store.run_dir(run_id) / "work")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - refuses
        raise IntegrationError("issue op candidate path unresolvable: "
                               f"{exc}") from exc
    if str(cand.get("head") or "") != orig_head \
            or str(cand.get("fingerprint") or "") != orig_fp \
            or str(cand.get("worktree") or "") != canon_wt \
            or str(cand.get("branch") or "") != ctx["branch"]:
        raise IntegrationError("issue op original candidate moved since "
                               "prepare")
    _, _, proof_err = _candidate_proof(run, run_id)
    if proof_err:
        raise IntegrationError(
            f"issue op original proof not fresh: {proof_err}")
    if str(rblk.get("url") or "") != ctx["url"]:
        raise IntegrationError("issue origin changed since prepare; a "
                               "changed GitHub profile cannot rebind a "
                               "prepared op")
    return ctx


def _check_op_source(rec: dict) -> dict:
    """Live prepared source must match the record, or refuse.

    Head + full fingerprint + source-aware clean AND the actual checked
    out named branch: a branch switch (or detached HEAD) is rejected,
    never silently adopted.
    """
    res = rec.get("result") or {}
    path = str(res.get("path") or "")
    want_branch = str(res.get("branch") or "")
    if not path or rec.get("status") != "done" or not want_branch:
        raise IntegrationError("operation has no prepared source to correct")
    snap = _snapshot(path)
    if (snap is None or snap.get("head") != (res.get("head") or "")
            or snap.get("fingerprint") != (res.get("fingerprint") or "")
            or not snap.get("clean")):
        raise IntegrationError("prepared source drifted or unreadable; "
                               "operator review required, nothing started")
    live_branch = _git_text(["branch", "--show-current"], path)
    if live_branch != want_branch:
        raise IntegrationError("worktree branch is "
                               f"{live_branch or 'unknown/detached'}, "
                               f"expected {want_branch}: branch switch "
                               "rejected, nothing started")
    return {"path": path, "head": snap["head"],
            "fingerprint": snap["fingerprint"], "branch": want_branch}


def _terminal_report(raw_text: str) -> str:
    """Human-readable final summary from a raw exec JSONL stream.

    Only the terminal run_terminal text is surfaced; incremental deltas
    stay in the registered raw log. Never invent a summary when the
    stream carries none.
    """
    final = ""
    for line in (raw_text or "").splitlines():
        kind, text = muse.parse_delta_line(line)
        if kind == "terminal" and text:
            final = text
    return final


def _retry_base(rec: dict) -> dict | None:
    """Match live source against the latest blocked attempt's after-state.

    Returns the retry base (path/head/fingerprint/branch/retry_of) on an
    exact match, else None. The match must be byte-exact (head +
    fingerprint) on the recorded branch; a clean tree is NOT required
    because the retained staged work is precisely what the retry
    resumes. Never invents a base.
    """
    res = rec.get("result") or {}
    path = str(res.get("path") or "")
    branch = str(res.get("branch") or "")
    if not path or not branch:
        return None
    blocked = [e for e in (rec.get("followups") or [])
               if isinstance(e, dict) and e.get("status") == "blocked"
               and isinstance(e.get("after"), dict)
               and (e.get("after") or {}).get("head")]
    if not blocked:
        return None
    after = blocked[-1]["after"]
    snap = _snapshot(path)
    if (snap is None or snap.get("head") != (after.get("head") or "")
            or snap.get("fingerprint") != (after.get("fingerprint") or "")):
        return None
    if _git_text(["branch", "--show-current"], path) != branch:
        return None
    return {"path": path, "head": snap["head"],
            "fingerprint": snap["fingerprint"], "branch": branch,
            "retry_of": blocked[-1].get("fid") or ""}


def request_followup(op_id: str, request_text: str) -> tuple:
    """Validate and queue one correction request. No model call here."""
    if not isinstance(request_text, str) or not request_text.strip():
        raise IntegrationError("correction request text is required",
                               http_status=400)
    text = request_text.strip()
    if len(text) > FOLLOWUP_MAX_CHARS:
        raise IntegrationError(f"correction request exceeds "
                               f"{FOLLOWUP_MAX_CHARS} characters",
                               http_status=400)
    opf, rec = _load_op(op_id)
    try:
        live = _check_op_source(rec)
        retry_of = ""
    except IntegrationError as strict_err:
        # Controlled retry: the live tree may exactly match a recorded
        # FAILED attempt's after-state (retained staged work, same
        # branch). Anything else is drift and stays refused.
        live = _retry_base(rec)
        if live is None:
            raise strict_err
        retry_of = live.pop("retry_of")
    active = rec.get("active_followup") or ""
    if active:
        # The caller holds the exclusive worker lease, so a running entry
        # cannot be live: it is orphaned by a lost worker. Settle it
        # explicitly blocked for operator audit, never silently dropped.
        orphan = next((e for e in (rec.get("followups") or [])
                       if isinstance(e, dict) and e.get("fid") == active
                       and e.get("status") == "running"), None)
        if orphan is not None:
            orphan["status"] = "blocked"
            orphan["error"] = ("orphaned mid-execution: the previous "
                               "worker never reported back; held for "
                               "operator audit, no source claimed")
            orphan["updated_at"] = utcnow()
            rec["active_followup"] = ""
            rec["updated_at"] = utcnow()
            write_json(opf, rec)
        else:
            raise IntegrationError("a followup is already queued on this "
                                   "operation")
    import secrets as _secrets
    fid = f"fu-{_secrets.token_hex(4)}"
    entry = {"fid": fid, "request": text, "status": "queued",
             "before": live, "after": {},
             "exit_code": None, "usage": {}, "error": "",
             "retry_of": retry_of,
             "created_at": utcnow(), "updated_at": utcnow()}
    rec.setdefault("followups", []).append(entry)
    rec["active_followup"] = fid
    rec["updated_at"] = utcnow()
    write_json(opf, rec)
    return rec, fid


def _followup_prompt(rec: dict, entry: dict, live: dict) -> str:
    return (
        "You are doing a source-only correction inside one prepared "
        "integration worktree. Worktree: "
        f"{live['path']} (branch {live.get('branch')}, base "
        f"{live['head'][:12]}). Do not touch anything outside it.\n"
        f"Operator request: {entry['request']}\n"
        "Allowed: read/search/edit tracked source in the worktree; "
        "regenerate lockfiles only through the repo's own documented "
        "tooling (pod install to sync Podfile.lock, never hand-edit "
        "version pins, never pod update, never touch Gemfile or "
        "Gemfile.lock); run fastchecks only "
        "(typecheck/lint/unit test as the repo defines them).\n"
        "Ruby tooling: use the existing Homebrew Ruby through a scoped "
        "PATH export (/opt/homebrew/opt/ruby/bin) inside your own "
        "commands only; never change the global Ruby default. If the "
        "toolchain is missing, report it and stop instead of working "
        "around it.\n"
        "Forbidden: git checkout/switch, rebase, merge, reset, clean, "
        "push, any protected-ref change, and making commits yourself "
        "(the backend owns the commit); native builds "
        "(xcodebuild/gradle), run-ios/run-android,"
        " adb/simctl/maestro, physical devices, simulators/emulators, "
        "publishing, public writes, network beyond package/spec fetches.\n"
        "Finish with: FILES CHANGED (exact paths + why), HOW VERIFIED "
        "(exact commands + results), and what remains for device QA. "
        "If you changed nothing, say NO CHANGE explicitly. "
        "Do not claim device/native results.")


def _stageable_paths(path: str) -> list:
    """Tracked-modified + relevant untracked source paths only.

    Mirrors the source-aware noise contract (_source_clean): reserved
    generated/cache paths and git-ignored files are never staged, so
    preexisting build noise (example/dist, caches) stays in the worktree
    and out of the commit. A broad `git add -u` is deliberately not
    used here.
    """
    lines = _git_lines(["-c", "core.quotePath=false", "status",
                        "--porcelain=v1", "--untracked-files=all"], path)
    if lines is None:
        return []
    out = []
    for line in lines:
        if len(line) < 4:
            continue
        xy, rel = line[:2], line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1].strip().strip('"')
        if runner._noise_rel(rel):
            continue
        if _git_text(["check-ignore", "-q", "--", rel], path) is not None:
            continue
        out.append(rel)
    return sorted(set(out))


def _commit_followup(path: str, branch: str, op_id: str, fid: str,
                     request_text: str, before_head: str) -> dict:
    """Local commit of the followup's relevant changes (hooks on).

    Only the operation's own branch, verified before and after; build
    noise is never staged. A model self-commit is detected (HEAD already
    advanced): adopted only when it sits on the recorded branch with a
    clean validated tree, else blocked.
    """
    if _git_text(["branch", "--show-current"], path) != branch:
        return {"committed": False,
                "reason": "branch changed during followup; commit refused"}
    if _git_head(path, "HEAD") != before_head:
        adopted = _snapshot(path)
        if (adopted is not None and adopted.get("clean")
                and _git_text(["branch", "--show-current"], path)
                == branch):
            return {"committed": False, "adopted": True,
                    "head": adopted["head"],
                    "fingerprint": adopted["fingerprint"],
                    "reason": "model self-committed; validated head "
                    "adopted on the recorded branch"}
        return {"committed": False,
                "reason": "model committed off-branch or left a dirty "
                "tree; operator review required"}
    stage = _stageable_paths(path)
    if not stage:
        return {"committed": False, "reason": "no changes"}
    if _run_git(["add", "--"] + stage, path).returncode != 0:
        return {"committed": False, "reason": "git add failed"}
    if _run_git(["diff", "--cached", "--quiet"], path).returncode == 0:
        return {"committed": False, "reason": "no changes"}
    subject = f"chore(integration): followup {fid} {op_id}"
    # commitlint footer-max-line-length (100): wrap every body line
    # while preserving the ids. Hooks stay OFF (_GIT_NO_HOOKS) — the
    # message is commitlint-shaped by construction instead.
    import textwrap as _tw
    raw_lines = [f"request: {request_text[:200]}",
                 f"before: {before_head}", f"branch: {branch}"]
    wrapped = []
    for line in raw_lines:
        wrapped.extend(_tw.wrap(line, width=100, break_long_words=True,
                                break_on_hyphens=False) or [""])
    body = "\n".join(wrapped)
    proc = _run_git(["-c", "user.name=openmainty",
                     "-c", "user.email=openmainty@localhost",
                     "commit", "-m", subject, "-m", body], path)
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        return {"committed": False, "reason": "git commit failed",
                "output": detail[-2000:]}
    if _git_text(["branch", "--show-current"], path) != branch:
        return {"committed": False,
                "reason": "branch changed during commit; operator review "
                "required"}
    head = _git_head(path, "HEAD")
    snap = _snapshot(path)
    if (snap is None or snap.get("head") != head
            or not snap.get("fingerprint") or not snap.get("clean")):
        return {"committed": False,
                "reason": "integrated source unreadable or dirty after "
                "commit"}
    return {"committed": True, "head": head,
            "fingerprint": snap["fingerprint"]}


def abandon_queued_followup(op_id: str, fid: str, reason: str) -> dict:
    """Roll back a queued (never started) followup, e.g. no job slot."""
    opf, rec = _load_op(op_id)
    rec["followups"] = [e for e in (rec.get("followups") or [])
                        if not (isinstance(e, dict)
                                and e.get("fid") == fid
                                and e.get("status") == "queued")]
    if rec.get("active_followup") == fid:
        rec["active_followup"] = ""
    rec["updated_at"] = utcnow()
    write_json(opf, rec)
    rec["abandoned"] = reason[:200]
    return rec


def execute_followup(op_id: str, fid: str, cancel_event=None) -> dict:
    """Run one queued followup to completion (called with lease held)."""
    from .util import Cancelled
    opf, rec = _load_op(op_id)
    entries = [e for e in (rec.get("followups") or [])
               if isinstance(e, dict) and e.get("fid") == fid]
    if not entries:
        raise IntegrationError(f"unknown followup {fid!r}",
                               http_status=404)
    entry = entries[0]
    if entry.get("status") not in ("queued",):
        return rec
    if entry.get("retry_of"):
        # Retry resumes the exact registered failed after-state: byte
        # match (head + fingerprint) on the recorded branch; clean is
        # not required because retained staged work is the resume point.
        base = entry.get("before") or {}
        snap_now = _snapshot(str(base.get("path") or ""))
        live_branch = _git_text(
            ["branch", "--show-current"], str(base.get("path") or ""))
        if (snap_now is None
                or snap_now.get("head") != (base.get("head") or "")
                or snap_now.get("fingerprint") != (base.get("fingerprint")
                                                  or "")
                or live_branch != ((rec.get("result") or {}).get("branch")
                                   or "")):
            raise IntegrationError("retry base moved since the request; "
                                   "operator review required, nothing "
                                   "started")
        live = {"path": str(base.get("path") or ""),
                "head": snap_now["head"],
                "fingerprint": snap_now["fingerprint"],
                "branch": live_branch}
    else:
        live = _check_op_source(rec)
    entry["status"] = "running"
    entry["updated_at"] = utcnow()
    rec["updated_at"] = utcnow()
    write_json(opf, rec)
    log_path = integration_ops_dir() / f"{op_id}-{fid}.log.txt"
    try:
        prompt = _followup_prompt(rec, entry, live)
        exit_code, text, usage = muse.exec_prompt(
            prompt, workdir=live["path"], contributor=True, timeout=1800,
            model=None, cancel_event=cancel_event)
        entry["exit_code"] = exit_code
        entry["usage"] = ({k: usage.get(k) for k in
                           ("model", "provider_model", "session_id",
                            "request_id", "response_id", "cli_version",
                            "exit_code", "stream_bytes")
                           } if isinstance(usage, dict) else {})
        entry["token_note"] = ("subscription billing: tokens "
                               "unmeasurable, no token numbers recorded")
        try:
            log_path.write_text(text or "", encoding="utf-8")
            entry["has_log"] = True
        except OSError:
            entry["has_log"] = False
        report = _terminal_report(text or "")
        entry["report"] = report
        entry["log_tail"] = report[-FOLLOWUP_LOG_CAP:] if report else ""
        commit = {"committed": False, "reason": "model step failed"}
        if exit_code == 0:
            commit = _commit_followup(
                live["path"], live.get("branch", ""),
                op_id, fid, entry["request"], live["head"])
            if commit.get("adopted"):
                res_adopt = rec.setdefault("result", {})
                res_adopt["head"] = commit["head"]
                res_adopt["fingerprint"] = commit["fingerprint"]
        entry["commit"] = commit
        after = _snapshot(live["path"])
        entry["after"] = {"head": (after or {}).get("head") or "",
                         "fingerprint": (after or {}).get("fingerprint")
                         or "",
                         "clean": bool((after or {}).get("clean"))}
        res = rec.setdefault("result", {})
        if commit.get("committed"):
            res["head"] = commit["head"]
            res["fingerprint"] = commit["fingerprint"]
        if exit_code == 0 and (commit.get("committed")
                               or commit.get("adopted")
                               or commit.get("reason") == "no changes"):
            gate_status = "pass"
            entry["status"] = "done"
            entry["error"] = ""
            if commit.get("reason") == "no changes":
                entry["note"] = ("worker finished with no source change; "
                                 "verification still pending, nothing "
                                 "proven fixed")
        else:
            gate_status = "blocked"
            entry["status"] = "blocked"
            err = (f"model exit {exit_code}; "
                   f"{commit.get('reason') or ''}").strip()
            if commit.get("output"):
                err += "\n" + str(commit["output"])[-800:]
            entry["error"] = err[:1500]
        rec.setdefault("gates", []).append(
            {"key": f"correction-{fid}", "status": gate_status,
             "note": (entry["error"] or
                      f"{res.get('branch', '')} @ "
                      f"{str(res.get('head') or '')[:12]}")[:300]})
    except Cancelled:
        entry["status"] = "cancelled"
        entry["error"] = "cancelled by operator"
        entry["updated_at"] = utcnow()
        rec["active_followup"] = ""
        rec["updated_at"] = utcnow()
        write_json(opf, rec)
        raise
    except IntegrationError as exc:
        # A late validation failure (source moved mid-run) must also
        # settle the running entry instead of stranding it.
        entry["status"] = "blocked"
        entry["error"] = str(exc)[:1000]
        entry["updated_at"] = utcnow()
        rec["active_followup"] = ""
        rec["updated_at"] = utcnow()
        write_json(opf, rec)
        raise
    except Exception as exc:
        entry["status"] = "blocked"
        entry["error"] = (f"interrupted: {type(exc).__name__}: {exc}"
                          )[:1000]
        entry["updated_at"] = utcnow()
        rec["active_followup"] = ""
        rec["updated_at"] = utcnow()
        write_json(opf, rec)
        raise
    entry["updated_at"] = utcnow()
    if rec.get("active_followup") == fid:
        rec["active_followup"] = ""
    rec["updated_at"] = utcnow()
    write_json(opf, rec)
    return rec


# ---- M1 glance snapshot ---------------------------------------------------
# A precomputed per-repo summary served verbatim by GET /api/summary.
# Verdicts are a deterministic ladder with stored rule ids; every line is
# assembled from fixed Korean templates with counts only.

M1_VERDICT_KO = {"blocked": "막힘", "unknown": "미확인",
                 "attention": "주의", "normal": "정상"}


def m1_verdict(*, ready, blocked, questions, unreadable,
               waiting_review, pending_release,
               days_since_release, issue_waiting=0) -> dict:
    """Pure verdict ladder. First match wins; rule id is stored.

    blocked>0 -> 막힘. Any unreadable input -> 미확인 (정상 is forbidden
    unless every input evaluated clean). Open questions, waiting reviews
    (dep candidates or issue-triage, counted separately so nothing is
    counted twice), or a stale release (30+ days with pending items) ->
    주의. Else 정상. `unreadable` is a list of input names; `blocked`
    counts blocked items.
    """
    unreadable = [str(x) for x in (unreadable or []) if str(x)]
    base = (f"검증된 업데이트 {ready}건이 출시 대기 중, "
            f"검토 대기 {waiting_review}건, "
            f"이슈 검토 대기 {issue_waiting}건, 막힌 것 {blocked}, "
            f"당신의 답이 필요한 질문 {questions}건")
    if blocked:
        return {"verdict": "blocked", "rule": "blocked-items",
                "line": f"막힘 — {base}"}
    if unreadable:
        shown = ", ".join(unreadable[:3])
        more = f" 외 {len(unreadable) - 3}건" if len(unreadable) > 3 \
            else ""
        return {"verdict": "unknown", "rule": "unreadable-inputs",
                "line": f"미확인 — {shown}{more} 읽기에 실패해 판단 보류"}
    rule = ""
    if questions:
        rule = "open-questions"
    elif waiting_review or issue_waiting:
        rule = "waiting-review"
    elif pending_release and days_since_release is not None \
            and days_since_release >= 30:
        rule = "stale-release"
    if rule:
        return {"verdict": "attention", "rule": rule,
                "line": f"주의 — {base}"}
    if ready:
        return {"verdict": "normal", "rule": "ready-and-clear",
                "line": f"정상 — {base}"}
    return {"verdict": "normal", "rule": "nothing-pending",
            "line": "정상 — 새로 들어온 업데이트 없음, 막힌 것 0"}


_CHOICE_JOIN_RE = re.compile(
    r"또는|\bor\b|\bvs\b|중에서|택일", re.IGNORECASE)
_WHICH_RE = re.compile(
    r"어느|어떤|which|고를|선택|choose|pick|결정", re.IGNORECASE)


def m1_suggest(question: str) -> dict:
    """Suggestion only for an explicit version CHOICE: at least two
    distinct versions joined by an either/or word (또는/or/vs), asking
    which one. Anything else — a repro-info request that merely mentions
    versions, a yes/no question, free prose — gets no suggestion text,
    because a wrong suggestion is worse than none. Returns {text,
    basis}; empty text means no suggestion (the item links to the run
    instead)."""
    text = str(question or "")
    versions = set(re.findall(r"\d+\.\d+\.\d+", text))
    if len(versions) >= 2 \
            and _CHOICE_JOIN_RE.search(text) \
            and _WHICH_RE.search(text):
        def _key(v):
            return tuple(int(x) for x in v.split("."))
        newest = sorted(versions, key=_key)[-1]
        return {"text": newest,
                "basis": "질문의 선택지 중 최신 버전입니다. "
                         "보내기 전에 꼭 확인하세요."}
    return {"text": "", "basis": ""}


def m1_question_age_days(run: dict, now_ts: float) -> int | None:
    """Whole days since the run's last update (question first-seen
    proxy). None when no timestamp is recorded — callers must print
    '날짜 미기록', never invent an age."""
    import datetime
    for key in ("updated_at", "created_at"):
        raw = str((run or {}).get(key) or "")
        if not raw:
            continue
        try:
            dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            break
        except ValueError:
            continue
    else:
        return None
    try:
        delta = now_ts - dt.timestamp()
    except (OverflowError, OSError):
        return None
    if delta < 0:
        return 0
    return int(delta // 86400)


_TITLE_KO = {"superseded:": "대체됨:", "skipped:": "건너뜀:",
               "resumed:": "다시 시작:", "cancelled:": "취소됨:",
               "failed:": "실패:", "error:": "오류:",
               "timeout:": "시간 초과:", "retry:": "다시 시도:"}

_DEP_BUMP_RE = re.compile(r"(\S+)\s+(\S+)\s*->\s*(\S+)\s*$")


def _m1_ko_title(title: str) -> str:
    """Translate a leading English system word (`superseded: ...`) so
    raw prefixes never reach the home page. Unknown words pass through
    (a partial translation beats a dropped line)."""
    text = str(title or "").strip()
    head, sep, rest = text.partition(":")
    if sep and " " not in head and head.strip().lower() + ":" in _TITLE_KO:
        return _TITLE_KO[head.strip().lower() + ":"] + rest
    return text


def m1_event_sentence(kind: str, title: str, detail: str = "") -> str:
    """One plain-Korean story sentence for a stored event. Titles (dep
    names, versions) pass through with leading system words translated;
    system wording is translated here so raw English never reaches the
    home page."""
    kind = str(kind or "")
    title = _m1_ko_title(title)
    # Kinds below add their own Korean prefix; drop a translated leading
    # prefix from the title so it never doubles ("취소됨: 대체됨: …").
    for _prefix in _TITLE_KO.values():
        if title.startswith(_prefix):
            title = title[len(_prefix):].strip()
            break
    if kind == "scan":
        return ""
    if kind == "run-done":
        bump = _DEP_BUMP_RE.search(title or "")
        if bump:
            dep, old, new = bump.group(1), bump.group(2), bump.group(3)
            return (f"{dep} {old} → {new} 검증 끝냄"
                    + (" (근거 있음)" if detail else ""))
        return f"Muse가 {title or '실행'} 끝냄" + (" (근거 있음)" if detail else "")
    if kind == "run-error":
        return f"실행 실패: {title or '이유 미기록'}"
    if kind == "run-retry":
        return f"다시 시도함: {title or ''}".rstrip()
    if kind == "run-no-code":
        return f"코드 변경 없이 끝남: {title or ''}".rstrip()
    if kind == "question":
        return f"질문 올라옴: {title}"
    if kind == "batch-ready":
        return f"{title} 묶음 준비됨"
    if kind == "rollup-done":
        return f"레인 합치기 마침: {title}"
    if kind == "rollup-blocked":
        return f"레인 합치기 막힘: {title}"
    if kind == "import-done":
        return f"묶음 가져옴: {title}"
    if kind == "cancelled":
        return f"취소됨: {title}"
    return ""


_SCAN_TITLE_RE = re.compile(
    r"scan:\s*(\d+)\s*deps,\s*(\d+)\s*drift,\s*(\d+)\s*lock-refresh")

_KST = None


def _m1_kst_short(at: str) -> str:
    """One timestamp as 'MM/DD HH:MM' in KST (fixed UTC+9, no DST).

    Story lines are pre-rendered server-side, so the client cannot
    localize them; the owner reads in KST like the rest of the app.
    Unparseable input passes through raw — never dropped."""
    global _KST
    dt = _m1_git_date(at)
    if dt is None:
        return str(at or "")
    try:
        import datetime
        if _KST is None:
            _KST = datetime.timezone(datetime.timedelta(hours=9))
        return dt.astimezone(_KST).strftime("%m/%d %H:%M")
    except Exception:  # noqa: BLE001 - clock failure reads raw
        return str(at or "")


def m1_collapse_scans(newest_first: list) -> list:
    """Fold consecutive no-change scans (server-side, at write time).

    `newest_first` holds (counts_tuple_or_None, at, has_change,
    found_text) in newest-first order. A run of 2+ scans with equal
    parseable counts and no change folds into one line 'MM/DD HH:MM~
    MM/DD HH:MM, N회 점검, 변화 없음' (KST span, oldest~newest, so two
    folded runs around a finding stay distinguishable; same-day spans
    print the date once). A scan with
    a finding breaks the run and renders found_text ('새 업데이트 감지:
    <dep> <ver>[ 외 N건]') or a generic line when the detail names
    nothing. An unparseable scan with no finding renders '점검 기록을
    읽지 못함 (MM/DD HH:MM)' rather than claiming no change. The
    `changed` flag is computed by the caller against the previous scan
    (counts differ, detail set differs, or +N queued with N>0) — a
    non-empty detail alone never means changed, because real scans
    repeat the same drift list every tick. Output keeps the traversal
    (newest-first) order.
    """
    out = []
    run = []

    def _flush():
        if len(run) >= 2:
            oldest, latest = run[-1][1], run[0][1]
            first, last = _m1_kst_short(oldest), _m1_kst_short(latest)
            day, _, clock = last.partition(" ")
            if first.startswith(day + " "):
                span = f"{first}~{clock}"
            else:
                span = f"{first}~{last}"
            out.append(f"{span}, {len(run)}회 점검, 변화 없음")
        else:
            out.extend(f"점검함 ({_m1_kst_short(at)})" for _, at, _ in run)
        del run[:]

    for counts, at, changed, found in newest_first:
        if counts is not None and not changed:
            if run and run[0][0] != counts:
                _flush()
            run.append((counts, at, found))
            continue
        _flush()
        if changed:
            out.append(found or f"점검에서 변화 발견 ({_m1_kst_short(at)})")
        elif counts is None:
            out.append(f"점검 기록을 읽지 못함 ({_m1_kst_short(at)})")
    _flush()
    return out


_COUNT_NAMES = ("의존성", "드리프트", "잠금 재생성")


def _m1_detail_parts(detail: str) -> list:
    return [p.strip() for p in str(detail or "").split(";") if "->" in p]


def _m1_scan_found(detail: str, prev_dset=frozenset(), queued: int = 0,
                   counts=None, prev_counts=None) -> str:
    """Name only what is new in this scan versus the previous one.

    Added detail entries first ('새 업데이트 감지: <dep> <ver>[ 외 N건]',
    in detail order). When only the counters moved, say which
    ('드리프트 43→44'). When only +N queued moved, say so
    ('대기 N건'). Empty when nothing nameable changed."""
    parts = _m1_detail_parts(detail)
    prev = set(prev_dset or ())
    added = [p for p in parts if p not in prev]
    if added:
        first = added[0].split("->", 1)
        dep = first[0].strip()[:40]
        ver = first[1].strip()[:20] if len(first) > 1 else ""
        line = f"새 업데이트 감지: {dep} {ver}".rstrip()
        if len(added) > 1:
            line += f" 외 {len(added) - 1}건"
        return line
    if counts is not None and prev_counts is not None \
            and tuple(counts) != tuple(prev_counts):
        moves = []
        for name, old, new in zip(_COUNT_NAMES, prev_counts, counts):
            try:
                if int(old) != int(new):
                    moves.append(f"{name} {old}→{new}")
            except (TypeError, ValueError):
                continue
        if moves:
            return "점검에서 변화 발견: " + ", ".join(moves)
    if queued:
        return f"대기 {queued}건"
    return ""


_QUEUED_RE = re.compile(r"\+(\d+)\s+queued")


def _m1_git_date(text: str):
    """Parse one git %cI date on 3.9+ (trailing Z normalized). None when
    unparseable — callers treat it as unknown, never as zero."""
    import datetime
    iso = str(text or "")
    iso = iso[:-1] + "+00:00" if iso.endswith(("Z", "z")) else iso
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def m1_release_info(checkout: str) -> dict:
    """Branch activity + newest release tag in a target checkout.

    Shows 'main 마지막 반영 X · next Y' separately (these are branch
    tips, NOT releases) and, when the repo has tags, the newest tag by
    creation date labeled as the release. Failures read as unknown —
    callers print '미확인', and the stale-release rule cannot fire
    without a date."""
    out = {"main_head": "", "main_date": None, "next_head": "",
           "next_date": None, "main_days": None, "next_days": None,
           "release_tag": "", "release_tag_date": None,
           "last_release": None, "basis": "unknown",
           "days_since_release": None}
    if not checkout:
        return out
    for branch, hk, dk in (("main", "main_head", "main_date"),
                           ("next", "next_head", "next_date")):
        proc = _run_git(["log", "-1", "--format=%H %cI", branch],
                        checkout)
        if proc.returncode != 0:
            continue
        parts = (proc.stdout or "").strip().split()
        if len(parts) >= 2:
            out[hk] = parts[0]
            out[dk] = parts[1]
    tags = _run_git(["tag", "--list", "--sort=-creatordate"], checkout)
    if tags.returncode == 0:
        names = [t for t in (tags.stdout or "").split() if t]
        if names:
            out["release_tag"] = names[0]
            dated = _run_git(["log", "-1", "--format=%cI", names[0]],
                             checkout)
            if dated.returncode == 0:
                stamp = (dated.stdout or "").strip().split()
                if stamp:
                    out["release_tag_date"] = stamp[0]
    import datetime
    basis_date = out["release_tag_date"]
    basis = "tag" if basis_date else "unknown"
    if not basis_date:
        branch_dates = [d for d in (out["main_date"], out["next_date"])
                        if d]
        if branch_dates:
            basis_date = max(branch_dates)
            basis = "branches"
    if not basis_date:
        return out
    out["last_release"] = basis_date[:10]
    out["basis"] = basis
    dt = _m1_git_date(basis_date)
    if dt is None:
        out["last_release"] = None
        out["basis"] = "unknown"
        return out
    now = datetime.datetime.now(datetime.timezone.utc)
    out["days_since_release"] = max(0, int((now - dt).total_seconds()
                                           // 86400))
    for _dk, _dayk in (("main_date", "main_days"),
                       ("next_date", "next_days")):
        _raw = out.get(_dk)
        _dtb = _m1_git_date(_raw) if _raw else None
        out[_dayk] = max(0, int((now - _dtb).total_seconds() // 86400)) \
            if _dtb else None
    return out


def m1_rollup_status(repo: str, batches_by_id: dict = None,
                     evals_by_id: dict = None,
                     cards_by_id: dict = None) -> dict:
    """Latest rollup op per lane from the ops dir. Missing files read as
    status 'none' — never an error. When the caller's evaluated
    batches are given, each row also carries M2.24 leaf `caveats`
    (read-only; the op file is never rewritten here). Snapshot cards
    fill in leaves the evaluations do not cover (same precomputed
    state the glance uses), so a degraded home still shows compiler,
    newer-ready, hold and pod-drift lines instead of silence."""
    status = {}
    try:
        files = sorted(integration_ops_dir().glob("rollup-*.json"))
    except OSError:
        files = []
    kept = {}
    for path in files:
        try:
            rec = read_json(path, default=None)
        except Exception:  # noqa: BLE001 - one bad op file skips it
            continue
        if not isinstance(rec, dict) or rec.get("op") != "rollup":
            continue
        if (rec.get("repo") or "") != repo:
            continue
        lane = str(rec.get("lane") or "")
        if lane not in ("main", "next"):
            continue
        prev = kept.get(lane)
        if prev is not None and str(prev.get("updated_at") or "") >= \
                str(rec.get("updated_at") or ""):
            continue
        kept[lane] = rec
    # M2.25: caveat states only for batch ids that appear as leaves
    # of the returned rollup ops — never for every repo batch.
    leaf_ids = set()
    for _rec in kept.values():
        for _leaf in _rec.get("leaves") or []:
            if isinstance(_leaf, dict) and _leaf.get("batch"):
                leaf_ids.add(str(_leaf.get("batch")))
    try:
        states = _rollup_eval_states(repo, batches_by_id, evals_by_id,
                                     only_ids=leaf_ids)
    except Exception:  # noqa: BLE001 - rows degrade to no caveats
        states = {}
    if isinstance(cards_by_id, dict):
        for _bid in leaf_ids:
            if _bid not in states:
                try:
                    _card = cards_by_id.get(_bid)
                    if isinstance(_card, dict):
                        states[_bid] = _rollup_leaf_state_from_card(
                            _card)
                except Exception:  # noqa: BLE001 - one leaf skips
                    continue
    status = {}
    for lane, rec in kept.items():
        leaves = rec.get("leaves") or []
        draft = rec.get("pr_draft") or {}
        res = rec.get("result") or {}
        if not isinstance(res, dict):
            res = {}
        try:
            caveats = _rollup_leaf_caveats(repo, leaves, states)
        except Exception:  # noqa: BLE001 - rows never raise
            caveats = []
        slim = []
        if isinstance(leaves, list):
            for _leaf in leaves:
                if not isinstance(_leaf, dict):
                    continue
                slim.append({"batch": str(_leaf.get("batch") or ""),
                             "dep": str(_leaf.get("dep") or ""),
                             "target": str(_leaf.get("target") or "")})
        gates = rec.get("gates")
        if not isinstance(gates, list):
            gates = []
        commands = rec.get("owner_commands")
        if not isinstance(commands, list):
            commands = []
        status[lane] = {
            "op": str(rec.get("op_id") or ""),
            "status": str(rec.get("status") or "started"),
            "error": str(rec.get("error") or "")[:300],
            "leaves": len(leaves) if isinstance(leaves, list) else 0,
            "leaves_list": slim,
            "branch": str(res.get("branch") or ""),
            "head": str(res.get("head") or ""),
            "result": {"branch": str(res.get("branch") or ""),
                       "head": str(res.get("head") or "")},
            "gates": gates,
            "owner_commands": [str(c) for c in commands
                               if isinstance(c, (str, int, float))],
            "pr_title": str(draft.get("title") or ""),
            "pr_body": str(draft.get("body") or ""),
            "stack_line": str(draft.get("stack_line") or ""),
            "ship_order": str(rec.get("ship_order") or ""),
            "stacked": rec.get("stacked_drafts")
            if isinstance(rec.get("stacked_drafts"), dict) else None,
            "caveats": caveats,
            "updated_at": str(rec.get("updated_at") or ""),
        }
    return status


def summary_dir() -> Path:
    return paths.home() / "summary"


def summary_snapshot_path() -> Path:
    return summary_dir() / "snapshot.json"


def summary_digest_day_path() -> Path:
    return summary_dir() / "last_digest_day.txt"
    return summary_dir() / "last_digest_day.txt"


def _m1_repo_batches(repo: str) -> list:
    out = []
    try:
        files = sorted(batches_dir().glob("*.json"))
    except OSError:
        return out
    for path in files:
        if path.name == "index.json":
            continue
        try:
            batch = load_batch(path.stem)
        except BatchError:
            continue
        except Exception:  # noqa: BLE001 - one bad batch file skips it
            continue
        if (batch.get("repo") or "") == repo:
            out.append(batch)
    return out


def _m1_involved(repo_batches: list) -> list:
    """Transitive inheritance closure via the one shared helper both
    rollup and the summary use, so the two readiness paths can never
    disagree about which ancestors belong in the live map."""
    return closure(repo_batches)


def _m1_fresh_evals(repo_batches: list, live: dict, diag) -> tuple:
    """Evaluate every batch fresh (record=False — a background refresh
    never mutates batch files). No cache: evaluate() also reads live
    sources, run bindings and inherited batches, so a file-sha-keyed
    cache can claim READY after a live drift (readiness flap). The tick
    is background, so the full evaluation belongs here, never on the
    request path. Returns (precomputed, unreadable_names)."""
    precomputed = {}
    unreadable = []
    for batch in repo_batches:
        bid = str(batch.get("id") or "")
        try:
            precomputed[bid] = evaluate(batch, live, live_diag=diag,
                                        record=False)
        except Exception as exc:  # noqa: BLE001 - one batch never kills it
            unreadable.append(f"묶음 {bid} ({type(exc).__name__})")
    return precomputed, unreadable


def _m1_story_cap(text: str) -> str:
    """Cap a story line at ~80 chars. The full text lives in the digest
    ('… (자세히)' points there); the snapshot never invents the rest."""
    text = str(text or "")
    return text if len(text) <= 80 else text[:80] + "… (자세히)"


def _m1_story(repo: str, buds: list, cap: bool = True) -> list:
    """24h story sentences newest-first: translated events + scan fold +
    fresh READY batches. `buds` holds (id, dep, target, updated_at);
    only entries from the last 24h are kept. Identical consecutive lines
    drop (keep the newest). With cap, lines cut at ~80 chars pointing at
    the digest; without, full text for the digest itself. Reads the
    event file; collapsing and sentences are pure."""
    import datetime
    try:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(hours=24)).isoformat()
    except Exception:  # noqa: BLE001 - no cutoff without a clock
        cutoff = ""
    evs = events.list_events(repo, limit=200)
    raw_scans = []
    lines = []
    for ev in evs:
        kind = str(ev.get("kind") or "")
        at = str(ev.get("at") or "")
        if cutoff and at and at < cutoff:
            continue
        if kind == "scan":
            title = str(ev.get("title") or "")
            detail = str(ev.get("detail") or "")
            m = _SCAN_TITLE_RE.search(title)
            counts = tuple(int(x) for x in m.groups()) if m else None
            qm = _QUEUED_RE.search(title)
            queued = int(qm.group(1)) if qm else 0
            dset = frozenset(p.strip() for p in detail.split(";")
                             if "->" in p)
            raw_scans.append({"counts": counts, "detail": detail,
                              "dset": dset, "queued": queued, "at": at})
            continue
        sent = m1_event_sentence(kind, str(ev.get("title") or ""),
                                 str(ev.get("detail") or ""))
        if sent:
            lines.append({"at": at, "text": sent})
    scans = []
    for i, sc in enumerate(raw_scans):
        older = raw_scans[i + 1] if i + 1 < len(raw_scans) else None
        if older is None:
            changed = sc["queued"] > 0
            found = _m1_scan_found("", queued=sc["queued"]) \
                if changed else ""
        else:
            changed = (sc["counts"] != older["counts"]
                       or sc["dset"] != older["dset"]
                       or sc["queued"] > 0)
            found = _m1_scan_found(
                sc["detail"], prev_dset=older["dset"],
                queued=sc["queued"], counts=sc["counts"],
                prev_counts=older["counts"]) if changed else ""
        scans.append((sc["counts"], sc["at"], changed, found))
    for text in m1_collapse_scans(scans):
        lines.append({"at": "", "text": text})
    for bid, dep, target, updated in buds:
        if cutoff and str(updated or "") < cutoff:
            continue
        lines.append({"at": str(updated or ""),
                      "text": f"{dep} {target} 묶음 준비됨"})
    lines.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
    deduped = []
    for entry in lines[:40]:
        if deduped and deduped[-1]["text"] == entry["text"]:
            continue
        deduped.append(entry)
    if cap:
        return [_m1_story_cap(x["text"]) for x in deduped]
    return [x["text"] for x in deduped]


def refresh_summary_snapshot(reason: str, obs: str = None) -> dict:
    """Recompute the glance snapshot (background triggers only).

    Per repo: incremental batch evaluations, verdict ladder, release
    info, rollup status, open questions with ages + suggestions, 24h
    story, worker state. Writes snapshot.json atomically (tmp+rename,
    previous kept as backup). Never raises: per-repo failures degrade
    that repo to 미확인 with the reason named. Tmp files carry the pid
    so a concurrent tick and import never collide on the same path.
    Removes the orphaned summary/evals.json left by the dropped cache
    (only that file, only if it exists). `obs` pins the observation id
    (see refresh_all_snapshots); None keeps the old behavior.

    M2.16b: the summary dir resolves ONCE — tmp, prev and target
    all build from that single Path, so they are always siblings
    even if HOME changes mid-run (live: a refresh computed rows
    in a temp home, re-resolved after HOME flipped, and orphaned
    snapshot.<pid>.tmp in the real home).
    """
    sdir = summary_dir()
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        (sdir / "evals.json").unlink()
    except OSError:
        pass
    now_ts = time.time()
    try:
        runs = store.list_runs(200)
    except Exception:  # noqa: BLE001 - unreadable runs degrade verdicts
        runs = None
    try:
        repo_names = repos.list_all()
    except Exception:  # noqa: BLE001 - no repo list, nothing to do
        repo_names = []
    rows = []
    for name in repo_names:
        try:
            row = _m1_repo_row(name, runs, now_ts)
            row["system"]["snapshot_writer"] = reason
            rows.append(row)
        # M25a-F1: SystemExit from deep helpers (store.load,
        # read_json) degrades one row, never the whole refresh.
        # KeyboardInterrupt still propagates.
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - contained
            rows.append({
                "name": name, "verdict": "unknown",
                "verdict_rule": "refresh-failed",
                "verdict_line": "미확인 — 상태 요약 계산 실패 "
                                f"({type(exc).__name__})",
                "unreadable": ["snapshot-refresh"],
                "pending_release": {}, "rollup": {},
                "need_you": [], "blocked": [], "story": [],
                "next_steps": [],
                "system": {"app_sha": _m1_app_sha()},
                "updated_at": utcnow()})
    snap = {"schema": 1, "at": utcnow(), "writer": reason,
            "obs": obs or ("obs-" + hashlib.sha256(
                f"{utcnow()}-{os.getpid()}".encode()).hexdigest()[:16]),
            "repos": rows}
    # Same rule as the batches snapshot: refresh_obs is the refresh
    # pass id shared across screens; obs is per-response on the wire.
    snap["refresh_obs"] = snap.get("obs")
    target = sdir / "snapshot.json"
    prev_path = sdir / "snapshot.prev.json"
    tmp = sdir / f"snapshot.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(snap, ensure_ascii=False),
                       encoding="utf-8")
        try:
            prev_path.write_bytes(target.read_bytes())
        except OSError:
            pass
        tmp.replace(target)
    except OSError:
        # M2.16b: a failed write/replace removes only this
        # call's own tmp (best effort) — never a record, never
        # another writer's tmp.
        try:
            tmp.unlink()
        except OSError:
            pass
    return snap


def batches_snapshot_path() -> Path:
    return summary_dir() / "batches-snapshot.json"


def snapshot_stale_on_startup() -> dict:
    """M2.9c: True when a restart would serve a dropped refresh.

    Compares the served batches snapshot against the newest
    hold/attest event and every batch file: if either is newer than
    the snapshot, a pending refresh died with the old worker (live:
    a hold landed, the worker was killed before it rewrote the
    file). Read-only; never raises. A same-second hold event counts
    as stale (one extra background refresh is the safe direction).
    """
    try:
        from . import events as _events_mod
        snap_path = batches_snapshot_path()
        try:
            snap_mtime = snap_path.stat().st_mtime
        except OSError:
            return {"stale": True, "reason": "스냅샷 없음"}
        snap = read_json(snap_path, default=None) or {}
        snap_at = str(snap.get("at") or "")
        newest = ""
        newest_kind = ""
        try:
            with open(_events_mod._file(), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if ev.get("kind") in ("hold", "attest"):
                        at = str(ev.get("at") or "")
                        if at and at >= newest:
                            newest = at
                            newest_kind = str(ev.get("kind") or "")
        except OSError:
            pass
        if newest and (not snap_at or newest >= snap_at):
            return {"stale": True,
                    "reason": f"확인 기록({newest_kind})이 스냅샷보다 새름 "
                              f"({newest})"}
        try:
            bdir = batches_dir()
            for path in list(bdir.glob("*.json")) + [
                    p for sub in bdir.iterdir() if sub.is_dir()
                    for p in sub.glob("*.json")]:
                try:
                    if path.stat().st_mtime > snap_mtime:
                        return {"stale": True,
                                "reason": f"묶음 파일이 스냅샷보다 새름 "
                                          f"({path.name})"}
                except OSError:
                    continue
        except OSError:
            pass
        return {"stale": False, "reason": ""}
    except Exception:  # noqa: BLE001 - a failed check refreshes
        return {"stale": True, "reason": "확인 실패"}


_BATCHES_SNAP_LOCK = threading.Lock()


def _root_pending_names(batch: dict) -> list:
    """Required Root/runtime gates still waiting on one batch.

    A gate lands with status pass AND a human attestor
    (root_attested or owner_attested — the N3 rule readiness uses),
    or with a recorded N/A reason — a merely passing worker gate
    never lands one. Shared by the reviews card and the glance
    count so the two screens cannot disagree."""
    catalog = (batch.get("policy") or {}).get("gates") or {}
    required = list((batch.get("policy") or {}).get("required_gates")
                    or [])
    have = {
        g.get("key")
        for i in (batch.get("integrations") or [])
        if isinstance(i, dict)
        for g in (i.get("gates") or [])
        if isinstance(g, dict)
        and ((g.get("status") == "pass"
              and (g.get("root_attested") or g.get("owner_attested")))
             or (isinstance(g.get("na"), dict)
                 and str((g.get("na") or {}).get("reason") or "")))}
    return [r for r in required
            if ((catalog.get(r) or {}).get("root")
                or (catalog.get(r) or {}).get("runtime"))
            and r not in have]


def _primary_items(batch: dict):
    """Compact typed primaries projection for list titles (read-only).

    Dep/target/lane/run_id plus a validated issue block as bounded
    strings — no source, secret, or evidence expansion. Malformed
    entries are skipped, so a hostile or legacy primary list yields []
    and the UI falls back to its legacy title path. The card carries
    the honest full "primaries" count next to this capped (64) list.
    Never raises."""
    try:
        prims = batch.get("primaries")
    except Exception:
        return []
    if not isinstance(prims, list):
        return []
    try:
        _pkey_repo = str(batch.get("repo") or "")
    except Exception:
        _pkey_repo = ""
    items = []
    for p in prims[:64]:
        if not isinstance(p, dict):
            continue

        def _s(v):
            return v if isinstance(v, str) else ""
        blk = p.get("issue")
        if isinstance(blk, dict):
            # A mixed dep+issue record is invalid (primary_result_key
            # marks it None); never display it as a valid pure issue.
            _mdep = p.get("dep")
            _mtarget = p.get("target")
            if (isinstance(_mdep, str) and _mdep) \
                    or (isinstance(_mtarget, str) and _mtarget):
                continue
            # Typed issue primary: strict fields only, same contract
            # as primary_result_key (repo binding, known lane match).
            _inum = _strict_issue_number(blk.get("issue"))
            _nlane = blk.get("lane")
            _plane = _s(p.get("lane"))
            if _inum is None or blk.get("repo") != _pkey_repo \
                    or not isinstance(_nlane, str) \
                    or _nlane not in _KNOWN_LANES or _nlane != _plane:
                continue
            items.append({"dep": "", "target": "",
                          "lane": _bound_text(_plane, 40),
                          "run_id": _bound_text(_s(p.get("run_id")), 80),
                          "issue": {"issue": _inum,
                                    "repo": _pkey_repo,
                                    "lane": _nlane}})
            continue
        dep = p.get("dep")
        if not isinstance(dep, str) or not dep:
            continue
        # dep bound 256: real npm names can exceed 80 chars; two
        # same-prefix packages must stay distinguishable in metadata.
        # Visual shortening belongs to the UI with a full-title path.
        items.append({"dep": _bound_text(dep, 256),
                      "target": _bound_text(_s(p.get("target")), 80),
                      "lane": _bound_text(_s(p.get("lane")), 40),
                      "run_id": _bound_text(_s(p.get("run_id")), 80)})
    return items


def _batch_pod_drift(batch: dict):
    """M2.5c native lock label for a batch card (read-only).

    Prefers the fastcheck receipt's pod_drift (exact podspec
    capture at receipt time); for older receipts falls back to a
    live check on the integration worktree with the package name
    as the pod (best-effort — a pod renamed from its package, e.g.
    RNCSafeAreaContext, misses until a future fastcheck captures
    its podspec). M2.22: the fallback runs over EVERY primary
    (not only one) plus the tree-based react-native check, so an
    older receipt with pod_drift null (a joint whose own dep is
    expo) still gets the RN label. Returns the first labelled
    drift or None. Never raises, never blocks."""
    try:
        op_id = str(batch.get("origin_m2") or "")
        if op_id:
            from . import fastcheck as _fc
            summary = read_json(_fc.op_fastcheck_path(op_id),
                                default=None)
            drift = (summary or {}).get("pod_drift") \
                if isinstance(summary, dict) else None
            if isinstance(drift, dict) and drift.get("label"):
                return drift
        primaries = batch.get("primaries") or []
        integs = batch.get("integrations") or []
        integ = integs[0] if integs and isinstance(integs[0], dict) \
            else {}
        wt = str(integ.get("worktree") or "")
        if isinstance(primaries, list) and primaries:
            from . import fastcheck as _fc

            def _labelled(drift):
                if isinstance(drift, dict) and drift.get("label"):
                    return drift
                return None
            for primary in primaries:
                if not isinstance(primary, dict):
                    continue
                hit = _labelled(_fc.check_pod_drift(
                    wt, primary.get("dep"), primary.get("target"),
                    fallback_pod=str(primary.get("dep") or "")))
                if hit is not None:
                    return hit
            try:
                rn_ver = _fc.tree_react_native_version(wt)
            except (Exception, SystemExit):  # noqa: BLE001 - unknown
                rn_ver = ""
            if rn_ver:
                for pod in ("React-Core-prebuilt",
                            "ReactNativeDependencies"):
                    hit = _labelled(_fc.check_pod_drift(
                        wt, "react-native", rn_ver,
                        fallback_pod=pod))
                    if hit is not None:
                        return hit
    except (Exception, SystemExit):  # noqa: BLE001 - no label
        return None
    return None


def _newer_ready_hints(bid, repo, primaries, inherited, prim_idx,
                       ready_of) -> list:
    """M2.16d: read-only newer-READY-leaf hints for one batch.

    For each own primary (dep/target/lane): another batch on the
    same repo with the same lane+dep, a semver-greater target, a
    fully READY evaluation and the same inherited base-batch set
    means a re-roll is possible. Display only — nothing re-rolls
    automatically (the rollup stage stays off). Never mutates;
    never raises.
    """
    hints = []
    own_inh = set()
    for h in inherited or []:
        if isinstance(h, dict) and h.get("batch"):
            own_inh.add(str(h.get("batch")))
    for p in primaries or []:
        if not isinstance(p, dict) or not p.get("dep"):
            continue
        dep = str(p.get("dep") or "")
        target = str(p.get("target") or "")
        lane = str(p.get("lane") or "")
        for obid, orepo, olane, odep, otarget, oinh in prim_idx:
            if obid == bid or orepo != repo:
                continue
            if olane != lane or odep != dep:
                continue
            if oinh != own_inh:
                continue
            try:
                greater = _versions.compare(otarget, target) == 1
            except Exception:  # noqa: BLE001 - unreadable never hints
                greater = False
            if not greater:
                continue
            oready, onprim = ready_of.get(obid, (0, 0))
            if onprim <= 0 or oready != onprim:
                continue
            hint = {"dep": dep, "from": target, "to": otarget,
                    "batch": obid}
            if hint not in hints:
                hints.append(hint)
    return hints


def build_batches_envelope(obs: str = None) -> dict:
    """Today's /api/batches computation, factored for reuse.

    One shared live-map pass over the reachable-inheritance closure,
    one evaluation per batch with record=True (the refresh is a
    top-level evaluation point like the old request path, so the
    readiness-negatives stream keeps its exactly-once-per-signature
    contract; the refresh lock serializes concurrent refreshes, which
    is stronger dedupe than the readgate ever gave). evaluate() itself
    performs zero file writes — record=True only appends diagnostics
    lines, never batch files — so concurrent tick + import refreshes
    cannot interleave batch-file writes. Then the same cards + repo
    summary + unregistered projection the request path used to build
    inline, so a card and its summary can never be two observations.
    Returns the envelope WITHOUT at/writer (the refresh stamps it).
    `obs` overrides the generated observation id so one refresh pass
    can stamp several snapshots (see refresh_all_snapshots).
    """
    batch_list = list_batches()
    closed = closure(batch_list)
    live_all, diag_all = live_map_with_diag(closed)
    live = {b.get("id"): live_all for b in batch_list
            if isinstance(b, dict)}
    live_diag = {b.get("id"): diag_all for b in batch_list
                 if isinstance(b, dict)}
    if not obs:
        obs = "obs-" + hashlib.sha256(
            f"{utcnow()}-{os.getpid()}".encode()).hexdigest()[:16]
    batch_by_id = {b.get("id"): b for b in batch_list
                   if isinstance(b, dict) and b.get("id")}
    evals: dict = {}
    for b in batch_list:
        bl = live.get(b.get("id")) or {}
        try:
            evals[b.get("id")] = evaluate(
                b, bl, live_diag=live_diag.get(b.get("id")),
                record=True)
        except (Exception, SystemExit):  # noqa: BLE001 - one batch
            # never kills it (util.read_json raises SystemExit on
            # corrupt JSON — that must not escape either).
            evals[b.get("id")] = {"results": []}
    cards = []
    try:
        joint_sup = _joint_supersede_map()
    except (Exception, SystemExit):  # noqa: BLE001 - notes are bonus
        joint_sup = {}
    # M2.15: newer-auto display map (display only — holds/files
    # untouched). Pure over the already-listed batches.
    try:
        auto_sup = _auto_supersede_map(batch_list)
    except (Exception, SystemExit):  # noqa: BLE001 - notes are bonus
        auto_sup = {}
    # M2.17b: joint-refresh hints (display only — holds/files
    # untouched). Queued newer members of done joints.
    try:
        joint_refresh_hints = _joint_refresh_hints(batch_list)
    except (Exception, SystemExit):  # noqa: BLE001 - hints bonus
        joint_refresh_hints = {}
    # M2.16d: primary index for newer-READY hints (display only).
    prim_idx = []
    ready_of = {}
    try:
        for _b in batch_list:
            if not isinstance(_b, dict) or not _b.get("id"):
                continue
            _ev = evals.get(_b.get("id")) or {"results": []}
            _rs = [r for r in (_ev.get("results") or [])
                   if isinstance(r, dict)]
            _ready = sum(1 for r in _rs if r.get("ready"))
            ready_of[_b.get("id")] = (_ready, len(_rs))
            for _p in _b.get("primaries") or []:
                if isinstance(_p, dict) and _p.get("dep"):
                    _inh = set()
                    for _h in _b.get("inherited") or []:
                        if isinstance(_h, dict) and _h.get("batch"):
                            _inh.add(str(_h.get("batch")))
                    prim_idx.append(
                        (_b.get("id"), str(_b.get("repo") or ""),
                         str(_p.get("lane") or ""),
                         str(_p.get("dep") or ""),
                         str(_p.get("target") or ""), _inh))
    except (Exception, SystemExit):  # noqa: BLE001 - hints are bonus
        prim_idx = []
        ready_of = {}
    for b in batch_list:
        bl = live.get(b.get("id")) or {}
        ev = evals.get(b.get("id")) or {"results": []}
        ready = sum(1 for r in (ev.get("results") or [])
                    if isinstance(r, dict) and r.get("ready"))
        # M2.16d: evaluate() attention items ride the card as
        # compiler notes — warnings, never blockers, so readiness
        # and the 막힘 wording stay untouched.
        compiler_notes = []
        for _r in (ev.get("results") or []):
            if not isinstance(_r, dict):
                continue
            for _a in _r.get("attention") or []:
                if _a and _a not in compiler_notes:
                    compiler_notes.append(_a)
        # M2.16d: newer READY same-dep/lane/base hints.
        try:
            newer_ready = _newer_ready_hints(
                b.get("id"), str(b.get("repo") or ""),
                b.get("primaries") or [], b.get("inherited") or [],
                prim_idx, ready_of)
        except (Exception, SystemExit):  # noqa: BLE001 - hints bonus
            newer_ready = []
        catalog = (b.get("policy") or {}).get("gates") or {}
        attention = []
        card_integs = []
        for i in b.get("integrations") or []:
            wl = bl.get("wt:" + (i.get("worktree") or "")) or {}
            sigs = []
            for g in i.get("gates") or []:
                if (g.get("status") in ("fail", "blocked")
                        and all(a["key"] != g.get("key")
                                for a in attention)):
                    spec = catalog.get(g.get("key") or "") or {}
                    attention.append({"key": g.get("key"),
                                      "label": spec.get("label")
                                      or g.get("key")})
                ins = g.get("inputs") or {}
                sigs.append({"key": g.get("key"),
                             "status": g.get("status"),
                             "inputs_head": ins.get("head"),
                             "inputs_fp": ins.get("fingerprint"),
                             "ev": sorted(e.get("sha256") or ""
                                          for e in g.get("evidence") or []
                                          if e.get("kind") != "note")})
            dupes = i.get("lock_dupes")
            if not (isinstance(dupes, dict)
                    and isinstance(dupes.get("items"), list)
                    and isinstance(dupes.get("total"), int)):
                dupes = {"items": [], "total": 0}
            card_integs.append({"lane": i.get("lane"),
                                "worktree": i.get("worktree"),
                                "branch": (b.get("origin") or {}).get(
                                    "base_branch"),
                                "head": (i.get("head") or "")[:12],
                                "fingerprint": i.get("fingerprint"),
                                "live_head": wl.get("head"),
                                "live_fingerprint": wl.get(
                                    "fingerprint"),
                                "live_clean": wl.get("clean"),
                                "status": i.get("status"),
                                "lock_dupes": dupes,
                                "gates": sigs})
        diag = card_diagnostic(b, ev, bl, batch_by_id, evals)
        # M2.4a: required Root/runtime gates still waiting (a gate
        # lands only with pass + root_attested — the N3 rule — so a
        # merely passing worker gate never lands one). The chip and
        # the glance read "Root 확인 대기 N건" from this.
        root_pending_names = _root_pending_names(b)
        root_pending = bool(root_pending_names)
        hold = b.get("hold") if isinstance(b.get("hold"), dict) \
            else None
        cards.append({"id": b.get("id"), "repo": b.get("repo"),
                      "target_branch": b.get("target_branch"),
                      "purpose": b.get("purpose"),
                      "origin_m2": b.get("origin_m2") or "",
                      "hold": hold,
                      "joint_superseded_by": joint_sup.get(
                          b.get("id") or ""),
                      "newer_auto": auto_sup.get(b.get("id") or ""),
                      "joint_refresh": joint_refresh_hints.get(
                          b.get("id") or ""),
                      "consumer_label": _batch_consumer_label(b),
                      "pod_drift": _batch_pod_drift(b),
                      "compiler_notes": compiler_notes,
                      "newer_ready": newer_ready,
                      "root_pending": bool(root_pending),
                      "root_pending_count": len(root_pending_names),
                      "root_pending_gates": root_pending_names[:32],
                      "updated_at": b.get("updated_at"),
                      "primaries": len(b.get("primaries") or []),
                      "primary_items": _primary_items(b),
                      "inherited": b.get("inherited") or [],
                      "integrations": card_integs,
                      "ready": ready, "attention": attention,
                      "evaluated_at": ev.get("evaluated_at"),
                      "obs": obs, "evaluation": diag})
    unregistered = {}
    try:
        repo_names = repos.list_all()
    except Exception:  # noqa: BLE001 - never break the list here
        repo_names = []
    for name in repo_names:
        try:
            queue = sched.load(name).get("queue", [])
        except SystemExit:
            queue = []
        except Exception:  # noqa: BLE001 - one repo never kills it
            queue = []
        items = uncovered_candidates(queue, batch_list, name)
        if items:
            unregistered[name] = items
    return {"batches": cards,
            "summary": summarize(batch_list, live,
                                 precomputed=evals),
            "unregistered": unregistered, "obs": obs}


def refresh_batches_snapshot(reason: str, obs: str = None) -> dict:
    """Recompute the /api/batches snapshot (background triggers only).

    Same pattern as the glance snapshot: atomic tmp+rename with pid
    tmp names (a concurrent tick and import never collide), stamped
    with at/writer/obs. One in-process lock: a refresh already running
    returns the current file instead of stacking work. Never raises:
    failures return whatever was last written (possibly {}). `obs`
    pins the observation id (see refresh_all_snapshots).
    """
    if not _BATCHES_SNAP_LOCK.acquire(blocking=False):
        try:
            return read_json(batches_snapshot_path(),
                             default=None) or {}
        except Exception:  # noqa: BLE001 - locked path stays silent
            return {}
    # M2.16b: same single-dir rule as refresh_summary_snapshot
    # — tmp, prev and target always siblings; a failed
    # write/replace removes only this call's own tmp.
    sdir = summary_dir()
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        try:
            env = build_batches_envelope(obs=obs)
        except Exception:  # noqa: BLE001 - keep the last good file
            try:
                return read_json(sdir / "batches-snapshot.json",
                                 default=None) or {}
            except Exception:  # noqa: BLE001 - nothing written yet
                return {}
        snap = {"schema": 1, "at": utcnow(), "writer": reason,
                "obs": env.get("obs"), "refresh_obs": env.get("obs")}
        snap.update(env)
        # refresh_obs pins the refresh pass id even if a later edit
        # touches obs: clients compare refresh_obs across /api/batches
        # and /api/summary for the one-observation check, while obs is
        # per-response on the wire (see api_batches).
        snap["refresh_obs"] = snap.get("obs")
        target = sdir / "batches-snapshot.json"
        tmp = sdir / f"batches-snapshot.{os.getpid()}.tmp"
        try:
            tmp.write_text(json.dumps(snap, ensure_ascii=False),
                           encoding="utf-8")
            try:
                (sdir / "batches-snapshot.prev.json").write_bytes(
                    target.read_bytes())
            except OSError:
                pass
            tmp.replace(target)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
        return snap
    finally:
        _BATCHES_SNAP_LOCK.release()


def refresh_all_snapshots(reason: str) -> dict:
    """Refresh the batches + glance snapshots in ONE pass with ONE
    shared observation id, so the reviews tab and the glance can never
    disagree about readiness: both files carry the same top-level
    `obs`. The two evaluations still run back-to-back (seconds apart);
    on quiet state their counts agree, and any mid-pass drift shows in
    the next pass with an honest stamp. Returns
    {"obs", "batches", "summary"}. Never raises (each half degrades
    independently, as before).
    """
    obs = "obs-" + hashlib.sha256(
        f"{utcnow()}-{os.getpid()}".encode()).hexdigest()[:16]
    # F9: each half degrades independently — a corrupt batch file
    # (SystemExit from read_json) in one half never skips the other.
    try:
        benv = refresh_batches_snapshot(reason, obs=obs)
    except SystemExit:
        benv = {}
    try:
        senv = refresh_summary_snapshot(reason, obs=obs)
    except SystemExit:
        senv = {}
    return {"obs": obs, "batches": benv, "summary": senv}


def _m1_batch_results_ready(batch: dict, precomputed: dict) -> bool:
    """Every evaluation result ready, whatever the primary count."""
    ev = precomputed.get(batch.get("id"))
    results = (ev or {}).get("results") if isinstance(ev, dict) else None
    return bool(results) and all(isinstance(r, dict) and r.get("ready")
                                 for r in results)


def _m1_batch_fully_ready(batch: dict, precomputed: dict) -> bool:
    """One batch counts as shippable work only when every evaluation
    result is ready (same rule as the story's buds list). Leaves keep
    the single-primary rule (rollup replays exactly one dep per leaf);
    a rollup BASE may carry several primaries, so bases use
    _m1_batch_results_ready instead."""
    if not _m1_batch_results_ready(batch, precomputed):
        return False
    prims = [p for p in (batch.get("primaries") or [])
             if isinstance(p, dict)]
    return len(prims) == 1


def _m1_next_steps(name: str, repo_batches: list, precomputed: dict,
                   rollups: dict) -> list:
    """One 'how does this ship' line per repo, derived from data only.

    A finished rollup with a PR draft points at the draft; a blocked
    rollup points at its reason; otherwise each lane with 2+ READY leaf
    batches (not inherited by another READY batch of the same lane)
    points at the reviews-tab rollup panel. A single leaf, or no READY
    work, yields no line — never invented."""
    steps = []
    link = f"#/repo/{name}?tab=reviews"
    # M2.5b: held batches name their hold first — the hold line
    # replaces the bare waiting line for them, each deep-linking
    # its own card.
    holds = [(b.get("id"), b.get("hold")) for b in (repo_batches or [])
             if isinstance(b, dict)
             and isinstance(b.get("hold"), dict)
             and str(b.get("hold").get("by") or "")]
    held_ids = {bid for bid, _ in holds}
    for bid, hold in sorted(holds):
        by = str(hold.get("by") or "")
        why = " ".join(str(hold.get("reason") or "").split())
        steps.append({
            "lane": "", "kind": "batch-hold",
            "text": f"보류: {why} — {by}" if why else f"보류 — {by}",
            "link": f"{link}&batch={bid}"})
    # M2.4a: auto batches waiting on Root/human confirmation come
    # first — nothing else ships until a human reviews them.
    auto_wait = [(b.get("id"), _root_pending_names(b))
                 for b in (repo_batches or [])
                 if isinstance(b, dict) and b.get("origin_m2")
                 and b.get("id") not in held_ids]
    auto_wait = [(bid, names) for bid, names in auto_wait if names]
    if auto_wait:
        total = sum(len(names) for _, names in auto_wait)
        # M2.5b wording: one waiting batch deep-links its card;
        # several keep the generic tab.
        # M2.5c wording: the count was gates (10 per auto batch),
        # which read as batches — say both.
        deep = (f"{link}&batch={auto_wait[0][0]}"
                if len(auto_wait) == 1 else link)
        steps.append({
            "lane": "", "kind": "root-review",
            "text": (f"Root 확인 대기 묶음 {len(auto_wait)}개 "
                     f"(게이트 {total}건) — 검토 후 승인"),
            "link": deep})
    for lane in ("next", "main"):
        r = (rollups or {}).get(lane) or {}
        status = r.get("status") or "none"
        if status == "done" and r.get("pr_title"):
            stacked = r.get("stacked") \
                if isinstance(r.get("stacked"), dict) else None
            if stacked:
                pr1 = stacked.get("pr1") or {}
                pr2 = stacked.get("pr2") or {}
                steps.append({
                    "lane": lane, "kind": "pr-draft",
                    "text": f"다음 단계: PR1 (main, "
                            f"{pr1.get('commits', '?')} commits) → PR2 "
                            f"({lane} rollup, {pr2.get('leaves', '?')} "
                            "leaves)",
                    "link": link})
                continue
            stack_txt = (f" ({r.get('stack_line')})"
                         if r.get("stack_line") else "")
            steps.append({
                "lane": lane, "kind": "pr-draft",
                "text": f"다음 단계: {lane} 레인 PR 초안 확인 — "
                        f"{r.get('pr_title')}{stack_txt}",
                "link": link})
        elif status == "blocked":
            steps.append({
                "lane": lane, "kind": "rollup-blocked",
                "text": f"다음 단계: {lane} 레인 합치기 막힘 사유부터 확인",
                "link": link})
    if steps:
        return steps
    by_id = {str(b.get("id") or ""): b for b in (repo_batches or [])
             if isinstance(b, dict)}
    ready_ids = {bid for bid, b in by_id.items()
                 if _m1_batch_fully_ready(b, precomputed)}
    for lane in ("next", "main"):
        lane_ready = [b for bid, b in by_id.items()
                      if bid in ready_ids
                      and (b.get("target_branch") or "") == lane]
        lane_all = [b for b in (repo_batches or [])
                    if isinstance(b, dict)
                    and (b.get("target_branch") or "") == lane]
        inherited_ids: set = set()
        ancestors: dict = {}
        for b in lane_all:
            try:
                ancestors[str(b.get("id") or "")] = \
                    set(_rollup_ancestor_ids(b))
            except Exception:  # noqa: BLE001 - ancestry is best-effort
                ancestors[str(b.get("id") or "")] = set()
            if _m1_batch_results_ready(b, precomputed):
                inherited_ids |= ancestors[str(b.get("id") or "")]
        leaves = [b for b in lane_ready
                  if str(b.get("id") or "") not in inherited_ids]
        # Match the reviews-tab rollup panel: one base anchors one
        # panel, so group the leaves by their most-covering same-lane
        # ready base. The rest ship on other bases ("다른 기준").
        bases = sorted(
            str(c.get("id") or "")
            for c in lane_all
            if str(c.get("id") or "") in inherited_ids
            and _m1_batch_results_ready(c, precomputed))
        best_cover = -1
        for cand in bases:
            cover = sum(1 for b in leaves
                        if cand in ancestors.get(
                            str(b.get("id") or ""), set()))
            if cover > best_cover:
                best_cover = cover
        if best_cover >= 2:
            rest = len(leaves) - best_cover
            rest_txt = f" · 나머지 {rest}개는 다른 기준" if rest else ""
            steps.append({
                "lane": lane, "kind": "rollup",
                "text": f"다음 단계: {lane} 레인 {best_cover}개 합치기 "
                        f"가능{rest_txt} → PR 초안 확인",
                "link": link})
    return steps


def _m1_prepare_retry_items(name: str, now_ts: float) -> list:
    """M2.11c: 사람이 할 일 entries for re-deriveable blocked prepares.

    Scans this repo's prepare op records for BLOCKED cherry-pick
    conflicts and keeps the ones whose rederive hint says ok. The
    entry points at the run's work view, where the M2.11b 다시 시도
    button lives. Hint failures only drop that entry; this never
    raises and never acts (the hint is read-only advice).
    """
    items = []
    try:
        files = sorted(integration_ops_dir().glob("prep-*.json"))
    except (Exception, SystemExit):  # noqa: BLE001 - no scan, no items
        return items
    for opf in files:
        try:
            rec = read_json(opf, default=None)
            if not isinstance(rec, dict):
                continue
            if rec.get("op") != "prepare" \
                    or rec.get("status") != "blocked":
                continue
            if "cherry-pick" not in str(rec.get("error") or ""):
                continue
            op_repo = str(rec.get("repo") or "")
            if not op_repo:
                try:
                    st = store.load(str(rec.get("run_id") or ""))
                    op_repo = str((st or {}).get("repo") or "")
                except SystemExit:
                    continue
            if op_repo != name:
                continue
            hint = prepare_rederive_hint(
                name, str(rec.get("op_id") or opf.stem))
            if not isinstance(hint, dict) \
                    or hint.get("verdict") != "ok":
                continue
            dep = str(rec.get("dep") or "")
            target = str(rec.get("target") or "")
            lane = str(rec.get("lane") or "")
            note = str(hint.get("note") or "")
            question = "기준에서 다시 적용 가능 — '다시 시도'로 진행"
            if note:
                question += f" ({note})"
            age = m1_question_age_days(
                {"updated_at": rec.get("updated_at"),
                 "created_at": rec.get("created_at")}, now_ts)
            items.append({
                "kind": "prepare-retry",
                "ref": str(rec.get("run_id") or ""),
                "question": question[:500],
                "age_days": age,
                "age_text": "날짜 미기록" if age is None
                            else ("오늘" if age == 0
                                  else f"{age}일째 대기"),
                "why": f"{dep} {target} ({lane}) — 충돌로 막힌 준비를 "
                       "기준 위에서 다시 적용" if dep else
                       "충돌로 막힌 준비를 기준 위에서 다시 적용",
                "suggested": "", "suggested_basis": ""})
        except (Exception, SystemExit):  # noqa: BLE001 - drop one entry
            continue
    return items


def _m1_joint_items(name: str, now_ts: float) -> list:
    """M2.14a: 사람이 할 일 entries for joint pairs.

    Scans this repo's op records for joint_hint caches naming a
    partner. Each pair reads as one entry per run (kind
    joint-retry, verdict-neutral like prepare-retry): X's entry
    names the partner dep/target, Y's entry names X's. Entries
    point at the run's work view, where the M2.14b 함께 다시 시도
    button lives on the hinted card. Hint failures only drop
    entries; this never raises and never acts.
    """
    items = []
    try:
        files = sorted(integration_ops_dir().glob("prep-*.json"))
    except (Exception, SystemExit):  # noqa: BLE001 - no scan, no items
        return items
    seen_runs = set()
    for opf in files:
        try:
            rec = read_json(opf, default=None)
            if not isinstance(rec, dict):
                continue
            hint = rec.get("joint_hint")
            if not isinstance(hint, dict):
                continue
            if not hint.get("partner_run"):
                continue
            op_repo = str(rec.get("repo") or "")
            if not op_repo:
                try:
                    st = store.load(str(rec.get("run_id") or ""))
                    op_repo = str((st or {}).get("repo") or "")
                except SystemExit:
                    continue
            if op_repo != name:
                continue
            x_run = str(rec.get("run_id") or "")
            x_dep = str(rec.get("dep") or "")
            x_target = str(rec.get("target") or "")
            y_run = str(hint.get("partner_run") or "")
            y_dep = str(hint.get("partner_dep") or "")
            y_target = str(hint.get("partner_target") or "")
            if not x_run or not y_run:
                continue
            age = m1_question_age_days(
                {"updated_at": rec.get("updated_at"),
                 "created_at": rec.get("created_at")}, now_ts)
            age_text = "날짜 미기록" if age is None \
                else ("오늘" if age == 0 else f"{age}일째 대기")
            # M2.14b-fix: both entries point at the run whose
            # card carries the button (this record's run — its
            # latest shows the hinted prep). The partner's entry
            # names this run's dep/target.
            for ref, dep, target in (
                    (x_run, y_dep, y_target),
                    (x_run, x_dep, x_target)):
                if not dep or (ref, dep) in seen_runs:
                    continue
                seen_runs.add((ref, dep))
                items.append({
                    "kind": "joint-retry",
                    "ref": ref,
                    "question": (f"{dep} {target}과 함께 올려야 함 — "
                                 "'함께 다시 시도'로 진행")[:500],
                    "age_days": age,
                    "age_text": age_text,
                    "why": f"{dep} {target} — 따로 올리면 RN 혼재로 "
                           "막힘, 함께 올려야 함" if dep else
                           "따로 올리면 RN 혼재로 막힘",
                    "suggested": "", "suggested_basis": ""})
        except (Exception, SystemExit):  # noqa: BLE001 - drop one entry
            continue
    return items


def _joint_supersede_map() -> dict:
    """M2.14b: batch id -> done joint op id (display only).

    A standalone batch built from a joint partner's op is
    superseded once the joint record is done; its card names the
    joint op. The hold itself is never changed or deleted here.
    Read-only; unreadable records simply never match.
    """
    out = {}
    try:
        # M2.17c: refreshed joints append -joint-<dep>-<target>
        # (derive_joint_refresh), so a bare -joint.json glob never
        # sees them — and the M2.17 refreshed half below maps
        # nothing. Scan both shapes; the kind/status filters below
        # still admit only done joint records.
        files = sorted(integration_ops_dir().glob("prep-*-joint*.json"))
    except (Exception, SystemExit):  # noqa: BLE001 - no scan, no notes
        return out
    for opf in files:
        try:
            rec = read_json(opf, default=None)
        except (Exception, SystemExit):  # noqa: BLE001 - skip one file
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("op") != "reresolve" \
                or rec.get("kind") != "joint" \
                or rec.get("status") != "done":
            continue
        joint_op = str(rec.get("op_id") or "")
        if not joint_op:
            continue
        for y_op in (rec.get("joint_with") or []):
            y_op = str(y_op or "")
            if not y_op:
                continue
            try:
                from . import m2 as _m2
                bid = _m2.batch_id_for_op(y_op)
            except (Exception, SystemExit):  # noqa: BLE001 - skip
                continue
            out.setdefault(bid, joint_op)
    # M2.17 B: a refreshed done joint supersedes the done joint it
    # refreshes. Same repo+lane and member-dep pair, one member
    # target strictly newer and the other equal: the old joint's
    # batch reads the same "함께 올림으로 대체" note keyed by the
    # new joint op id. Display-only; holds and attests untouched.
    try:
        from . import versions as _versions

        def _pair(rec):
            cands = [c for c in (rec.get("joint_candidates") or [])
                     if isinstance(c, dict)]
            if len(cands) != 2:
                return None
            return sorted(
                (str(c.get("dep") or ""), str(c.get("target") or ""))
                for c in cands)

        def _refreshed(newer, older):
            pn, po = _pair(newer), _pair(older)
            if pn is None or po is None:
                return False
            if [d for d, _t in pn] != [d for d, _t in po]:
                return False
            try:
                cmps = [_versions.compare(nt, ot)
                        for (_d, nt), (_e, ot) in zip(pn, po)]
            except (Exception, SystemExit):  # noqa: BLE001 - no cmp
                return False
            return sorted(cmps) == [0, 1]

        joints = [rec for rec in [
            read_json(opf, default=None) for opf in files]
            if isinstance(rec, dict)
            and rec.get("op") == "reresolve"
            and rec.get("kind") == "joint"
            and rec.get("status") == "done"
            and str(rec.get("op_id") or "")]
        try:
            batches_here = list_batches()
        except (Exception, SystemExit):  # noqa: BLE001 - no map
            batches_here = []
        by_op = {}
        for b in batches_here or []:
            if isinstance(b, dict) and b.get("origin_m2"):
                by_op.setdefault(str(b.get("origin_m2") or ""),
                                 str(b.get("id") or ""))
        for old in joints:
            old_key = (str(old.get("repo") or ""),
                       str(old.get("lane") or ""))
            old_base = str((old.get("base") or {}).get("batch") or "")
            cands = [new for new in joints
                     if new is not old
                     and str(new.get("repo") or "") == old_key[0]
                     and str(new.get("lane") or "") == old_key[1]
                     and str((new.get("base") or {}).get("batch")
                             or "") == old_base
                     and _refreshed(new, old)]
            if not cands:
                continue
            best = sorted(
                cands,
                key=lambda r: (str(r.get("updated_at") or ""),
                              str(r.get("op_id") or "")))[-1]
            best_op = str(best.get("op_id") or "")
            old_bid = by_op.get(str(old.get("op_id") or ""))
            if best_op and old_bid:
                out.setdefault(old_bid, best_op)
    except (Exception, SystemExit):  # noqa: BLE001 - no map, no notes
        pass
    return out


def _auto_supersede_map(batch_list: list | None = None) -> dict:
    """M2.15: auto batch id -> newer auto batch id (display only).

    An auto batch whose single primary is superseded by a newer
    auto batch for the same repo/dep/lane (live: the webpack
    5.112.1 batch vs 5.112.2) reads "더 새 후보로 대체: <batch>"
    on its card. Grouped by (repo, dep, lane) over single-primary
    origin_m2 batches; every older member maps to the newest one
    (strictly newer updated_at — ties never map, fail-closed).
    Never touches holds or files.
    """
    out = {}
    try:
        items = batch_list if batch_list is not None \
            else list_batches()
    except (Exception, SystemExit):  # noqa: BLE001 - no map
        return out
    groups: dict = {}
    try:
        for b in items or []:
            if not isinstance(b, dict):
                continue
            if not b.get("origin_m2"):
                continue
            primaries = [p for p in (b.get("primaries") or [])
                         if isinstance(p, dict)]
            if len(primaries) != 1:
                continue
            dep = str(primaries[0].get("dep") or "")
            lane = str(primaries[0].get("lane") or "")
            bid = str(b.get("id") or "")
            if not (dep and lane and bid):
                continue
            key = (str(b.get("repo") or ""), dep, lane)
            groups.setdefault(key, []).append(
                (str(b.get("updated_at") or ""), bid))
    except (Exception, SystemExit):  # noqa: BLE001 - no map
        return out
    for _key, rows in groups.items():
        if len(rows) < 2:
            continue
        newest = max(rows)
        top_upd, top_bid = newest
        if not top_upd:
            continue
        for upd, bid in rows:
            if bid != top_bid and upd and upd < top_upd:
                out.setdefault(bid, top_bid)
    return out


def _joint_refresh_hints(batch_list) -> dict:
    """M2.17b C: batch id -> refresh hint for joint batches with a
    queued newer member (display only).

    For a batch built from a done joint op (origin_m2): when the
    sched queue holds a same-repo+lane item for one of the
    joint's member deps with a semver-greater target, the old
    joint's card names the refresh
    {dep, from, to, y_run, x_op, base} — the manual
    /api/integration/joint-refresh inputs (x_op is the joint's
    derived_from partner solo op, base its batch). No hint when
    a done or blocked joint already covers the member run and
    the queued run (the refresh already happened). Read-only;
    holds, attests and files untouched; never raises.
    """
    out: dict = {}
    try:
        items = batch_list if batch_list is not None \
            else list_batches()
    except (Exception, SystemExit):  # noqa: BLE001 - no hints
        return out
    queues: dict = {}
    covered_cache: dict = {}

    def _queue(repo):
        if repo not in queues:
            try:
                queues[repo] = sched.load(repo).get("queue") or []
            except (Exception, SystemExit):  # noqa: BLE001 - none
                queues[repo] = []
        return queues[repo]

    def _covered(repo, lane, base_id, run_a, run_b):
        key = (repo, lane)
        if key not in covered_cache:
            try:
                covered_cache[key] = m2._joint_recs(
                    repo, lane, ("done", "blocked"))
            except (Exception, SystemExit):  # noqa: BLE001 - none
                covered_cache[key] = []
        for rec in covered_cache[key]:
            if base_id and str(rec.get("batch_id") or "") \
                    != base_id:
                continue
            runs = set(rec.get("runs") or [])
            if run_a in runs and run_b in runs:
                return True
        return False

    try:
        for b in items or []:
            if not isinstance(b, dict) or not b.get("origin_m2"):
                continue
            bid = str(b.get("id") or "")
            if not bid or bid in out:
                continue
            try:
                jrec = read_json(op_file(str(b.get("origin_m2"))),
                                 default=None)
            except (Exception, SystemExit):  # noqa: BLE001 - skip
                continue
            if not isinstance(jrec, dict) \
                    or jrec.get("kind") != "joint":
                continue
            repo = str(b.get("repo") or jrec.get("repo") or "")
            lane = str(jrec.get("lane") or "")
            base_id = str(jrec.get("batch_id") or "")
            x_op = str(jrec.get("derived_from") or "")
            members = {}
            for c in jrec.get("joint_candidates") or []:
                if isinstance(c, dict) and c.get("dep"):
                    members[str(c.get("dep"))] = (
                        str(c.get("target") or ""),
                        str(c.get("run") or ""))
            if not repo or not lane or not members or not x_op:
                continue
            partner_of = {}
            if len(members) == 2:
                deps = list(members)
                partner_of[deps[0]] = members[deps[1]][1]
                partner_of[deps[1]] = members[deps[0]][1]
            best = None
            for item in _queue(repo):
                if not isinstance(item, dict):
                    continue
                if str(item.get("repo") or repo) != repo:
                    continue
                if str(item.get("lane") or "") != lane:
                    continue
                dep = str(item.get("dep") or "")
                if dep not in members:
                    continue
                mtarget, mrun = members[dep]
                itarget = str(item.get("target") or "")
                y_run = str(item.get("run_id") or "")
                if not itarget or not y_run or not mrun:
                    continue
                try:
                    greater = _versions.compare(itarget,
                                                mtarget) == 1
                except (Exception, SystemExit):  # noqa: BLE001
                    greater = False
                if not greater:
                    continue
                # Covered means a refresh was already derived for
                # THIS pair: the partner run plus the queued run.
                x_run = partner_of.get(dep) or ""
                if base_id and x_run and _covered(
                        repo, lane, base_id, x_run, y_run):
                    continue
                if best is None:
                    best = {"dep": dep, "from": mtarget,
                            "to": itarget, "y_run": y_run,
                            "x_op": x_op, "base": base_id}
                else:
                    try:
                        newer = _versions.compare(
                            itarget, best["to"]) == 1
                    except (Exception, SystemExit):  # noqa: BLE001
                        newer = False
                    if newer:
                        best.update({"dep": dep, "from": mtarget,
                                     "to": itarget, "y_run": y_run,
                                     "x_op": x_op, "base": base_id})
            if best:
                out[bid] = best
    except (Exception, SystemExit):  # noqa: BLE001 - hints bonus
        pass
    return out


def _batch_consumer_label(batch: dict):
    """M2.15: the consumer-build label for a batch card (or "").

    The op's fastcheck receipt consumer record ("소비 스크립트
    build:web 통과 (N초)") — display only, like _batch_pod_drift.
    Read-only; never raises, never blocks.
    """
    try:
        op_id = str(batch.get("origin_m2") or "")
        if not op_id:
            return ""
        from . import fastcheck as _fc
        summary = read_json(_fc.op_fastcheck_path(op_id),
                            default=None)
        cons = (summary or {}).get("consumer") \
            if isinstance(summary, dict) else None
        if isinstance(cons, dict) and cons.get("status") == "pass" \
                and cons.get("label"):
            return str(cons.get("label") or "")
    except (Exception, SystemExit):  # noqa: BLE001 - no label
        pass
    return ""


def _pr_norm_target(value) -> str:
    """Compare versions across ^/~ range spellings (display aid only)."""
    return str(value or "").strip().lstrip("^~>=< ")


def _pr_run_diffstat(run: dict) -> str:
    """The run's work tree vs its base, one read-only git call."""
    from . import prs as _prs
    rid = str((run or {}).get("id") or "")
    if not rid:
        return "실행 내역 읽기 실패"
    try:
        ward = store.run_dir(rid) / "work"
    except (Exception, SystemExit):  # noqa: BLE001 - no dir, no stat
        return "실행 내역 읽기 실패"
    return _prs.work_diffstat(str(ward), str((run or {}).get("baseCommit")
                                             or ""))


def _pr_scan_diffstat(pr: dict) -> str:
    """The PR's size from the scan fields (no fetch)."""
    try:
        adds = int((pr or {}).get("additions") or 0)
        dels = int((pr or {}).get("deletions") or 0)
        files = int((pr or {}).get("files") or 0)
    except (TypeError, ValueError):
        return "규모 미기록"
    if adds <= 0 and dels <= 0 and files <= 0:
        return "규모 미기록"
    return f"+{adds}/-{dels} · 파일 {files}개"


def _m1_pr_items(name: str, runs, sched_state,
                 now_ts: float) -> tuple:
    """M3.5: 사람이 할 일 entries for external PRs + glance counts.

    Returns (items, counts). Deterministic, no model. An external
    PR closing an issue OpenMainty ran reads as one entry (kind
    external-pr, verdict-neutral like prepare-retry) with both
    diffstats. Bot bumps match queue deps or are counted. Titles,
    bodies and author names are untrusted display text (bounded in
    prs.sanitize_pr, escaped again by the dashboard).
    """
    from . import prs as _prs
    items = []
    counts = {"open": 0, "external": 0, "bots": 0, "error": "",
              "bot_lines": []}
    try:
        state = sched_state or {}
        stored = [p for p in (state.get("last_prs") or [])
                  if isinstance(p, dict)]
        counts["error"] = str(state.get("prs_error") or "")
        counts["open"] = len(stored)
        counts["bots"] = sum(1 for p in stored if p.get("bot"))
        counts["external"] = len(stored) - counts["bots"]
        by_issue = {}
        for run in runs or []:
            if not isinstance(run, dict):
                continue
            if (run.get("repo") or "") != name:
                continue
            issue = str(run.get("issue") or "")
            if issue.isdigit() and issue not in by_issue:
                by_issue[issue] = run
        queue_deps = {}
        for it in (state.get("queue") or []):
            if not isinstance(it, dict):
                continue
            if (it.get("repo") or "") not in ("", name):
                continue
            dep = str(it.get("dep") or "")
            if dep:
                queue_deps.setdefault(dep.lower(), it)
        closers = _prs.closing_map(stored)
        for issue_no in sorted(closers):
            pr = closers[issue_no]
            run = by_issue.get(str(issue_no))
            if run is None:
                continue  # 2b renders on the issue board, not here
            run_stat = _pr_run_diffstat(run)
            pr_stat = _pr_scan_diffstat(pr)
            age = m1_question_age_days(run, now_ts)
            items.append({
                "kind": "external-pr",
                "ref": str(run.get("id") or ""),
                "question": (f"외부 PR #{pr.get('number')} "
                             f"({pr.get('author') or '?'}) — 이슈 "
                             f"#{issue_no}를 닫음 · OpenMainty 초안"
                             f"({run.get('id') or '?'})과 비교 후 "
                             f"결정")[:500],
                "age_days": age,
                "age_text": "날짜 미기록" if age is None
                            else ("오늘" if age == 0
                                  else f"{age}일째 대기"),
                "why": f"초안: {run_stat} / PR: {pr_stat}",
                "run_stat": run_stat, "pr_stat": pr_stat,
                "pr_number": pr.get("number"),
                "repo": name,
                "suggested": "", "suggested_basis": ""})
        for pr in stored:
            if not pr.get("bot"):
                continue
            bump = _prs.parse_dependabot(str(pr.get("title") or ""))
            if bump is None:
                counts["bot_lines"].append({
                    "pr": pr.get("number"), "dep": "",
                    "note": "제목에서 의존성을 읽지 못함"})
                continue
            queued = queue_deps.get(str(bump.get("dep") or "").lower())
            if queued is None:
                counts["bot_lines"].append({
                    "pr": pr.get("number"), "dep": bump["dep"],
                    "note": f"{bump['from']} -> {bump['to']}"})
                continue
            same = (_pr_norm_target(bump.get("to"))
                    == _pr_norm_target(queued.get("target")))
            counts["bot_lines"].append({
                "pr": pr.get("number"), "dep": bump["dep"],
                "note": (f"{bump['from']} -> {bump['to']} · 같은 목표"
                         if same else
                         f"{bump['from']} -> {bump['to']} · 다른 목표 "
                         f"(우리: {queued.get('target') or '?'})")})
    except (Exception, SystemExit):  # noqa: BLE001 - PRs never break
        pass
    return items, counts


def _m1_repo_row(name: str, runs, now_ts: float) -> dict:
    """One repo's snapshot row (raises only for the caller's containment).
    Evaluations are always fresh (see _m1_fresh_evals): a live drift
    changes the verdict on the next refresh even when no batch file
    moved."""
    unreadable = []
    try:
        profile = repos.load(name)
    except SystemExit as exc:
        raise IntegrationError(f"repo profile unreadable: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - profile is essential
        raise IntegrationError(f"repo profile unreadable: {exc}") from exc
    checkout = ""
    try:
        from . import pcconfig as _pc
        checkout = (str(((_pc.load().get("checkouts", {})) or {}).get(name)
                        or ""))
    except Exception:  # noqa: BLE001 - checkout is best-effort
        checkout = ""
    if not checkout:
        try:
            checkout = str(profile.get("checkout") or "")
        except Exception:  # noqa: BLE001 - dict-shaped profiles only
            checkout = ""
    repo_batches = _m1_repo_batches(name)
    involved = _m1_involved(repo_batches)
    try:
        live, diag = live_map_with_diag(involved)
    except Exception as exc:  # noqa: BLE001 - live read degrades verdict
        live, diag = {}, {}
        unreadable.append(f"원본 읽기 ({type(exc).__name__})")
    precomputed, eval_bad = _m1_fresh_evals(repo_batches, live, diag)
    unreadable.extend(eval_bad)
    try:
        summ = summarize(repo_batches, live, precomputed=precomputed)
    except Exception as exc:  # noqa: BLE001 - summary degrades verdict
        summ = {}
        unreadable.append(f"묶음 집계 ({type(exc).__name__})")
    repo_summ = (summ.get(name) or {}) if isinstance(summ, dict) else {}
    ready = int(repo_summ.get("ready") or 0)
    try:
        sched_state = sched.load(name)
    except Exception:  # noqa: BLE001 - worker state is display-only
        sched_state = {}
    try:
        waiting_review = len(uncovered_candidates(
            (sched_state or {}).get("queue", []), repo_batches,
            name) or [])
    except Exception as exc:  # noqa: BLE001 - fail closed, never hide
        unreadable.append(f"검토 대기 집계 ({type(exc).__name__})")
        waiting_review = 0
    pending_release = ready
    rel = m1_release_info(checkout)
    if not rel.get("last_release"):
        unreadable.append("출시일 읽기")
    # M2.24: the status rows carry leaf caveats from the same
    # fresh evaluations (read-only; op files untouched).
    try:
        _cav_by_id = {b.get("id"): b for b in repo_batches
                      if isinstance(b, dict) and b.get("id")}
        rollups = m1_rollup_status(name, _cav_by_id, precomputed)
    except Exception:  # noqa: BLE001 - rows degrade to no caveats
        rollups = m1_rollup_status(name)
    blocked_items = []
    for lane in ("next", "main"):
        r = rollups.get(lane)
        if r and r.get("status") == "blocked":
            blocked_items.append({
                "what": f"{lane} 레인 합치기 ({r.get('leaves', 0)}개 묶음)",
                "reason": r.get("error") or "사유 미기록",
                "op": r.get("op") or ""})
    need_you = []
    if runs is None:
        unreadable.append("실행 기록 읽기")
    else:
        for run in runs:
            if not isinstance(run, dict):
                continue
            if (run.get("repo") or "") != name:
                continue
            wait = str(run.get("waitReason") or "")
            if not wait.startswith("question:"):
                continue
            question = wait[len("question:"):].strip()
            age = m1_question_age_days(run, now_ts)
            sug = m1_suggest(question)
            dep = str(run.get("dep") or "")
            target = str(run.get("target") or "")
            issue_no = str(run.get("issue") or "")
            run_title = str(run.get("title") or "").strip()
            if issue_no.isdigit():
                context = f"#{issue_no}" + (f" {run_title}" if run_title
                                            else "")
                question = f"{context} — {question}"
            why = (f"{dep} {target} — 답이 없으면 묶음에 넣지 못합니다"
                   if dep else "답이 없으면 진행이 멈춥니다")
            need_you.append({
                "kind": "question",
                "ref": str(run.get("id") or ""),
                "question": question[:500],
                "age_days": age,
                "age_text": "날짜 미기록" if age is None
                            else ("오늘" if age == 0 else f"{age}일째 대기"),
                "why": why,
                "suggested": sug.get("text") or "",
                "suggested_basis": sug.get("basis") or ""})
    question_runs = {str(n.get("ref") or "") for n in need_you}
    issue_waiting = []
    for it in ((sched_state or {}).get("queue", []) or []):
        if not isinstance(it, dict):
            continue
        if str(it.get("status") or "") != "waiting-review":
            continue
        if it.get("repo") and it.get("repo") != name:
            continue
        if it.get("dep"):
            continue
        if str(it.get("kind") or "") != "issue-triage":
            continue
        run_id = str(it.get("run_id") or "")
        if not run_id or run_id in question_runs:
            continue
        age = m1_question_age_days(it, now_ts)
        issue_no = str(it.get("issue") or "")
        issue_title = str(it.get("title") or "").strip()
        if issue_no and issue_title:
            shown_title = f"#{issue_no} {issue_title}"
        elif issue_title:
            shown_title = issue_title
        else:
            shown_title = f"이슈 #{issue_no}" if issue_no else "이슈"
        issue_waiting.append({
            "kind": "issue-review",
            "ref": run_id,
            "title": shown_title[:300],
            "issue": str(it.get("issue") or ""),
            "age_days": age,
            "age_text": "날짜 미기록" if age is None
                        else ("오늘" if age == 0 else f"{age}일째 대기"),
            "why": "Muse가 이슈를 분석해 둠 — 내용 확인 후 "
                   "답변/종료 결정",
            "suggested": "", "suggested_basis": ""})
    need_you.extend(issue_waiting)
    need_you.sort(key=lambda x: (x.get("age_days") is None,
                                 x.get("age_days") or 0),
                  reverse=True)
    # N1: a reshaped lane the driver refused to roll in parallel
    # (pure gate below: shown only while the named op is still the
    # newest done rollup for the lane).
    item = reshaped_note_item(sched_state, rollups, now_ts)
    if item is not None:
        need_you.append(item)
    # S15(b): refresh runs queued while execute is off (pure gate
    # below: shown only while such an item is live — queued work the
    # owner must switch on to move).
    item = refresh_pending_item(sched_state, now_ts)
    if item is not None:
        need_you.append(item)
    item = refresh_deferred_item(sched_state, now_ts)
    if item is not None:
        need_you.append(item)
    # M2.11c: blocked cherry-pick prepares whose re-derive hint
    # says ok read as one 사람이 할 일 entry each (kind
    # prepare-retry, verdict-neutral like issue-review).
    for b in _m1_prepare_retry_items(name, now_ts):
        need_you.append(b)
    # M2.14a: joint pairs read as one entry per run (kind
    # joint-retry, verdict-neutral like prepare-retry).
    for b in _m1_joint_items(name, now_ts):
        need_you.append(b)
    # M3.5: external PRs (read-only linkage, verdict-neutral like
    # prepare-retry). Titles/authors are untrusted display text.
    pr_items, pr_counts = _m1_pr_items(
        name, runs, sched_state, now_ts)
    for b in pr_items:
        need_you.append(b)
    buds = []
    for batch in repo_batches:
        ev = precomputed.get(batch.get("id"))
        results = (ev or {}).get("results") if isinstance(ev, dict) \
            else None
        if not results or not all(isinstance(r, dict) and r.get("ready")
                                  for r in results):
            continue
        prims = [p for p in (batch.get("primaries") or [])
                 if isinstance(p, dict)]
        if len(prims) != 1:
            continue
        buds.append((batch.get("id"), str(prims[0].get("dep") or ""),
                     str(prims[0].get("target") or ""),
                     str(batch.get("updated_at") or "")))
    story = _m1_story(name, buds, cap=True)
    story_full = _m1_story(name, buds, cap=False)
    verdict = m1_verdict(
        ready=ready, blocked=len(blocked_items),
        questions=sum(1 for n in need_you
                      if n.get("kind") not in ("issue-review",
                                              "rollup-reshaped",
                                              "prepare-retry",
                                              "joint-retry",
                                              "external-pr")),
        unreadable=unreadable,
        waiting_review=waiting_review,
        issue_waiting=len(issue_waiting),
        pending_release=pending_release,
        days_since_release=rel.get("days_since_release"))
    worker = "busy" if ((sched_state or {}).get("current_job")
                        or _m1_lease_live()) else "idle"
    if rel.get("last_release") and repo_batches:
        all_unpub: bool | None = all(
            str(b.get("updated_at") or "")[:10]
            > str(rel.get("last_release") or "")
            for b in repo_batches)
    elif rel.get("last_release"):
        all_unpub = True
    else:
        all_unpub = None
    next_steps = _m1_next_steps(name, repo_batches, precomputed,
                                rollups)
    row = {
        "name": name, "verdict": verdict["verdict"],
        "verdict_rule": verdict["rule"],
        "verdict_line": verdict["line"],
        "next_steps": next_steps,
        "pending_release": {
            "count": pending_release,
            "main_date": (rel.get("main_date") or "")[:10] or None,
            "next_date": (rel.get("next_date") or "")[:10] or None,
            "main_days": rel.get("main_days"),
            "next_days": rel.get("next_days"),
            "release_tag": rel.get("release_tag") or "",
            "release_tag_date": (rel.get("release_tag_date") or "")[:10]
                                or None,
            "last_release": rel.get("last_release"),
            "basis": rel.get("basis") or "unknown",
            "days_since": rel.get("days_since_release"),
            "all_unpublished": all_unpub},
        "rollup": {lane: rollups.get(lane) or {"status": "none",
                                               "leaves": 0}
                   for lane in ("next", "main")},
        "need_you": need_you, "blocked": blocked_items,
        "prs": pr_counts,
        "story": story, "story_full": story_full,
        "system": {"worker": worker,
                   "next_scan": sched_state.get("next_scan"),
                   "snapshot_writer": "",
                   "repos_updated": "",
                   "app_sha": _m1_app_sha(),
                   "m2": _m1_m2_line(name)},
        "unreadable": unreadable,
        "updated_at": utcnow()}
    return row


def _m1_app_sha():
    """Running-promotion sha baked at refresh time (None on dev).
    The glance shows it with no second request."""
    try:
        return promote.running_sha()
    except Exception:  # noqa: BLE001 - never break a snapshot row
        return None


def _m1_m2_line(name):
    """Autonomy line baked at refresh time: {policy, line, guard_ok,
    guard_reason}. The glance renders it with no second request; a
    blocked guard shows the plain message, never silence."""
    try:
        policy = m2.get_policy(name)
        guard = m2.promoted_guard()
        return {"policy": policy, "line": m2.policy_line(policy),
                "guard_ok": bool(guard.get("ok")),
                "guard_reason": str(guard.get("reason") or ""),
                "fastcheck_line": m2.fastcheck_count_line(name)}
    # M25a-F1: a CLI-style SystemExit from deep code must never kill
    # the glance either. KeyboardInterrupt still propagates.
    except (Exception, SystemExit):  # noqa: BLE001 - never break a row
        return {"policy": {}, "line": "꺼짐", "guard_ok": False,
                "guard_reason": ""}


def _m1_lease_live() -> bool:
    try:
        return bool((sched.global_lock_status() or {}).get("live"))
    except Exception:  # noqa: BLE001 - lease probe is best-effort
        return False


_SAFE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _re_safe_date(text: str) -> bool:
    return bool(_SAFE_DATE_RE.match(str(text or "")))


def digest_file(date_str: str) -> Path:
    return summary_dir() / "digest" / (date_str + ".json")


def read_digest(date_str: str) -> dict | None:
    """One stored digest, or None when the day was never written."""
    if not _re_safe_date(date_str):
        return None
    try:
        data = read_json(digest_file(date_str), default=None)
    except Exception:  # noqa: BLE001 - unreadable day reads as missing
        return None
    return data if isinstance(data, dict) else None


def digest_md_file(date_str: str) -> Path:
    return summary_dir() / "digest" / (date_str + ".md")


def m1_local_day(offset_days: int = 0) -> str:
    """Local (owner-timezone) calendar day as YYYY-MM-DD. The machine
    runs in the owner's timezone; no zone is hardcoded."""
    import datetime
    try:
        now = datetime.datetime.now().astimezone()
    except Exception:  # noqa: BLE001 - without tz, fall back to UTC
        now = datetime.datetime.now(datetime.timezone.utc)
    if offset_days:
        now = now + datetime.timedelta(days=offset_days)
    return now.strftime("%Y-%m-%d")


def m1_local_hhmm() -> str:
    import datetime
    try:
        return datetime.datetime.now().astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001 - clock failure reads empty
        return ""


def write_digest(date_str: str = "", provisional: bool = False,
                 snap: dict | None = None) -> dict:
    """Build one in-app daily digest from a snapshot + events.

    Stored under <home>/summary/digest/ as JSON (inputs referenced) and
    the same Korean Markdown the page shows. NEVER writes to ~/Desktop
    (those files belong to the owner's own cron). A mid-day write is
    titled '오늘 지금까지' and never marks the day finished
    (last_digest_day only advances on a finished day). Returns the digest.
    """
    if not date_str:
        date_str = m1_local_day()
    if snap is None:
        snap = refresh_summary_snapshot("digest")
    if provisional:
        title = f"오늘 지금까지 ({m1_local_hhmm()} 기준)"
    else:
        title = f"{date_str} 하루 요약"
    digest_dir = summary_dir() / "digest"
    digest_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for row in snap.get("repos") or []:
        if not isinstance(row, dict):
            continue
        cards.append({
            "repo": row.get("name"), "verdict": row.get("verdict"),
            "verdict_line": row.get("verdict_line"),
            "released": row.get("pending_release"),
            "need_you": row.get("need_you"),
            "blocked": row.get("blocked"),
            "story": (row.get("story_full")
                      or row.get("story") or [])[:12]})
    data = {"schema": 1, "date": date_str, "at": utcnow(),
            "snapshot_at": snap.get("at"), "repos": cards,
            "provisional": provisional, "title": title}
    _atomic_write_json(digest_file(date_str), data)
    lines = [f"# {title}", ""]
    for card in cards:
        lines.append(f"## {card.get('repo')}")
        lines.append(str(card.get("verdict_line") or ""))
        lines.append("")
        for text in (card.get("story") or [])[:8]:
            lines.append(f"- {text}")
        lines.append("")
        for item in (card.get("need_you") or []):
            line = m1_need_line(item)
            if line:
                lines.append(line)
        for item in (card.get("blocked") or []):
            lines.append(f"- 막힘: {item.get('what')} — {item.get('reason')}")
        lines.append("")
    digest_md_file(date_str).write_text("\n".join(lines),
                                        encoding="utf-8")
    if not provisional:
        try:
            summary_digest_day_path().write_text(date_str,
                                                 encoding="utf-8")
        except OSError:
            pass
    return data


def refresh_pending_item(sched_state, now_ts):
    """S15(b) display gate (pure): a queued refresh run while execute
    is off, or None. Shown only while such an item is live — the
    owner must switch execute on for it to move. Never invents text:
    names the dep, target and age from the item itself."""
    st = sched_state or {}
    if st.get("execute_enabled"):
        return None
    pend = [i for i in (st.get("queue") or []) if isinstance(i, dict)
            and i.get("origin") == "m2-refresh"
            and str(i.get("status") or "") in sched.LIVE_STATUSES]
    if not pend:
        return None
    first = sorted(pend,
                   key=lambda i: str(i.get("created_at") or ""))[0]
    age = m1_question_age_days({"updated_at": first.get("created_at")},
                               now_ts)
    dep = str(first.get("dep") or "?")
    target = str(first.get("target") or "?")
    return {
        "kind": "refresh-pending",
        "ref": str(first.get("key") or ""),
        "title": (f"새 기준 실행 대기 {len(pend)}건 ({dep} {target}…) — "
                  f"실행이 꺼져 있어 진행하지 못합니다")[:300],
        "age_days": age,
        "age_text": "날짜 미기록" if age is None
                    else ("오늘" if age == 0 else f"{age}일째 대기"),
        "why": "사람이 실행을 켜야 대기열이 진행됩니다",
        "suggested": "", "suggested_basis": ""}


def refresh_deferred_item(sched_state, now_ts):
    """S16 display gate (pure): a queued refresh item whose base batch
    head is unresolvable, or None. Shown only while such an item is
    live — the owner must check the base batch. Never invents text:
    names the dep, target and base batch from the item itself."""
    st = sched_state or {}
    pend = [i for i in (st.get("queue") or []) if isinstance(i, dict)
            and i.get("origin") == "m2-refresh"
            and i.get("refresh_deferred")
            and str(i.get("status") or "") in sched.LIVE_STATUSES]
    if not pend:
        return None
    first = sorted(pend,
                   key=lambda i: str(i.get("created_at") or ""))[0]
    age = m1_question_age_days({"updated_at": first.get("created_at")},
                               now_ts)
    dep = str(first.get("dep") or "?")
    target = str(first.get("target") or "?")
    base = str(first.get("refresh_base") or "?")
    return {
        "kind": "refresh-deferred",
        "ref": str(first.get("key") or ""),
        "title": (f"새로고침 보류 {len(pend)}건 ({dep} {target}) — 새 기준 "
                  f"묶음 {base}을 찾지 못함")[:300],
        "age_days": age,
        "age_text": "날짜 미기록" if age is None
                    else ("오늘" if age == 0 else f"{age}일째 대기"),
        "why": "기준 묶음을 확인해야 대기열이 진행됩니다",
        "suggested": "", "suggested_basis": ""}


def reshaped_note_item(sched_state, rollups, now_ts):
    """N1 display gate (pure): the reshaped-lane need_you item, or
    None. Shown only while the stashed op is still the newest done
    rollup for the lane — a newer rollup hides it without touching
    anything."""
    reshaped = ((sched_state or {}).get("m2_rollup_reshaped") or {})
    if not isinstance(reshaped, dict) or not reshaped.get("op"):
        return None
    lane = str(reshaped.get("lane") or "")
    newest = (rollups.get(lane) or {}) if isinstance(rollups,
                                                    dict) else {}
    if newest.get("op") != reshaped.get("op"):
        return None
    age = m1_question_age_days(
        {"updated_at": str(reshaped.get("at") or "")}, now_ts)
    return {
        "kind": "rollup-reshaped",
        "ref": str(reshaped.get("op") or ""),
        "title": (f"{lane} 레인 구성이 바뀜 — 이전 합치기 "
                  f"{reshaped.get('op')}와 다름, 사람이 확인")[:300],
        "age_days": age,
        "age_text": "날짜 미기록" if age is None
                    else ("오늘" if age == 0 else f"{age}일째 대기"),
        "why": "자동 합치기를 멈춤 — 어떤 묶음을 합칠지 사람이 결정",
        "suggested": "", "suggested_basis": ""}


def m1_need_line(item: dict) -> str:
    """One digest '사람이 할 일' line (S10): an issue-review item
    carries title/ref instead of question text — fall back instead of
    printing an empty line, and return "" when nothing names the item
    so the caller drops it."""
    if not isinstance(item, dict):
        return ""
    text = (str(item.get("question") or "")[:160]
            or str(item.get("title") or "")[:160]
            or str(item.get("ref") or item.get("issue") or ""))
    if not text:
        return ""
    # L1: issue-review lines match their card count ("이슈 검토"),
    # never the question wording.
    if (item.get("kind") or "") == "issue-review":
        title = (str(item.get("title") or "")[:160] or text)
        return f"- 이슈 검토({item.get('age_text')}): {title}"
    # N1: a reshaped lane is owner work, not a question.
    if (item.get("kind") or "") == "rollup-reshaped":
        title = (str(item.get("title") or "")[:160] or text)
        return f"- 확인 필요({item.get('age_text')}): {title}"
    return f"- 질문({item.get('age_text')}): {text}"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON via a pid-unique tmp file + rename, so concurrent
    writers never share a path."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
