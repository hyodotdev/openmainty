"""Read-only conflict investigation for blocked typed-issue preparations.

Stage17 is investigation only: it reviews WHY a blocked prepare op
conflicted and WHAT a safe next action is. It never creates a derived
worktree/branch, never mutates target sources, and never marks the
parent BLOCKED op as done. A later Stage18 may implement an explicit
approved derived resolution; this module only produces the validated
read-only review that such a step would require.

Parent op bytes stay exact. Review input/report/attempt/job identities
are separate immutable artifacts under ``issue_recovery/<op_id>/``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

_OP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
# Stored review artifacts use only these controlled names. Anything
# else (traversal, symlink alias target, foreign file) is rejected
# before any read or write — never rebound or silently skipped.
_RECORD_RE = re.compile(
    r"^(input-[0-9a-f]{16}"
    r"|attempt-rr-[0-9a-f]{12}(-r[1-9][0-9]*)?"
    r"|report-[0-9a-f]{16}"
    r"|context-[0-9a-f]{16}"
    r"|owner-decision-[0-9a-f]{16}"
    r"|latest)\.json$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,6}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LANES = ("main", "next")
_FETCH_TIMEOUT_S = 20
_WINDOW_EACH_CAP = 20000
_WINDOW_TOTAL_CAP = 60000
_MAX_FILES = 8
# Review-input contract version. Version 3 additionally binds the
# FULL original candidate/base descriptors (branch/worktree, base
# batch/lane/checkout) — not only heads and fingerprints — into the
# fingerprint. Version 2 bound the canonical snapshot refs map and
# the full original attribution block. Records without a matching
# contract are preserved but explicitly unverifiable — never
# migrated, healed, or rebound.
_CONTRACT_VERSION = 3
# Supplemental-context record version. Version 1 is the only known
# version: an operator-provided context payload (see
# record_recovery_context) linked to one originating review input
# and report. Unknown context versions refuse as corrupt — never
# migrated or healed.
_CONTEXT_VERSION = 1
# Bound on one supplemental context text, in characters. The prompt
# section carries at most this much operator text, separate from the
# code evidence windows.
_CONTEXT_TEXT_CAP = 4000
# Bound on context records projected through status.
_CONTEXT_LIST_CAP = 20
# Owner-decision record version. Version 1 is the only known
# version: Root-recorded owner-tentative selections with exact
# provenance (see record_owner_decision), linked to one originating
# review input and report. Unknown versions refuse as corrupt.
_OWNER_DECISION_VERSION = 1
# The provenance literal is required verbatim on every decision and
# is never synthesized server-side: the record must say exactly how
# tentative the selections are and who adopted them.
_OWNER_DECISION_PROVENANCE = ("owner-tentative + delegated to Root "
                              "(Root adopts as operating decision)")
# Known certainty literals, shown verbatim in the UI. In v1 the only
# provenance literal is the owner-tentative + delegated one, so
# certainty MUST be "tentative" — "confirmed" would contradict it and
# is refused (write: 400, stored: corrupt). "confirmed" stays
# reserved for a future version with its own provenance literal.
_OWNER_DECISION_CERTAINTIES = ("tentative",)
# Bound on one decision selection, in characters.
_OWNER_DECISION_SELECTION_CAP = 200
# Bound on decisions per record and records projected through status.
_OWNER_DECISION_COUNT_CAP = 20
_OWNER_DECISION_LIST_CAP = 20
# Pinned build-metadata evidence (Source22 E1). Exactly these two
# package manifests are read at the three pinned refs (origin,
# candidate, base) — no other paths, no worktree reads, no network.
# The strings are SOURCE-BACKED DATA for the model to read, never
# permissions and never executable commands: matching declared text
# proves a declaration existed, nothing about builds or outputs.
_BUILD_PATHS = ("package.json", "example/package.json")
_BUILD_VERSION = 1
_BUILD_RAW_CAP = 32768
_BUILD_SCRIPTS_CAP = 64
_BUILD_NAME_CAP = 128
_BUILD_CMD_CAP = 2000
_BUILD_SECTION_CAP = 8192
# Stable taxonomy for per-script omission reasons. The renderer groups
# by this kind — never by the exact reason string, which embeds
# varying lengths — so an adversarial spread of command lengths
# cannot defeat the global section budget. Exact per-script reasons
# stay bound in the stored omitted_scripts entries and the status
# disclosure, never in the transmitted section text.
_BUILD_OMIT_KIND_LABEL = {
    "long-command": "긴 명령어 제외",
    "budget": "전체 예산 초과로 제외",
}
# Manifest path -> working directory the declared scripts run in.
# Derived from the allowlisted location only, never inferred.
_BUILD_CWD = {"package.json": "", "example/package.json": "example"}
_BUILD_FILE_KEYS = frozenset(
    ["path", "ref_kind", "ref", "blob_oid", "raw_sha256", "raw_len",
     "scripts", "omitted_scripts", "scripts_truncated", "text",
     "sent_sha256", "sent_bytes"])
_BUILD_OMIT_KEYS = frozenset(
    ["path", "ref_kind", "ref", "absent", "tree", "blob_oid", "size",
     "reason"])


class IssueRecoveryError(RuntimeError):
    """Typed failure; bytes on disk are always preserved."""

    def __init__(self, message="", status=500, code="error"):
        super().__init__(message)
        self.http_status = status
        self.code = code


def _home():
    from . import paths as _paths
    return _paths.home()


def _checked_root(root: Path) -> Path:
    """Fail-closed store directory: no symlink escape, no outside path.

    The op id is already regex-validated (single safe component), so
    the lexical tail below the unresolved home must be exactly
    ``issue_recovery/<op>`` and the resolved path must equal the
    canonical store path with the SAME name. Checking only the
    resolved tail lets an in-store alias (``store/A -> store/B``)
    hide behind its target while this function returns the
    unchecked alias ``A`` — that shape is refused here without
    touching history.
    """
    try:
        home = _home()
        home_r = home.resolve()
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable review home: {exc}",
                                 status=502, code="unreadable") from exc
    try:
        lex = Path(root).relative_to(home / "issue_recovery")
    except ValueError:
        raise IssueRecoveryError("review store escapes the app home",
                                 status=502, code="corrupt") from None
    if len(lex.parts) != 1:
        raise IssueRecoveryError("review store is not the op directory",
                                 status=502, code="corrupt")
    try:
        resolved = Path(root).resolve()
    except (OSError, RuntimeError) as exc:
        raise IssueRecoveryError(f"unreadable review store: {exc}",
                                 status=502, code="corrupt") from exc
    if resolved != home_r / "issue_recovery" / lex.parts[0]:
        # Alias, dangling target, loop, or a tail that only matches
        # after resolution: never follow it into reads or writes.
        raise IssueRecoveryError("refused symlinked review store",
                                 status=502, code="corrupt")
    for cand in (home_r / "issue_recovery",
                 home_r / "issue_recovery" / lex.parts[0]):
        try:
            st = os.lstat(cand)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise IssueRecoveryError(f"unreadable review store: {exc}",
                                     status=502,
                                     code="unreadable") from exc
        if stat.S_ISLNK(st.st_mode):
            raise IssueRecoveryError("refused symlinked review store",
                                     status=502, code="corrupt")
        if not stat.S_ISDIR(st.st_mode):
            raise IssueRecoveryError("review store is not a directory",
                                     status=502, code="corrupt")
    return root


def rec_root(op_id: str) -> Path:
    if not isinstance(op_id, str) or not _OP_RE.fullmatch(op_id):
        raise IssueRecoveryError(f"bad op id: {op_id!r}",
                                 status=400, code="bad-op")
    return _checked_root(_home() / "issue_recovery" / op_id)


def op_path(op_id: str) -> Path:
    from . import batches as _b
    try:
        return _b.op_file(op_id)
    except _b.IntegrationError as exc:
        raise IssueRecoveryError(f"bad op id: {op_id!r}: {exc}",
                                 status=exc.http_status,
                                 code="bad-op") from exc


def _read_op_file(op_id: str):
    """Read the parent op bytes without following symlinks."""
    if not isinstance(op_id, str) or not _OP_RE.fullmatch(op_id):
        raise IssueRecoveryError(f"bad op id: {op_id!r}", status=400,
                                 code="bad-op")
    path = op_path(op_id)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise IssueRecoveryError(f"unknown op: {op_id}",
                                 status=404, code="unknown-op")
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable op record: {exc}",
                                 status=502, code="unreadable") from exc
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        raise IssueRecoveryError("op record is not a regular file",
                                 status=502, code="unreadable")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable op record: {exc}",
                                 status=502, code="unreadable") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IssueRecoveryError(f"corrupt op record: {exc}",
                                 status=502, code="corrupt") from exc
    if not isinstance(data, dict):
        raise IssueRecoveryError("corrupt op record: not an object",
                                 status=502, code="corrupt")
    return data, hashlib.sha256(raw).hexdigest()


def _issue_block(op: dict) -> dict:
    """Structural shape of the op's typed-issue binding.

    ``issue.repo`` is the REGISTERED LOCAL repo name (for example
    ``react-native-nitro-sound``), kept separate from the canonical
    GitHub slug (``owner/name``), which comes from the registered
    profile via the shared validators. Commit IDs are strict 40-hex;
    content fingerprints are strict 64-hex — the two must never be
    confused. Full queue/run/profile/proof binding is enforced by
    the shared held validator in load_blocked_op, never copied here.
    """
    issue = op.get("issue")
    if not isinstance(issue, dict):
        raise IssueRecoveryError("op has no typed issue identity",
                                 status=400, code="bad-identity")
    number = _strict_number(issue.get("issue"))
    repo = str(issue.get("repo") or "")
    if not repo or "/" in repo or repo.startswith("."):
        raise IssueRecoveryError(f"bad issue repo name: {repo!r}",
                                 status=400, code="bad-identity")
    lane = str(issue.get("lane") or "")
    if lane not in _LANES:
        raise IssueRecoveryError(f"bad issue lane: {lane!r}",
                                 status=400, code="bad-identity")
    branch = str(issue.get("branch") or "")
    if not branch or branch.startswith("/") or ".." in branch.split("/"):
        raise IssueRecoveryError("bad issue branch",
                                 status=400, code="bad-identity")
    return {"issue": number, "repo": repo, "lane": lane,
            "url": str(issue.get("url") or ""), "branch": branch}


def _strict_number(value) -> str:
    from . import batches as _b
    try:
        out = _b._strict_issue_number(value)
    except Exception as exc:
        raise IssueRecoveryError(f"bad issue number: {value!r}: {exc}",
                                 status=400, code="bad-identity") from exc
    if not out:
        raise IssueRecoveryError(f"bad issue number: {value!r}",
                                 status=400, code="bad-identity")
    return out


def _strict_lane(value) -> str:
    lane = str(value or "")
    if lane not in _LANES:
        raise IssueRecoveryError(f"bad issue lane: {value!r}",
                                 status=400, code="bad-identity")
    return lane


def _binding_ctx(op: dict, raw_sha: str) -> dict:
    """Enforce live queue/run/profile/proof binding via held validators.

    Reuses batches._assert_issue_op_binding_current by call; nothing
    is copied or weakened. Raises stale/unreadable IssueRecoveryError
    on any drift; the op bytes on disk are never touched.
    """
    from . import batches as _b
    from . import store as _store
    repo = str(op.get("repo") or "")
    try:
        ctx = _b._assert_issue_op_binding_current(repo, op)
    except _b.IntegrationError as exc:
        raise IssueRecoveryError(f"issue binding not current: {exc}",
                                 status=409, code="stale") from exc
    except SystemExit as exc:
        # Shared store/read_json uses SystemExit for missing/corrupt
        # records; surface it as typed unreadable, never a crash.
        raise IssueRecoveryError(f"issue binding unreadable: {exc}",
                                 status=502, code="unreadable") from exc
    try:
        run_raw = _store.run_file(ctx["run_id"]).read_bytes()
    except OSError as exc:
        raise IssueRecoveryError(f"issue op run unreadable: {exc}",
                                 status=502, code="unreadable") from exc
    ctx = dict(ctx)
    ctx["run_sha"] = hashlib.sha256(run_raw).hexdigest()
    return ctx


def _validate_base_binding(ctx: dict) -> None:
    """Validate the recorded integration base against live sources.

    Read-only: the registered batch manifest, the recorded lane rules,
    and the CURRENT base checkout snapshot are compared to the stored
    base head/fingerprint. A missing base checkout or an unresolvable
    pinned ref is unreadable; drifted/clean-violated/unknown base
    metadata is stale. Never falls back to another ref or checkout.
    """
    from . import batches as _batches
    repo = ctx["issue"]["repo"]
    lane = ctx["issue"]["lane"]
    based = ctx["base"]
    batch_id = based.get("batch")
    if not isinstance(batch_id, str) or not batch_id or "/" in batch_id \
            or "\\" in batch_id or batch_id.startswith("."):
        raise IssueRecoveryError(f"invalid base batch id {batch_id!r}",
                                 status=409, code="stale")
    blane = based.get("lane")
    checkout = based.get("checkout") or ""
    head = based.get("head") or ""
    try:
        raw = (_batches.batches_dir() / (batch_id + ".json")).read_bytes()
    except FileNotFoundError:
        raise IssueRecoveryError(f"unknown base batch {batch_id!r}",
                                 status=409, code="stale")
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable base batch: {exc}",
                                 status=409, code="stale")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise IssueRecoveryError("unreadable base batch manifest",
                                 status=409, code="stale")
    if not isinstance(manifest, dict) or manifest.get("repo") != repo:
        raise IssueRecoveryError("base batch does not match this repo",
                                 status=409, code="stale")
    integrations = manifest.get("integrations")
    if not isinstance(integrations, list) or not any(
            isinstance(i, dict) and i.get("lane") == blane
            for i in integrations):
        raise IssueRecoveryError("base batch has no matching lane",
                                 status=409, code="stale")
    if blane not in ("main", "next"):
        raise IssueRecoveryError(f"unknown base lane {blane!r}",
                                 status=409, code="stale")
    if lane == "main" and blane != "main":
        raise IssueRecoveryError("base lane incompatible with issue lane",
                                 status=409, code="stale")
    if not checkout or not os.path.isdir(checkout):
        raise IssueRecoveryError("unreadable base checkout",
                                 status=502, code="unreadable")
    snap = _batches._snapshot(checkout)
    if snap is None:
        raise IssueRecoveryError("unreadable base checkout snapshot",
                                 status=502, code="unreadable")
    if snap.get("head") != head or snap.get("fingerprint") != based.get(
            "fingerprint"):
        raise IssueRecoveryError("base source changed since recording",
                                 status=409, code="stale")
    if not snap.get("clean"):
        raise IssueRecoveryError("base checkout is not clean",
                                 status=409, code="stale")
    cand_wt = ctx["candidate"]["worktree"]
    for label, repo_dir, ref in (("candidate", cand_wt,
                                  ctx["candidate"]["head"]),
                                 ("origin", cand_wt, ctx["base_commit"]),
                                 ("base", checkout, head)):
        if not _ref_readable(repo_dir, ref):
            raise IssueRecoveryError(
                f"unreadable pinned {label} ref {str(ref)[:12]}",
                status=502, code="unreadable")


def _hex40(value, name: str) -> str:
    text = str(value or "")
    if isinstance(value, bool) or not _SHA_RE.fullmatch(text):
        raise IssueRecoveryError(f"bad {name}: not a 40-hex commit SHA",
                                 status=400, code="bad-identity")
    return text


def _hex64(value, name: str) -> str:
    text = str(value or "")
    if isinstance(value, bool) or not _FP_RE.fullmatch(text):
        raise IssueRecoveryError(f"bad {name}: not a 64-hex fingerprint",
                                 status=400, code="bad-identity")
    return text


def load_blocked_op(op_id: str) -> dict:
    """Load and validate a BLOCKED typed-issue prepare op.

    Returns {"op", "raw_sha", "issue", "candidate", "base", "result",
    "ctx", "run"}. Commit IDs are strict 40-hex, content fingerprints
    strict 64-hex. Live queue/run/profile/proof/base binding is
    enforced through the shared held validator; the parent bytes are
    never modified. Anything missing/moved/unreadable/corrupt
    refuses; a non-blocked or non-typed-issue op refuses with
    409/400 and writes nothing.
    """
    op, raw_sha = _read_op_file(op_id)
    if op.get("op") != "prepare":
        raise IssueRecoveryError("only prepare ops are reviewable here",
                                 status=409, code="wrong-kind")
    if op.get("status") != "blocked":
        raise IssueRecoveryError("op is not blocked; nothing to investigate",
                                 status=409, code="not-blocked")
    issue = _issue_block(op)
    if issue["lane"] != str(op.get("lane") or ""):
        raise IssueRecoveryError("op lane moved since prepare",
                                 status=409, code="stale")
    cand = op.get("candidate")
    if not isinstance(cand, dict):
        raise IssueRecoveryError("op has no candidate binding",
                                 status=502, code="corrupt")
    candidate = {
        "head": _hex40(cand.get("head"), "candidate head"),
        "fingerprint": _hex64(cand.get("fingerprint"),
                              "candidate fingerprint"),
        "branch": str(cand.get("branch") or ""),
        "worktree": str(cand.get("worktree") or ""),
    }
    if not candidate["branch"] or not candidate["worktree"]:
        raise IssueRecoveryError("op has no candidate branch/worktree",
                                 status=502, code="corrupt")
    base = op.get("base") or {}
    if not isinstance(base, dict):
        raise IssueRecoveryError("op has no base binding",
                                 status=502, code="corrupt")
    based = {
        "batch": str(base.get("batch") or ""),
        "lane": _strict_lane(base.get("lane")),
        "head": _hex40(base.get("head"), "base head"),
        "fingerprint": _hex64(base.get("fingerprint"),
                              "base fingerprint"),
        "checkout": str(base.get("checkout") or ""),
    }
    if not based["batch"]:
        raise IssueRecoveryError("op has no valid base binding",
                                 status=502, code="corrupt")
    ctx = _binding_ctx(op, raw_sha)
    run = ctx.get("run") or {}
    if not isinstance(run, dict) or not run:
        raise IssueRecoveryError("issue op run is missing",
                                 status=502, code="corrupt")
    base_commit = _hex40(run.get("baseCommit"), "run baseCommit")
    res = op.get("result") or {}
    if not isinstance(res, dict):
        raise IssueRecoveryError("op has no result binding",
                                 status=502, code="corrupt")
    cps = res.get("conflicted_paths") or []
    if not isinstance(cps, list) or not cps or not all(
            isinstance(p, str) and p and not p.startswith("/")
            and ".." not in p.split("/") for p in cps):
        raise IssueRecoveryError("op has no valid conflicted_paths",
                                 status=502, code="corrupt")
    if res.get("head") or res.get("fingerprint"):
        raise IssueRecoveryError("blocked op must not carry result head/fp",
                                 status=502, code="corrupt")
    out = {"op": op, "op_id": op_id, "raw_sha": raw_sha, "issue": issue,
           "candidate": candidate, "base": based,
           "base_commit": base_commit, "result": res,
           "ctx": {"url": ctx.get("url"), "branch": ctx.get("branch"),
                   "run_sha": ctx.get("run_sha")},
           "run": {"id": run.get("id"), "baseCommit": base_commit,
                   "workCommit": candidate["head"],
                   "workTreeFingerprint": candidate["fingerprint"]}}
    _validate_base_binding(out)
    return out


def parse_original_touched(error_text: str) -> list:
    """Extract the re-derive refusal path list from the stored error.

    This parses the app's own recorded refusal note; it is evidence,
    not a new Git read. Unknown shapes yield [] rather than guessed
    paths.
    """
    text = str(error_text or "")
    mark = "re-derive refused:"
    idx = text.find(mark)
    if idx < 0:
        return []
    tail = text[idx + len(mark):].strip()
    prefix = "candidate touches "
    if not tail.startswith(prefix):
        return []
    tail = tail[len(prefix):].strip()
    suffix = "besides manifests/locks"
    if not tail.endswith(suffix):
        return []
    tail = tail[: -len(suffix)].strip()
    out = []
    for part in tail.split(","):
        part = part.strip()
        if not part or part.startswith("/") or " " in part:
            return []
        if ".." in part.split("/"):
            return []
        out.append(part)
    return out[:16]


def human_cause(ctx: dict) -> dict:
    """Human-first cause with the two path lists kept distinct."""
    op = ctx["op"]
    res = ctx["result"]
    conflicted = list(res.get("conflicted_paths") or [])
    original = parse_original_touched(op.get("error") or "")
    return {
        "title": "준비 충돌 — 통합 브랜치를 만들지 못함",
        "conflicted_paths": conflicted,
        "original_touched": original,
        "next_action": (f"충돌 조사 검토를 실행하면 실제 {len(conflicted)}개 "
                        f"충돌 파일과 원본 {len(original)}개 변경을 구분해 "
                        "안전한 다음 단계를 제안합니다. "
                        "파생 준비는 별도 승인 후에만 가능합니다."),
    }


_RECEIPT_MAX_BYTES = 1048576


def fastcheck_truth(ctx: dict) -> dict:
    """Backend truth: not-run ONLY for a valid blocked prepare with no receipt.

    The op-owned receipt FILE (<op>.fastcheck.json via the held
    fastcheck helper) is the authority — never an invented op field.
    A blocked prepare never executes fastcheck, so a genuinely
    absent receipt file on a valid binding is not_run; a present
    corrupt/unreadable/oversize file, or any present receipt at all,
    is an error. No receipt bytes are invented here.
    """
    from . import fastcheck as _fc
    op_id = str(ctx.get("op_id") or "")
    if not op_id:
        # No verified op identity: the receipt helper would run with
        # an empty identity. Never invent not-run/error/current from
        # unbound result fields — report unknown instead.
        return {"state": "unknown", "done_source": False,
                "reason": "검사 상태를 확인할 수 없음"}
    op = ctx["op"]
    gates = op.get("gates") or []
    fc = next((g for g in gates if isinstance(g, dict)
               and g.get("key") == "fastchecks"), None)
    try:
        blob = _fc.op_fastcheck_path(str(ctx.get("op_id") or ""))
        raw = blob.read_bytes()
    except FileNotFoundError:
        if isinstance(fc, dict) and fc.get("status") == "pending":
            return {"state": "not_run", "done_source": False,
                    "reason": "준비 충돌로 미실행 — 검사가 실행되지 않음"}
        return {"state": "unknown", "done_source": False,
                "reason": "검사 상태를 확인할 수 없음"}
    except (OSError, ValueError) as exc:
        return {"state": "error", "done_source": False,
                "reason": f"검사 기록을 읽지 못함: {exc}"}
    if len(raw) > _RECEIPT_MAX_BYTES:
        return {"state": "error", "done_source": False,
                "reason": "검사 기록이 너무 큼 — 저장된 수신증을 신뢰할 수 없음"}
    try:
        summary = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"state": "error", "done_source": False,
                "reason": "검사 기록 손상 — 저장된 수신증을 신뢰할 수 없음"}
    if not isinstance(summary, dict):
        return {"state": "error", "done_source": False,
                "reason": "검사 기록 손상 — 저장된 수신증을 신뢰할 수 없음"}
    return {"state": "error", "done_source": False,
            "reason": "막힌 준비에 검사 기록이 있어 상태를 단정할 수 없음"}


def review_fingerprint(ctx: dict, sent: list, scope: list,
                       prompt_sha: str, meta: dict | None = None,
                       original: dict | None = None,
                       contract: int = _CONTRACT_VERSION,
                       context_digest: str = "") -> str:
    """Bind parent record SHA, frozen 3-way refs, sent code, and scope.

    The immutable original run (baseCommit/workCommit/fingerprint),
    the selected integration base (batch/lane/head/fingerprint/
    checkout), the FULL original candidate descriptor
    (head/fingerprint/branch/worktree), and the canonical origin URL
    are all bound; only volatile IDs, job handles, and timestamps
    are excluded. Two reviews share an input only when everything
    the model sees is identical — including the exact transmitted
    prompt bytes (headers, ref labels, code windows, omission list,
    intended ranges).

    ``meta`` carries the full file/omitted snapshot metadata
    (blob OIDs, whole-source sizes, partial flags, refs, reasons)
    so stored informational fields cannot drift without changing
    the fingerprint even when the rendered prompt bytes match.

    ``original`` carries the full original attribution block
    (op_error text, conflicted_paths, partial scope): the displayed
    failure and conflict context is fingerprinted exactly like the
    sent code, so edited original fields cannot replay an old
    fingerprint.
    """
    candidate = ctx["candidate"]
    base = ctx["base"]
    if meta is not None and not isinstance(meta, dict):
        raise IssueRecoveryError("review metadata must be a mapping",
                                 status=500, code="fingerprint")
    if original is not None and not isinstance(original, dict):
        raise IssueRecoveryError("review original block must be a mapping",
                                 status=500, code="fingerprint")
    if not isinstance(context_digest, str) \
            or (context_digest
                and not _FP_RE.fullmatch(context_digest)):
        raise IssueRecoveryError("review context digest malformed",
                                 status=500, code="fingerprint")
    canonical = {
        "contract": contract,
        "parent_sha": ctx["raw_sha"],
        "run_sha": ctx["ctx"].get("run_sha"),
        "origin_url": ctx["ctx"].get("url"),
        "issue": ctx["issue"],
        "candidate_head": candidate["head"],
        "candidate_fp": candidate["fingerprint"],
        "candidate_branch": candidate["branch"],
        "candidate_worktree": candidate["worktree"],
        "base_batch": base["batch"],
        "base_lane": base["lane"],
        "base_head": base["head"],
        "base_fp": base["fingerprint"],
        "base_checkout": base["checkout"],
        "base_commit": ctx["base_commit"],
        "run": ctx["run"],
        "sent": sent,
        "scope": sorted(scope),
        "prompt_sha256": prompt_sha,
        "meta": meta if meta is not None else {},
        "original": original if original is not None else {},
    }
    # The context digest is bound ONLY when a supplemental context is
    # attached: legacy context-free inputs hash byte-identically to
    # before, so valid v3 history stays verifiable and current.
    # Same source plus new context always yields a different
    # fingerprint; an empty digest never collides with a real one.
    if context_digest:
        canonical["context_digest"] = context_digest
    raw = json.dumps(canonical, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stored_sent_scope_meta(stored: dict):
    """Rebuild sent ranges, scope, prompt hash, and metadata from INPUT.

    This is the stored-data half of fingerprint replay: every file's
    text is hash-checked against its recorded sent hash/bytes, the
    transmitted prompt bytes are hash-checked against their recorded
    hash, and scope/meta are rebuilt exactly as the writer stored
    them. Any tampering with stored text, sizes, omission lists, or
    the assembled prompt raises a corrupt error. Pure stored-data
    check — it binds nothing to a live source by itself.
    """
    snap = stored.get("snapshot")
    if not isinstance(snap, dict):
        raise IssueRecoveryError("stored input snapshot unreadable",
                                 status=502, code="corrupt")
    files = snap.get("files")
    omitted = snap.get("omitted")
    if not isinstance(files, list) or not isinstance(omitted, list):
        raise IssueRecoveryError("stored input snapshot unreadable",
                                 status=502, code="corrupt")
    sent = []
    for entry in files:
        if not isinstance(entry, dict):
            raise IssueRecoveryError("stored input snapshot unreadable",
                                     status=502, code="corrupt")
        # The stored bytes are bound to their recorded hash: tampered
        # record text can neither replay the same fingerprint nor pass
        # quote validation against a different body.
        text = entry.get("text")
        if not isinstance(text, str):
            raise IssueRecoveryError("stored snapshot text malformed",
                                     status=502, code="corrupt")
        raw_text = text.encode("utf-8")
        if hashlib.sha256(raw_text).hexdigest() \
                != entry.get("sent_sha256") \
                or len(raw_text) != entry.get("sent_bytes"):
            raise IssueRecoveryError(
                "stored snapshot text does not match its hash",
                status=502, code="corrupt")
        try:
            sent.append({"path": entry["path"],
                         "ref_kind": entry["ref_kind"],
                         "ref": entry["ref"],
                         "sent_sha256": entry["sent_sha256"],
                         "sent_bytes": entry["sent_bytes"],
                         "line_count": entry["line_count"]})
        except KeyError:
            raise IssueRecoveryError("stored input snapshot unreadable",
                                     status=502, code="corrupt")
    if any(not isinstance(o, dict) for o in omitted):
        raise IssueRecoveryError("stored input snapshot unreadable",
                                 status=502, code="corrupt")
    # Full snapshot metadata (blob OIDs, whole-source sizes, partial
    # flags, refs, omission reasons) is bound into the fingerprint
    # too: an informational field that drifts without changing the
    # rendered prompt bytes must still break replay equality. The
    # canonical refs map is part of that metadata: a swapped ref
    # that leaves every file's own head untouched still changes
    # what the review claims to have read.
    refs = snap.get("refs")
    if not isinstance(refs, dict) or not refs \
            or any(not isinstance(k, str) or not k
                   or not isinstance(v, str) or not v
                   for k, v in refs.items()):
        raise IssueRecoveryError("stored snapshot refs malformed",
                                 status=502, code="corrupt")
    meta = {"files": [{k: v for k, v in e.items() if k != "text"}
                      for e in files],
            "omitted": omitted,
            "refs": dict(sorted(refs.items()))}
    # Optional build-evidence block: ABSENT means legacy (exact prior
    # meta, no addition). PRESENT means the shared validator must
    # accept its full metadata/parsed/omission/reason/ref/digest
    # fields — present null/malformed never bypasses as legacy.
    if "build_evidence" in snap:
        meta["build_evidence"] = _project_build_meta(
            snap["build_evidence"])
    scope = sorted({str(f.get("path", "")) for f in files
                    if isinstance(f, dict)}
                   | {str(o.get("path", "")) for o in omitted
                      if isinstance(o, dict)})
    # The exact transmitted prompt bytes are bound too: tampering only
    # the assembled sent_text (headers, ref labels, omission list,
    # intended ranges) without touching files[] still breaks the chain.
    full = snap.get("sent_text")
    if not isinstance(full, str):
        raise IssueRecoveryError("stored input snapshot unreadable",
                                 status=502, code="corrupt")
    prompt_sha = hashlib.sha256(full.encode("utf-8")).hexdigest()
    if prompt_sha != snap.get("sent_text_sha256"):
        raise IssueRecoveryError(
            "stored prompt text does not match its hash",
            status=502, code="corrupt")
    return sent, scope, prompt_sha, meta


def _stored_original_block(stored: dict) -> dict:
    """Canonical original attribution block bound into the fingerprint.

    The displayed failure text, conflict scope, and partial scope are
    fingerprinted exactly like the sent code: editing the original
    error or conflict list without touching any file must break
    replay equality. Callers validate the field shapes first.
    """
    cps = stored.get("conflicted_paths") or []
    partial = stored.get("partial") or []
    return {"op_error": stored.get("op_error") or "",
            "conflicted_paths": sorted(cps),
            "partial": list(partial)}


def _current_fp(ctx: dict, stored: dict) -> str:
    """Recompute the review fingerprint from CURRENT binding + stored input.

    The stored sent ranges/scope are replayed against the freshly
    loaded binding (run SHA, parent SHA, candidate/base/baseCommit,
    issue, origin). Equality with the stored fingerprint proves the
    current sources still match the reviewed input; any drift in run
    bytes, base identity, or recorded refs yields a different value
    and marks the review stale. Never rebinds or heals.
    """
    sent, scope, prompt_sha, meta = _stored_sent_scope_meta(stored)
    original = _stored_original_block(stored)
    # A context-linked input replays its bound digest; legacy inputs
    # pass "" and hash exactly as before. rec_root(rec-op) is safe
    # here: every caller proves stored identity (hence a valid op)
    # before recomputing currentness.
    context_digest = _stored_context_digest(
        stored["op"], stored, rec_root(stored["op"])) \
        if isinstance(stored.get("op"), str) and stored.get("op") else ""
    return review_fingerprint(ctx, sent, scope, prompt_sha, meta,
                              original, context_digest=context_digest)


def _context_canonical_bytes(actor: str, text: str, origin: dict,
                              question_refs: list) -> bytes:
    """Canonical bytes bound into a supplemental-context digest.

    The digest covers the exact operator text, the originating
    input/report/fingerprint triple, the referenced question indices,
    and the fixed actor label — never code citations, never approval.
    """
    canonical = {"actor": actor, "text": text, "origin": origin,
                 "question_refs": list(question_refs)}
    return json.dumps(canonical, ensure_ascii=False,
                      sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _context_digest_for(actor: str, text: str, origin: dict,
                        question_refs: list) -> str:
    return hashlib.sha256(
        _context_canonical_bytes(actor, text, origin,
                                 question_refs)).hexdigest()


def _context_names(root: Path) -> list:
    try:
        return sorted(p.name for p in root.glob("context-*.json"))
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable context store: {exc}",
                                 status=502, code="unreadable") from exc


def _stored_context_digest(op_id: str, stored: dict, root: Path) -> str:
    """Return the bound context digest for a stored review input.

    Legacy context-free inputs return "": their fingerprint path is
    untouched. A present block must be version 1 with a well-formed
    digest/id whose record exists, matches this input exactly, is
    internally self-consistent, and — critically — whose bound
    origin input/report bytes still match the live origin records
    byte-for-byte with unchanged question texts. A schema-valid
    origin report edited after the record was written therefore
    fails here (before model persistence can consume it and in
    currentness checks), instead of persisting done/current.
    Otherwise corrupt. Nothing is migrated, healed, or rebound.
    """
    blk = stored.get("context")
    if blk is None:
        return ""
    if not isinstance(blk, dict):
        raise IssueRecoveryError("stored input context block malformed",
                                 status=502, code="corrupt")
    if blk.get("version") != _CONTEXT_VERSION:
        raise IssueRecoveryError("stored input context version unknown",
                                 status=502, code="corrupt")
    digest = blk.get("digest")
    cid = blk.get("id")
    if not isinstance(digest, str) or not _FP_RE.fullmatch(digest):
        raise IssueRecoveryError("stored input context digest malformed",
                                 status=502, code="corrupt")
    if not isinstance(cid, str) \
            or not _RECORD_RE.fullmatch(f"{cid}.json") \
            or not cid.startswith("context-"):
        raise IssueRecoveryError("stored input context link malformed",
                                 status=502, code="corrupt")
    rec = _read_record(root, f"{cid}.json")
    if rec is None:
        raise IssueRecoveryError("linked context record is missing",
                                 status=502, code="corrupt")
    if rec.get("id") != cid:
        raise IssueRecoveryError("linked context id does not match "
                                 "its name",
                                 status=502, code="corrupt")
    if rec.get("op") != op_id or rec.get("op") != stored.get("op"):
        raise IssueRecoveryError("linked context belongs to another op",
                                 status=502, code="corrupt")
    # The record's origin is the ORIGINATING chain, which differs
    # from this input's own fingerprint by design (the digest is what
    # binds them and is hashed into this input's fingerprint). What
    # must hold exactly is that the snapshot block echoes the
    # record's own origin bytes — no silent rebinding — and that the
    # live origin bytes still match those bound bytes.
    if blk.get("origin") != rec.get("origin"):
        raise IssueRecoveryError("stored input context origin drifted",
                                 status=502, code="corrupt")
    _verify_origin_live(op_id, root, rec)
    if rec.get("digest") != digest:
        raise IssueRecoveryError("stored input context digest drifted",
                                 status=502, code="corrupt")
    return digest


def record_recovery_context(op_id: str, text, question_refs=None,
                            origin_input=None, origin_report=None,
                            origin_fingerprint=None,
                            expected_input_sha=None,
                            expected_report_sha=None,
                            by: str = "owner") -> dict:
    """Validate and persist an immutable supplemental-context record.

    CALLER MUST HOLD the global worker lease and the same-op
    exclusion; this function writes nothing before that. It performs
    NO model call and starts NO review: the record only becomes a new
    review input when an explicit confirmed review request names it.

    The text is operator-provided investigation context for the
    named report's questions — never a code citation, never an
    approval or owner-policy decision. The actor is fixed to
    "operator": any submitted actor label is ignored. The origin
    binds EXACT bytes (raw input/report SHA-256 plus the actual
    referenced question texts). A never-recorded context requires
    the origin to equal the CURRENT latest chain; an identical
    resubmission re-proves its unchanged origin chain and current
    sources instead — even after latest moved on (its legitimate
    continuation). Stale, foreign, missing, corrupt, or
    source-unreadable origins refuse before any write.
    Optional expected_input_sha/expected_report_sha carry the
    raw-byte hashes the caller displayed when composing the note;
    the live origin bytes must still match them or the call refuses
    409 with no write, so an old read cannot silently answer edited
    question text under unchanged ids.
    """
    if by not in ("owner", "root"):
        raise IssueRecoveryError("by must be owner or root",
                                 status=400, code="bad-by")
    if not isinstance(text, str) or not text.strip():
        raise IssueRecoveryError("context text is empty",
                                 status=400, code="bad-context")
    if len(text) > _CONTEXT_TEXT_CAP:
        raise IssueRecoveryError(
            f"context text exceeds {_CONTEXT_TEXT_CAP} characters",
            status=413, code="context-too-long")
    refs = [] if question_refs is None else question_refs
    if not isinstance(refs, list) or any(
            not isinstance(r, int) or isinstance(r, bool) or r < 0
            for r in refs):
        raise IssueRecoveryError("context question refs malformed",
                                 status=400, code="bad-context")
    if not isinstance(origin_input, str) or not origin_input \
            or not isinstance(origin_report, str) or not origin_report:
        raise IssueRecoveryError("context origin is missing",
                                 status=400, code="bad-context")
    ctx = load_blocked_op(op_id)
    root = rec_root(op_id)
    latest = _read_record(root, "latest.json")
    if not isinstance(latest, dict) or not latest:
        raise IssueRecoveryError("no review chain to attach context to",
                                 status=409, code="stale-context-origin")
    if not isinstance(origin_fingerprint, str) or not origin_fingerprint:
        if latest.get("input") == origin_input \
                and latest.get("report") == origin_report \
                and isinstance(latest.get("fingerprint"), str):
            origin_fingerprint = latest["fingerprint"]
        else:
            # Resubmission naming only input/report after latest moved
            # on: recover the bound fingerprint from the identical
            # already-recorded context, if one exists. Nothing found
            # means a stale foreign origin, refused below.
            origin_fingerprint = _find_identical_context_fp(
                root, origin_input, origin_report, text, refs)
    if not origin_fingerprint:
        raise IssueRecoveryError("context origin is not the current "
                                 "review chain",
                                 status=409, code="stale-context-origin")
    for _label, _expected in (("input", expected_input_sha),
                              ("report", expected_report_sha)):
        if _expected is not None \
                and (not isinstance(_expected, str)
                     or not _SHA256_RE.fullmatch(_expected)):
            raise IssueRecoveryError("context expected origin revision "
                                     "malformed",
                                     status=400, code="bad-context")
    stored, report_doc, input_sha, report_sha = _load_origin_records(
        op_id, root, origin_input, origin_report, origin_fingerprint)
    # An old UI may name unchanged ids while answering question text
    # it read before a schema-valid origin edit: when the caller
    # passes the byte hashes it displayed, the live bytes must still
    # match them — otherwise 409 with no write, never a silent bind
    # of the operator note to different questions.
    if expected_input_sha is not None \
            and input_sha != expected_input_sha:
        raise IssueRecoveryError("context origin input changed since "
                                 "it was read",
                                 status=409, code="stale-context-origin")
    if expected_report_sha is not None \
            and report_sha != expected_report_sha:
        raise IssueRecoveryError("context origin report changed since "
                                 "it was read",
                                 status=409, code="stale-context-origin")
    bound_questions = _origin_questions(report_doc, refs)
    origin = {"input": origin_input, "report": origin_report,
              "fingerprint": origin_fingerprint,
              "input_sha256": input_sha, "report_sha256": report_sha,
              "questions": bound_questions}
    digest = _context_digest_for("operator", text, origin, refs)
    cid = f"context-{digest[:16]}"
    existing = _read_record(root, f"{cid}.json")
    if existing is not None:
        # Identical bytes, re-proved live: the bound origin records
        # must still match byte-for-byte and the live binding must
        # still reproduce the origin fingerprint. The latest pointer
        # may since have advanced past the origin — that is the
        # legitimate already-created continuation, not a rebind.
        # Anything changed is stale or corrupt.
        if not isinstance(existing, dict) \
                or existing.get("digest") != digest:
            raise IssueRecoveryError("context record does not match "
                                     "its digest",
                                     status=502, code="corrupt")
        _prove_origin_current(op_id, ctx, root, existing)
        return {"context": cid, "reused": True, "digest": digest}
    if origin_input != latest.get("input") \
            or origin_report != latest.get("report") \
            or origin_fingerprint != latest.get("fingerprint"):
        raise IssueRecoveryError("context origin is not the current "
                                 "review chain",
                                 status=409, code="stale-context-origin")
    _validate_stored_identity(op_id, stored)
    if _current_fp(ctx, stored) != stored.get("fingerprint"):
        raise IssueRecoveryError("context origin is stale",
                                 status=409, code="stale-context-origin")
    _validate_report_chain(root, op_id, latest, stored)
    from .util import utcnow as _utcnow
    try:
        _write_new(root / f"{cid}.json", {
            "schema": 1, "kind": "recovery-context", "op": op_id,
            "by": by, "actor": "operator", "id": cid, "origin": origin,
            "question_refs": list(refs), "text": text,
            "text_sha256": hashlib.sha256(
                text.encode("utf-8")).hexdigest(),
            "text_len": len(text), "digest": digest,
            "context_version": _CONTEXT_VERSION,
            "created": _utcnow(),
        })
    except IssueRecoveryError as exc:
        if exc.code == "duplicate":
            return {"context": cid, "reused": True, "digest": digest}
        raise
    return {"context": cid, "reused": False, "digest": digest}


def _owner_decision_names(root: Path) -> list:
    try:
        return sorted(p.name for p in root.glob("owner-decision-*.json"))
    except OSError as exc:
        raise IssueRecoveryError("unreadable owner-decision store: "
                                 f"{exc}",
                                 status=502, code="unreadable") from exc


def _owner_decision_questions(report_doc: dict, refs: list) -> list:
    """Exact referenced question texts bound into an owner decision.

    Returns [{"index": i, "text": t}] for each ref. The texts come
    from the validated origin report bytes — never from the request.
    """
    inner = report_doc.get("report")
    questions = inner.get("questions") if isinstance(inner, dict) else None
    if not isinstance(questions, list) or not questions:
        raise IssueRecoveryError("owner decision references questions "
                                 "but the origin report has none",
                                 status=400, code="bad-owner-decision")
    for r in refs:
        if r >= len(questions):
            raise IssueRecoveryError("owner decision question ref out "
                                     "of range",
                                     status=400, code="bad-owner-decision")
        if not isinstance(questions[r], str):
            raise IssueRecoveryError("owner decision origin question "
                                     "is not text",
                                     status=502, code="corrupt")
    return [{"index": r, "text": questions[r]} for r in refs]


def _validate_owner_decisions(decisions, report_doc: dict) -> list:
    """Strict-shape owner decision entries; unknown fields refused."""
    if not isinstance(decisions, list) or not decisions \
            or len(decisions) > _OWNER_DECISION_COUNT_CAP:
        raise IssueRecoveryError("owner decisions must be a non-empty "
                                 "list",
                                 status=400, code="bad-owner-decision")
    refs = []
    for entry in decisions:
        if not isinstance(entry, dict):
            raise IssueRecoveryError("owner decision entry must be an "
                                     "object",
                                     status=400, code="bad-owner-decision")
        if set(entry) != {"question_ref", "selection", "certainty",
                          "provenance"}:
            raise IssueRecoveryError("owner decision entry has missing "
                                     "or unknown fields",
                                     status=400, code="bad-owner-decision")
        ref = entry["question_ref"]
        if not isinstance(ref, int) or isinstance(ref, bool) or ref < 0:
            raise IssueRecoveryError("owner decision question_ref must "
                                     "be a non-negative integer",
                                     status=400, code="bad-owner-decision")
        selection = entry["selection"]
        if not isinstance(selection, str) \
                or len(selection) > _OWNER_DECISION_SELECTION_CAP:
            raise IssueRecoveryError("owner decision selection must be "
                                     "text within cap",
                                     status=400, code="bad-owner-decision")
        if entry["certainty"] not in _OWNER_DECISION_CERTAINTIES:
            raise IssueRecoveryError("owner decision certainty unknown",
                                     status=400, code="bad-owner-decision")
        if entry["provenance"] != _OWNER_DECISION_PROVENANCE:
            raise IssueRecoveryError("owner decision provenance must be "
                                     "the exact literal",
                                     status=400, code="bad-owner-decision")
        refs.append(ref)
    _owner_decision_questions(report_doc, refs)
    return refs


def _assert_owner_decision_self_consistent(rec: dict) -> str:
    """Self-consistency of one owner-decision record's own bytes."""
    if rec.get("version") != _OWNER_DECISION_VERSION:
        raise IssueRecoveryError("stored owner decision version unknown",
                                 status=502, code="corrupt")
    for key in ("id", "op", "by", "digest"):
        if not isinstance(rec.get(key), str) or not rec.get(key):
            raise IssueRecoveryError("stored owner decision identity "
                                     "malformed",
                                     status=502, code="corrupt")
    if not rec["id"].startswith("owner-decision-"):
        raise IssueRecoveryError("stored owner decision id malformed",
                                 status=502, code="corrupt")
    if rec.get("by") != "root":
        raise IssueRecoveryError("stored owner decision recorder is "
                                 "not root",
                                 status=502, code="corrupt")
    origin = rec.get("origin")
    if not isinstance(origin, dict):
        raise IssueRecoveryError("stored owner decision origin "
                                 "malformed",
                                 status=502, code="corrupt")
    for key in ("input", "report", "fingerprint", "input_sha256",
                "report_sha256", "questions"):
        if key not in origin:
            raise IssueRecoveryError("stored owner decision origin "
                                     "malformed",
                                     status=502, code="corrupt")
    decisions = rec.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise IssueRecoveryError("stored owner decision entries "
                                 "malformed",
                                 status=502, code="corrupt")
    for entry in decisions:
        if not isinstance(entry, dict) \
                or set(entry) != {"question_ref", "selection",
                                  "certainty", "provenance"}:
            raise IssueRecoveryError("stored owner decision entry "
                                     "malformed",
                                     status=502, code="corrupt")
        if entry["certainty"] not in _OWNER_DECISION_CERTAINTIES \
                or entry["provenance"] != _OWNER_DECISION_PROVENANCE:
            raise IssueRecoveryError("stored owner decision entry "
                                     "malformed",
                                     status=502, code="corrupt")
    return rec["digest"]


def _verify_owner_decision_live(op_id: str, root, rec: dict) -> None:
    """Re-prove an owner-decision record's bound origin live."""
    _assert_owner_decision_self_consistent(rec)
    origin = rec.get("origin")
    _stored, live_report, input_sha, report_sha = _load_origin_records(
        op_id, root, origin.get("input"), origin.get("report"),
        origin.get("fingerprint"))
    if input_sha != origin.get("input_sha256") \
            or report_sha != origin.get("report_sha256"):
        raise IssueRecoveryError("owner decision origin bytes changed",
                                 status=502, code="corrupt")
    refs = [e["question_ref"] for e in rec.get("decisions")]
    if _owner_decision_questions(live_report, refs) \
            != origin.get("questions"):
        raise IssueRecoveryError("owner decision origin questions "
                                 "changed",
                                 status=502, code="corrupt")


def _projection_owner_decisions(root: Path, op_id: str) -> list:
    """Newest-first owner-decision projections (read-only).

    Skips nothing silently: an unreadable record fails the whole
    status read, and a corrupt one raises corrupt — the same
    fail-closed contract as contexts.
    """
    out = []
    for name in _owner_decision_names(root):
        rec = _read_record(root, name)
        if rec is None:
            raise IssueRecoveryError("owner decision record unreadable: "
                                     f"{name}",
                                     status=502, code="unreadable")
        _verify_owner_decision_live(op_id, root, rec)
        out.append({
            "id": rec["id"], "by": rec["by"],
            "decisions": [dict(e) for e in rec["decisions"]],
            "origin": {
                "input": rec["origin"]["input"],
                "report": rec["origin"]["report"],
                "questions": [dict(q)
                              for q in rec["origin"]["questions"]]},
            "created": rec.get("created"),
        })
    out.sort(key=lambda e: str(e.get("created") or ""), reverse=True)
    return out[:_OWNER_DECISION_LIST_CAP]


def list_owner_decisions(op_id: str) -> list:
    """Read-only owner-decision projections for one op (never writes)."""
    return _projection_owner_decisions(rec_root(op_id), op_id)


def record_owner_decision(op_id: str, decisions, origin_input=None,
                          origin_report=None, expected_input_sha=None,
                          expected_report_sha=None) -> dict:
    """Record one immutable owner-decision set (Root recorder only).

    The caller must hold the op exclusion; no worker lease is needed
    (pure record write, no model call). The origin must be the live
    latest chain and the expected SHAs must match the live origin
    bytes exactly, else 409 with no write. Every 4xx raises before
    any byte is written. A byte-identical re-submission reuses the
    existing record id (no duplicate file).
    """
    if not isinstance(op_id, str) or not _OP_RE.fullmatch(op_id):
        raise IssueRecoveryError(f"bad op id: {op_id!r}",
                                 status=400, code="bad-op")
    for value in (origin_input, origin_report):
        if not isinstance(value, str) or not value:
            raise IssueRecoveryError("owner decision origin is missing",
                                     status=400,
                                     code="bad-owner-decision")
    ctx = load_blocked_op(op_id)
    root = rec_root(op_id)
    latest = _read_record(root, "latest.json")
    if not isinstance(latest, dict) or not latest:
        raise IssueRecoveryError("no review chain to attach the owner "
                                 "decision to",
                                 status=409, code="stale-owner-decision")
    if origin_input != latest.get("input") \
            or origin_report != latest.get("report") \
            or not isinstance(latest.get("fingerprint"), str):
        raise IssueRecoveryError("owner decision origin is not the "
                                 "current chain",
                                 status=409, code="stale-owner-decision")
    origin_fingerprint = latest["fingerprint"]
    for _label, _expected in (("input", expected_input_sha),
                              ("report", expected_report_sha)):
        if not isinstance(_expected, str) \
                or not _SHA256_RE.fullmatch(_expected):
            raise IssueRecoveryError("owner decision expected origin "
                                     "revision malformed",
                                     status=400,
                                     code="bad-owner-decision")
    stored, report_doc, input_sha, report_sha = _load_origin_records(
        op_id, root, origin_input, origin_report, origin_fingerprint)
    if input_sha != expected_input_sha:
        raise IssueRecoveryError("owner decision origin input changed "
                                 "since it was read",
                                 status=409, code="stale-owner-decision")
    if report_sha != expected_report_sha:
        raise IssueRecoveryError("owner decision origin report changed "
                                 "since it was read",
                                 status=409, code="stale-owner-decision")
    refs = _validate_owner_decisions(decisions, report_doc)
    questions = _owner_decision_questions(report_doc, refs)
    origin = {"input": origin_input, "report": origin_report,
              "fingerprint": origin_fingerprint,
              "input_sha256": input_sha, "report_sha256": report_sha,
              "questions": questions}
    digest = hashlib.sha256(json.dumps(
        {"op": op_id, "by": "root", "decisions": decisions,
         "origin": origin},
        sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    did = "owner-decision-" + digest[:16]
    existing = _read_record(root, f"{did}.json")
    if existing is not None:
        if not isinstance(existing, dict) \
                or existing.get("digest") != digest:
            raise IssueRecoveryError("owner decision record does not "
                                     "match its digest",
                                     status=502, code="corrupt")
        _verify_owner_decision_live(op_id, root, existing)
        return {"id": did, "reused": True, "digest": digest}
    _validate_stored_identity(op_id, stored)
    if _current_fp(ctx, stored) != stored.get("fingerprint"):
        raise IssueRecoveryError("owner decision origin is stale",
                                 status=409,
                                 code="stale-owner-decision")
    _validate_report_chain(root, op_id, latest, stored)
    from .util import utcnow as _utcnow
    try:
        _write_new(root / (did + ".json"), {
            "schema": 1, "kind": "owner-decision", "version": 1,
            "op": op_id, "by": "root", "id": did, "origin": origin,
            "decisions": [dict(e) for e in decisions],
            "digest": digest, "created": _utcnow(),
        })
    except IssueRecoveryError as exc:
        if exc.code == "duplicate":
            return {"id": did, "reused": True, "digest": digest}
        raise
    return {"id": did, "reused": False, "digest": digest}


def _record_bytes(root: Path, name: str) -> bytes:
    """Raw bytes of a stored record, fail-closed.

    Returns bytes only; callers hash or parse them. A record that was
    never written is a corrupt link here (not an empty value):
    callers only name records that must already exist.
    """
    path = _record_path(root, name)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise IssueRecoveryError(f"missing record {name}",
                                 status=502, code="corrupt") from None
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable record {name}: {exc}",
                                 status=502, code="unreadable") from exc


def _parse_record_bytes(raw: bytes, name: str) -> dict:
    """Parse one stored record from its already-captured raw bytes.

    Parsing the SAME bytes that are hashed (never a second read)
    guarantees the parsed document and the bound digest cannot come
    from mixed revisions. Errors mirror _read_record exactly.
    """
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IssueRecoveryError(f"corrupt record {name}: {exc}",
                                 status=502, code="corrupt") from exc
    if not isinstance(doc, dict):
        raise IssueRecoveryError(f"corrupt record {name}: not an object",
                                 status=502, code="corrupt")
    return doc


def _load_origin_records(op_id: str, root, origin_input: str,
                         origin_report: str, origin_fp: str):
    """Load and shape-check an origin triple's records.

    Returns (stored_input, report_doc, input_sha256, report_sha256).
    Each record is read ONCE as raw bytes; the parsed document and
    the bound hash always come from that single capture, never from
    mixed revisions. Ownership between input, report, and fingerprint
    is enforced. Shape or ownership failures are corrupt; a
    never-written record is an unknown origin, never an empty chain.
    """
    for value in (origin_input, origin_report):
        if not isinstance(value, str) or not value:
            raise IssueRecoveryError("context origin is missing",
                                     status=400, code="bad-context")
    if not isinstance(origin_fp, str) or not _FP_RE.fullmatch(origin_fp):
        raise IssueRecoveryError("context origin fingerprint malformed",
                                 status=400, code="bad-context")
    if origin_report != f"report-{origin_fp[:16]}":
        raise IssueRecoveryError("context origin report does not match "
                                 "its input",
                                 status=502, code="corrupt")
    try:
        raw_input = _record_bytes(root, f"{origin_input}.json")
    except IssueRecoveryError as exc:
        if exc.code == "corrupt":
            raise IssueRecoveryError("context origin input is unknown",
                                     status=404,
                                     code="unknown-context-origin") from exc
        raise
    try:
        raw_report = _record_bytes(root, f"{origin_report}.json")
    except IssueRecoveryError as exc:
        if exc.code == "corrupt":
            raise IssueRecoveryError("context origin report is unknown",
                                     status=404,
                                     code="unknown-context-origin") from exc
        raise
    stored = _parse_record_bytes(raw_input, f"{origin_input}.json")
    if stored.get("kind") != "recovery-input" \
            or stored.get("fingerprint") != origin_fp:
        raise IssueRecoveryError("context origin input is corrupt",
                                 status=502, code="corrupt")
    report_doc = _parse_record_bytes(raw_report, f"{origin_report}.json")
    for key, want in (("schema", 1), ("kind", "recovery-report"),
                      ("op", op_id), ("fingerprint", origin_fp),
                      ("input", origin_input)):
        if report_doc.get(key) != want:
            raise IssueRecoveryError("context origin report is corrupt",
                                     status=502, code="corrupt")
    input_sha = hashlib.sha256(raw_input).hexdigest()
    report_sha = hashlib.sha256(raw_report).hexdigest()
    return stored, report_doc, input_sha, report_sha


def _assert_context_self_consistent(rec: dict) -> str:
    """Self-consistency of one context record's own bytes (no live I/O).

    Returns the verified digest. Checks kind/schema/version/fixed
    operator actor/id-prefix, text shape, refs shape, text_sha256 and
    text_len derivation, question entry shapes, and digest
    recomputation from the record's own bytes. Anything else is
    corrupt — never healed, never displayed as valid.
    """
    if not isinstance(rec, dict) or rec.get("kind") != "recovery-context" \
            or rec.get("schema") != 1 \
            or rec.get("context_version") != _CONTEXT_VERSION \
            or rec.get("actor") != "operator" \
            or not isinstance(rec.get("digest"), str):
        raise IssueRecoveryError("stored context is foreign",
                                 status=502, code="corrupt")
    if not _FP_RE.fullmatch(rec["digest"]) \
            or rec.get("id") != f"context-{rec['digest'][:16]}":
        raise IssueRecoveryError("stored context id does not match "
                                 "its digest",
                                 status=502, code="corrupt")
    text = rec.get("text")
    refs = rec.get("question_refs")
    if not isinstance(text, str) \
            or not isinstance(refs, list) \
            or any(not isinstance(r, int) or isinstance(r, bool) or r < 0
                   for r in refs):
        raise IssueRecoveryError("stored context payload malformed",
                                 status=502, code="corrupt")
    if rec.get("text_sha256") != hashlib.sha256(
            text.encode("utf-8")).hexdigest() \
            or rec.get("text_len") != len(text):
        raise IssueRecoveryError("stored context bytes do not match "
                                 "their derived sizes",
                                 status=502, code="corrupt")
    origin = rec.get("origin")
    if not isinstance(origin, dict):
        raise IssueRecoveryError("stored context origin malformed",
                                 status=502, code="corrupt")
    for key in ("input", "report"):
        if not isinstance(origin.get(key), str) or not origin.get(key):
            raise IssueRecoveryError("stored context origin malformed",
                                     status=502, code="corrupt")
    if not isinstance(origin.get("fingerprint"), str) \
            or not _FP_RE.fullmatch(origin.get("fingerprint")):
        raise IssueRecoveryError("stored context origin malformed",
                                 status=502, code="corrupt")
    for key in ("input_sha256", "report_sha256"):
        if not isinstance(origin.get(key), str) \
                or not _SHA256_RE.fullmatch(origin.get(key)):
            raise IssueRecoveryError("stored context origin hashes "
                                     "malformed",
                                     status=502, code="corrupt")
    bound_qs = origin.get("questions")
    if not isinstance(bound_qs, list) \
            or any(not isinstance(q, dict)
                   or not isinstance(q.get("index"), int)
                   or isinstance(q.get("index"), bool)
                   or q.get("index") < 0
                   or not isinstance(q.get("text"), str)
                   for q in bound_qs):
        raise IssueRecoveryError("stored context origin questions "
                                 "malformed",
                                 status=502, code="corrupt")
    if _context_digest_for("operator", text, origin, refs) \
            != rec["digest"]:
        raise IssueRecoveryError("stored context bytes do not match "
                                 "their digest",
                                 status=502, code="corrupt")
    return rec["digest"]


def _verify_origin_live(op_id: str, root, rec: dict) -> dict:
    """Re-prove a context record's bound origin against live bytes.

    Shared by request, replay, currentness, and status paths: the
    record must first be self-consistent, then its bound raw input
    and report SHA-256 must still match the live origin records
    byte-for-byte, and the live origin question texts must still
    match the bound questions. Returns the origin's stored input.
    Anything changed is corrupt (immutable records never change
    legitimately) — never healed, never rebound, never displayed
    as valid. One level only: no ancestry walk.
    """
    _assert_context_self_consistent(rec)
    origin = rec.get("origin")
    stored, report_doc, input_sha, report_sha = _load_origin_records(
        op_id, root, origin.get("input"), origin.get("report"),
        origin.get("fingerprint"))
    if input_sha != origin.get("input_sha256") \
            or report_sha != origin.get("report_sha256"):
        raise IssueRecoveryError("context origin bytes changed",
                                 status=502, code="corrupt")
    if _origin_questions(report_doc, rec.get("question_refs")) \
            != origin.get("questions"):
        raise IssueRecoveryError("context origin questions changed",
                                 status=502, code="corrupt")
    return stored


def _origin_questions(report_doc: dict, refs: list) -> list:
    """Exact referenced question texts bound into an origin.

    Returns [{"index": i, "text": t}] for each ref. The texts come
    from the validated origin report bytes — never from the request
    — so the digest binds what the model actually answered.
    """
    inner = report_doc.get("report")
    questions = inner.get("questions") if isinstance(inner, dict) else None
    if refs and (not isinstance(questions, list) or not questions):
        raise IssueRecoveryError("context references questions but the "
                                 "origin report has none",
                                 status=400, code="bad-context")
    if isinstance(questions, list):
        for r in refs:
            if r >= len(questions):
                raise IssueRecoveryError("context question ref out of "
                                         "range",
                                         status=400, code="bad-context")
            if not isinstance(questions[r], str):
                raise IssueRecoveryError("context origin question is "
                                         "not text",
                                         status=502, code="corrupt")
        if refs:
            return [{"index": r, "text": questions[r]} for r in refs]
    return []


def _find_identical_context_fp(root, origin_input: str,
                               origin_report: str, text: str,
                               refs: list) -> str:
    """Bound fingerprint of an already-recorded identical context.

    Matches on exact origin input/report ids, exact text bytes, and
    exact refs against well-formed operator records only. Returns ""
    when nothing matches: the caller then refuses the stale foreign
    origin. Malformed records fail closed here, consistent with the
    status projection.
    """
    try:
        names = _context_names(root)
    except IssueRecoveryError:
        return ""
    for name in names:
        if not _RECORD_RE.fullmatch(name):
            continue
        try:
            cand = _read_record(root, name)
        except IssueRecoveryError:
            continue
        if not isinstance(cand, dict):
            continue
        if cand.get("kind") != "recovery-context" \
                or cand.get("schema") != 1 \
                or cand.get("context_version") != _CONTEXT_VERSION \
                or cand.get("actor") != "operator":
            continue
        origin = cand.get("origin")
        if not isinstance(origin, dict):
            continue
        if origin.get("input") != origin_input \
                or origin.get("report") != origin_report:
            continue
        if cand.get("text") != text \
                or list(cand.get("question_refs") or []) != list(refs):
            continue
        fp = origin.get("fingerprint")
        if isinstance(fp, str) and _FP_RE.fullmatch(fp):
            return fp
    return ""


def _prove_origin_current(op_id: str, ctx, root, rec: dict) -> dict:
    """Re-prove a stored context record's bound origin, live.

    The record's own bytes must recompute to its digest with the
    fixed operator actor and a matching id prefix; its bound raw-byte
    hashes must still match the live origin records byte-for-byte;
    the bound question texts must still match; the origin input
    identity must still prove; and the live binding must still
    reproduce the origin fingerprint. Anything changed is stale or
    corrupt — never rebound. Returns the origin's stored input.
    Predecessor verification stays one level deep: no chain walking,
    no healing.
    """
    if not isinstance(rec, dict) or rec.get("op") != op_id:
        raise IssueRecoveryError("stored context belongs to another op",
                                 status=502, code="corrupt")
    stored = _verify_origin_live(op_id, root, rec)
    _validate_stored_identity(op_id, stored)
    if _current_fp(ctx, stored) != stored.get("fingerprint"):
        raise IssueRecoveryError("context origin is stale",
                                 status=409, code="stale-context-origin")
    return stored


def _validate_stored_identity(op_id: str, stored) -> dict:
    """Prove the immutable original identity of a stored review input.

    Checks the stored record's own shape (schema/kind/op, issue,
    candidate, base, base_commit, run, origin, conflict scope, and
    snapshot), then recomputes the review fingerprint from the
    STORED original binding alone and requires it to equal the
    stored fingerprint. A mismatch means the stored bytes were
    tampered with or bound to a different source: corrupt.

    Records written before the original-identity binding (no stored
    run SHA) cannot prove that linkage; they are returned as
    explicitly unverifiable, never blessed. History display may only
    use a stored identity this function accepts as proven.
    """
    if not isinstance(stored, dict):
        raise IssueRecoveryError("stored review input is not an object",
                                 status=502, code="corrupt")
    if stored.get("schema") != 1 or stored.get("kind") != "recovery-input":
        raise IssueRecoveryError("stored review input has a wrong shape",
                                 status=502, code="corrupt")
    if stored.get("op") != op_id:
        raise IssueRecoveryError("stored review input belongs to "
                                 "another op", status=502, code="corrupt")
    fp = stored.get("fingerprint")
    parent_sha = stored.get("parent_sha")
    run_sha = stored.get("run_sha")
    if not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        raise IssueRecoveryError("stored review fingerprint malformed",
                                 status=502, code="corrupt")
    if not isinstance(parent_sha, str) or not _FP_RE.fullmatch(parent_sha):
        raise IssueRecoveryError("stored parent SHA malformed",
                                 status=502, code="corrupt")
    if not isinstance(run_sha, str) or not _FP_RE.fullmatch(run_sha):
        # Written before the original-identity binding captured the
        # run SHA: this record's original linkage cannot be proved.
        # Keep its bytes untouched and refuse to attribute it.
        raise IssueRecoveryError(
            "stored review input predates original-identity binding; "
            "its artifacts are preserved but cannot be attributed",
            status=502, code="unverifiable")
    issue = stored.get("issue")
    if not isinstance(issue, dict):
        raise IssueRecoveryError("stored issue identity malformed",
                                 status=502, code="corrupt")
    number = issue.get("issue")
    if isinstance(number, bool) or not isinstance(number, str) \
            or not _NUMBER_RE.fullmatch(number):
        raise IssueRecoveryError("stored issue number malformed",
                                 status=502, code="corrupt")
    for key in ("repo", "lane", "url", "branch"):
        if not isinstance(issue.get(key), str) or not issue.get(key):
            raise IssueRecoveryError("stored issue identity malformed",
                                     status=502, code="corrupt")
    if issue.get("lane") not in _LANES:
        raise IssueRecoveryError("stored issue lane is unknown",
                                 status=502, code="corrupt")
    cand = stored.get("candidate")
    if not isinstance(cand, dict):
        raise IssueRecoveryError("stored candidate identity malformed",
                                 status=502, code="corrupt")
    if not isinstance(cand.get("head"), str) \
            or not _SHA_RE.fullmatch(cand.get("head")):
        raise IssueRecoveryError("stored candidate head malformed",
                                 status=502, code="corrupt")
    if not isinstance(cand.get("fingerprint"), str) \
            or not _FP_RE.fullmatch(cand.get("fingerprint")):
        raise IssueRecoveryError("stored candidate fingerprint malformed",
                                 status=502, code="corrupt")
    for key in ("branch", "worktree"):
        if not isinstance(cand.get(key), str) or not cand.get(key):
            raise IssueRecoveryError("stored candidate identity malformed",
                                     status=502, code="corrupt")
    base = stored.get("base")
    if not isinstance(base, dict):
        raise IssueRecoveryError("stored base identity malformed",
                                 status=502, code="corrupt")
    if not isinstance(base.get("batch"), str) or not base.get("batch"):
        raise IssueRecoveryError("stored base identity malformed",
                                 status=502, code="corrupt")
    if base.get("lane") not in _LANES:
        raise IssueRecoveryError("stored base lane is unknown",
                                 status=502, code="corrupt")
    if not isinstance(base.get("head"), str) \
            or not _SHA_RE.fullmatch(base.get("head")):
        raise IssueRecoveryError("stored base head malformed",
                                 status=502, code="corrupt")
    if not isinstance(base.get("fingerprint"), str) \
            or not _FP_RE.fullmatch(base.get("fingerprint")):
        raise IssueRecoveryError("stored base fingerprint malformed",
                                 status=502, code="corrupt")
    if not isinstance(base.get("checkout"), str) or not base.get("checkout"):
        raise IssueRecoveryError("stored base identity malformed",
                                 status=502, code="corrupt")
    if not isinstance(stored.get("base_commit"), str) \
            or not _SHA_RE.fullmatch(stored.get("base_commit")):
        raise IssueRecoveryError("stored base commit malformed",
                                 status=502, code="corrupt")
    run = stored.get("run")
    if not isinstance(run, dict) or not isinstance(run.get("id"), str) \
            or not run.get("id"):
        raise IssueRecoveryError("stored run identity malformed",
                                 status=502, code="corrupt")
    if not isinstance(stored.get("origin_url"), str):
        raise IssueRecoveryError("stored origin URL malformed",
                                 status=502, code="corrupt")
    cps = stored.get("conflicted_paths")
    if not isinstance(cps, list) or any(not isinstance(p, str) for p in cps):
        raise IssueRecoveryError("stored conflict scope malformed",
                                 status=502, code="corrupt")
    if "op_error" in stored and not isinstance(stored.get("op_error"), str):
        raise IssueRecoveryError("stored op error malformed",
                                 status=502, code="corrupt")
    if not isinstance(stored.get("partial"), list):
        raise IssueRecoveryError("stored partial scope malformed",
                                 status=502, code="corrupt")
    refs = (stored.get("snapshot") or {}).get("refs") \
        if isinstance(stored.get("snapshot"), dict) else None
    if not isinstance(refs, dict):
        raise IssueRecoveryError("stored snapshot refs malformed",
                                 status=502, code="corrupt")
    # Contract gate: records written before the original-attribution
    # binding (refs map, op_error/conflict/partial scope) cannot prove
    # that binding. They stay preserved but explicitly unverifiable —
    # never migrated or healed.
    if stored.get("contract") != _CONTRACT_VERSION:
        raise IssueRecoveryError(
            "stored review input predates the current identity "
            "contract and cannot be verified",
            status=502, code="unverifiable")
    sent, scope, prompt_sha, meta = _stored_sent_scope_meta(stored)
    original = _stored_original_block(stored)
    context_digest = _stored_context_digest(op_id, stored,
                                              rec_root(op_id))
    recomputed = review_fingerprint(
        {"raw_sha": parent_sha,
         "ctx": {"run_sha": run_sha, "url": stored.get("origin_url")},
         "candidate": cand, "base": base,
         "base_commit": stored.get("base_commit"),
         "run": run, "issue": issue},
        sent, scope, prompt_sha, meta, original,
        context_digest=context_digest)
    if recomputed != fp:
        raise IssueRecoveryError("stored review input does not match "
                                 "its fingerprint",
                                 status=502, code="corrupt")
    return {"run_sha": run_sha, "fingerprint": fp,
            "parent_sha": parent_sha}


def _validate_owner_link(root: Path, op_id: str,
                        latest: dict, stored: dict) -> dict:
    """Validate latest -> owner attempt -> stored input ownership.

    Shared by EVERY lifecycle status (queued, running, error, stale,
    cancelled, interrupted, done): no attempt may be projected,
    attached, or reused until its latest pointer, owner record, and
    stored input provably belong to the same op, kind, schema, rid,
    input, and fingerprint. Controlled record names are enforced and
    foreign or incomplete links are typed corruptions — never healthy
    history, and never silently emptied. Report presence is NOT
    checked here; report-schema validation is the separate done step.
    """
    if not isinstance(latest, dict) or not latest:
        raise IssueRecoveryError("latest review pointer is malformed",
                                 status=502, code="corrupt")
    if not isinstance(stored, dict):
        raise IssueRecoveryError("latest input link is broken",
                                 status=502, code="corrupt")
    name = latest.get("input")
    if not isinstance(name, str) or not name \
            or not _RECORD_RE.fullmatch(f"{name}.json"):
        raise IssueRecoveryError("latest input link is broken",
                                 status=502, code="corrupt")
    rid = latest.get("rid")
    if not isinstance(rid, str) or not rid \
            or not _RECORD_RE.fullmatch(f"attempt-{rid}.json"):
        raise IssueRecoveryError("latest attempt link is broken",
                                 status=502, code="corrupt")
    fp = latest.get("fingerprint")
    if not isinstance(fp, str) or not _FP_RE.fullmatch(fp) \
            or fp != stored.get("fingerprint"):
        raise IssueRecoveryError("latest fingerprint link is broken",
                                 status=502, code="corrupt")
    owner = _read_record(root, f"attempt-{rid}.json")
    if owner is None:
        raise IssueRecoveryError("owner attempt is missing",
                                 status=502, code="corrupt")
    if not isinstance(owner, dict):
        raise IssueRecoveryError("owner attempt is unreadable",
                                 status=502, code="corrupt")
    if owner.get("kind") != "recovery-attempt" \
            or owner.get("schema") != 1:
        raise IssueRecoveryError("owner attempt has unknown kind",
                                 status=502, code="corrupt")
    if owner.get("op") != op_id or owner.get("rid") != rid \
            or owner.get("input") != name \
            or owner.get("fingerprint") != stored.get("fingerprint"):
        raise IssueRecoveryError("owner attempt does not match input",
                                 status=502, code="corrupt")
    return owner


def _validate_report_chain(root: Path, op_id: str, latest: dict,
                           stored: dict):
    """Full original linkage latest -> attempt -> input -> report.

    Every link is checked against the validated stored input
    identity: record shapes, op/rid/input/fingerprint ownership, the
    deterministic report id, and the exact stored schema/kind chain.
    Returns the validated report mapping. Anything broken is a
    corrupt store (bytes preserved, never healed or skipped), never
    a healthy empty view. Callers only invoke this for a latest
    pointer that claims a completed review with a report id.
    """
    if not isinstance(latest, dict) or not latest:
        raise IssueRecoveryError("latest review pointer is malformed",
                                 status=502, code="corrupt")
    rid = latest.get("rid")
    input_id = latest.get("input")
    fp = latest.get("fingerprint")
    if not isinstance(rid, str) \
            or not _RECORD_RE.fullmatch(f"attempt-{rid}.json"):
        raise IssueRecoveryError("latest attempt link is malformed",
                                 status=502, code="corrupt")
    if not isinstance(input_id, str) \
            or not _RECORD_RE.fullmatch(f"{input_id}.json"):
        raise IssueRecoveryError("latest input link is malformed",
                                 status=502, code="corrupt")
    if not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        raise IssueRecoveryError("latest fingerprint link is malformed",
                                 status=502, code="corrupt")
    if latest.get("status") != "done":
        raise IssueRecoveryError("completed review pointer lost its status",
                                 status=502, code="corrupt")
    report_id = latest.get("report")
    if report_id != f"report-{fp[:16]}":
        raise IssueRecoveryError("latest report link is malformed",
                                 status=502, code="corrupt")
    # Shared ownership for every status: latest -> owner attempt ->
    # stored input. A completed latest without its report is corrupt
    # here, never silently empty.
    attempt = _validate_owner_link(root, op_id, latest, stored)
    if attempt.get("status") != "done" \
            or attempt.get("report") != report_id:
        raise IssueRecoveryError("attempt link does not match latest",
                                 status=502, code="corrupt")
    report_doc = _read_record(root, f"{report_id}.json")
    if report_doc is None:
        raise IssueRecoveryError("completed review lost its report",
                                 status=502, code="corrupt")
    for key, want in (("schema", 1), ("kind", "recovery-report"),
                      ("op", op_id), ("rid", rid), ("fingerprint", fp),
                      ("input", input_id)):
        if not isinstance(report_doc, dict) or report_doc.get(key) != want:
            raise IssueRecoveryError("report link does not match attempt",
                                     status=502, code="corrupt")
    return validate_report(report_doc.get("report"),
                           stored["snapshot"]["files"],
                           stored["snapshot"].get("omitted"),
                           stored["snapshot"].get("build_evidence"))


def _git(repo_dir: str, args: list, timeout: int = _FETCH_TIMEOUT_S,
         *, git_bound=None):
    """Run git in a repo dir; failures are typed.

    Always over the hardened path: -c overrides plus the scrubbed
    env (system/global/XDG config off), in the unbound branch too.
    Bound (worker-touched tree): re-prove the .git pointer first;
    a changed pointer refuses git-pointer-changed before any
    spawn. Because global config is off everywhere here, these
    calls no longer see a global LFS filter.
    302d: argv construction runs inside the typed handling, so a
    probe refusal (SourceUnreadable) or a hung probe
    (TimeoutExpired) surfaces as the typed 502 "unreadable",
    never raw. Pointer refusal stays git-pointer-changed.
    """
    from . import runner as _runner
    if git_bound is None:
        bound_paths = None
    else:
        try:
            bound_paths = _runner.reprove_git_pointer(
                repo_dir, git_bound)
        except _runner.SourceUnreadable as exc:
            raise IssueRecoveryError(
                "worktree git identity changed since prepare",
                status=502, code="git-pointer-changed") from exc
    try:
        if bound_paths is None:
            argv = _runner.hardened_git_argv(
                ["-C", str(repo_dir)] + list(args))
        else:
            argv = _runner.hardened_git_argv(
                list(args), gitdir=bound_paths["gitdir"],
                worktree=bound_paths["worktree"])
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=_runner.hardened_git_env())
    except _runner.SourceUnreadable as exc:
        raise IssueRecoveryError(f"git unreadable: {exc}",
                                 status=502,
                                 code="unreadable") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IssueRecoveryError(f"git read failed: {exc}",
                                 status=502, code="unreadable") from exc
    return proc


def _ref_readable(repo_dir: str, head: str, *,
                  git_bound=None) -> bool:
    """True only when the pinned commit itself resolves.

    A bad revision, missing checkout, or Git failure means the
    SOURCE is unreadable — never a valid deletion. Only a path
    absent from the tree of a READABLE pinned commit is a genuine
    absence, recorded with that commit as evidence.
    """
    probe = _git(repo_dir, ["cat-file", "-e", f"{head}^{{commit}}"],
                   git_bound=git_bound)
    return probe.returncode == 0


def _path_absent(repo_dir: str, head: str, path: str, *,
                 git_bound=None) -> bool:
    probe = _git(repo_dir, ["ls-tree", head, "--", path],
                 git_bound=git_bound)
    if probe.returncode != 0:
        raise IssueRecoveryError(
            f"unreadable tree at {head[:12]} for {path}",
            status=502, code="unreadable")
    return not probe.stdout.decode("utf-8", "ignore").strip()


def _render_build_file(path: str, ref_kind: str, ref: str,
                       blob_oid: str, raw_len: int, raw_sha256: str,
                       cwd: str, scripts: dict,
                       omitted_scripts: list) -> str:
    """Canonical rendered build-evidence section for one manifest.

    Shared by the writer and stored replay: both sides compute this
    exact text from the entry fields, so any metadata-only tamper
    (scripts, omission kinds, and the bound raw_sha256 alike) breaks
    the stored text equality and refuses as corrupt. Never healed
    or defaulted. Every eligible entry transmits its raw_sha256 and
    its allowlisted cwd IN THE PROMPT ITSELF: the model cannot
    derive the SHA256 of an untransmitted manifest from a Git blob
    SHA or a script subset, so the declaration link is only sound
    when these bytes are actually sent. Omitted/unread entries carry
    no raw SHA claim. Omitted scripts render as grouped counts by
    stable taxonomy kind only — never by the exact reason string,
    which embeds varying lengths. Exact per-script names and reasons
    stay in the stored structure (and the meta projection), so even
    a worst-case spread of omission reasons can never defeat the
    global section budget.
    """
    lines = [f"--- build: {ref_kind}:{path} (ref {ref}, blob {blob_oid}, "
             f"raw {raw_len}B, scripts {len(scripts)}, cwd \"{cwd}\") ---",
             f"raw_sha256: {raw_sha256}"]
    for name in sorted(scripts):
        lines.append(f"{name} = {scripts[name]}")
    groups: dict = {}
    for entry in omitted_scripts:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind not in _BUILD_OMIT_KIND_LABEL:
            kind = entry.get("reason") or ""
        groups[kind] = groups.get(kind, 0) + 1
    order = list(_BUILD_OMIT_KIND_LABEL) + sorted(
        k for k in groups if k not in _BUILD_OMIT_KIND_LABEL)
    for kind in order:
        if kind not in groups:
            continue
        label = _BUILD_OMIT_KIND_LABEL.get(kind, kind)
        lines.append(f"-- 제외 스크립트 {groups[kind]}건: {label}")
    return "\n".join(lines)


def _render_build_section(files: list, omitted: list) -> str:
    """Canonical full transmitted build-evidence section (shared).

    Header plus every entry text plus every file-omission line.
    The writer transmits exactly this; the global budget pass drops
    script lines (never omission facts) until it fits.
    """
    lines = ["빌드 근거(고정 커밋 package.json의 선언 사실만 "
             "전달한다. 실행 권한이 아니며, 코드 인용 대상이 "
             "아니다. 아래 명령 문자열은 실행된 사실이 아니라 "
             "선언된 텍스트다. build_contract의 declaration을 쓸 때는 "
             "아래 transmitted 항목의 manifest·ref_kind·ref·script·"
             "raw_sha256·cwd 값을 그대로 복사하라(추정·계산 금지). "
             "미전송 항목에는 raw 해시가 없으므로 선언할 수 없다):"]
    for entry in files:
        lines.append(entry["text"])
    for entry in omitted:
        detail = entry.get("reason", "제외")
        at = ""
        if entry.get("ref_kind") and entry.get("ref"):
            at = f" @{entry['ref_kind']}:{str(entry['ref'])[:12]}"
        if entry.get("absent"):
            detail += " (고정 커밋의 트리에 없음이 확인됨)"
        lines.append(f"- 빌드 근거 제외 "
                     f"{entry.get('path', '?')}{at}: {detail}")
    return "\n".join(lines)


def _fit_build_budget(files: list, omitted: list) -> None:
    """Enforce the GLOBAL section budget in place (writer only).

    Drops the largest rendered script line across all manifests
    (deterministic tiebreak) into that file's omitted_scripts until
    the complete canonical section fits _BUILD_SECTION_CAP. Omission
    facts are never dropped and render as taxonomy-kind counts only,
    so the loop always terminates with bounded truthful output. When
    no script line remains the section still must fit: anything over
    budget at that point fails closed as corrupt (unreachable with
    the bounded fixed parts, but never silently transmitted).
    """
    while True:
        texts = {}
        for e in files:
            texts[id(e)] = _render_build_file(
                e["path"], e["ref_kind"], e["ref"], e["blob_oid"],
                e["raw_len"], e["raw_sha256"],
                _BUILD_CWD[e["path"]], e["scripts"],
                e["omitted_scripts"])
        section = _render_build_section(
            [{"text": texts[id(e)]} for e in files], omitted)
        if len(section.encode("utf-8")) <= _BUILD_SECTION_CAP:
            for e in files:
                e["text"] = texts[id(e)]
                raw = e["text"].encode("utf-8")
                e["sent_sha256"] = hashlib.sha256(raw).hexdigest()
                e["sent_bytes"] = len(raw)
                e["scripts_truncated"] = bool(e["omitted_scripts"])
            return
        widest = None
        for e in files:
            for name, cmd in e["scripts"].items():
                cand = (len(f"{name} = {cmd}".encode("utf-8")),
                        e["path"], e["ref_kind"], e["ref"], name)
                if widest is None or cand > widest[0]:
                    widest = (cand, e)
        if widest is None:
            # No script lines left to drop: fixed parts (headers,
            # taxonomy group counts, file-omission lines) are bounded
            # by construction, so reaching this point over budget
            # means the representation itself is broken — fail
            # closed rather than transmit more than the cap.
            raise IssueRecoveryError(
                "build section over budget",
                status=502, code="corrupt")
        (_, _, _, _, name), entry = widest[0], widest[1]
        del entry["scripts"][name]
        entry["omitted_scripts"].append(
            {"name": name, "kind": "budget",
             "reason": f"전송 예산 초과({_BUILD_SECTION_CAP}바이트) — "
                       "미전송"})


def _project_build_meta(blk) -> dict:
    """Validate a build_evidence block; return its canonical projection.

    The ONE shared validator used by the writer (fresh collection)
    and stored replay/currentness: both compute the identical
    projection, so any stored mutation breaks equality or raises
    corrupt. Exact key sets, pinned-ref shapes, unique (path,
    ref_kind, ref) coverage, and byte bounds are all enforced — no
    unknown fields, no unbound provenance. A missing key is NOT
    handled here: callers treat a missing key as the legacy path
    (exact prior meta, no addition). Present null / wrong version /
    malformed never bypasses as legacy.
    """
    if not isinstance(blk, dict) or set(blk.keys()) != {
            "version", "files", "omitted"}:
        raise IssueRecoveryError("build evidence block malformed",
                                 status=502, code="corrupt")
    if blk.get("version") != _BUILD_VERSION:
        raise IssueRecoveryError("build evidence version unknown",
                                 status=502, code="corrupt")
    files = blk.get("files")
    omitted = blk.get("omitted")
    if not isinstance(files, list) or not isinstance(omitted, list):
        raise IssueRecoveryError("build evidence block malformed",
                                 status=502, code="corrupt")
    seen: set = set()
    proj_files = []
    for e in files:
        if not isinstance(e, dict) or set(e.keys()) != _BUILD_FILE_KEYS:
            raise IssueRecoveryError("build evidence file malformed",
                                     status=502, code="corrupt")
        if e.get("path") not in _BUILD_PATHS \
                or e.get("ref_kind") not in ("origin", "candidate",
                                              "base"):
            raise IssueRecoveryError("build evidence file malformed",
                                     status=502, code="corrupt")
        for key in ("ref", "blob_oid"):
            val = e.get(key)
            if not isinstance(val, str) \
                    or not _REF_SHA_RE.fullmatch(val):
                raise IssueRecoveryError(
                    "build evidence source identity malformed",
                    status=502, code="corrupt")
        triple = (e.get("path"), e.get("ref_kind"), e.get("ref"))
        if triple in seen:
            raise IssueRecoveryError(
                "build evidence source duplicated",
                status=502, code="corrupt")
        seen.add(triple)
        raw_sha = e.get("raw_sha256")
        sent_sha = e.get("sent_sha256")
        if not isinstance(raw_sha, str) \
                or not _SHA256_RE.fullmatch(raw_sha) \
                or not isinstance(sent_sha, str) \
                or not _SHA256_RE.fullmatch(sent_sha):
            raise IssueRecoveryError(
                "build evidence digest malformed",
                status=502, code="corrupt")
        raw_len = e.get("raw_len")
        sent_bytes = e.get("sent_bytes")
        if not isinstance(raw_len, int) or not 0 <= raw_len \
                <= _BUILD_RAW_CAP \
                or not isinstance(sent_bytes, int) \
                or not 0 <= sent_bytes <= _BUILD_SECTION_CAP:
            raise IssueRecoveryError("build evidence size malformed",
                                     status=502, code="corrupt")
        scripts = e.get("scripts")
        cut = e.get("omitted_scripts")
        if not isinstance(scripts, dict) or not isinstance(cut, list):
            raise IssueRecoveryError("build evidence scripts malformed",
                                     status=502, code="corrupt")
        if len(scripts) + len(cut) > _BUILD_SCRIPTS_CAP:
            raise IssueRecoveryError("build evidence scripts malformed",
                                     status=502, code="corrupt")
        for name, cmd in scripts.items():
            if not isinstance(name, str) or not name \
                    or len(name) > _BUILD_NAME_CAP \
                    or not isinstance(cmd, str) \
                    or len(cmd) > _BUILD_CMD_CAP:
                raise IssueRecoveryError(
                    "build evidence scripts malformed",
                    status=502, code="corrupt")
        for entry in cut:
            if not isinstance(entry, dict) \
                    or set(entry.keys()) != {"name", "kind", "reason"} \
                    or not isinstance(entry.get("name"), str) \
                    or not entry.get("name") \
                    or entry.get("kind") not in _BUILD_OMIT_KIND_LABEL \
                    or not isinstance(entry.get("reason"), str) \
                    or not entry.get("reason"):
                raise IssueRecoveryError(
                    "build evidence scripts malformed",
                    status=502, code="corrupt")
        if not isinstance(e.get("scripts_truncated"), bool) \
                or bool(cut) != e.get("scripts_truncated"):
            raise IssueRecoveryError(
                "build evidence truncation flag mismatched",
                status=502, code="corrupt")
        text = e.get("text")
        if not isinstance(text, str):
            raise IssueRecoveryError("build evidence text malformed",
                                     status=502, code="corrupt")
        if _render_build_file(e["path"], e["ref_kind"], e["ref"],
                              e["blob_oid"], raw_len,
                              e.get("raw_sha256"),
                              _BUILD_CWD[e["path"]], scripts,
                              cut) != text:
            raise IssueRecoveryError(
                "build evidence text does not match its metadata",
                status=502, code="corrupt")
        raw_text = text.encode("utf-8")
        if hashlib.sha256(raw_text).hexdigest() != sent_sha \
                or len(raw_text) != sent_bytes:
            raise IssueRecoveryError(
                "build evidence text does not match its hash",
                status=502, code="corrupt")
        proj_files.append({k: v for k, v in e.items() if k != "text"})
    proj_omitted = []
    for o in omitted:
        if not isinstance(o, dict) \
                or not set(o.keys()) <= _BUILD_OMIT_KEYS \
                or o.get("path") not in _BUILD_PATHS \
                or o.get("ref_kind") not in ("origin", "candidate",
                                              "base") \
                or not isinstance(o.get("ref"), str) \
                or not _REF_SHA_RE.fullmatch(o.get("ref")):
            raise IssueRecoveryError("build evidence omission malformed",
                                     status=502, code="corrupt")
        if "absent" in o and not isinstance(o["absent"], bool):
            raise IssueRecoveryError("build evidence omission malformed",
                                     status=502, code="corrupt")
        if "tree" in o and not isinstance(o["tree"], str):
            raise IssueRecoveryError("build evidence omission malformed",
                                     status=502, code="corrupt")
        if "blob_oid" in o and (
                not isinstance(o["blob_oid"], str)
                or not _REF_SHA_RE.fullmatch(o["blob_oid"])):
            raise IssueRecoveryError("build evidence omission malformed",
                                     status=502, code="corrupt")
        if "size" in o and (
                not isinstance(o["size"], int) or o["size"] < 0):
            raise IssueRecoveryError("build evidence omission malformed",
                                     status=502, code="corrupt")
        if "reason" in o and (not isinstance(o["reason"], str)
                              or not o["reason"]):
            raise IssueRecoveryError("build evidence omission malformed",
                                     status=502, code="corrupt")
        proj_omitted.append({k: o.get(k) for k in
                             ("path", "ref_kind", "ref", "absent",
                              "tree", "blob_oid", "size", "reason")
                             if k in o})
    return {"version": _BUILD_VERSION, "files": proj_files,
            "omitted": proj_omitted}


def _collect_build_entry(path: str, kind: str, head: str,
                         repo_dir: str):
    """Read one allowlisted manifest at one pinned ref (read-only).

    Returns (is_file, entry). A path genuinely absent from the tree
    of a READABLE pinned commit is a recorded absence. Type and size
    are proven before any content read; only a proven eligible blob
    is fully read. Missing pinned refs or Git IO failures raise
    unreadable (fail-closed); present non-file, binary, invalid, or
    wrong-shaped manifests raise corrupt — never a silent omission
    and never a null success. Per-script length violations omit that
    script with an exact reason; shape violations refuse the file.
    Entries carry parsed scripts WITHOUT rendered text: the global
    budget pass renders afterwards, so one canonical section budget
    binds all entries. Oversized blobs keep size/blob identity in
    their omission entry but no computed raw SHA.
    """
    if _path_absent(repo_dir, head, path):
        return False, {"path": path, "ref_kind": kind, "ref": head,
                       "absent": True, "tree": head,
                       "reason": "읽기 가능한 고정 커밋의 트리에 없음"}
    # Type and size are proven BEFORE any content read: only a proven
    # eligible blob (a file, <= _BUILD_RAW_CAP bytes) is fully read.
    # Missing pinned refs or Git IO failures are unreadable
    # (fail-closed); a present non-file or binary/invalid manifest
    # is corrupt — never a silent absence or null success.
    typ = _git(repo_dir, ["cat-file", "-t", f"{head}:{path}"])
    if typ.returncode != 0:
        raise IssueRecoveryError(
            f"unreadable build blob {kind}:{path}",
            status=502, code="unreadable")
    if typ.stdout.decode("utf-8", "ignore").strip() != "blob":
        raise IssueRecoveryError(
            f"build manifest is not a file: {kind}:{path}",
            status=502, code="corrupt")
    blob = _git(repo_dir, ["rev-parse", f"{head}:{path}"])
    blob_id = blob.stdout.decode("utf-8", "ignore").strip()
    if blob.returncode != 0 or not blob_id:
        raise IssueRecoveryError(
            f"unreadable build blob {kind}:{path}",
            status=502, code="unreadable")
    sizep = _git(repo_dir, ["cat-file", "-s", f"{head}:{path}"])
    try:
        size = int(sizep.stdout.decode("utf-8", "ignore").strip())
    except ValueError:
        size = -1
    if sizep.returncode != 0 or size < 0:
        raise IssueRecoveryError(
            f"unreadable build blob {kind}:{path}",
            status=502, code="unreadable")
    if size > _BUILD_RAW_CAP:
        # size comes from `git cat-file -s`: bytes, never chars.
        # Omission is not transmitted / unknown — never proof of
        # no declaration.
        return False, {"path": path, "ref_kind": kind, "ref": head,
                       "blob_oid": blob_id, "size": size,
                       "reason": f"빌드 근거 제외(대용량 {size}바이트 > "
                                 f"{_BUILD_RAW_CAP}바이트) — 미전송"
                                 "(이름만 기록; 선언 여부는 알 수 없음)"}
    show = _git(repo_dir, ["show", f"{head}:{path}"])
    if show.returncode != 0:
        raise IssueRecoveryError(
            f"unreadable build blob {kind}:{path}",
            status=502, code="unreadable")
    data = show.stdout
    if b"\x00" in data:
        raise IssueRecoveryError(
            f"build manifest is binary: {kind}:{path}",
            status=502, code="corrupt")
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IssueRecoveryError(
            f"build manifest is not valid JSON: {kind}:{path}",
            status=502, code="corrupt")
    if not isinstance(doc, dict):
        raise IssueRecoveryError(
            f"build manifest is not an object: {kind}:{path}",
            status=502, code="corrupt")
    raw_scripts = doc.get("scripts", {})
    if not isinstance(raw_scripts, dict):
        raise IssueRecoveryError(
            f"build manifest scripts malformed: {kind}:{path}",
            status=502, code="corrupt")
    if len(raw_scripts) > _BUILD_SCRIPTS_CAP:
        return False, {"path": path, "ref_kind": kind, "ref": head,
                       "blob_oid": blob_id, "size": len(data),
                       "reason": f"빌드 근거 제외(스크립트 수 초과: "
                                 f"{len(raw_scripts)}건) — 미전송"
                                 "(이름만 기록; 선언 여부는 알 수 없음)"}
    scripts: dict = {}
    cut: list = []
    for name in sorted(raw_scripts):
        cmd = raw_scripts[name]
        if not isinstance(name, str) or not name \
                or not isinstance(cmd, str):
            raise IssueRecoveryError(
                f"build manifest script malformed: {kind}:{path}",
                status=502, code="corrupt")
        if len(name) > _BUILD_NAME_CAP:
            raise IssueRecoveryError(
                f"build manifest script malformed: {kind}:{path}",
                status=502, code="corrupt")
        if len(cmd) > _BUILD_CMD_CAP:
            cut.append({"name": name, "kind": "long-command",
                        "reason": f"스크립트 명령 길이 초과"
                                  f"({len(cmd)}자) — 미전송"})
            continue
        scripts[name] = cmd
    raw = hashlib.sha256(data).hexdigest()
    entry = {"path": path, "ref_kind": kind, "ref": head,
             "blob_oid": blob_id, "raw_sha256": raw,
             "raw_len": len(data), "scripts": scripts,
             "omitted_scripts": sorted(
                 cut, key=lambda e: e.get("name", ""))}
    return True, entry


def collect_windows(ctx: dict, paths=None) -> dict:
    """Read pinned blobs for the conflicted/original paths (read-only).

    Each path is read at three distinct pinned refs: the original run
    base commit ("origin", read from the candidate worktree), the
    original candidate head ("candidate"), and the selected integration
    base head ("base", read from the base checkout). A path genuinely
    absent at a pinned ref is recorded as absent WITH that ref as
    evidence, distinctly from an unreadable command/blob failure. A
    missing candidate OR base checkout is unreadable, never a silent
    omission. Generated or minified assets over the cap are omitted
    from MODEL text with exact hash/size lists — omission from model
    text never omits the original patch intent from the review record.
    Paths beyond the scope cap are recorded as dropped with an exact
    count and reason instead of being silently cut.
    """
    op = ctx["op"]
    cand = ctx["candidate"]
    base = ctx["base"]
    res = ctx["result"]
    wanted = list(paths) if paths is not None else list(
        res.get("conflicted_paths") or [])
    extra = parse_original_touched(op.get("error") or "")
    for path in extra:
        if path not in wanted:
            wanted.append(path)
    for path in wanted:
        if not isinstance(path, str) or not path or path.startswith("/") \
                or ".." in path.split("/") or len(path) > 256:
            raise IssueRecoveryError(f"rejected path: {path!r}",
                                     status=400, code="bad-path")
    before_scope = list(wanted)
    dropped = wanted[_MAX_FILES:]
    wanted = wanted[:_MAX_FILES]
    cand_wt = cand["worktree"]
    base_wt = base["checkout"]
    if not cand_wt or not os.path.isdir(cand_wt):
        raise IssueRecoveryError("unreadable candidate checkout",
                                 status=502, code="unreadable")
    if not base_wt or not os.path.isdir(base_wt):
        raise IssueRecoveryError("unreadable base checkout",
                                 status=502, code="unreadable")
    base_commit = ctx["base_commit"]
    refs = [("origin", base_commit, cand_wt),
            ("candidate", cand["head"], cand_wt),
            ("base", base["head"], base_wt)]
    for kind, head, repo_dir in refs:
        if not _ref_readable(repo_dir, head):
            raise IssueRecoveryError(
                f"unreadable pinned {kind} ref {head[:12]}",
                status=502, code="unreadable")
    windows: list[dict] = []
    omitted: list[dict] = []
    total = 0
    for path in dropped:
        omitted.append({"path": path, "scope": "path",
                        "ref_kinds": 3,
                        "reason": f"범위 초과(최대 {_MAX_FILES}건) — "
                                  "전체 의도 유지, 미전송"})
    for path in wanted:
        for kind, head, repo_dir in refs:
            if _path_absent(repo_dir, head, path):
                omitted.append({"path": path, "ref_kind": kind,
                                "ref": head, "absent": True,
                                "tree": head,
                                "reason": "읽기 가능한 고정 커밋의 트리에 없음"})
                continue
            blob = _git(repo_dir, ["rev-parse", f"{head}:{path}"])
            blob_id = blob.stdout.decode("utf-8", "ignore").strip()
            if blob.returncode != 0 or not blob_id:
                raise IssueRecoveryError(
                    f"unreadable blob {kind}:{path}",
                    status=502, code="unreadable")
            show = _git(repo_dir, ["show", f"{head}:{path}"])
            if show.returncode != 0:
                raise IssueRecoveryError(
                    f"unreadable blob {kind}:{path}",
                    status=502, code="unreadable")
            data = show.stdout
            if b"\x00" in data[:4096] or len(data) > _WINDOW_EACH_CAP * 4:
                omitted.append({"path": path, "ref_kind": kind,
                                "ref": head, "blob_oid": blob_id,
                                "size": len(data),
                                "reason": "모델 입력 제외(바이너리/대용량) — "
                                          "원본 변경 의도는 유지"})
                continue
            text = data.decode("utf-8", "ignore")
            raw = text.encode("utf-8")[:_WINDOW_EACH_CAP]
            if total + len(raw) > _WINDOW_TOTAL_CAP:
                omitted.append({"path": path, "ref_kind": kind,
                                "ref": head, "blob_oid": blob_id,
                                "size": len(data),
                                "reason": "모델 입력 제외(예산 초과) — "
                                          "원본 변경 의도는 유지"})
                continue
            total += len(raw)
            sent = raw.decode("utf-8", "ignore")
            windows.append({"path": path, "ref_kind": kind, "ref": head,
                            "blob_oid": blob_id,
                            "sent_sha256": hashlib.sha256(
                                sent.encode("utf-8")).hexdigest(),
                            "sent_bytes": len(sent.encode("utf-8")),
                            "total_bytes": len(data),
                            "line_count": sent.count("\n") + 1,
                            "partial": len(data) > len(raw),
                            "text": sent})
    if not windows and not omitted:
        raise IssueRecoveryError("no readable code at pinned refs",
                                 status=502, code="unreadable")
    # Pinned build metadata: the fixed two manifests at the same
    # three pinned refs, through the same git readers. This never
    # satisfies the no-readable-code gate above and never widens the
    # code scope — it is a separate evidence domain.
    build_files: list = []
    build_omitted: list = []
    for path in _BUILD_PATHS:
        for kind, head, repo_dir in refs:
            is_file, entry = _collect_build_entry(path, kind, head,
                                                  repo_dir)
            (build_files if is_file else build_omitted).append(entry)
    # One GLOBAL section budget across all entries (not per entry):
    # rendered text, hashes, and the truncation flag are fixed here,
    # so writer and stored replay compute identical bytes.
    _fit_build_budget(build_files, build_omitted)
    build_evidence = {"version": _BUILD_VERSION, "files": build_files,
                      "omitted": build_omitted}
    # Shared validation on write: the writer and stored replay must
    # compute the identical canonical projection.
    _project_build_meta(build_evidence)
    return {"windows": windows, "omitted": omitted,
            "build_evidence": build_evidence,
            "refs": {"origin": base_commit, "candidate": cand["head"],
                     "base": base["head"]},
            "counts": {"requested_paths": len(before_scope),
                       "collected_paths": len(wanted),
                       "dropped_paths": len(dropped),
                       "ref_kinds": len(refs),
                       "windows": len(windows),
                       "omitted": len(omitted),
                       "build_files": len(build_files),
                       "build_omitted": len(build_omitted),
                       "sent_bytes": total}}


_REPORT_SCHEMA = (
    "반드시 아래 스키마의 JSON 객체 하나로만 답하라. 다른 텍스트를 덧붙이지 마라.\n"
    '{"status": "needs_changes" | "ready_for_derived" | "undecided", '
    '"summary": str(비어 있지 않음), "cause": str(비어 있지 않음), '
    '"conflict_files": [str, ...](전송 목록 안의 경로만), '
    '"citations": [{"path": str(전송된 경로), "ref": str(전송된 40-hex 커밋), '
    '"start_line": int(>=1, 필수), "end_line": int(>=start_line, 필수), '
    '"quote": str(선언한 줄 범위 안 실제 전송 텍스트의 부분 문자열, 생략 가능)}], '
    '"intended_changes": [str, ...], '
    '"proposed_paths": [str, ...](전송된 경로만; ready는 비어 있지 않음), '
    '"build_contract": null 또는 {"builder": str(비어 있지 않음), '
    '"command": str(비어 있지 않음), "outputs": [str, ...](비어 있지 않음), '
    '"declaration": {"manifest": str(전송된 package.json 경로), '
    '"ref_kind": str(origin|candidate|base), "ref": str(전송된 커밋), '
    '"raw_sha256": str(전송된 원본 바이트 해시), '
    '"script": str(전송된 스크립트명), "cwd": str(매니페스트 위치의 작업 디렉터리)}}, '
    '"questions": [str, ...], "uncertainties": [str, ...], '
    '"limits": [str, ...], "next_action": str(비어 있지 않음)}\n'
    "규칙: needs_changes/ready 판단은 citations 또는 전송된 증거로 뒷받침하라. "
    "전송되지 않은 경로·ref·줄 범위를 인용하면 안 된다. "
    "각 citation은 start_line/end_line을 1부터 시작하는 실제 전송 줄 번호로 "
    "반드시 기재하라(생략·추측 금지). quote가 있으면 선언한 줄 범위 안의 실제 "
    "전송 텍스트와 한 글자도 다르지 않은 부분 문자열이어야 하며, 전송에서 빠진 "
    "범위나 줄을 인용한 것처럼 쓰면 안 된다. "
    "생성물(예: dist 번들)을 모델에 보내지 않은 경우 그 내용을 안다고 주장하지 마라. "
    "ready_for_derived는 proposed_paths가 비어 있지 않고, 전송에서 빠진 생성물을 "
    "제안하면 outputs가 그것을 모두 덮는 build_contract가 있어야 한다. "
    "build_contract의 command는 실행 증명이 아니다: command가 비어 있지 않으면 "
    "declaration이 있어야 하며, 선언된 manifest·ref·script와 명령의 호출 형태가 "
    "정확히 일치해야 한다(선언되지 않은 출처·모호한 이름·추정 금지)."
)


def build_prompt(ctx: dict, collected: dict, context=None) -> dict:
    """Build the bounded model prompt and exact sent snapshot.

    ``context`` is an optional supplemental operator context
    (``{"id", "text", "question_refs", "origin"}`` as validated by
    record_recovery_context). It is rendered as a clearly labeled
    OPERATOR section — investigation context for the report's
    questions, never code evidence, never approval or owner policy.
    """
    issue = ctx["issue"]
    lines = [
        "막힌 타입 이슈 준비의 충돌 원인을 조사하고 안전한 다음 단계를 "
        "제안하라. 코드를 수정하지 마라. 파일이나 저장소를 읽거나 쓰지 마라.",
        f"저장소: {issue['repo']} 이슈 #{issue['issue']} 레인 {issue['lane']}",
        f"원본 baseCommit {ctx['base_commit']} "
        f"원본 후보 head {ctx['candidate']['head']} "
        f"(지문 {ctx['candidate']['fingerprint']}) "
        f"선택 베이스 head {ctx['base']['head']} "
        f"(지문 {ctx['base']['fingerprint']})",
        f"충돌 파일: {', '.join(ctx['result'].get('conflicted_paths') or [])}",
        "아래 CONTEXT는 실제 전송된 범위다. 전송되지 않은 파일·ref·줄은 인용하지 마라.",
        _REPORT_SCHEMA,
    ]
    for win in collected["windows"]:
        lines.append(f"--- {win['ref_kind']}:{win['path']} "
                     f"(ref {win['ref']}, blob {win['blob_oid']}, "
                     f"{win['sent_bytes']}B/{win['line_count']}줄"
                     f"{', 일부' if win['partial'] else ''}) ---")
        lines.append(win["text"])
    omitted = collected["omitted"]
    if omitted:
        lines.append("전송 제외(원본 의도 유지·모델 미전송 — 완료로 보지 마라):")
        for entry in omitted:
            detail = entry.get("reason", "제외")
            at = ""
            if entry.get("ref_kind") and entry.get("ref"):
                at = f" @{entry['ref_kind']}:{str(entry['ref'])[:12]}"
            if entry.get("absent"):
                detail += " (고정 커밋의 트리에 없음이 확인됨)"
            lines.append(f"- 제외 {entry.get('path', '?')}{at}: {detail}")
    bev = collected.get("build_evidence")
    if bev is not None:
        if not isinstance(bev, dict):
            raise IssueRecoveryError("collected build evidence malformed",
                                     status=500, code="fingerprint")
        # Shared validation on write: the exact block the replay
        # side must reproduce byte-for-byte.
        _project_build_meta(bev)
        # The transmitted section is the ONE shared canonical render
        # (global header + entry OWN texts + omission lines) — never
        # a second inline copy — so the budgeted bytes, the stored
        # entries, and the prompt cannot diverge. Entry texts are
        # concatenated verbatim, never re-rendered.
        lines.append(_render_build_section(
            [{"text": e["text"]} for e in bev.get("files") or []],
            bev.get("omitted") or []))
    original = parse_original_touched(ctx["op"].get("error") or "")
    if original:
        lines.append("원본 후보 변경(재적용 거부된 의도 전달 범위): "
                     + ", ".join(original))
    lines.append("의도 전달 범위: 위 전송 코드 + 위 제외 목록이 전체 의도다. "
                 "제외를 전달 완료·확인된 것으로 보지 마라.")
    if context is not None:
        if not isinstance(context, dict):
            raise IssueRecoveryError("review context malformed",
                                     status=500, code="fingerprint")
        for key in ("id", "text", "origin"):
            if key not in context:
                raise IssueRecoveryError("review context malformed",
                                         status=500, code="fingerprint")
        refs = context.get("question_refs") or []
        if not isinstance(refs, list):
            raise IssueRecoveryError("review context malformed",
                                     status=500, code="fingerprint")
        origin = context.get("origin") or {}
        questions = context.get("questions") or []
        if not isinstance(questions, list):
            raise IssueRecoveryError("review context malformed",
                                     status=500, code="fingerprint")
        lines.append(
            "운용자 제공 추가 맥락(이 보고서를 읽은 운용자가 보낸 보충 "
            "설명이다. 코드 증거가 아니며, 승인·동의·소유자 정책 결정이 "
            "아니다. 아래를 코드 인용처럼 취급하지 마라):")
        lines.append(
            f"- 기록 {context.get('id')} "
            f"(보고서 {origin.get('report')} · 입력 {origin.get('input')}) "
            f"· 출처: 운용자(operator)")
        if refs:
            # The model receives the ACTUAL originating question text
            # per referenced number — bare indices alone cannot be
            # interpreted. Boundedly rendered, separately from code.
            qlines = []
            for r in refs:
                hit = next((q for q in questions
                            if isinstance(q, dict)
                            and q.get("index") == r
                            and isinstance(q.get("text"), str)), None)
                if hit is None:
                    raise IssueRecoveryError("review context question "
                                             "missing from origin",
                                             status=500, code="fingerprint")
                qlines.append(f"[{r + 1}] {hit['text']}")
            joined = "\n".join(qlines)
            if len(joined) > _CONTEXT_TEXT_CAP:
                joined = joined[:_CONTEXT_TEXT_CAP] \
                    + f"\n…(원본 질문 {sum(len(q) for q in qlines)}자 중 " \
                    f"{_CONTEXT_TEXT_CAP}자만 전송)"
            lines.append("- 관련 원본 질문(모델 보고서의 실제 문구):")
            lines.append(joined)
        lines.append("- 운용자 메모:")
        lines.append(str(context.get("text") or "")[:_CONTEXT_TEXT_CAP])
    prompt = "\n".join(lines)
    raw = prompt.encode("utf-8")
    snapshot = {
        "sent_text": prompt,
        "sent_bytes": len(raw),
        "sent_text_sha256": hashlib.sha256(raw).hexdigest(),
        "files": [{"path": w["path"], "ref_kind": w["ref_kind"],
                   "ref": w["ref"], "blob_oid": w["blob_oid"],
                   "sent_sha256": w["sent_sha256"],
                   "sent_bytes": w["sent_bytes"],
                   "total_bytes": w["total_bytes"],
                   "line_count": w["line_count"],
                   "partial": w["partial"],
                   "text": w["text"]} for w in collected["windows"]],
        "omitted": collected["omitted"],
        "refs": collected["refs"],
    }
    if "build_evidence" in collected:
        # Validated above; the replay side must reproduce this exact
        # block. Legacy direct callers without the key keep the exact
        # prior snapshot shape.
        _project_build_meta(collected["build_evidence"])
        snapshot["build_evidence"] = collected["build_evidence"]
    return {"prompt": prompt, "snapshot": snapshot,
            "partial": [o["reason"] for o in collected["omitted"]]}


def _write_new(path: Path, payload: dict) -> None:
    if not _RECORD_RE.fullmatch(path.name or ""):
        raise IssueRecoveryError(f"refused record name: {path.name!r}",
                                 status=500, code="bad-record")
    _checked_root(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise IssueRecoveryError("review already recorded for this input",
                                 status=409, code="duplicate")
    except OSError as exc:
        raise IssueRecoveryError(f"unwritable review store: {exc}",
                                 status=502, code="unreadable") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
    except OSError as exc:
        raise IssueRecoveryError(f"unwritable review store: {exc}",
                                 status=502, code="unreadable") from exc


def _str_list(report, key: str) -> list:
    val = report.get(key)
    if not isinstance(val, list):
        raise IssueRecoveryError(f"report field must be a list: {key}",
                                 status=502, code="invalid-report")
    for item in val:
        if not isinstance(item, str):
            raise IssueRecoveryError(
                f"report field {key} must contain only strings",
                status=502, code="invalid-report")
    return val


_RUNNER_EXACT_RE = re.compile(
    r"^(npm run|npm run-script|yarn run)"
    r" ([A-Za-z0-9_:@./][A-Za-z0-9_:@./-]{0,127})$")
_REF_SHA_RE = re.compile(r"^[0-9a-f]{7,128}$")


def _validate_contract_declaration(contract: dict, blk) -> None:
    """Sound declaration-link validation for a non-null build contract.

    Only meaningful on evidence-bearing inputs (blk is the stored
    snapshot's proven build_evidence block; None means a legacy
    input the caller must have handled before calling). A non-null
    contract must carry a bounded nonempty string command AND a
    valid declaration link: empty objects, null/empty/wrong-type
    commands, and builder/outputs objects without a command are all
    refused as invalid-report — never silently accepted as proven
    new contracts. An absent/null build_contract stays the honest
    "no contract" form. The accepted command is still an UNEXECUTED
    MODEL PROPOSAL — the trace proves the text was declared at the
    pinned source, nothing about builds, outputs, or execution
    permission.
    """
    decl = contract.get("declaration")
    cmd = contract.get("command")
    if not isinstance(cmd, str) or not cmd.strip() \
            or len(cmd) > _BUILD_CMD_CAP:
        # No command, an empty/blank command, a wrong-type command,
        # or an over-bound command never passes as valid new proof —
        # even alongside a declaration or builder/outputs objects.
        raise IssueRecoveryError(
            "build contract command malformed",
            status=502, code="invalid-report")
    if "builder" in contract and (
            not isinstance(contract["builder"], str)
            or not contract["builder"].strip()):
        raise IssueRecoveryError(
            "build contract builder malformed",
            status=502, code="invalid-report")
    if "outputs" in contract:
        outputs = contract["outputs"]
        if not isinstance(outputs, list) or not all(
                isinstance(o, str) and o.strip() for o in outputs):
            raise IssueRecoveryError(
                "build contract outputs malformed",
                status=502, code="invalid-report")
    if decl is None:
        raise IssueRecoveryError(
            "build contract command without a declaration link",
            status=502, code="invalid-report")
    entry, _manifest, _kind, _ref, script, _cwd = \
        _validate_declaration_shape(contract, decl, blk)
    raw_cmd = entry["scripts"][script]
    if cmd == raw_cmd:
        # Exact declared raw script text: authoritative regardless
        # of runner, since the declaration pins the source.
        return None
    # Only unambiguous, option-free runner invocations. Bare
    # `npm <script>` does NOT run scripts; `npm test`-style native
    # aliases, bare `yarn <script>`, flags, extra args, npx/pnpm,
    # cd chains, and shell operators are all refused rather than
    # assumed. A selector beginning with `-` is an option/help flag
    # to the runner, never a script invocation — even when a script
    # of that name is transmitted. cwd comes from the declared
    # manifest path only.
    head = _RUNNER_EXACT_RE.fullmatch(cmd)
    if head is None:
        raise IssueRecoveryError(
            "build contract command is not a declared invocation",
            status=502, code="invalid-report")
    if head.group(2) != script:
        raise IssueRecoveryError(
            "build contract command names an undeclared script",
            status=502, code="invalid-report")
    return None


def _validate_declaration_shape(contract: dict, decl, blk):
    """Validate a present declaration link; return its bound source.

    Returns (entry, manifest, ref_kind, ref, script, cwd). Every
    shape, provenance, byte-identity, and directory rule is
    enforced; anything else raises invalid-report. Never blesses
    silently and never repairs.
    """
    if not isinstance(decl, dict):
        raise IssueRecoveryError("build contract declaration malformed",
                                 status=502, code="invalid-report")
    manifest = decl.get("manifest")
    kind = decl.get("ref_kind")
    ref = decl.get("ref")
    script = decl.get("script")
    cwd = decl.get("cwd")
    draw = decl.get("raw_sha256")
    if manifest not in _BUILD_PATHS \
            or kind not in ("origin", "candidate", "base"):
        raise IssueRecoveryError("build contract declaration malformed",
                                 status=502, code="invalid-report")
    if not isinstance(ref, str) or not _REF_SHA_RE.fullmatch(ref):
        raise IssueRecoveryError("build contract declaration malformed",
                                 status=502, code="invalid-report")
    if not isinstance(script, str) or not script \
            or len(script) > _BUILD_NAME_CAP:
        raise IssueRecoveryError("build contract declaration malformed",
                                 status=502, code="invalid-report")
    # The working directory derives from the allowlisted manifest
    # location only — never inferred from a script name that may
    # exist in both manifests or refs.
    if cwd != _BUILD_CWD[manifest]:
        raise IssueRecoveryError(
            "build contract declaration cwd mismatched",
            status=502, code="invalid-report")
    if not isinstance(draw, str) or not _SHA256_RE.fullmatch(draw):
        raise IssueRecoveryError("build contract declaration malformed",
                                 status=502, code="invalid-report")
    if not isinstance(blk, dict):
        raise IssueRecoveryError(
            "build contract declaration source not transmitted",
            status=502, code="invalid-report")
    matches = [e for e in blk.get("files") or []
               if isinstance(e, dict)
               and e.get("path") == manifest
               and e.get("ref_kind") == kind
               and e.get("ref") == ref]
    if len(matches) != 1:
        raise IssueRecoveryError(
            "build contract declaration source not transmitted",
            status=502, code="invalid-report")
    entry = matches[0]
    if not isinstance(entry.get("scripts"), dict) \
            or draw != entry.get("raw_sha256"):
        raise IssueRecoveryError(
            "build contract declaration bytes differ from source",
            status=502, code="invalid-report")
    if script not in entry["scripts"]:
        raise IssueRecoveryError(
            "declared script not in transmitted manifest",
            status=502, code="invalid-report")
    return entry, manifest, kind, ref, script, _BUILD_CWD[manifest]


def validate_report(report, sent_files: list, omitted_files: list | None = None, build_evidence=None) -> dict:
    """Strict report contract against ACTUALLY SENT ranges only.

    Conflict files may name sent or omitted (absent/generated with
    recorded evidence) paths but never unlisted ones; citations must
    match an actually sent (path, ref) window and stay within its
    line count, with any quote present verbatim in the sent text.
    Affirmative outcomes require grounded evidence; generated
    content that was omitted from the model input can never make a
    review ready through an empty proposal or a fake build contract.
    """
    if not isinstance(report, dict):
        raise IssueRecoveryError("model report is not an object",
                                 status=502, code="invalid-report")
    status = report.get("status")
    if status not in ("needs_changes", "ready_for_derived", "undecided"):
        raise IssueRecoveryError("report status is not a known outcome",
                                 status=502, code="invalid-report")
    for key in ("summary", "cause", "next_action"):
        val = report.get(key)
        if not isinstance(val, str) or not val.strip():
            raise IssueRecoveryError(f"report field missing: {key}",
                                     status=502, code="invalid-report")
    windows = [f for f in sent_files if isinstance(f, dict)]
    sent_paths = {f.get("path") for f in windows if f.get("path")}
    by_key = {(f.get("path"), f.get("ref")): f for f in windows
              if f.get("path") and f.get("ref")}
    omitted_paths = {f["path"] for f in (omitted_files or [])
                     if isinstance(f, dict) and f.get("path")}
    conflict_files = _str_list(report, "conflict_files")
    _str_list(report, "intended_changes")
    _str_list(report, "questions")
    if "uncertainties" in report:
        _str_list(report, "uncertainties")
    _str_list(report, "limits")
    citations = report.get("citations") or []
    if not isinstance(citations, list):
        raise IssueRecoveryError("citations must be a list",
                                 status=502, code="invalid-report")
    for path in conflict_files:
        if path not in sent_paths and path not in omitted_paths:
            raise IssueRecoveryError(
                f"report cites unsent file: {path!r}",
                status=502, code="invalid-report")
    for cite in citations:
        if not isinstance(cite, dict):
            raise IssueRecoveryError("report cites unsent code range",
                                     status=502, code="invalid-report")
        win = by_key.get((cite.get("path"), cite.get("ref")))
        if win is None:
            raise IssueRecoveryError(
                f"report cites unsent path+ref: "
                f"{cite.get('path')!r}@{str(cite.get('ref') or '')[:12]}",
                status=502, code="invalid-report")
        text = str(win.get("text") or "")
        line_count = int(win.get("line_count") or 0)
        start = cite.get("start_line", 1)
        end = cite.get("end_line", start)
        if isinstance(start, bool) or isinstance(end, bool):
            raise IssueRecoveryError("citation lines must be integers",
                                     status=502, code="invalid-report")
        if not isinstance(start, int) or not isinstance(end, int):
            raise IssueRecoveryError("citation lines must be integers",
                                     status=502, code="invalid-report")
        if start < 1 or end < start or end > line_count:
            raise IssueRecoveryError(
                f"citation range outside sent lines 1..{line_count}",
                status=502, code="invalid-report")
        quote = cite.get("quote")
        if quote is not None:
            if not isinstance(quote, str) or not quote.strip():
                raise IssueRecoveryError("citation quote must be non-empty",
                                         status=502, code="invalid-report")
            # The quote must appear within the declared line window,
            # not merely anywhere in the sent file.
            window = "\n".join(text.split("\n")[start - 1:end])
            if quote not in window:
                raise IssueRecoveryError(
                    "citation quote not found in declared sent lines",
                    status=502, code="invalid-report")
    if status in ("needs_changes", "ready_for_derived") and not citations \
            and not conflict_files:
        raise IssueRecoveryError(
            "affirmative findings need grounded evidence",
            status=502, code="invalid-report")
    proposed = _str_list(report, "proposed_paths")
    for path in proposed:
        if path not in sent_paths and path not in omitted_paths:
            raise IssueRecoveryError(
                f"proposed path was never sent: {path!r}",
                status=502, code="invalid-report")
    omitted_proposed = [p for p in proposed if p in omitted_paths
                        and p not in sent_paths]
    contract = report.get("build_contract")
    if status == "ready_for_derived":
        if not proposed:
            raise IssueRecoveryError(
                "ready reviews need non-empty proposed_paths",
                status=502, code="invalid-report")
        if omitted_proposed:
            if not isinstance(contract, dict):
                raise IssueRecoveryError(
                    "generated assets need an explicit build contract",
                    status=502, code="invalid-report")
            for key in ("builder", "command"):
                if not isinstance(contract.get(key), str) \
                        or not contract[key].strip():
                    raise IssueRecoveryError(
                        f"build_contract.{key} must be non-empty",
                        status=502, code="invalid-report")
            outputs = contract.get("outputs")
            if not isinstance(outputs, list) or not outputs \
                    or not all(isinstance(o, str) and o.strip()
                               for o in outputs):
                raise IssueRecoveryError(
                    "build_contract.outputs must be a non-empty string list",
                    status=502, code="invalid-report")
            missing = [p for p in omitted_proposed if p not in outputs]
            if missing:
                raise IssueRecoveryError(
                    f"build contract does not cover: {missing!r}",
                    status=502, code="invalid-report")
    elif contract is not None and not isinstance(contract, dict):
        raise IssueRecoveryError("build_contract must be an object or null",
                                 status=502, code="invalid-report")
    # Structured declaration link (evidence-bearing inputs only):
    # a non-null contract must carry a bounded nonempty command and
    # declare the exact transmitted manifest/ref/script it was read
    # from; an empty object or a commandless/builder-only object is
    # refused, never silently accepted as new proof. An absent/null
    # contract stays the honest "no contract" form. Legacy inputs
    # without build evidence keep the exact prior validator.
    # Acceptance is NOT execution proof — the command stays an
    # unexecuted model proposal.
    if build_evidence is not None and isinstance(contract, dict):
        if not contract:
            raise IssueRecoveryError(
                "build contract must be a non-empty object or null",
                status=502, code="invalid-report")
        _validate_contract_declaration(contract, build_evidence)
    return report


def _scrubbed_env() -> dict:
    from . import discussions as _d
    return _d._scrubbed_env()


def _model_flags() -> list:
    from . import discussions as _d
    return list(_d._REVIEW_MODEL_FLAGS)


def _model_timeout() -> int:
    from . import discussions as _d
    return int(_d._REVIEW_TIMEOUT_S)


_MAX_ATTEMPTS = 5
# Bounded failure diagnostics (units are characters unless a field
# name ends in _bytes): the stored raw stream keeps a bounded
# character prefix as a prior diagnostic and may contain opaque SDK
# metadata; it is not claimed to be visible output. Only the ADDED
# model_text field is the extracted final visible output, kept
# separately with explicit TOTAL vs SAVED character and UTF-8 byte
# counts plus caps/truncation. The SDK stream can be far longer than
# the stored raw prefix, so the final response must still survive
# for failure diagnosis. Hidden reasoning is never decrypted or
# stored: model_text holds only the extractor's final visible text,
# bounded by _MODEL_TEXT_CAP characters.
_RAW_STREAM_CAP = 200000
_MODEL_TEXT_CAP = 20000


def _record_path(root: Path, name: str) -> Path:
    """Join a controlled record name, rejecting traversal/symlinks.

    Only the exact generated artifact names are addressable; a stored
    pointer naming anything else is corruption, and a symlink where a
    record belongs is refused rather than followed.
    """
    if not _RECORD_RE.fullmatch(name or ""):
        raise IssueRecoveryError(f"refused record name: {name!r}",
                                 status=502, code="corrupt")
    _checked_root(root)
    path = root / name
    try:
        if os.path.islink(path):
            raise IssueRecoveryError(
                f"refused symlinked record: {name}",
                status=502, code="corrupt")
        st = os.lstat(path)
    except FileNotFoundError:
        return path
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable record {name}: {exc}",
                                 status=502, code="unreadable") from exc
    if not stat.S_ISREG(st.st_mode):
        raise IssueRecoveryError(f"refused non-regular record: {name}",
                                 status=502, code="corrupt")
    return path


def _attempt_filename(rid: str) -> str:
    """Controlled attempt filename for a caller-supplied attempt id."""
    name = f"attempt-{rid}.json"
    if not _RECORD_RE.fullmatch(name):
        raise IssueRecoveryError(f"bad attempt id: {rid!r}",
                                 status=400, code="bad-rid")
    return name


def _read_record(root: Path, name: str):
    """Read a stored record, fail-closed on corruption.

    Returns None only when the record was never attempted. Present
    but unreadable/corrupt records raise a typed storage error and
    the bytes are preserved — never treated as empty.
    """
    path = _record_path(root, name)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable record {name}: {exc}",
                                 status=502, code="unreadable") from exc
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IssueRecoveryError(f"corrupt record {name}: {exc}",
                                 status=502, code="corrupt") from exc
    if not isinstance(doc, dict):
        raise IssueRecoveryError(f"corrupt record {name}: not an object",
                                 status=502, code="corrupt")
    return doc


def _live_job(job_id) -> bool:
    """True only when the dashboard process still runs that job."""
    if not job_id:
        return False
    try:
        from . import serve as _serve
        job = _serve._JOBS.get(job_id)
    except Exception:  # noqa: BLE001 - unknown means not live
        return False
    return isinstance(job, dict) and job.get("status") == "running"


def _attempt_rid(fp: str, n: int) -> str:
    base = f"rr-{fp[:12]}"
    return base if n == 0 else f"{base}-r{n + 1}"


def _attempt_names(root: Path) -> list:
    """Sorted attempt record names present under ``root``.

    A missing store directory is not an error: there is simply no
    stored attempt yet. Any other store read failure is an explicit
    storage error — an unreadable store never masquerades as empty.
    """
    try:
        names = sorted(p.name for p in root.iterdir()
                       if p.name.startswith("attempt-rr-")
                       and p.name.endswith(".json"))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable attempt store: {exc}",
                                 status=502, code="unreadable") from exc
    return names


def _load_request_context(op_id: str, root, context_id: str) -> dict:
    """Load and prove a supplemental-context record for a review.

    Shape, kind, schema, op, version, and digest are all rechecked
    here: a well-formed name alone proves nothing. The digest is
    recomputed from the record's own bytes.
    """
    if not isinstance(context_id, str) or not context_id \
            or not _RECORD_RE.fullmatch(f"{context_id}.json") \
            or not context_id.startswith("context-"):
        raise IssueRecoveryError("review context id malformed",
                                 status=400, code="bad-context")
    rec = _read_record(root, f"{context_id}.json")
    if rec is None:
        raise IssueRecoveryError("review context is unknown",
                                 status=404, code="unknown-context")
    _assert_context_self_consistent(rec)
    if rec.get("id") != context_id:
        raise IssueRecoveryError("review context id does not match "
                                 "its record",
                                 status=502, code="corrupt")
    if rec.get("op") != op_id:
        raise IssueRecoveryError("review context belongs to another op",
                                 status=502, code="corrupt")
    return rec


def _find_done_attempt(root, op_id: str, fp: str, input_id: str,
                       stored: dict):
    """Find an already-completed attempt for this exact input.

    Returns (rid, validated_report, report_id) or None. Validation of
    the candidate chain reuses the same fail-closed chain the normal
    reuse path uses; a non-matching or invalid candidate is skipped
    only when it cannot belong to this input — corrupt links raise.
    """
    for name in _attempt_names(root):
        cand_att = _read_record(root, name)
        if not isinstance(cand_att, dict):
            continue
        if cand_att.get("status") != "done":
            continue
        if cand_att.get("fingerprint") != fp:
            continue
        if cand_att.get("input") != input_id:
            continue
        if not cand_att.get("report"):
            continue
        rep = _validate_report_chain(root, op_id, cand_att, stored)
        return cand_att.get("rid"), rep, cand_att.get("report")
    return None


def request_review(op_id: str, by: str = "owner",
                   context_id: str | None = None) -> dict:
    """Validate and persist an immutable recovery-review input.

    CALLER MUST HOLD the global worker lease and the same-op
    exclusion; this function writes nothing before that. It never
    writes a running/orphan record for busy/job-slot failures —
    those are refused before any mutation.

    An identical fingerprint with a validated completed report is
    reused without spending model quota. A live running attempt is
    attached, never duplicated. A dead running attempt is marked
    interrupted (preserved) and a bounded explicit retry rid is
    issued; exhausted retries refuse with 409.

    ``context_id`` names a supplemental operator-context record
    (see record_recovery_context). The context text is rendered into
    the prompt as a labeled operator section and bound into the
    fingerprint, so same source plus new context always yields a
    different input. The origin triple must equal the CURRENT latest
    chain and the live binding must still match it — otherwise 409,
    never a silent rebind of an old draft onto new sources.
    """
    if by not in ("owner", "root"):
        raise IssueRecoveryError("by must be owner or root",
                                 status=400, code="bad-by")
    if context_id is not None \
            and (not isinstance(context_id, str) or not context_id):
        raise IssueRecoveryError("review context id malformed",
                                 status=400, code="bad-context")
    ctx = load_blocked_op(op_id)
    collected = collect_windows(ctx)
    context = None
    context_digest = ""
    context_relaxed = False
    if context_id is not None:
        root = rec_root(op_id)
        rec = _load_request_context(op_id, root, context_id)
        # The origin is re-proved live from its own bytes every time:
        # exact record hashes, exact question texts, proven input
        # identity, and a still-current live binding. A resubmission
        # whose chain has since advanced resolves to its legitimate
        # already-created continuation below; anything changed is
        # stale or corrupt, never rebound.
        _prove_origin_current(op_id, ctx, root, rec)
        latest_now = _read_record(root, "latest.json")
        if not isinstance(latest_now, dict) or not latest_now:
            raise IssueRecoveryError("no review chain to attach context to",
                                     status=409,
                                     code="stale-context-origin")
        context_relaxed = (
            latest_now.get("input") != rec["origin"].get("input")
            or latest_now.get("report") != rec["origin"].get("report")
            or latest_now.get("fingerprint")
            != rec["origin"].get("fingerprint"))
        context_digest = rec["digest"]
        context = {"id": context_id, "text": rec["text"],
                   "question_refs": rec.get("question_refs") or [],
                   "origin": rec["origin"],
                   "questions": rec["origin"].get("questions") or []}
    built = build_prompt(ctx, collected, context=context)
    sent = [{"path": f["path"], "ref_kind": f["ref_kind"],
               "ref": f["ref"], "sent_sha256": f["sent_sha256"],
               "sent_bytes": f["sent_bytes"],
               "line_count": f["line_count"]}
              for f in built["snapshot"]["files"]]
    scope = sorted({f["path"] for f in built["snapshot"]["files"]}
                   | {o["path"] for o in built["snapshot"]["omitted"]
                      if isinstance(o, dict) and o.get("path")})
    prompt_sha = hashlib.sha256(
        built["prompt"].encode("utf-8")).hexdigest()
    refs = built["snapshot"].get("refs")
    if not isinstance(refs, dict) or not refs:
        raise IssueRecoveryError("collected snapshot refs malformed",
                                 status=502, code="corrupt")
    meta = {"files": [{k: v for k, v in f.items() if k != "text"}
                      for f in built["snapshot"]["files"]],
            "omitted": built["snapshot"]["omitted"],
            "refs": dict(sorted(refs.items()))}
    if "build_evidence" in built["snapshot"]:
        # Same shared projection the replay side computes: writer
        # and stored replay must agree byte-for-byte.
        meta["build_evidence"] = _project_build_meta(
            built["snapshot"]["build_evidence"])
    original = {"op_error": (ctx["op"].get("error") or "")[:4000],
                "conflicted_paths": sorted(
                    ctx["result"].get("conflicted_paths") or []),
                "partial": list(built["partial"])}
    fp = review_fingerprint(ctx, sent, scope, prompt_sha, meta,
                            original, context_digest=context_digest)
    root = rec_root(op_id)
    input_id = f"input-{fp[:16]}"
    stored = _read_record(root, f"{input_id}.json")
    if stored is None and context_digest and context_relaxed:
        # An orphan recorded context consumed for the FIRST time after
        # latest moved elsewhere: refuse before ANY new input/attempt
        # write. The relaxed path exists only for an already-created
        # legitimate matching continuation/retry — never to fork an
        # old draft into a new analysis on a moved-on chain.
        raise IssueRecoveryError("stale unconsumed context: latest moved "
                                 "on; re-read and answer the current "
                                 "report",
                                 status=409,
                                 code="stale-context-origin")
    if stored is None:
        stored = {
            "schema": 1, "kind": "recovery-input", "op": op_id,
            "by": by, "fingerprint": fp, "contract": _CONTRACT_VERSION,
            "parent_sha": ctx["raw_sha"],
            "run_sha": ctx["ctx"].get("run_sha"),
            "issue": ctx["issue"], "candidate": ctx["candidate"],
            "base": ctx["base"], "base_commit": ctx["base_commit"],
            "run": ctx["run"], "origin_url": ctx["ctx"].get("url"),
            "conflicted_paths": sorted(
                ctx["result"].get("conflicted_paths") or []),
            # The failure text the review investigates: historical
            # display must quote the ORIGINAL error, never a mutated
            # live record's text.
            "op_error": (ctx["op"].get("error") or "")[:4000],
            "snapshot": built["snapshot"], "partial": built["partial"],
        }
        # A context-linked input echoes its exact origin triple plus
        # the bound digest: replay rechecks all three against the
        # record bytes (§_stored_context_digest). Context-free inputs
        # carry no key at all, so their fingerprint path is untouched.
        if context is not None:
            stored["context"] = {
                "version": _CONTEXT_VERSION,
                "id": context["id"], "digest": context_digest,
                "origin": context["origin"],
            }
        _write_new(root / f"{input_id}.json", stored)
    elif stored.get("fingerprint") != fp \
            or stored.get("parent_sha") != ctx["raw_sha"]:
        raise IssueRecoveryError("stored input does not match request",
                                 status=502, code="corrupt")
    # Prove the full original linkage of the input this request will
    # reuse — shape, ownership, and its own fingerprint recomputed
    # from stored bytes alone — before any reuse or new attempt.
    _validate_stored_identity(op_id, stored)
    latest = _read_record(root, "latest.json")
    if latest is not None:
        if latest.get("status") == "done" and latest.get("report"):
            # Validate the OLD completed chain against its OWN stored
            # original input first. A different VALID input (explicit
            # new review after harmless parent drift) must create a
            # bounded new attempt — it is not corruption of the old
            # chain. Only reuse when genuinely identical/current.
            old_name = latest.get("input")
            old_stored = _read_record(root, f"{old_name}.json") \
                if isinstance(old_name, str) and old_name else None
            if old_stored is None:
                raise IssueRecoveryError(
                    "latest input link is broken",
                    status=502, code="corrupt")
            # The OLD chain is validated against its OWN stored
            # original input first: recomputed fingerprint, refs map,
            # original attribution, and full ownership. A corrupt old
            # chain raises here; a valid-but-different one falls
            # through to a bounded new attempt below.
            _validate_stored_identity(op_id, old_stored)
            old_fp = old_stored.get("fingerprint")
            report = _validate_report_chain(root, op_id, latest,
                                            old_stored)
            if old_fp == fp and old_name == input_id:
                return {"input": input_id, "rid": latest.get("rid"),
                        "reused": True, "attached": False,
                        "fingerprint": fp, "report": report}
            if context_digest:
                # A context-linked resubmission whose review already
                # completed while latest moved on resolves to that
                # legitimate already-created result — returned WITHOUT
                # re-anchoring latest, which keeps pointing at the
                # newest completed chain. No duplicate attempt.
                found = _find_done_attempt(root, op_id, fp, input_id,
                                           stored)
                if found is not None:
                    found_rid, found_rep, _found_report_id = found
                    return {"input": input_id, "rid": found_rid,
                            "reused": True, "attached": False,
                            "fingerprint": fp, "report": found_rep}
            # Old review stays preserved; fall through to create the
            # new input/attempt for this explicit request.
    else:
        found = _find_done_attempt(root, op_id, fp, input_id, stored)
        if found is not None:
            found_rid, found_rep, found_report_id = found
            _write_latest(root, {
                "input": input_id, "rid": found_rid,
                "fingerprint": fp, "status": "done",
                "report": found_report_id, "error": ""})
            return {"input": input_id, "rid": found_rid,
                    "reused": True, "attached": False,
                    "fingerprint": fp, "report": found_rep}
    for n in range(_MAX_ATTEMPTS + 1):
        rid = _attempt_rid(fp, n)
        att = _read_record(root, f"attempt-{rid}.json")
        if att is None:
            _write_new(root / f"attempt-{rid}.json", {
                "schema": 1, "kind": "recovery-attempt", "rid": rid,
                "op": op_id, "input": input_id, "fingerprint": fp,
                "status": "queued", "by": by, "job_id": None,
            })
            _write_latest(root, {
                "input": input_id, "rid": rid, "fingerprint": fp,
                "status": "queued", "report": None, "job_id": None,
                "error": ""})
            return {"input": input_id, "rid": rid, "reused": False,
                    "attached": False, "fingerprint": fp}
        if att.get("fingerprint") != fp or att.get("input") != input_id:
            raise IssueRecoveryError(
                f"stored attempt {rid} does not match input",
                status=502, code="corrupt")
        # Ownership before any attach/reuse: a running record whose
        # op/kind/schema link was swapped cannot be attached to this
        # request, even when its fingerprint still matches.
        if att.get("op") != op_id or att.get("kind") != "recovery-attempt" \
                or att.get("schema") != 1 or att.get("rid") != rid:
            raise IssueRecoveryError(
                f"stored attempt {rid} does not match input",
                status=502, code="corrupt")
        status = att.get("status")
        if status in ("done", "error", "stale", "cancelled",
                      "interrupted"):
            continue
        if status == "running" and _live_job(att.get("job_id")):
            _write_latest(root, {
                "input": input_id, "rid": rid, "fingerprint": fp,
                "status": "running", "report": None,
                "job_id": att.get("job_id"), "error": ""})
            return {"input": input_id, "rid": rid, "reused": True,
                    "attached": True, "fingerprint": fp,
                    "job_id": att.get("job_id")}
        if status in ("running", "queued"):
            att = dict(att)
            att["status"] = "interrupted"
            att["error"] = "worker did not complete; preserved for retry"
            _replace_record(root, f"attempt-{rid}.json", att)
            continue
        raise IssueRecoveryError(f"unknown attempt status: {status!r}",
                                 status=502, code="corrupt")
    raise IssueRecoveryError(
        "bounded retries exhausted; inspect failed attempts first",
        status=409, code="retries-exhausted")


def _replace_record(root: Path, name: str, payload: dict) -> None:
    # Validate before touching the filesystem: no traversal, foreign
    # name, or symlink alias may be created or replaced.
    _record_path(root, name)
    dest = root / name
    tmp = root / f"{name}.tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        if os.path.islink(tmp) or os.path.islink(dest):
            raise IssueRecoveryError(
                f"refused symlinked record: {name}",
                status=502, code="corrupt")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(str(tmp), str(dest))
    except OSError as exc:
        raise IssueRecoveryError(f"unwritable review store: {exc}",
                                 status=502, code="unreadable") from exc


def mark_startup_failed(op_id: str, rid: str, reason: str) -> None:
    """Truthfully close a queued attempt that never executed.

    Used when the dashboard job slot (or op lock) is unavailable
    after the immutable input was recorded: the queued attempt
    becomes a preserved terminal error, never a phantom running
    record, and the lease is released by the caller.
    """
    if not _OP_RE.fullmatch(rid or ""):
        raise IssueRecoveryError(f"bad attempt id: {rid!r}",
                                 status=400, code="bad-rid")
    fname = _attempt_filename(rid)
    root = rec_root(op_id)
    att = _read_record(root, fname)
    if att is None:
        raise IssueRecoveryError("unknown attempt",
                                 status=404, code="unknown-attempt")
    if att.get("status") != "queued":
        raise IssueRecoveryError("only a queued attempt can fail startup",
                                 status=409, code="not-queued")
    att = dict(att)
    att["status"] = "error"
    att["error"] = str(reason or "startup failed")[:500]
    _replace_record(root, fname, att)
    # The queued latest pointer must follow its attempt: otherwise the
    # UI would show a queued review forever while its only attempt is
    # a preserved terminal error.
    latest = _read_record(root, "latest.json")
    if isinstance(latest, dict) and latest.get("rid") == rid \
            and latest.get("status") == "queued":
        synced = dict(latest)
        synced["status"] = "error"
        synced["report"] = None
        synced["error"] = str(reason or "startup failed")[:500]
        _replace_record(root, "latest.json", synced)


def _write_latest(root: Path, payload: dict) -> None:
    _replace_record(root, "latest.json", payload)


def execute_review(op_id: str, rid: str, job_id=None,
                   cancel_event=None) -> int:
    """Run the read-only model review and persist the validated report.

    CALLER MUST HOLD the same exclusion used by request_review.
    Revalidates the stored input against the CURRENT parent binding
    both before the model runs and immediately before committing
    success; any drift or unreadable source marks the attempt stale
    or error and preserves every artifact. Report extraction uses
    the real Muse JSONL contract (terminal/delta) shared with the
    held Discussion path — never a bespoke envelope.
    """
    from . import muse as _muse
    from . import discussions as _d
    from .util import Cancelled as _util_Cancelled
    if not _OP_RE.fullmatch(rid or ""):
        raise IssueRecoveryError(f"bad attempt id: {rid!r}",
                                 status=400, code="bad-rid")
    fname = _attempt_filename(rid)
    root = rec_root(op_id)
    attempt = _read_record(root, fname)
    if attempt is None:
        raise IssueRecoveryError("unknown attempt",
                                 status=404, code="unknown-attempt")
    if attempt.get("status") not in ("queued", "running"):
        raise IssueRecoveryError("attempt is not runnable",
                                 status=409, code="not-running")
    stored_input = _read_record(root, f"{attempt.get('input')}.json")
    if stored_input is None:
        raise IssueRecoveryError("attempt lost its input",
                                 status=502, code="corrupt")
    fp = str(attempt.get("fingerprint") or "")
    if stored_input.get("fingerprint") != fp \
            or stored_input.get("parent_sha") is None:
        raise IssueRecoveryError("attempt input linkage broken",
                                 status=502, code="corrupt")
    # Shared ownership BEFORE the quota boundary: an attempt whose
    # stored schema/kind/op/rid/input link is invalid must be refused
    # without invoking the model transport at all.
    if attempt.get("op") != op_id or attempt.get("rid") != rid \
            or attempt.get("kind") != "recovery-attempt" \
            or attempt.get("schema") != 1 \
            or attempt.get("input") != f"input-{fp[:16]}":
        raise IssueRecoveryError("attempt input linkage broken",
                                 status=502, code="corrupt")
    _validate_stored_identity(op_id, stored_input)
    try:
        current = load_blocked_op(op_id)
    except IssueRecoveryError as exc:
        _finish_attempt(root, rid, "stale", str(exc), attempt,
                        stored_input)
        return 1
    if current["raw_sha"] != stored_input.get("parent_sha"):
        _finish_attempt(root, rid, "stale",
                        "원본 준비 기록이 바뀌어 이 입력으로는 판단하지 않음",
                        attempt, stored_input)
        return 1
    try:
        now_fp = _current_fp(current, stored_input)
    except IssueRecoveryError as exc:
        _finish_attempt(root, rid, "error",
                        f"저장된 입력 스냅샷 검증 실패: {exc}",
                        attempt, stored_input)
        return 1
    if now_fp != fp:
        _finish_attempt(root, rid, "stale",
                        "현재 원본/기준이 저장된 입력과 달라 판단하지 않음",
                        attempt, stored_input)
        return 1
    snap = stored_input.get("snapshot")
    if not isinstance(snap, dict) \
            or not isinstance(snap.get("sent_text"), str):
        _finish_attempt(root, rid, "error",
                        "저장된 입력에 전송 문구가 없음",
                        attempt, stored_input)
        return 1
    if cancel_event is not None and cancel_event.is_set():
        # Already cancelled: spend no model quota and record a
        # truthful cancelled attempt, matching the _job_run contract.
        _finish_attempt(root, rid, "cancelled",
                        "operator cancelled before the review started; "
                        "no report recorded",
                        attempt, stored_input, usage=None, raw_stream="")
        return 130
    prompt = snap["sent_text"]
    env = _scrubbed_env()
    attempt = dict(attempt)
    attempt["status"] = "running"
    attempt["job_id"] = job_id
    _replace_record(root, f"attempt-{rid}.json", attempt)
    # The running worker must be visible through latest as soon as it
    # starts: a duplicate POST/GET attaches to this same job identity
    # instead of starting quota-spending work or seeing a stale queue.
    _write_latest(root, {
        "input": attempt["input"], "rid": rid, "fingerprint": fp,
        "status": "running", "report": None, "job_id": job_id, "error": ""})
    try:
        exit_code, raw_stream, usage = _muse.exec_prompt(
            prompt, workdir=None, contributor=True,
            timeout=_model_timeout(), env=env,
            extra=_model_flags(), cancel_event=cancel_event)
    except _util_Cancelled:
        _finish_attempt(root, rid, "cancelled",
                        "operator cancelled the review; no report recorded",
                        attempt, stored_input, usage=None, raw_stream="")
        return 130
    except Exception as exc:  # noqa: BLE001 - record, never raise
        raw = f"review execution failed: {exc}"
        _finish_attempt(root, rid, "error", raw, attempt,
                        stored_input, usage=None, raw_stream="")
        return 1
    if cancel_event is not None and cancel_event.is_set():
        _finish_attempt(root, rid, "cancelled",
                        "operator cancelled the review; no report recorded",
                        attempt, stored_input, usage=None,
                        raw_stream=raw_stream)
        return 130
    if exit_code != 0:
        kind, reason = _d._classify_model_failure(exit_code, raw_stream)
        _finish_attempt(root, rid, "error", reason, attempt,
                        stored_input,
                        usage={"exit_code": exit_code, "usage": usage},
                        error_kind=kind, raw_stream=raw_stream)
        return 1
    model_text, _stream_kind = _d._extract_model_text(raw_stream)
    if not (model_text or "").strip():
        _finish_attempt(root, rid, "error",
                        "model produced no output text; no review recorded",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    parsed, parse_error = _d._extract_report_json(model_text)
    if parsed is None:
        _finish_attempt(root, rid, "error",
                        f"unparseable model output: {parse_error}",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    try:
        validate_report(parsed, stored_input["snapshot"]["files"],
                        stored_input["snapshot"].get("omitted"),
                        stored_input["snapshot"].get("build_evidence"))
    except IssueRecoveryError as exc:
        _finish_attempt(root, rid, "error", str(exc), attempt,
                        stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    try:
        after = load_blocked_op(op_id)
    except IssueRecoveryError as exc:
        _finish_attempt(root, rid, "stale", f"모델 실행 후 재검증 실패: {exc}",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    if after["raw_sha"] != stored_input.get("parent_sha"):
        _finish_attempt(root, rid, "stale",
                        "모델 실행 중 원본 준비 기록이 바뀜 — 보고서를 버림",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    try:
        after_fp = _current_fp(after, stored_input)
    except IssueRecoveryError as exc:
        _finish_attempt(root, rid, "error",
                        f"모델 실행 후 입력 스냅샷 검증 실패: {exc}",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    if after_fp != fp:
        _finish_attempt(root, rid, "stale",
                        "모델 실행 중 현재 원본/기준이 달라짐 — 보고서를 버림",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    # Corruption during the transport seam must never be persisted:
    # re-read the stored attempt/input from disk and revalidate the
    # full original identity before committing success. An in-memory
    # dict is not proof of current bytes.
    try:
        fresh_attempt = _read_record(root, fname)
        fresh_name = attempt.get("input")
        fresh_input = _read_record(root, f"{fresh_name}.json") \
            if isinstance(fresh_name, str) and fresh_name else None
        if not isinstance(fresh_attempt, dict) \
                or fresh_attempt.get("op") != op_id \
                or fresh_attempt.get("rid") != rid \
                or fresh_attempt.get("kind") != "recovery-attempt" \
                or fresh_attempt.get("schema") != 1 \
                or fresh_attempt.get("input") != attempt.get("input") \
                or fresh_attempt.get("fingerprint") != fp \
                or fresh_attempt.get("status") != "running":
            raise IssueRecoveryError(
                "stored attempt changed during review",
                status=502, code="corrupt")
        _validate_stored_identity(op_id, fresh_input)
        if fresh_input.get("fingerprint") != fp:
            raise IssueRecoveryError(
                "stored input changed during review",
                status=502, code="corrupt")
    except IssueRecoveryError as exc:
        _finish_attempt(root, rid, "error",
                        f"모델 실행 후 저장 기록 재검증 실패: {exc}",
                        attempt, stored_input, usage={
                            "exit_code": exit_code, "usage": usage},
                        raw_stream=raw_stream, model_text=model_text)
        return 1
    report_id = f"report-{fp[:16]}"
    _write_new(root / f"{report_id}.json", {
        "schema": 1, "kind": "recovery-report", "op": op_id,
        "rid": rid, "fingerprint": fp, "report": parsed,
        "input": attempt["input"],
        "usage": {"exit_code": exit_code, "usage": usage},
        "raw_stream": (raw_stream or "")[:_RAW_STREAM_CAP],
    })
    _finish_attempt(root, rid, "done", "", attempt, stored_input,
                    report_id=report_id, usage={
                        "exit_code": exit_code, "usage": usage},
                    raw_stream=raw_stream)
    return 0


def _finish_attempt(root: Path, rid: str, status: str, error: str,
                    attempt: dict, stored_input: dict, report_id=None,
                    usage=None, error_kind=None,
                    raw_stream: str | None = None,
                    model_text: str | None = None) -> None:
    payload = dict(attempt)
    payload["status"] = status
    if error:
        payload["error"] = error[:2000]
    else:
        payload.pop("error", None)
    if report_id:
        payload["report"] = report_id
    if usage is not None:
        payload["usage"] = usage
    if error_kind:
        payload["error_kind"] = error_kind
    if raw_stream:
        saved_raw = raw_stream[:_RAW_STREAM_CAP]
        payload["raw_stream"] = saved_raw
        payload["raw_stream_total_chars"] = len(raw_stream)
        payload["raw_stream_total_bytes"] = len(
            raw_stream.encode("utf-8"))
        payload["raw_stream_saved_chars"] = len(saved_raw)
        payload["raw_stream_saved_bytes"] = len(
            saved_raw.encode("utf-8"))
        payload["raw_stream_cap_chars"] = _RAW_STREAM_CAP
        payload["raw_stream_truncated"] = len(raw_stream) > _RAW_STREAM_CAP
        # Legacy total-only aliases (total counts); the explicit
        # *_total_* fields above are authoritative.
        payload["raw_stream_chars"] = len(raw_stream)
        payload["raw_stream_bytes"] = len(raw_stream.encode("utf-8"))
    # The ADDED model_text field is the extracted final visible
    # response and survives separately: a long SDK prefix can push it
    # past the stored raw-stream prefix cap. The existing raw_stream
    # prefix is a separate prior bounded diagnostic and may contain
    # opaque SDK metadata. Recorded whenever the extractor ran — even
    # empty output stays explicit with zero totals. Paths that never
    # extracted text pass None and record nothing, never invented
    # content.
    if isinstance(model_text, str):
        saved_text = model_text[:_MODEL_TEXT_CAP]
        payload["model_text"] = saved_text
        payload["model_text_total_chars"] = len(model_text)
        payload["model_text_total_bytes"] = len(
            model_text.encode("utf-8"))
        payload["model_text_saved_chars"] = len(saved_text)
        payload["model_text_saved_bytes"] = len(
            saved_text.encode("utf-8"))
        payload["model_text_cap_chars"] = _MODEL_TEXT_CAP
        payload["model_text_truncated"] = len(model_text) > _MODEL_TEXT_CAP
        # Legacy total-only aliases (total counts); the explicit
        # *_total_* fields above are authoritative.
        payload["model_text_chars"] = len(model_text)
        payload["model_text_bytes"] = len(model_text.encode("utf-8"))
    _replace_record(root, f"attempt-{rid}.json", payload)
    latest = {"input": attempt.get("input"), "rid": rid,
              "fingerprint": attempt.get("fingerprint"),
              "status": status, "report": report_id,
              "error": error[:500] if error else ""}
    _replace_record(root, "latest.json", latest)


def _projection_attempts(root: Path, op_id: str,
                         names: list) -> list:
    """Project stored attempt records like the current-status view.

    Only the pointer fields the UI needs — never raw internals —
    with a live running job observed read-only so the view can
    attach to the exact worker while a different busy owner still
    refuses writes. Bounded to the first 20 sorted names; the full
    count travels separately.
    """
    out = []
    for name in names[:20]:
        # Controlled names only: a foreign or renamed record must
        # not render as this op's history.
        if not _RECORD_RE.fullmatch(name):
            raise IssueRecoveryError("stored attempt name is foreign",
                                     status=502, code="corrupt")
        doc = _read_record(root, name)
        if not isinstance(doc, dict):
            raise IssueRecoveryError("stored attempt is not an object",
                                     status=502, code="corrupt")
        rid = name[len("attempt-"):-len(".json")]
        if doc.get("kind") != "recovery-attempt" \
                or doc.get("schema") != 1 \
                or doc.get("op") != op_id \
                or doc.get("rid") != rid:
            raise IssueRecoveryError("stored attempt is foreign",
                                     status=502, code="corrupt")
        out.append({"rid": doc.get("rid"),
                    "status": doc.get("status"),
                    "input": doc.get("input"),
                    "job_id": doc.get("job_id"),
                    "job_live": _live_job(doc.get("job_id"))
                    if doc.get("status") == "running" else None,
                    "error": str(doc.get("error") or "")[:300]})
    return out


def _project_current_context(op_id: str, root: Path,
                             stored: dict):
    """Project the supplemental context ACTUALLY consumed by one input.

    Derived only from the VALIDATED stored input's own ``context``
    block — the exact record id, digest, and origin the input's
    proven fingerprint already binds — never from record existence
    alone. The linked record is re-verified live (self-consistency
    plus bound origin bytes/questions), so a drifted note can never
    render as consumed. Returns None for context-free inputs.
    """
    blk = stored.get("context")
    if blk is None:
        return None
    if not isinstance(blk, dict) or not isinstance(blk.get("id"), str) \
            or not blk.get("id"):
        raise IssueRecoveryError("stored input context block malformed",
                                 status=502, code="corrupt")
    rec = _read_record(root, f"{blk['id']}.json")
    if rec is None or rec.get("digest") != blk.get("digest") \
            or rec.get("origin") != blk.get("origin"):
        raise IssueRecoveryError("stored input context link broken",
                                 status=502, code="corrupt")
    # Already proven by _stored_context_digest during identity
    # validation, re-asserted here so the projected note can only
    # ever describe bytes that still verify live.
    _verify_origin_live(op_id, root, rec)
    return {"id": rec.get("id"), "actor": rec.get("actor"),
            "question_refs": rec.get("question_refs"),
            "text": rec.get("text"), "text_len": rec.get("text_len"),
            "created": rec.get("created"),
            "origin": rec.get("origin"),
            "consumed_by": stored.get("fingerprint")}


def _projection_contexts(root: Path, op_id: str) -> list:
    """Project stored supplemental-context records for status.

    Each entry carries the verified operator note text (bounded at
    write time), its referenced question indices with the exact
    bound origin question texts, and the exact origin triple plus
    raw-byte hashes — so the view can show what was actually
    stored, link each record to its originating chain, and name the
    originating report/input bytes. The note is operator-provided
    investigation context, never an owner-policy answer: the UI
    must keep owner-policy questions marked unconfirmed.
    Bounded to the newest _CONTEXT_LIST_CAP records by creation
    order. Foreign, malformed, or origin-drifted records refuse as
    corrupt — they are never rendered as anyone's context.
    """
    out = []
    for name in _context_names(root):
        if not _RECORD_RE.fullmatch(name) \
                or not name.startswith("context-"):
            raise IssueRecoveryError("stored context name is foreign",
                                     status=502, code="corrupt")
        doc = _read_record(root, name)
        if doc is None:
            raise IssueRecoveryError("stored context is missing",
                                     status=502, code="corrupt")
        cid = name[:-len(".json")]
        if doc.get("id") != cid:
            raise IssueRecoveryError("stored context id does not match "
                                     "its name",
                                     status=502, code="corrupt")
        if doc.get("op") != op_id:
            raise IssueRecoveryError("stored context belongs to "
                                     "another op",
                                     status=502, code="corrupt")
        _verify_origin_live(op_id, root, doc)
        out.append({"id": cid,
                    "actor": doc.get("actor"),
                    "question_refs": doc.get("question_refs"),
                    "text": doc.get("text"),
                    "text_len": doc.get("text_len"),
                    "created": doc.get("created"),
                    "origin": doc.get("origin")})
    out.sort(key=lambda e: str(e.get("created") or ""))
    return out[-_CONTEXT_LIST_CAP:]


def _snapshot_file_view(stored: dict):
    """Bounded file/omitted evidence views from a validated input."""
    input_files: list = []
    input_omitted: list = []
    snap = stored.get("snapshot") or {}
    if isinstance(snap, dict):
        files = snap.get("files")
        if isinstance(files, list):
            input_files = [
                {k: f.get(k) for k in
                 ("path", "ref_kind", "ref", "blob_oid",
                  "sent_sha256", "sent_bytes", "total_bytes",
                  "line_count", "partial", "text")}
                for f in files if isinstance(f, dict)]
        omitted = snap.get("omitted")
        if isinstance(omitted, list):
            input_omitted = [
                {k: o.get(k) for k in
                 ("path", "ref_kind", "ref", "blob_oid",
                  "size", "absent", "reason")}
                for o in omitted if isinstance(o, dict)]
    return input_files, input_omitted


def _snapshot_build_view(stored: dict):
    """Bounded build-evidence view from a validated input.

    Mirrors _snapshot_file_view for the build domain: full entry
    fields including the rendered scripts text and the omission
    reasons. Legacy inputs without the block project (None, None)
    so the UI hides the section instead of inventing absence.
    """
    snap = stored.get("snapshot") or {}
    blk = snap.get("build_evidence") if isinstance(snap, dict) else None
    if not isinstance(blk, dict):
        return None, None
    input_build: list = []
    input_build_omitted: list = []
    files = blk.get("files")
    if isinstance(files, list):
        input_build = [
            {k: f.get(k) for k in
             ("path", "ref_kind", "ref", "blob_oid", "raw_sha256",
              "raw_len", "scripts", "omitted_scripts",
              "scripts_truncated", "sent_sha256", "sent_bytes", "text")}
            for f in files if isinstance(f, dict)]
    omitted = blk.get("omitted")
    if isinstance(omitted, list):
        input_build_omitted = [
            {k: o.get(k) for k in
             ("path", "ref_kind", "ref", "blob_oid", "size", "absent",
              "tree", "reason")}
            for o in omitted if isinstance(o, dict)]
    return input_build, input_build_omitted


def _historical_status(op_id: str, raw_op: dict,
                       exc: IssueRecoveryError) -> dict:
    """Inspectable history when the live binding is stale/unreadable.

    Only live-binding failures (drifted or currently unreadable
    queue/run/proof/base on a structurally valid parent) reach here:
    wrong-kind, not-blocked, bad-identity, missing, and corrupt
    parents are refused before this point and stay errors. The
    stored input/attempt/report chain is validated against its
    ORIGINAL identity — recomputed from stored bytes alone, never
    rebound to the drifted live source — and every displayed
    identity/summary comes from that stored original, clearly
    marked current=false with the binding error. Present corrupt
    records still raise; missing history stays empty.
    """
    root = rec_root(op_id)
    try:
        names = sorted(p.name for p in root.glob("attempt-*.json"))
    except OSError as oexc:
        raise IssueRecoveryError(f"unreadable attempt store: {oexc}",
                                 status=502, code="unreadable") from oexc
    latest = _read_record(root, "latest.json")
    if latest is None:
        # Never attempted: no stored identity exists, so only the
        # structurally valid live parent summary is shown — with no
        # report and no history claims.
        cause = human_cause({"op": raw_op,
                             "result": raw_op.get("result") or {}})
        return {
            "op": op_id, "status": raw_op.get("status"),
            "issue": raw_op.get("issue"),
            "candidate": raw_op.get("candidate"),
            "base": raw_op.get("base"),
            "error": raw_op.get("error") or "",
            "cause_title": cause["title"],
            "conflicted_paths": cause["conflicted_paths"],
            "original_touched": cause["original_touched"],
            "next_action": cause["next_action"],
            "fastcheck": fastcheck_truth({"op": raw_op, "op_id": op_id}),
            "has_prepared_source": False,
            "latest": None, "attempts": [], "attempt_total": 0,
            "report": None, "current": False,
            "binding_error": {"code": exc.code,
                              "message": str(exc)[:300]},
            "input_files": [], "input_omitted": [],
            "followup_allowed": False,
            "derived_action": {
                "available": False,
                "reason": "파생 준비는 별도 승인 후에만 가능"},
        }
    stored = _read_record(root, f"{latest.get('input')}.json") \
        if isinstance(latest.get("input"), str) and latest.get("input") \
        else None
    if stored is None or not isinstance(stored, dict):
        raise IssueRecoveryError("latest input link is broken",
                                 status=502, code="corrupt")
    # Prove the full original linkage of the stored input — shape,
    # ownership, and its own fingerprint recomputed from stored
    # bytes — before displaying anything beneath it. A record that
    # only drifted in its live binding stays inspectable; a record
    # whose own bytes no longer prove their identity raises.
    _validate_stored_identity(op_id, stored)
    # Shared ownership for EVERY lifecycle status, not only done:
    # a queued/running/error owner whose op/kind/rid/input link was
    # swapped under live drift must raise, never render as history.
    _validate_owner_link(root, op_id, latest, stored)
    report = None
    if latest.get("status") == "done":
        if not latest.get("report"):
            raise IssueRecoveryError("completed review has no report",
                                     status=502, code="corrupt")
        report = _validate_report_chain(root, op_id, latest, stored)
    # Display context comes from the validated STORED original, never
    # from the drifted live parent: human cause from the original
    # failure text, conflict scope from the original input.
    cause = human_cause({"op": {"error": stored.get("op_error") or ""},
                         "result": {"conflicted_paths":
                                    stored.get("conflicted_paths") or []}})
    attempts = _projection_attempts(root, op_id, names)
    # The displayed pointer is stale by construction: the live binding
    # that would make it current has failed. Stored bytes keep their
    # original statuses; only the projected view is marked.
    shown_latest = dict(latest) if isinstance(latest, dict) else latest
    if isinstance(shown_latest, dict):
        shown_latest["status"] = "stale"
    input_files, input_omitted = _snapshot_file_view(stored)
    # Stored build evidence projects as read-only history alongside
    # the code windows — same treatment, no proof claims, never a
    # reason to hide available evidence merely because no new
    # submission is allowed. Legacy inputs project nulls.
    input_build, input_build_omitted = _snapshot_build_view(stored)
    return {
        # Proven original identities: issue/candidate/base/error
        # describe the source the stored review investigated.
        "op": op_id, "status": raw_op.get("status"),
        "issue": stored.get("issue"),
        "candidate": stored.get("candidate"),
        "base": stored.get("base"),
        "error": stored.get("op_error") or "",
        "cause_title": cause["title"],
        "conflicted_paths": cause["conflicted_paths"],
        "original_touched": cause["original_touched"],
        "next_action": cause["next_action"],
        # Receipt lookup is op-file bound, but the live gates can no
        # longer be rebound to current source: the op has no prepared
        # source and no current review.
        "fastcheck": fastcheck_truth({"op": raw_op, "op_id": op_id}),
        "has_prepared_source": False,
        "latest": shown_latest,
        "attempts": attempts, "attempt_total": len(names),
        "report": report, "current": False,
        "binding_error": {"code": exc.code,
                          "message": str(exc)[:300]},
        "input_files": input_files, "input_omitted": input_omitted,
        "input_build": input_build,
        "input_build_omitted": input_build_omitted,
        "followup_allowed": False,
        "derived_action": {"available": False,
                           "reason": "파생 준비는 별도 승인 후에만 가능"},
    }


def read_status(op_id: str) -> dict:
    """Cached read-only status: blocked summary + latest review state.

    Missing (never attempted) differs from present-corrupt/
    unreadable/stale: corruption raises storage errors, and a done
    pointer whose report linkage no longer validates is reported as
    stale with the stored payload preserved, never as current.

    Read failures are classified: only a stale or currently
    unreadable live binding on a structurally valid parent falls back
    to inspectable stored history (current=false with an explicit
    binding_error). Wrong-kind, not-blocked, bad-identity, missing,
    and corrupt records refuse as typed errors — no history can be
    attributed to a source that never validated.
    """
    try:
        ctx = load_blocked_op(op_id)
    except IssueRecoveryError as exc:
        # Structural failures stay errors: there is no validated
        # source to which any history could belong. A defensive
        # re-read guards the race where the op file vanished or
        # became unreadable between load and this fallback.
        if exc.code not in ("stale", "unreadable"):
            raise
        try:
            raw_op, _raw = _read_op_file(op_id)
        except IssueRecoveryError:
            raise exc
        return _historical_status(op_id, raw_op, exc)
    cause = human_cause(ctx)
    truth = fastcheck_truth(ctx)
    root = rec_root(op_id)
    latest = _read_record(root, "latest.json")
    try:
        names = sorted(p.name for p in root.glob("attempt-*.json"))
    except OSError as exc:
        raise IssueRecoveryError(f"unreadable attempt store: {exc}",
                                 status=502, code="unreadable") from exc
    attempts = _projection_attempts(root, op_id, names)
    report = None
    current = False
    current_context = None
    input_files: list = []
    input_omitted: list = []
    input_build = None
    input_build_omitted = None
    if isinstance(latest, dict) and latest:
        stored = _read_record(root, f"{latest.get('input')}.json") \
            if isinstance(latest.get("input"), str) \
            and latest.get("input") else None
        # The stored input's own identity is proved before anything
        # beneath it is shown or linked: shape, ownership, and its
        # own fingerprint recomputed from stored bytes alone.
        _validate_stored_identity(op_id, stored)
        # Shared ownership for EVERY lifecycle status, not only done:
        # a latest pointer whose owner op/kind/rid/input link drifted
        # raises here too, never renders as current history.
        _validate_owner_link(root, op_id, latest, stored)
        # The note ACTUALLY consumed by this reviewed input — proven
        # above through the input's own context block, never inferred
        # from record existence. Survives latest moves because it is
        # bound to the input, not to the origin chain position.
        current_context = _project_current_context(op_id, root, stored)
        if latest.get("status") == "done":
            if not latest.get("report"):
                raise IssueRecoveryError(
                    "completed review has no report",
                    status=502, code="corrupt")
            # A done pointer whose full report chain no longer
            # validates is an explicit storage error, never a healthy
            # empty view. current is NOT the pointer's own claim: the
            # stored sent ranges are replayed against the CURRENT
            # binding and must reproduce the stored fingerprint. Drift
            # in run bytes, base identity, or recorded refs marks the
            # review stale while preserving the payload for history.
            report = _validate_report_chain(root, op_id, latest, stored)
            try:
                now_fp = _current_fp(ctx, stored)
            except IssueRecoveryError:
                now_fp = None
            current = bool(now_fp) \
                and now_fp == stored.get("fingerprint")
        input_files, input_omitted = _snapshot_file_view(stored)
        # Build evidence projects only from the validated stored
        # input above; legacy inputs project nulls (UI hides).
        input_build, input_build_omitted = _snapshot_build_view(stored)
    # Authoritative prepared-source semantics: a blocked op that
    # carries no validated result head/fingerprint has NO prepared
    # source, even when the residual result.path string is non-empty
    # (it points at a removed worktree). A path string is not
    # presence.
    res = ctx["result"]
    has_prepared = bool(res.get("head") and res.get("fingerprint"))
    # Fresh raw-byte hashes of the CURRENT latest chain, read live:
    # the form echoes them back as the expected revision so a POST
    # from an old read cannot silently answer edited question text
    # under unchanged ids. None when there is no chain to answer.
    origin_shas = None
    if isinstance(latest, dict) and latest \
            and isinstance(latest.get("input"), str) \
            and latest.get("input"):
        origin_shas = {
            "input": hashlib.sha256(
                _record_bytes(root, f"{latest['input']}.json")
            ).hexdigest(),
            "report": None,
        }
        if isinstance(latest.get("report"), str) and latest.get("report"):
            origin_shas["report"] = hashlib.sha256(
                _record_bytes(root, f"{latest['report']}.json")
            ).hexdigest()
    contexts = _projection_contexts(root, op_id)
    owner_decisions = _projection_owner_decisions(root, op_id)
    busy = bool(isinstance(latest, dict) and latest
                and latest.get("status") in ("running", "queued"))
    # A supplemental context may only start a re-review from a
    # settled, current, valid chain: never during a live run, never
    # for a stale pointer, never without a report to answer.
    context_allowed = bool(current and report and not busy)
    return {"op": op_id, "status": ctx["op"].get("status"),
            "issue": ctx["issue"], "candidate": ctx["candidate"],
            "base": ctx["base"],
            "conflicted_paths": cause["conflicted_paths"],
            "original_touched": cause["original_touched"],
            "cause_title": cause["title"],
            "next_action": cause["next_action"],
            "error": ctx["op"].get("error"),
            "fastcheck": truth, "latest": latest,
            "attempts": attempts, "attempt_total": len(names),
            "report": report, "current": current,
            "input_files": input_files, "input_omitted": input_omitted,
            "input_build": input_build,
            "input_build_omitted": input_build_omitted,
            "contexts": contexts, "context_allowed": context_allowed,
            "current_context": current_context,
            "owner_decisions": owner_decisions,
            "origin_shas": origin_shas,
            "has_prepared_source": has_prepared,
            "followup_allowed": False,
            "derived_action": {"available": False,
                               "reason": "파생 준비는 별도 승인 후에만 가능"}}
