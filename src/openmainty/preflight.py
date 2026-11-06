"""App-owned capability preflight (Stage2B).

Bounded proof, BEFORE any actual source-worker action, that the
dedicated source-repair profile can run file-only: pinned binary,
requested policy, config/source/auth identity, and per-channel
positive-before-negative controls. A fresh genuine preflight bound to
the SAME actual binary/config/auth/policy/source identity is
mandatory before target-A; missing/stale/unknown/corrupt/auth/quota/
offline/format refusals create no model/job/worktree mutation.

Honesty rules (no fabricated capability):
* Unsupported or unobserved channels stay UNKNOWN and deny target-A.
  A model enumerating its own tools is never a startup control;
  positive controls must be trusted app/harness observations.
* META_API_KEY presence is necessary-but-insufficient evidence:
  absent key never proves a subscription session, and a stored key
  takes priority over login, so a present key refuses without spawn.
* Raw requested flags are REQUESTED state, never observed
  enforcement. Effective file-only/MCP/hooks/delegation proof is a
  separate blocking gate; until then the authority check refuses.
* No automatic deletion of fixtures or receipts; no phantom success
  from a status string. Missing/corrupt/stale receipts can never
  become success.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import secrets
import stat as _stat
import threading
import time

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None

PREFLIGHT_SCHEMA = 1
PREFLIGHT_DIRNAME = "preflight"
PREFLIGHT_TIMEOUT_S = 300
PREFLIGHT_INPUT_MAX_BYTES = 32768
PREFLIGHT_OUTCOME_MAX_BYTES = 32768
PREFLIGHT_STATUS_MAX_BYTES = 4096
PREFLIGHT_SCAN_CAP = 4096
PREFLIGHT_DETAIL_MAX_CHARS = 500
# Full per-value env bind bound: the allowlisted child-env values are
# pinned completely (never truncated prefixes); larger values refuse
# at record time instead of binding a prefix the spawn does not use.
_ENV_VALUE_MAX_CHARS = 65536

# Channels with a required direction. "disabled" means the channel must
# be proven absent (positive control present, channel not observed);
# "restricted" means the single restricted network mode must be
# proven requested AND no broader egress observed.
REQUIRED_CHANNELS = ("shell", "web", "mcp", "hook", "network")
REQUIRED_DIRECTION = {
    "shell": "disabled",
    "web": "disabled",
    "mcp": "disabled",
    "hook": "disabled",
    "network": "restricted",
}

# Managed source files bound by SHA at every gate (package version
# alone cannot prove the running code).
_MANAGED_SOURCE_FILES = ("muse.py", "preflight.py", "derived_fix.py",
                          "serve.py")
_MANAGED_SOURCE_MAX_BYTES = 4 * 1024 * 1024

# Exact dedicated-profile policy a preflight binds. Must match
# muse.SOURCE_REPAIR_PROFILE on the managed keys; any drift refuses.
# max_model_steps / max_tool_output_bytes are the confirmed CLI
# bounds (help surface: "Cap the number of model steps" / "Cap tool
# output bytes fed back to the model"); step/output safety never
# relies on app timeout text alone.
REQUIRED_POLICY = {
    "profile": "source-repair",
    "provider": "meta",
    "model": "muse-spark-1.3",
    "contributor_mode": True,
    "approval_mode": "never",
    "approval_judge": "off",
    "network_mode": "restricted",
    "disable_request_user_input": True,
    "trust_workspace": True,
    "max_model_steps": 25,
    "max_tool_output_bytes": 200000,
}

# Trust is structural, not a string set: ONLY observations produced
# by the admitted channel observer count (the real observer in
# production, an explicit fake in offline tests). The observer is
# product code; callers, model output, and env can never supply
# trusted facts. A credential_source of "unknown" (or a META_API_KEY
# presence) refuses: the supported trusted auth-source mechanism is
# still unproved, which stays an explicit block — never simulated
# success. Offline tests NEVER reach production through env: this
# module has no fixture env seams. Tests redirect identity facts by
# mocking the I/O boundary (current_identity / the channel observer)
# under fresh owned fixtures, exactly as they already mock the model
# transport. The comparison, schema, and fail-closed logic below run
# unmocked in every test.

_PFID_RE = None  # compiled lazily (see _valid_pfid)
_LEAF_RE = None
_LOCK_LEAF_RE = None  # compiled lazily (see _valid_lock_leaf)


def _valid_pfid(pfid) -> bool:
    global _PFID_RE
    if _PFID_RE is None:
        import re
        _PFID_RE = re.compile(r"^pf-[0-9a-f]{16}$")
    return isinstance(pfid, str) and _PFID_RE.fullmatch(pfid) is not None


def _valid_leaf(leaf) -> bool:
    global _LEAF_RE
    if _LEAF_RE is None:
        import re
        _LEAF_RE = re.compile(
            r"^pf-[0-9a-f]{16}\.(input|outcome|status|cancel|claim|"
            r"spawn)\.json$")
    return isinstance(leaf, str) and _LEAF_RE.fullmatch(leaf) is not None


class PreflightError(Exception):
    """Typed preflight failure (mirrors DerivedFixError/ApiError shape)."""

    def __init__(self, message: str, status: int = 409, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.http_status = status
        self.code = code or ""


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _home():
    try:
        from . import paths as _paths
        return _paths.home()
    except OSError as exc:
        raise PreflightError(f"unreadable app home: {exc}",
                             status=502, code="unreadable") from exc


def _resolve_home(home):
    """Configured home, resolved exactly once.

    The configured home path itself is deployment configuration, not
    attacker-planted content (live: ~/.openmainty is a symlink to the
    owner's SSD store). A no-follow open of that final component
    raises ELOOP/ENOTDIR and would report the whole store
    "unreadable", so resolve it once here — the single funnel every
    preflight entry uses. Per-component no-follow discipline BELOW
    the resolved home (_open_dir_fd, _load_doc, locks) is unchanged:
    symlinks inside the home still refuse exactly as before.
    """
    raw = str(home) if home is not None else str(_home())
    try:
        return os.path.realpath(raw)
    except OSError as exc:
        raise PreflightError(f"unresolvable app home: {exc}",
                             status=502, code="unreadable") from exc


def _reject_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key: {key!r}")
        out[key] = value
    return out


def _loads_closed(raw: bytes):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PreflightError(f"preflight bytes not UTF-8: {exc}",
                             status=502, code="corrupt") from exc
    try:
        doc = json.loads(text, object_pairs_hook=_reject_dupes)
    except ValueError as exc:
        raise PreflightError(f"preflight JSON malformed: {exc}",
                             status=502, code="corrupt") from exc
    if not isinstance(doc, dict):
        raise PreflightError("preflight document must be an object",
                             status=502, code="corrupt")
    return doc


def _dumps_canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _open_home_fd(home_path: str) -> int:
    try:
        return os.open(home_path, os.O_RDONLY | os.O_DIRECTORY
                       | os.O_NOFOLLOW)
    except OSError as exc:
        raise PreflightError(
            f"preflight home unavailable: {home_path}: {exc}",
            status=502, code="unreadable") from exc


def _open_dir_fd(home_path: str, create: bool):
    """Open the owned preflight dir.

    Returns (dir_fd, dev, ino, home_dev, home_ino). Read paths never
    create: a missing layout reads as missing-required ("absent"),
    never an empty store. Every document binds BOTH the home dir and
    the preflight dir (dev, ino); ancestor swaps above the leaf are
    caught by rechecking both identities on every read. A no-follow
    open of the final home component alone does not prove every
    ancestor — the residual above the app home is documented, and
    the configured home itself is resolved exactly once by
    _resolve_home (deployment configuration, not planted content)
    before this no-follow open; nothing below it is ever followed.
    """
    home_fd = _open_home_fd(home_path)
    try:
        try:
            hst = os.fstat(home_fd)
        except OSError as exc:
            raise PreflightError(
                f"preflight home stat failed: {exc}",
                status=502, code="unreadable") from exc
        try:
            dfd = os.open(PREFLIGHT_DIRNAME, os.O_RDONLY | os.O_DIRECTORY
                          | os.O_NOFOLLOW, dir_fd=home_fd)
        except OSError as exc:
            if not create or exc.errno != errno.ENOENT:
                if exc.errno == errno.ELOOP:
                    raise PreflightError(
                        "preflight directory is a symlink: refusing",
                        status=502, code="unreadable") from exc
                if exc.errno == errno.ENOENT and not create:
                    raise PreflightError(
                        f"preflight directory missing: {exc}",
                        status=404, code="missing") from exc
                raise PreflightError(
                    f"preflight directory unavailable: {exc}",
                    status=502, code="unreadable") from exc
            try:
                os.mkdir(PREFLIGHT_DIRNAME, 0o700, dir_fd=home_fd)
            except OSError as exc2:
                if exc2.errno != errno.EEXIST:
                    raise PreflightError(
                        f"preflight directory cannot be created: {exc2}",
                        status=502, code="unreadable") from exc2
                # Lost the first-create race: a sibling creator's
                # directory now exists. Fall through to the normal
                # validated open below — the identical nofollow /
                # directory / stat / home-continuity checks as any
                # existing identity. A planted symlink still
                # refuses ELOOP there; nothing is healed, rebound,
                # or retried more than this once.
            try:
                dfd = os.open(PREFLIGHT_DIRNAME,
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=home_fd)
            except OSError as exc3:
                raise PreflightError(
                    f"preflight directory unavailable after create: {exc3}",
                    status=502, code="unreadable") from exc3
        try:
            st = os.fstat(dfd)
        except OSError as exc:
            os.close(dfd)
            raise PreflightError(
                f"preflight directory stat failed: {exc}",
                status=502, code="unreadable") from exc
        return dfd, st.st_dev, st.st_ino, hst.st_dev, hst.st_ino
    finally:
        os.close(home_fd)


def _write_owned_json(dfd, leaf: str, obj: dict, *, immutable: bool,
                      max_bytes: int):
    """Store one JSON doc under an owned dir fd.

    Immutable docs publish no-overwrite: a securely-created owned temp
    is hard-linked onto the final name; EEXIST with identical bytes
    confirms the stored artifact (never a duplicate dispatch), EEXIST
    with differing bytes refuses and the old bytes survive. Mutable
    status pointers use an owned temp + atomic replace. Symlinked or
    non-regular finals refuse; nothing is deleted or rebound.
    """
    if not _valid_leaf(leaf):
        raise PreflightError(f"bad preflight leaf name: {leaf!r}",
                             status=500, code="bad-leaf")
    raw = _dumps_canonical(obj)
    if not (0 < len(raw) <= max_bytes):
        raise PreflightError(
            f"preflight document size out of bounds: {len(raw)}",
            status=500, code="bad-size")
    tmp = f"{leaf}.tmp-{secrets.token_hex(8)}"
    tmp_fd = None
    try:
        tmp_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                         dir_fd=dfd)
    except OSError as exc:
        raise PreflightError(f"preflight temp cannot be created: {exc}",
                             status=502, code="unreadable") from exc
    try:
        view = memoryview(raw)
        while view:
            try:
                wrote = os.write(tmp_fd, view)
            except OSError as exc:
                raise PreflightError(
                    f"preflight temp write failed: {exc}",
                    status=502, code="unreadable") from exc
            view = view[wrote:]
        try:
            os.fsync(tmp_fd)
        except OSError as exc:
            raise PreflightError(
                f"preflight temp fsync failed: {exc}",
                status=502, code="unreadable") from exc
    except BaseException:
        try:
            os.close(tmp_fd)
        except OSError:
            pass
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    os.close(tmp_fd)
    try:
        if immutable:
            try:
                os.link(tmp, leaf, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise PreflightError(
                        f"preflight publish failed: {exc}",
                        status=502, code="unreadable") from exc
                existing = _read_owned_json(dfd, leaf, max_bytes)
                if existing != raw:
                    raise PreflightError(
                        f"preflight artifact exists with different bytes: "
                        f"{leaf}; old bytes preserved, nothing dispatched",
                        status=409, code="exists-different") from exc
                return "exists-identical"
            return "created"
        else:
            try:
                os.replace(tmp, leaf, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError as exc:
                raise PreflightError(
                    f"preflight status write failed: {exc}",
                    status=502, code="unreadable") from exc
            return "replaced"
    finally:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass


def _assert_leaf_identity(dir_fd, leaf: str, fd: int, opened_st,
                          label: str) -> None:
    """Re-verify the live NAME still resolves to our open inode.

    Closing fstat/timestamps only describe the already-open file:
    a rename that swaps the name for another inode keeps the old
    fd valid, so open/close comparisons alone cannot catch it.
    Re-stat the name itself (no-follow, dir-relative) and require
    the same (dev, ino) as the descriptor we read/locked. A
    missing, unstatable, or repointed name fails closed; the
    suspect name is never cleaned up or rebound here.
    """
    try:
        current = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        raise PreflightError(
            f"{label} name no longer resolves: {leaf}: {exc}",
            status=502, code="unreadable") from exc
    if (current.st_dev, current.st_ino) != (
            opened_st.st_dev, opened_st.st_ino):
        raise PreflightError(
            f"{label} replaced during read: {leaf}",
            status=502, code="corrupt")


def _read_owned_json(dfd, leaf: str, max_bytes: int):
    """Read bounded bytes via a no-follow leaf open; returns raw bytes.

    The leaf must be a regular file within bounds; reads run to EOF
    so a grown file can never pass as the pinned prefix. Non-regular
    leaves refuse without hanging (O_NONBLOCK open fails fast on
    FIFOs/devices). After the closing fstat, the live name is
    re-resolved and must still point at the same inode we read:
    open/close fstat alone cannot catch a rename-swap of the name
    onto another inode while our fd stays valid. (An earlier
    comment claiming closing dev/ino catches renames was wrong;
    this explicit name check is the actual guard.) Directory-level
    identity is additionally checked by callers against the
    identity recorded inside each document.
    """
    if not _valid_leaf(leaf):
        raise PreflightError(f"bad preflight leaf name: {leaf!r}",
                             status=500, code="bad-leaf")
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=dfd)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise PreflightError(f"preflight artifact missing: {leaf}",
                                 status=404, code="missing") from exc
        if exc.errno == errno.ELOOP:
            raise PreflightError(
                f"preflight artifact is a symlink: {leaf}",
                status=502, code="unreadable") from exc
        raise PreflightError(f"preflight artifact unavailable: {leaf}: "
                             f"{exc}", status=502,
                             code="unreadable") from exc
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise PreflightError(
                f"preflight artifact stat failed: {leaf}: {exc}",
                status=502, code="unreadable") from exc
        if not _stat.S_ISREG(st.st_mode):
            raise PreflightError(
                f"preflight artifact not a regular file: {leaf}",
                status=502, code="unreadable")
        if st.st_size < 0 or st.st_size > max_bytes:
            raise PreflightError(
                f"preflight artifact size out of bounds: {leaf}",
                status=502, code="corrupt")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            try:
                part = os.read(fd, min(65536, remaining))
            except OSError as exc:
                raise PreflightError(
                    f"preflight artifact read failed: {leaf}: {exc}",
                    status=502, code="unreadable") from exc
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        else:
            raise PreflightError(
                f"preflight artifact larger than bound: {leaf}",
                status=502, code="corrupt")
        raw = b"".join(chunks)
        try:
            closing = os.fstat(fd)
        except OSError as exc:
            raise PreflightError(
                f"preflight artifact closing stat failed: {leaf}: "
                f"{exc}", status=502, code="unreadable") from exc
        if (closing.st_dev, closing.st_ino, closing.st_size,
                closing.st_mtime_ns, closing.st_ctime_ns) != (
                st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                st.st_ctime_ns):
            # mtime/ctime join dev/ino/size: a same-length in-place
            # rewrite keeps the size but updates the timestamps, so
            # a size-only comparison would wrongly accept it. This
            # only describes our open fd — a rename swapping the
            # NAME onto another inode is checked explicitly below.
            raise PreflightError(
                f"preflight artifact replaced during read: {leaf}",
                status=502, code="corrupt")
        if len(raw) != st.st_size:
            raise PreflightError(
                f"preflight artifact changed during read: {leaf}",
                status=502, code="corrupt")
        _assert_leaf_identity(dfd, leaf, fd, st, "preflight artifact")
        return raw
    finally:
        os.close(fd)


def _check_closed_str(value, name: str, max_chars: int,
                      allow_empty: bool = False) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise PreflightError(f"preflight field {name} must be a string",
                             status=502, code="corrupt")
    if not allow_empty and not value:
        raise PreflightError(f"preflight field {name} must be non-empty",
                             status=502, code="corrupt")
    if len(value) > max_chars:
        raise PreflightError(f"preflight field {name} too long",
                             status=502, code="corrupt")
    return value


def _check_closed_shape(doc: dict, spec: dict, name: str) -> dict:
    """Validate exact closed keys/types; rejects unknown keys.

    `spec` maps key -> ("str"|"estr"|"bool"|"dict"|"list"|"int"|
    "opt-str", max_chars). "estr" is a string that may be empty
    (verified outcomes carry empty error/code). Unknown keys refuse
    (no silent contract drift).
    """
    if not isinstance(doc, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in doc:
        if key not in spec:
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    out = {}
    for key, (kind, bound) in spec.items():
        if key not in doc:
            raise PreflightError(f"preflight {name} missing key: {key}",
                                 status=502, code="corrupt")
        value = doc[key]
        if kind == "str":
            out[key] = _check_closed_str(value, f"{name}.{key}", bound)
        elif kind == "estr":
            out[key] = _check_closed_str(value, f"{name}.{key}", bound,
                                         allow_empty=True)
        elif kind == "opt-str":
            if value is not None:
                _check_closed_str(value, f"{name}.{key}", bound)
            out[key] = value
        elif kind == "bool":
            if not isinstance(value, bool):
                raise PreflightError(
                    f"preflight {name}.{key} must be a boolean",
                    status=502, code="corrupt")
            out[key] = value
        elif kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise PreflightError(
                    f"preflight {name}.{key} must be an integer",
                    status=502, code="corrupt")
            out[key] = value
        elif kind == "dict":
            if not isinstance(value, dict):
                raise PreflightError(
                    f"preflight {name}.{key} must be an object",
                    status=502, code="corrupt")
            out[key] = value
        elif kind == "list":
            if not isinstance(value, list):
                raise PreflightError(
                    f"preflight {name}.{key} must be a list",
                    status=502, code="corrupt")
            out[key] = value
    return out


_INPUT_SPEC = {
    "schema": ("int", 0),
    "pfid": ("str", 64),
    "created_at": ("str", 64),
    "dedupe_key": ("str", 128),
    "policy": ("dict", 0),
    "binary": ("dict", 0),
    "config": ("dict", 0),
    "source": ("dict", 0),
    "env": ("dict", 0),
    "auth_observed": ("dict", 0),
    "dir": ("list", 0),
    "home": ("list", 0),
}

_OUTCOME_SPEC = {
    "schema": ("int", 0),
    "pfid": ("str", 64),
    "state": ("str", 32),
    "inputs_sha256": ("str", 64),
    "channels": ("dict", 0),
    "auth": ("dict", 0),
    "error": ("estr", 2000),
    "code": ("estr", 64),
    "reason": ("str", 2000),
    "ended_at": ("str", 64),
    "dir": ("list", 0),
    "home": ("list", 0),
}

_AUTH_KEYS = ("meta_api_key_present", "credential_source",
              "billing_evidence", "observed_how",
              "contributor_env_present")

_STATUS_SPEC = {
    "schema": ("int", 0),
    "pfid": ("str", 64),
    "state": ("str", 32),
    "updated_at": ("str", 64),
}

_TERMINAL_STATES = ("verified", "unknown", "refused", "cancelled")

# Structural refusal codes: re-running cannot succeed until the named
# evidence exists, so the top-level reason must never say "run again".
# Each text is Korean-first, names its code, and states that running
# now refuses identically with no model call.
_STRUCTURAL_BLOCK_REASONS = {
    "config-unestablished":
        "격리 점검이 구조적으로 차단되었습니다 (코드 "
        "config-unestablished): 설정 표면이 아직 확정되지 "
        "않았습니다. 게이트 (2)의 설정 증명과 구독 인증 증명이 있기 "
        "전에는 지금 실행해도 같은 사유로 거부되며 모델 호출은 "
        "없습니다.",
    "auth-unestablished":
        "격리 점검이 구조적으로 차단되었습니다 (코드 "
        "auth-unestablished): 구독 로그인 자격이 확인되지 "
        "않았습니다. 구독 인증 증명이 있기 전에는 지금 실행해도 "
        "같은 사유로 거부되며 모델 호출은 없습니다.",
    "native-containment-unproved":
        "격리 점검이 구조적으로 차단되었습니다 (코드 "
        "native-containment-unproved): 네이티브 격리 래퍼가 아직 "
        "검증되지 않았습니다. 승인된 네이티브 정책 증명이 있기 "
        "전에는 지금 실행해도 같은 사유로 거부되며 모델 호출은 "
        "없습니다.",
    "api-key-present":
        "격리 점검이 구조적으로 차단되었습니다 (코드 "
        "api-key-present): 환경에 API 키가 있어 구독 전용 실행이 "
        "거부됩니다. 키를 제거하고 새로 요청하기 전에는 지금 "
        "실행해도 같은 사유로 거부되며 모델 호출은 없습니다.",
    "contributor-env-present":
        "격리 점검이 구조적으로 차단되었습니다 (코드 "
        "contributor-env-present): 기여자 모드 환경 변수가 고정 "
        "요청과 충돌합니다. 변수를 제거하고 새로 요청하기 전에는 "
        "지금 실행해도 같은 사유로 거부되며 모델 호출은 없습니다.",
    "bad-policy":
        "격리 점검이 구조적으로 차단되었습니다 (코드 bad-policy): "
        "고정 프로필과 어긋난 요청입니다. 고정 프로필로 새로 "
        "요청하기 전에는 지금 실행해도 같은 사유로 거부되며 모델 "
        "호출은 없습니다.",
}


def _validate_input_doc(doc: dict) -> dict:
    checked = _check_closed_shape(doc, _INPUT_SPEC, "input")
    if checked["schema"] != PREFLIGHT_SCHEMA or isinstance(
            checked["schema"], bool):
        raise PreflightError("preflight input has unknown schema",
                             status=502, code="corrupt")
    if not _valid_pfid(checked["pfid"]):
        raise PreflightError("preflight input has bad pfid",
                             status=502, code="corrupt")
    if checked["policy"] != REQUIRED_POLICY:
        raise PreflightError("preflight input policy is not the dedicated "
                             "source-repair profile",
                             status=502, code="corrupt")
    for label in ("dir", "home"):
        if not isinstance(checked[label], list) or \
                len(checked[label]) != 2:
            raise PreflightError(
                f"preflight input has bad {label} identity",
                status=502, code="corrupt")
    _validate_binary_shape(checked["binary"], "input.binary")
    _validate_config_shape(checked["config"], "input.config")
    _validate_source_shape(checked["source"], "input.source")
    _validate_env_shape(checked["env"], "input.env")
    _validate_auth_shape(checked["auth_observed"], "input.auth_observed")
    return checked


def _validate_source_shape(source: dict, name: str) -> None:
    """Source identity: package version PLUS managed file SHAs.

    A version string alone cannot prove the running code: code can
    change without a version bump, so the four dedicated-path files
    are hashed and compared exactly at every gate.
    """
    if not isinstance(source, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in source:
        if key not in ("app_version", "status", "files"):
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    if source.get("status", "ok") != "ok":
        raise PreflightError(f"preflight {name} source unavailable",
                             status=502, code="corrupt")
    version = source.get("app_version")
    if not isinstance(version, str) or not version:
        raise PreflightError(f"preflight {name} has bad app_version",
                             status=502, code="corrupt")
    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise PreflightError(f"preflight {name} has no source files",
                             status=502, code="corrupt")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise PreflightError(f"preflight {name} has bad file entry",
                                 status=502, code="corrupt")
        for key in entry:
            if key not in ("path", "sha256"):
                raise PreflightError(
                    f"preflight {name} file entry has unknown key: "
                    f"{key!r}", status=502, code="corrupt")
        path = entry.get("path")
        sha = entry.get("sha256")
        if path not in _MANAGED_SOURCE_FILES or path in seen:
            raise PreflightError(
                f"preflight {name} has bad file identity: {path!r}",
                status=502, code="corrupt")
        seen.add(path)
        if not isinstance(sha, str) or len(sha) != 64:
            raise PreflightError(
                f"preflight {name} has bad file SHA: {path!r}",
                status=502, code="corrupt")
    # Exact managed set: omitting a file hides code changes, so a
    # subset (or superset) is corrupt, not a narrower witness.
    if seen != set(_MANAGED_SOURCE_FILES):
        raise PreflightError(
            f"preflight {name} must witness exactly the managed "
            f"source set: {sorted(seen)!r}",
            status=502, code="corrupt")


def _validate_env_shape(env: dict, name: str) -> None:
    """Child-environment binding (key sets plus observed values)."""
    if not isinstance(env, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in env:
        if key not in ("passthrough_keys", "cache_keys", "values"):
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    for key in ("passthrough_keys", "cache_keys"):
        value = env.get(key)
        if not isinstance(value, list) or not value or not all(
                isinstance(item, str) for item in value):
            raise PreflightError(
                f"preflight {name}.{key} must be a non-empty string "
                f"list", status=502, code="corrupt")
    values = env.get("values")
    if not isinstance(values, dict):
        raise PreflightError(f"preflight {name}.values must be an "
                             f"object", status=502, code="corrupt")
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str) \
                or len(key) > 256 or len(value) > _ENV_VALUE_MAX_CHARS:
            raise PreflightError(
                f"preflight {name}.values has a bad entry",
                status=502, code="corrupt")


def _validate_binary_shape(binary: dict, name: str) -> None:
    """Closed pinned-binary identity (no invented hashes)."""
    if not isinstance(binary, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in binary:
        if key not in ("status", "path", "real_path", "sha256",
                       "error"):
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    status = binary.get("status")
    if status not in ("ok", "unavailable"):
        raise PreflightError(f"preflight {name} has bad status",
                             status=502, code="corrupt")
    if status == "ok":
        for key in ("path", "real_path"):
            value = binary.get(key)
            if not isinstance(value, str) or not value or \
                    len(value) > 1024:
                raise PreflightError(
                    f"preflight {name}.{key} must be a non-empty "
                    f"path", status=502, code="corrupt")
        sha = binary.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(
                c not in "0123456789abcdefABCDEF" for c in sha):
            raise PreflightError(
                f"preflight {name}.sha256 must be 64 hex chars",
                status=502, code="corrupt")
    else:
        if binary.get("sha256") not in ("", None):
            raise PreflightError(
                f"preflight {name} claims a hash with no readable "
                f"binary", status=502, code="corrupt")
        error = binary.get("error", "")
        if not isinstance(error, str) or len(error) > 300:
            raise PreflightError(
                f"preflight {name}.error must be bounded text",
                status=502, code="corrupt")


def _validate_worker_binding(worker: dict, name: str) -> None:
    """Closed same-alive worker binding (slice 297).

    Names the exact live worker an established config was
    proved on: registry handle, child pid, and the slice-293
    served-settings sha. Unknown keys are corrupt; a missing
    key is corrupt. Legacy records omit "worker" entirely.
    """
    if not isinstance(worker, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in ("handle", "pid", "sha256", "provider", "model",
                "contributor"):
        if key not in worker:
            raise PreflightError(
                f"preflight {name} is missing key: {key!r}",
                status=502, code="corrupt")
    for key in worker:
        if key not in ("handle", "pid", "sha256", "provider",
                       "model", "contributor"):
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    handle = worker["handle"]
    if not isinstance(handle, str) or not handle \
            or len(handle) > 128:
        raise PreflightError(
            f"preflight {name}.handle must be a non-empty string",
            status=502, code="corrupt")
    pid = worker["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) \
            or pid <= 0:
        raise PreflightError(
            f"preflight {name}.pid must be a positive "
            f"integer", status=502, code="corrupt")
    sha = worker["sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(
            c not in "0123456789abcdefABCDEF" for c in sha):
        raise PreflightError(
            f"preflight {name}.sha256 must be 64 hex chars",
            status=502, code="corrupt")
    provider = worker["provider"]
    if not isinstance(provider, str) or not provider \
            or len(provider) > 64:
        raise PreflightError(
            f"preflight {name}.provider must be a non-empty "
            f"string", status=502, code="corrupt")
    model = worker["model"]
    if not isinstance(model, str) or not model \
            or len(model) > 128:
        raise PreflightError(
            f"preflight {name}.model must be a non-empty string",
            status=502, code="corrupt")
    if worker["contributor"] is not True \
            and worker["contributor"] is not False:
        raise PreflightError(
            f"preflight {name}.contributor must be a boolean",
            status=502, code="corrupt")


def _validate_config_shape(config: dict, name: str) -> None:
    """Closed config-surface identity (explicit unestablished)."""
    if not isinstance(config, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in config:
        if key not in ("status", "files", "note", "worker"):
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    status = config.get("status")
    if not isinstance(status, str) or not status or len(status) > 64:
        raise PreflightError(
            f"preflight {name}.status must be a non-empty string",
            status=502, code="corrupt")
    files = config.get("files")
    if not isinstance(files, list):
        raise PreflightError(f"preflight {name}.files must be a list",
                             status=502, code="corrupt")
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise PreflightError(
                f"preflight {name} has bad file entry",
                status=502, code="corrupt")
        for key in entry:
            if key not in ("path", "sha256"):
                raise PreflightError(
                    f"preflight {name} file entry has unknown key: "
                    f"{key!r}", status=502, code="corrupt")
        path = entry.get("path")
        sha = entry.get("sha256")
        if not isinstance(path, str) or not path or \
                len(path) > 256 or path in seen:
            raise PreflightError(
                f"preflight {name} has bad file identity: {path!r}",
                status=502, code="corrupt")
        seen.add(path)
        if not isinstance(sha, str) or len(sha) != 64 or any(
                c not in "0123456789abcdefABCDEF" for c in sha):
            raise PreflightError(
                f"preflight {name} has bad file SHA: {path!r}",
                status=502, code="corrupt")
    note = config.get("note", "")
    if not isinstance(note, str) or len(note) > 500:
        raise PreflightError(
            f"preflight {name}.note must be bounded text",
            status=502, code="corrupt")
    if "worker" in config:
        _validate_worker_binding(config["worker"],
                                 f"{name}.worker")


def _validate_auth_shape(auth: dict, name: str) -> None:
    """Closed auth-observation shape (presence facts, never secrets)."""
    if not isinstance(auth, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for key in auth:
        if key not in _AUTH_KEYS:
            raise PreflightError(
                f"preflight {name} has unknown key: {key!r}",
                status=502, code="corrupt")
    if not isinstance(auth.get("meta_api_key_present"), bool):
        raise PreflightError(
            f"preflight {name}.meta_api_key_present must be a boolean "
            f"(presence, never a value)", status=502, code="corrupt")
    # Optional for backward compatibility: records written before
    # the contributor-env check existed carry no such key (legacy).
    # Absent reads as legacy; present must be a bool; anything else
    # is corrupt. Unknown keys stay corrupt (closed shape above).
    if "contributor_env_present" in auth and not isinstance(
            auth["contributor_env_present"], bool):
        raise PreflightError(
            f"preflight {name}.contributor_env_present must be a "
            f"boolean (presence, never a value)", status=502,
            code="corrupt")
    source = auth.get("credential_source")
    if not isinstance(source, str) or len(source) > 128:
        raise PreflightError(
            f"preflight {name}.credential_source must be a string",
            status=502, code="corrupt")
    evidence = auth.get("billing_evidence")
    if evidence is not None and not isinstance(evidence, dict):
        raise PreflightError(
            f"preflight {name}.billing_evidence must be null or an "
            f"object", status=502, code="corrupt")
    how = auth.get("observed_how")
    if not isinstance(how, str) or len(how) > 200:
        raise PreflightError(
            f"preflight {name}.observed_how must be a string",
            status=502, code="corrupt")


def _validate_channels(channels: dict, name: str) -> dict:
    """Closed per-channel verdicts from the admitted observer.

    Validation ONLY: shape, required directions, bounded detail.
    Never invents confirmations — an rc0 spawn with perfect
    requested flags still yields unknown/positive False unless the
    observer positively proved the channel. A model enumerating its
    own tools is never accepted as a positive control here.
    """
    if not isinstance(channels, dict):
        raise PreflightError(f"preflight {name} must be an object",
                             status=502, code="corrupt")
    for cname in channels:
        if cname not in REQUIRED_CHANNELS:
            raise PreflightError(
                f"preflight {name} has unknown channel: {cname!r}",
                status=502, code="corrupt")
    out = {}
    for cname in REQUIRED_CHANNELS:
        entry = channels.get(cname)
        if not isinstance(entry, dict):
            raise PreflightError(
                f"preflight {name} missing channel: {cname}",
                status=502, code="corrupt")
        for key in entry:
            if key not in ("required", "observed", "positive", "detail"):
                raise PreflightError(
                    f"preflight channel {cname} has unknown key: "
                    f"{key!r}", status=502, code="corrupt")
        if entry.get("required") != REQUIRED_DIRECTION[cname]:
            raise PreflightError(
                f"preflight channel {cname} has wrong required "
                f"direction", status=502, code="corrupt")
        if entry.get("observed") not in ("disabled", "restricted",
                                         "allowed", "unknown"):
            raise PreflightError(
                f"preflight channel {cname} has bad observed state",
                status=502, code="corrupt")
        if not isinstance(entry.get("positive"), bool):
            raise PreflightError(
                f"preflight channel {cname} has bad positive flag",
                status=502, code="corrupt")
        detail = entry.get("detail")
        if not isinstance(detail, str) or not detail or len(detail) > \
                PREFLIGHT_DETAIL_MAX_CHARS:
            raise PreflightError(
                f"preflight channel {cname} has bad detail",
                status=502, code="corrupt")
        out[cname] = {"required": entry["required"],
                      "observed": entry["observed"],
                      "positive": entry["positive"],
                      "detail": detail}
    return out


def _validate_outcome_doc(doc: dict) -> dict:
    """State-dependent outcome validation (validated BEFORE publish).

    Verified/unknown/refused carry the FULL channel set; a cancelled
    receipt carries NO channels (nothing was observed) and a
    non-empty cancellation reason. Verified receipts carry empty
    error/code with a non-empty reason. Every terminal state
    round-trips through read validation.
    """
    checked = _check_closed_shape(doc, _OUTCOME_SPEC, "outcome")
    if checked["schema"] != PREFLIGHT_SCHEMA or isinstance(
            checked["schema"], bool):
        raise PreflightError("preflight outcome has unknown schema",
                             status=502, code="corrupt")
    if not _valid_pfid(checked["pfid"]):
        raise PreflightError("preflight outcome has bad pfid",
                             status=502, code="corrupt")
    if checked["state"] not in _TERMINAL_STATES:
        raise PreflightError("preflight outcome has unknown state",
                             status=502, code="corrupt")
    if len(checked["inputs_sha256"]) != 64:
        raise PreflightError("preflight outcome has bad inputs pin",
                             status=502, code="corrupt")
    if checked["state"] == "cancelled":
        if checked["channels"] != {}:
            raise PreflightError(
                "preflight cancelled outcome must carry no channels",
                status=502, code="corrupt")
        if not checked["reason"] or not checked["code"]:
            raise PreflightError(
                "preflight cancelled outcome needs reason+code",
                status=502, code="corrupt")
    else:
        checked["channels"] = _validate_channels(
            checked["channels"], "outcome.channels")
        if checked["state"] == "verified":
            if checked["error"] or checked["code"]:
                raise PreflightError(
                    "preflight verified outcome must carry empty "
                    "error/code", status=502, code="corrupt")
            if not checked["reason"]:
                raise PreflightError(
                    "preflight verified outcome needs a reason",
                    status=502, code="corrupt")
        elif not checked["reason"] or not checked["code"]:
            raise PreflightError(
                "preflight terminal outcome needs reason+code",
                status=502, code="corrupt")
    _validate_auth_shape(checked["auth"], "outcome.auth")
    for label in ("dir", "home"):
        if not isinstance(checked[label], list) or \
                len(checked[label]) != 2:
            raise PreflightError(
                f"preflight outcome has bad {label} identity",
                status=502, code="corrupt")
    return checked


def _load_doc(home_path: str, pfid: str, kind: str,
              max_bytes: int):
    """Read + verify one owned document (no creation, no mutation)."""
    if not _valid_pfid(pfid):
        raise PreflightError(f"bad preflight id: {pfid!r}",
                             status=400, code="bad-pfid")
    leaf = f"{pfid}.{kind}.json"
    opened = _open_dir_fd(home_path, create=False)
    dfd, dev, ino, hdev, hino = opened
    try:
        raw = _read_owned_json(dfd, leaf, max_bytes)
    finally:
        os.close(dfd)
    doc = _loads_closed(raw)
    if kind == "input":
        checked = _validate_input_doc(doc)
    elif kind == "outcome":
        checked = _validate_outcome_doc(doc)
    elif kind == "cancel":
        checked = _check_closed_shape(doc, _CANCEL_SPEC, "cancel")
        if checked["schema"] != PREFLIGHT_SCHEMA or isinstance(
                checked["schema"], bool):
            raise PreflightError("preflight cancel has unknown schema",
                                 status=502, code="corrupt")
        if checked["pfid"] != pfid:
            raise PreflightError("preflight cancel mismatch",
                                 status=502, code="corrupt")
        for label in ("dir", "home"):
            ident = checked.get(label)
            if not isinstance(ident, list) or len(ident) != 2 or \
                    not all(isinstance(n, int) and not isinstance(
                        n, bool) for n in ident):
                raise PreflightError(
                    f"preflight cancel has bad {label} identity",
                    status=502, code="corrupt")
    else:
        checked = _check_closed_shape(doc, _STATUS_SPEC, "status")
        if checked["schema"] != PREFLIGHT_SCHEMA or isinstance(
                checked["schema"], bool):
            raise PreflightError("preflight status has unknown schema",
                                 status=502, code="corrupt")
        if checked["pfid"] != pfid or checked["state"] not in \
                _TERMINAL_STATES + ("created", "running"):
            raise PreflightError("preflight status mismatch",
                                 status=502, code="corrupt")
    if kind != "status":
        for label, live in (("dir", [dev, ino]),
                            ("home", [hdev, hino])):
            if list(checked.get(label) or []) != live:
                raise PreflightError(
                    f"preflight {label} identity changed for {pfid}: "
                    f"refusing a swapped or rebound store",
                    status=502, code="unreadable")
    return checked, raw


def _read_managed_source_bytes(pkg_dir_fd, name: str) -> bytes:
    """Bounded no-follow regular-file read of one managed source file.

    `name` must be exactly one entry of _MANAGED_SOURCE_FILES (never
    a path): no traversal, no symlinks, no devices/FIFOs (O_NONBLOCK
    fails fast), regular-file only, size-capped, read to EOF with a
    closing dev/ino/size/mtime/ctime identity check so an in-place
    mutation cannot pass as the pinned bytes, plus an explicit
    name-to-inode re-resolution so a rename-swap of the name onto
    another inode is also caught. Any failure raises OSError for
    the caller to mark unavailable.
    """
    if name not in _MANAGED_SOURCE_FILES:
        raise OSError(errno.EINVAL, f"bad managed name: {name!r}")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=pkg_dir_fd)
    except OSError as exc:
        raise exc
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise exc
        if not _stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL,
                          f"managed source not a regular file: {name}")
        if st.st_size < 0 or st.st_size > _MANAGED_SOURCE_MAX_BYTES:
            raise OSError(errno.EFBIG,
                          f"managed source size out of bounds: {name}")
        chunks = []
        remaining = st.st_size + 1
        while remaining > 0:
            try:
                part = os.read(fd, min(65536, remaining))
            except OSError as exc:
                raise exc
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        else:
            raise OSError(errno.EFBIG,
                          f"managed source larger than bound: {name}")
        raw = b"".join(chunks)
        try:
            closing = os.fstat(fd)
        except OSError as exc:
            raise exc
        if (closing.st_dev, closing.st_ino, closing.st_size,
                closing.st_mtime_ns, closing.st_ctime_ns) != (
                st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                st.st_ctime_ns):
            raise OSError(errno.EIO,
                          f"managed source changed during read: {name}")
        if len(raw) != st.st_size:
            raise OSError(errno.EIO,
                          f"managed source changed during read: {name}")
        try:
            _assert_leaf_identity(pkg_dir_fd, name, fd, st,
                                  "managed source")
        except PreflightError as exc:
            raise OSError(errno.EIO,
                          f"managed source replaced during read: "
                          f"{name}") from exc
        return raw
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _collect_source_identity():
    """Managed source identity: version PLUS dedicated-file SHAs.

    Reads the four dedicated-path files through bounded no-follow
    regular-file reads with closing identity (no unbounded
    read_bytes, no symlink/device reads, no size check after a full
    in-memory read); any unreadable file marks the whole source
    unavailable (fail-closed: code changed without a version bump
    must refuse). No secrets, no values beyond SHAs.
    """
    from . import muse as _muse
    try:
        from . import __version__ as _app_version
        app_version = str(_app_version)
    except Exception:  # noqa: BLE001 - version is informational only
        app_version = "unknown"
    import pathlib
    try:
        pkg = pathlib.Path(__file__).resolve().parent
    except OSError:
        return {"status": "unavailable", "app_version": app_version,
                "files": []}
    try:
        pkg_fd = os.open(str(pkg), os.O_RDONLY | os.O_DIRECTORY
                         | os.O_NOFOLLOW)
    except OSError:
        return {"status": "unavailable", "app_version": app_version,
                "files": []}
    try:
        files = []
        for name in _MANAGED_SOURCE_FILES:
            try:
                raw = _read_managed_source_bytes(pkg_fd, name)
            except OSError:
                return {"status": "unavailable",
                        "app_version": app_version, "files": []}
            files.append({"path": name,
                          "sha256": hashlib.sha256(raw).hexdigest()})
    finally:
        try:
            os.close(pkg_fd)
        except OSError:
            pass
    return {"status": "ok", "app_version": app_version, "files": files}


def _child_env_shape():
    """Child-environment binding (key sets PLUS observed values).

    Values are bound because the key set alone is not the world:
    the spawn keeps the ACTUAL full value, so a same-key change
    that swaps HOME or PATH underneath a recorded check must
    refuse rather than run in a different world. Every allowlisted
    value binds COMPLETELY — never a truncated prefix: two values
    differing only past a prefix compare equal under truncation
    and would evade drift detection while spawning differently.
    An unreadable value refuses (it must never degrade into a
    missing entry that later mismatches honestly), as does a value
    too large to pin. Cache values are workdir-derived at spawn,
    so only their names bind here; the passthrough values below
    are the live readings at record time.
    """
    from . import muse as _muse
    try:
        cache_keys = sorted(_muse.worker_cache_env("/x").keys())
    except Exception:  # noqa: BLE001 - shape is best-effort
        cache_keys = []
    names = sorted(_muse._DEDICATED_PASSTHROUGH_ENV)
    values = {}
    for name in names:
        try:
            value = os.environ.get(name)
        except Exception as exc:  # noqa: BLE001 - env unreadable
            raise PreflightError(
                f"child env value unreadable: {name}: {exc}",
                status=502, code="unreadable") from exc
        if value is None:
            continue
        if not isinstance(value, str):
            raise PreflightError(
                f"child env value not a string: {name}",
                status=502, code="unreadable")
        if len(value) > _ENV_VALUE_MAX_CHARS:
            raise PreflightError(
                f"child env value too large to bind: {name} "
                f"({len(value)} chars)",
                status=502, code="unreadable")
        values[name] = value
    return {"passthrough_keys": names, "cache_keys": cache_keys,
            "values": values}


# Slice 297 (S1+S2): observer-backed identity from a live worker.
# The slice-293 worker record is the config source of truth; the
# worker's own account observation plus its owned trace
# (credential.status) is the credential source of truth. A handle
# names a live registry entry in THIS process only: nothing on
# disk can stand in for it, and a closed or replaced worker no
# longer counts. No model turn is sent on this path.
_WORKER_IDENTITY_PROFILE = "subscription-broker"
_CRED_SOURCE_LOGIN = "subscription-login"
_CRED_TRACE_FIELDS = ("event", "provider", "source",
                        "masks_login")
_CRED_TRACE_FILES_MAX = 8
_CRED_TRACE_BYTES_MAX = 1 << 20
_CRED_VALUE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_.:/-")
_HEX_CHARS = frozenset("0123456789abcdef")


def _cred_scalar_ok(value):
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= 64:
        return False
    for char in value:
        if char not in _CRED_VALUE_CHARS:
            return False
    return True


def _trace_log_files(root):
    """Sorted owned .log paths under root/data, or (None, reason).

    Shared enumeration for the owned-trace readers: more
    than _CRED_TRACE_FILES_MAX files refuses with a named
    reason instead of silently dropping some.
    """
    if not isinstance(root, str) or not root:
        return None, "trace-root-missing"
    found = []
    try:
        for dirpath, _dirnames, filenames in os.walk(
                os.path.join(root, "data")):
            for leaf in sorted(filenames):
                if leaf.endswith(".log"):
                    found.append(os.path.join(dirpath, leaf))
                    if len(found) > _CRED_TRACE_FILES_MAX:
                        return None, "trace-too-many-files"
    except OSError:
        return None, "trace-unreadable"
    return sorted(found), None


def _read_trace_tail_lines(path):
    """All lines of a log file's tail (last
    _CRED_TRACE_BYTES_MAX bytes, dropping a possibly
    partial first line). Raises OSError on failure.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        if size > _CRED_TRACE_BYTES_MAX:
            fh.seek(size - _CRED_TRACE_BYTES_MAX)
            fh.readline()  # drop a partial first line
        else:
            fh.seek(0)
        blob = fh.read()
    return blob.decode("utf-8", errors="replace").splitlines()


def _read_worker_credential_trace(root):
    """Latest meta credential.status from the worker's OWNED trace.

    Returns ({"source", "masks_login"}, None) or (None, reason).
    Only the two enum scalars are ever kept; raw lines are
    never retained. Only provider=meta lines count: another
    provider's line must never decide this worker's result.
    The newest matching line wins: it names the current
    credential state, not history, so each file is read from
    the tail. More than _CRED_TRACE_FILES_MAX files refuses
    with a named reason instead of silently dropping some.
    """
    found, reason = _trace_log_files(root)
    if found is None:
        return None, reason
    latest = None
    try:
        for path in found:
            for line in _read_trace_tail_lines(path):
                if "credential.status" not in line:
                    continue
                fields = {}
                for token in line.split():
                    key, sep, value = token.partition("=")
                    if not sep \
                            or key not in _CRED_TRACE_FIELDS:
                        continue
                    if len(value) >= 2 and value[:1] == '"' \
                            and value[-1:] == '"':
                        value = value[1:-1]
                    if _cred_scalar_ok(value):
                        fields[key] = value
                if fields.get("event") == "credential.status" \
                        and fields.get("provider") == "meta" \
                        and "source" in fields \
                        and "masks_login" in fields:
                    latest = {"source": fields["source"],
                              "masks_login":
                                  fields["masks_login"]}
    except OSError:
        return None, "trace-unreadable"
    if latest is None:
        return None, "credential-status-missing"
    return latest, None


def _read_worker_transport_trace(root):
    """Proxy/direct transport marks from the worker's OWNED trace.

    Returns ({"proxy": n, "direct": m}, None) or (None,
    reason). Only endpoint_transport.resolve and
    provider_transport.build lines count, and of each
    only the exact transport value "proxy" or "direct":
    counts only, raw lines and every other value are
    never retained. Zero lines is a zero count, not a
    missing root: the caller decides what zero proves.
    """
    found, reason = _trace_log_files(root)
    if found is None:
        return None, reason
    counts = {"proxy": 0, "direct": 0}
    try:
        for path in found:
            for line in _read_trace_tail_lines(path):
                if "endpoint_transport.resolve" not in line \
                        and "provider_transport.build" \
                        not in line:
                    continue
                for token in line.split():
                    key, sep, value = token.partition("=")
                    if not sep or key != "transport":
                        continue
                    if len(value) >= 2 and value[:1] == '"' \
                            and value[-1:] == '"':
                        value = value[1:-1]
                    if value == "proxy":
                        counts["proxy"] += 1
                    elif value == "direct":
                        counts["direct"] += 1
    except OSError:
        return None, "trace-unreadable"
    return counts, None


def _observe_worker_identity(handle):
    """(config, auth) proved by one live worker, or unestablished.

    Every runtime failure degrades to status "unestablished"
    (config) and source "unknown" (auth) with the worker's
    recorded reason. Only a non-string handle raises, as a
    caller bug: it can never name a live worker.
    """
    from . import muse as _muse
    if not isinstance(handle, str) or not handle:
        raise ValueError(
            "worker handle must be a non-empty str")
    key_present = "META_API_KEY" in os.environ
    contrib_present = "MUSE_CONTRIBUTOR_MODE" in os.environ

    def _unknown_config(reason):
        return {"status": "unestablished", "files": [],
                "note": str(reason)[:500]}

    def _unknown_auth(how):
        return {"meta_api_key_present": key_present,
                "contributor_env_present": contrib_present,
                "credential_source": "unknown",
                "billing_evidence": None,
                "observed_how": str(how)[:200]}

    rec = _muse._MSP_WORKERS.get(handle)
    if rec is None or rec.get("closed"):
        return (_unknown_config(
                    "worker gone: no live handle %s" % handle),
                _unknown_auth("worker gone: no live handle"))
    broken = rec.get("broken")
    if broken:
        return (_unknown_config(
                    "worker broken: %s" % (broken,)),
                _unknown_auth("worker broken"))
    proc = rec.get("proc")
    if proc is not None and proc.poll() is not None:
        return (_unknown_config("worker child exited"),
                _unknown_auth("worker child exited"))
    try:
        _muse._worker_check_owner(rec)
    except _muse.MspError as exc:
        return (_unknown_config(
                    "worker owner check failed: %s"
                    % (exc.code or "owner-mismatch",)),
                _unknown_auth("worker owner mismatch"))
    profile = rec.get("profile")
    if profile != _WORKER_IDENTITY_PROFILE:
        return (_unknown_config(
                    "worker profile %s: need %s"
                    % (profile, _WORKER_IDENTITY_PROFILE)),
                _unknown_auth("worker profile %s" % (profile,)))
    # Re-observe the worker itself, never just the record: a
    # config that changed after the last read must not read
    # as established. A local read only; no model turn.
    try:
        _muse.msp_worker_settings_read(handle, timeout=10.0)
    except _muse.MspError as exc:
        return (_unknown_config(
                    "worker settings-read failed: %s"
                    % (exc.code or "error",)),
                _unknown_auth("worker settings-read failed"))
    if rec.get("config_established") is not True:
        reason = rec.get("config_unset_reason") \
            or "config not established"
        return (_unknown_config(reason),
                _unknown_auth("config not established"))
    identity = rec.get("config_identity") or {}
    live_pid = rec.get("child_pid")
    if identity.get("pid") != live_pid:
        return (_unknown_config(
                    "worker pid changed: identity pid %r, "
                    "live pid %r"
                    % (identity.get("pid"), live_pid)),
                _unknown_auth("worker pid changed"))
    sha = identity.get("sha256")
    provider = identity.get("provider")
    model = identity.get("model")
    contributor = identity.get("contributor")
    sha_ok = isinstance(sha, str) and len(sha) == 64 \
        and all(char in _HEX_CHARS for char in sha)
    if not (sha_ok and isinstance(provider, str)
            and isinstance(model, str)
            and contributor is True):
        return (_unknown_config("worker identity incomplete"),
                _unknown_auth("worker identity incomplete"))
    config = {
        "status": "established", "files": [],
        "note": ("worker-proved %s config: handle %s pid %s "
                 "sha %s" % (profile, handle, live_pid,
                             sha))[:500],
        "worker": {"handle": handle, "pid": live_pid,
                   "sha256": sha, "provider": provider,
                   "model": model, "contributor": True},
    }
    account = rec.get("worker_account")
    if not isinstance(account, dict):
        return config, _unknown_auth("account unobserved")
    lane, _note = _muse.msp_credential_lane(account)
    if key_present or lane in ("apiKey", "envKey"):
        return config, _unknown_auth(
            "caller key takes precedence: env key=%s "
            "lane=%s" % (key_present, lane))
    if lane != "accountLogin-candidate":
        return config, _unknown_auth(
            "account lane %s: login unobserved" % (lane,))
    cred, cred_reason = _read_worker_credential_trace(
        rec.get("root"))
    if cred is None:
        return config, _unknown_auth(
            "trace credential.status %s" % (cred_reason,))
    if cred["source"] != "login":
        return config, _unknown_auth(
            "trace credential source %s" % (cred["source"],))
    if cred["masks_login"] != "false":
        return config, _unknown_auth(
            "trace credential masks_login true")
    auth = {
        "meta_api_key_present": key_present,
        "contributor_env_present": contrib_present,
        "credential_source": _CRED_SOURCE_LOGIN,
        "billing_evidence": None,
        "observed_how": (
            "worker %s pid %s: accountLogin/"
            "accountLogin-candidate; trace credential.status "
            "source=login masks_login=false; billing not "
            "observed" % (handle, live_pid))[:200],
    }
    return config, auth


def current_identity(worker_handle=None):
    """Snapshot the live world the dedicated profile would run in.

    Returns {"binary", "policy", "config", "source", "env", "auth"}.
    Binary is re-resolved + re-hashed on every call (unavailable →
    explicit status, never a silent skip). Config carries an
    explicit "unestablished" status until a later gate pins the real
    surface — no guessed filenames. Source binds the package version
    PLUS the managed dedicated-file SHAs (a version string alone
    cannot prove the running code). Env binds the narrowed child-env
    key shape PLUS the complete observed values (never truncated
    prefixes: the spawn keeps full values, so the record must too).
    Auth reports META_API_KEY PRESENCE (even empty-valued: a stored
    key takes priority over login) plus an "unknown" credential
    source: absence is necessary but not sufficient, and values are
    never logged. Offline tests mock this whole function;
    production has no fixture path.

    With worker_handle None (the only production path today),
    config/auth stay hardcoded unestablished/unknown, exactly as
    before. With a live worker handle, both legs are observed
    from that same worker; anything unproved stays unestablished
    with the worker's recorded reason. No production caller
    passes a handle yet: the spawn-and-bind lifecycle is a
    later slice.
    """
    from . import muse as _muse
    try:
        pin = _muse.resolve_pinned_muse()
        binary = {"status": "ok", "path": pin["path"],
                  "real_path": pin["real_path"], "sha256": pin["sha256"]}
    except OSError as exc:
        binary = {"status": "unavailable", "error": str(exc)[:300],
                  "path": "", "real_path": "", "sha256": ""}
    if worker_handle is None:
        config = {"status": "unestablished", "files": [],
                  "note": "config surface not yet pinned; target-A "
                          "refused until a later gate establishes it"}
        auth = {"meta_api_key_present": "META_API_KEY" in os.environ,
                "contributor_env_present":
                    "MUSE_CONTRIBUTOR_MODE" in os.environ,
                "credential_source": "unknown",
                "billing_evidence": None,
                "observed_how": "app env presence check only; no "
                                "session proof observed"}
    else:
        config, auth = _observe_worker_identity(worker_handle)
    return {
        "binary": binary,
        "policy": dict(_muse.SOURCE_REPAIR_PROFILE),
        "config": config,
        "source": _collect_source_identity(),
        "env": _child_env_shape(),
        "auth": auth,
    }


def evaluate_outcome(inputs: dict, channels: dict, auth: dict):
    """Pure authority verdict over recorded inputs/observations.

    Returns (state, reason, code) with state "verified", "unknown",
    or "refused". No I/O, no model contact: every unproved channel
    and every unestablished identity is UNKNOWN/refused, never
    success. Trust is structural — ONLY observations produced by the
    admitted channel observer count (the real observer in
    production, an explicit fake in offline tests); callers, model
    output, and env can never supply trusted facts. A
    credential_source of "unknown" (or a META_API_KEY presence)
    refuses: the supported trusted auth-source mechanism is still
    unproved, which stays an explicit block — never simulated
    success.
    """
    if not isinstance(inputs, dict) or inputs.get("policy") != \
            REQUIRED_POLICY:
        return ("refused",
                "preflight policy is not the dedicated source-repair "
                "profile (meta/muse-spark-1.3/contributor-ON/restricted "
                "network, 25 steps, 200000 tool-output bytes); "
                "re-create the preflight under that profile. "
                "다음: 전용 프로필로 프리플라이트를 다시 만드세요.",
                "bad-policy")
    config = inputs.get("config") if isinstance(inputs, dict) else None
    if not isinstance(config, dict) or config.get("status") != \
            "established":
        return ("refused",
                "preflight config surface is unestablished: no config "
                "identity to match, so target-A stays blocked. 다음: "
                "설정 표면이 확정될 때까지 대기하세요.",
                "config-unestablished")
    if not isinstance(auth, dict):
        return ("refused", "preflight auth observation missing. 다음: "
                            "인증 관측을 다시 수집하세요.", "auth-unknown")
    if auth.get("meta_api_key_present") is True:
        return ("refused",
                "META_API_KEY is present and would take priority over "
                "subscription login: unset it for subscription-only "
                "runs; nothing spawned. 다음: 키를 제거하세요.",
                "api-key-present")
    if not isinstance(auth.get("credential_source"), str) or \
            auth.get("credential_source") in ("unknown", ""):
        return ("refused",
                "no supported trusted credential source observed "
                "(subscription session UNVERIFIED): target-A refused "
                "until the auth path is proved by the admitted "
                "observer. 다음: 인증 경로가 확정될 때까지 대기하세요.",
                "auth-unestablished")
    if not isinstance(channels, dict):
        return ("unknown", "preflight channel observations missing. "
                           "다음: 격리 점검을 다시 실행하세요.",
                "channels-missing")
    for name in REQUIRED_CHANNELS:
        entry = channels.get(name)
        if not isinstance(entry, dict):
            return ("unknown",
                    f"preflight channel {name} has no observation. "
                    f"다음: {name} 채널 점검을 다시 실행하세요.",
                    "channel-unknown")
        if entry.get("required") != REQUIRED_DIRECTION[name]:
            return ("refused",
                    f"preflight channel {name} requires an unexpected "
                    f"direction; refusing a drifted contract. 다음: "
                    f"프리플라이트를 다시 만드세요.", "bad-channel")
        if entry.get("observed") != REQUIRED_DIRECTION[name] or \
                entry.get("positive") is not True:
            return ("unknown",
                    f"preflight channel {name} unproved "
                    f"(observed={entry.get('observed')}, "
                    f"positive={entry.get('positive')}): positive "
                    f"controls must precede the negative reading. "
                    f"다음: {name} 채널의 양성 대조를 먼저 확보하세요.",
                    "channel-unknown")
    return ("verified",
            "preflight verified: pinned binary, dedicated policy, "
            "config/source/auth identity, and every required channel "
            "confirmed by positive-before-negative controls. "
            "다음: 소스 작업을 시작할 수 있습니다.",
            "")


def _scan_pfids(home_path: str):
    """List owned preflight ids (bounded, read-only).

    Returns (pfids, complete): `complete` is False when the scan
    hit the entry cap before the directory was exhausted — the
    returned subset is an ARBITRARY readdir slice, never the newest
    subset, so authority paths must withhold (never relabel) on a
    truncated scan. Selection among scanned ids is by recorded
    time (_order_pfids_by_time / latest_verified), never by
    readdir order; the cap is a DoS bound only.

    A missing layout reads as ([], True) (legitimate absence); a
    directory whose listing itself fails raises unreadable — an
    independent openability precheck cannot fix a failed readdir,
    and a failed listing must never masquerade as an empty
    history. The listing streams via scandir (never a fully
    materialized listdir), and the dir fd closes EXACTLY once in
    the single finally — never in an inner error branch, where a
    second close under concurrent fd reuse could hit another
    request's resource.
    """
    try:
        opened = _open_dir_fd(home_path, create=False)
    except PreflightError as exc:
        if exc.code == "missing":
            return [], True
        raise
    dfd = opened[0]
    try:
        with os.scandir(dfd) as iterator:
            out = []
            for entry in iterator:
                name = entry.name
                if not isinstance(name, str):
                    continue
                if name.endswith(".status.json") and _valid_leaf(
                        name):
                    out.append(name[:-len(".status.json")])
                    if len(out) > PREFLIGHT_SCAN_CAP:
                        return out, False
            return out, True
    except OSError as exc:
        raise PreflightError(
            f"preflight history listing failed: {exc}",
            status=502, code="unreadable") from exc
    finally:
        try:
            os.close(dfd)
        except OSError:
            pass


def _live_states():
    return ("created", "running")


def _same_world(recorded: dict, live: dict) -> bool:
    """True when a recorded input still describes the live world.

    Dedupe by dedupe_key alone would reuse historical proof after a
    binary/config/source/env/auth change. Verified reuse requires the
    FULL recorded identity (pinned path+SHA, exact config/source
    files, env shape, auth facts, policy) to equal the live reading;
    any drift starts a fresh check instead of relabeling old proof.
    """
    if not isinstance(recorded, dict) or not isinstance(live, dict):
        return False
    rec_bin = recorded.get("binary") or {}
    live_bin = live.get("binary") or {}
    for key in ("path", "real_path", "sha256"):
        if str(rec_bin.get(key) or "") != str(live_bin.get(key) or ""):
            return False
    rec_cfg = recorded.get("config") or {}
    live_cfg = live.get("config") or {}
    if str(rec_cfg.get("status") or "") != str(
            live_cfg.get("status") or ""):
        return False
    if list(rec_cfg.get("files") or []) != list(
            live_cfg.get("files") or []):
        return False
    # Same-alive worker binding (slice 297): like the legacy
    # contributor key above, absent != present. A replaced
    # worker (handle, pid, or served-settings sha differs)
    # is a new world, never relabeled proof.
    rec_worker = rec_cfg.get("worker")
    live_worker = live_cfg.get("worker")
    if (rec_worker is None) != (live_worker is None):
        return False
    if rec_worker is not None:
        if not isinstance(rec_worker, dict) \
                or not isinstance(live_worker, dict):
            return False
        for key in ("handle", "pid", "sha256"):
            if rec_worker.get(key) != live_worker.get(key):
                return False
    rec_src = recorded.get("source") or {}
    live_src = live.get("source") or {}
    if str(rec_src.get("app_version") or "") != str(
            live_src.get("app_version") or ""):
        return False
    if list(rec_src.get("files") or []) != list(
            live_src.get("files") or []):
        return False
    rec_env = recorded.get("env") or {}
    live_env = live.get("env") or {}
    for key in ("passthrough_keys", "cache_keys"):
        if sorted(rec_env.get(key) or []) != sorted(
                live_env.get(key) or []):
            return False
    if dict(rec_env.get("values") or {}) != dict(
            live_env.get("values") or {}):
        return False
    rec_auth = recorded.get("auth_observed") or {}
    live_auth = live.get("auth") or {}
    if bool(rec_auth.get("meta_api_key_present")) != bool(
            live_auth.get("meta_api_key_present")):
        return False
    # Legacy records (key absent) are NEVER the same world as a
    # live identity: absent != False. They start a fresh check,
    # never relabel as proof.
    if ("contributor_env_present" in rec_auth) != (
            "contributor_env_present" in live_auth):
        return False
    if "contributor_env_present" in rec_auth and bool(
            rec_auth["contributor_env_present"]) != bool(
                live_auth["contributor_env_present"]):
        return False
    if str(rec_auth.get("credential_source") or "") != str(
            live_auth.get("credential_source") or ""):
        return False
    if dict(recorded.get("policy") or {}) != dict(REQUIRED_POLICY):
        return False
    return True


def create_preflight(home=None, dedupe_key: str = "") -> dict:
    """Record one preflight request (explicit UI action only).

    Snapshots current_identity() into an immutable input doc and a
    mutable created status. A live receipt with the same dedupe_key
    attaches instead of duplicating ({reused True}); a terminal
    verified receipt with the same key is also reused. Terminal-bad
    keys start a new pfid on explicit re-creation. Concurrent
    creators with the same key serialize on a per-key lock (the
    loser reuses or fails fast busy) instead of publishing
    duplicates. No model contact,
    no target mutation. Offline tests mock current_identity; the
    store/compare path below is never mocked.
    """
    home_path = _resolve_home(home)
    if not isinstance(dedupe_key, str) or not dedupe_key or len(
            dedupe_key) > 128:
        raise PreflightError("dedupe_key must be a non-empty string "
                             "(<=128 chars)", status=400,
                             code="bad-dedupe-key")
    # Per-key create lock (non-blocking): two concurrent creators
    # with the same dedupe_key serialize instead of publishing
    # duplicate receipts; the loser reuses or fails fast busy.
    # Lock name derives from the key hash (never the raw key).
    # create=True: the explicit first create on a fresh owned home
    # may establish the validated owned layout (missing dir only —
    # a symlinked/unreadable existing identity still refuses and
    # is never healed). Read/run/cancel paths never create.
    create_lock = _acquire_nb_lock(
        home_path, "dl-" + hashlib.sha256(
            dedupe_key.encode("utf-8")).hexdigest()[:16] + ".lock",
        create=True)
    try:
        return _create_preflight_locked(home_path, dedupe_key)
    finally:
        _release_nb_lock(create_lock)


def _history_class(home_path: str, pfids: list) -> str:
    """Classify a no-verified-receipt history: readable/tamper/none.

    "readable" — at least one pfid has a parseable status and every
    present outcome/input doc parses too (created receipts with no
    outcome yet count as readable, never as missing). "tamper" — a
    readable status with a present-but-invalid outcome, or a valid
    outcome with a missing/invalid input: evidence was altered or
    partially deleted after the write. "none" — nothing parses.
    Outcome/input reads are dir-bound (_load_doc), so a swapped-in
    foreign history classifies as tamper/unreadable, never as a
    clean readable slate. Pure helper: no writes, no healing.
    """
    seen_readable = False
    for pfid in pfids:
        try:
            _load_doc(home_path, pfid, "status",
                      PREFLIGHT_STATUS_MAX_BYTES)
        except PreflightError:
            continue
        try:
            _load_doc(home_path, pfid, "outcome",
                      PREFLIGHT_OUTCOME_MAX_BYTES)
        except PreflightError as exc:
            if exc.code == "missing":
                seen_readable = True
                continue
            return "tamper"
        try:
            _load_doc(home_path, pfid, "input",
                      PREFLIGHT_INPUT_MAX_BYTES)
        except PreflightError:
            return "tamper"
        seen_readable = True
    return "readable" if seen_readable else "none"


def _create_preflight_locked(home_path: str, dedupe_key: str) -> dict:
    """Create body: scan-then-publish runs under the per-key lock."""
    current = current_identity()
    pfids, complete = _scan_pfids(home_path)
    if not complete:
        # A truncated scan cannot prove no same-key receipt hides
        # beyond the cap: publishing here could duplicate. Refuse
        # instead of relabeling partial knowledge.
        raise PreflightError(
            "preflight history truncated at scan cap; cannot prove "
            "dedupe uniqueness. 다음: 기록 폴더를 확인하세요.",
            status=502, code="history-truncated")
    for pfid in pfids:
        try:
            status, _raw = _load_doc(home_path, pfid, "status",
                                     PREFLIGHT_STATUS_MAX_BYTES)
        except PreflightError:
            continue
        try:
            inputs, _raw2 = _load_doc(home_path, pfid, "input",
                                      PREFLIGHT_INPUT_MAX_BYTES)
        except PreflightError:
            continue
        if inputs.get("dedupe_key") != dedupe_key:
            continue
        if status.get("state") in _live_states():
            return {"pfid": pfid, "reused": True, "state": "live"}
        if status.get("state") == "verified":
            # Verified reuse is identity-bound, not key-bound: the
            # recorded world must still equal the live world, or a
            # config/source change would relabel historical proof.
            if _same_world(inputs, current):
                return {"pfid": pfid, "reused": True,
                        "state": "verified"}
            continue
    pfid = "pf-" + secrets.token_hex(8)
    opened = _open_dir_fd(home_path, create=True)
    dfd, dev, ino, hdev, hino = opened
    try:
        binary = current.get("binary") or {}
        config = current.get("config") or {}
        source = current.get("source") or {}
        env = current.get("env") or {}
        auth = current.get("auth") or {}
        source_files = source.get("files") if isinstance(source, dict) \
            else None
        inputs = {
            "schema": PREFLIGHT_SCHEMA,
            "pfid": pfid,
            "created_at": _utcnow(),
            "dedupe_key": dedupe_key,
            "policy": dict(REQUIRED_POLICY),
            "binary": {"status": str(binary.get("status") or ""),
                       "path": str(binary.get("path") or ""),
                       "real_path": str(binary.get("real_path") or ""),
                       "sha256": str(binary.get("sha256") or "")},
            "config": {"status": str(config.get("status") or ""),
                       "files": list(config.get("files") or [])},
            "source": {"app_version": str(source.get("app_version")
                                          or "unknown"),
                       "files": list(source_files or [])},
            "env": {"passthrough_keys": list(
                env.get("passthrough_keys") or []),
                "cache_keys": list(env.get("cache_keys") or []),
                "values": {str(k): str(v)
                           for k, v in (env.get("values") or {}).items()
                           if isinstance(k, str)
                           and isinstance(v, str)}},
            "auth_observed": {
                "meta_api_key_present": bool(
                    auth.get("meta_api_key_present")),
                "contributor_env_present": bool(
                    auth.get("contributor_env_present")),
                "credential_source": str(auth.get("credential_source")
                                         or "unknown"),
                "billing_evidence": None,
                "observed_how": str(auth.get("observed_how") or "")},
            "dir": [dev, ino],
            "home": [hdev, hino],
        }
        # Same-alive binding (slice 297): the record names the
        # exact worker the config was proved on. A present
        # "worker" key is always projected and validated
        # below, so a malformed binding refuses the create
        # instead of silently falling back to legacy.
        if "worker" in config:
            worker = config["worker"]
            if isinstance(worker, dict):
                inputs["config"]["worker"] = {
                    "handle": str(worker.get("handle") or ""),
                    "pid": worker.get("pid"),
                    "sha256": str(worker.get("sha256") or ""),
                    "provider": str(
                        worker.get("provider") or ""),
                    "model": str(worker.get("model") or ""),
                    "contributor": bool(
                        worker.get("contributor")),
                }
            else:
                inputs["config"]["worker"] = worker
        _validate_input_doc(inputs)
        _write_owned_json(dfd, f"{pfid}.input.json", inputs,
                          immutable=True,
                          max_bytes=PREFLIGHT_INPUT_MAX_BYTES)
        status = {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
                  "state": "created", "updated_at": _utcnow()}
        _write_owned_json(dfd, f"{pfid}.status.json", status,
                          immutable=False,
                          max_bytes=PREFLIGHT_STATUS_MAX_BYTES)
    finally:
        os.close(dfd)
    return {"pfid": pfid, "reused": False, "state": "created"}


_CLAIM_MAX_BYTES = 4096
_SPAWN_MAX_BYTES = 8192

_CANCEL_SPEC = {
    "schema": ("int", 0),
    "pfid": ("str", 64),
    "at": ("str", 64),
    "home": ("list", 0),
    "dir": ("list", 0),
}

_CLAIM_SPEC = {
    "schema": ("int", 0),
    "pfid": ("str", 64),
    "pid": ("int", 0),
    "token": ("str", 64),
    "at": ("str", 64),
}


def _pid_alive(pid: int) -> bool:
    """True when a pid exists (fail-closed: errors mean alive)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _valid_lock_leaf(leaf) -> bool:
    global _LOCK_LEAF_RE
    if _LOCK_LEAF_RE is None:
        import re
        _LOCK_LEAF_RE = re.compile(
            r"^(pf-[0-9a-f]{16}\.lock|dl-[0-9a-f]{16}\.lock)$")
    return isinstance(leaf, str) and _LOCK_LEAF_RE.fullmatch(
        leaf) is not None


def _acquire_nb_lock(home_path: str, leaf: str, *, create: bool = False):
    """Non-blocking exclusive lock for one critical section.

    Returns a held fd (close to release), or raises 409 busy when
    a live holder owns it. `create` mirrors _open_dir_fd: the
    explicit first-create path may establish the validated owned
    layout (missing dir only — a symlinked/unreadable existing
    identity still refuses and is never healed); every read/run/
    cancel path passes create=False. A platform without flock
    REFUSES instead of proceeding lockless: lockless progress
    cannot honestly claim serialization. The leaf opens
    O_NOFOLLOW (a planted lock symlink refuses, never resolves
    outside the owned dir), must be a regular file, and is
    re-verified against the live name AFTER the flock succeeds:
    a name swapped mid-acquisition releases and refuses instead
    of letting two holders believe they exclude each other.
    Only our own preflight lock leaves are ever locked; a
    suspicious leaf is never cleaned up or healed. The kernel
    releases a dead holder's lock automatically, so recovery can
    never delete a newer live owner's state to proceed — unlike
    a pid file, which needs reaping.
    """
    if not _valid_lock_leaf(leaf):
        raise PreflightError(f"bad preflight lock name: {leaf!r}",
                             status=500, code="bad-leaf")
    if _fcntl is None:
        raise PreflightError(
            "file locking unavailable on this platform; refusing "
            "rather than serializing lockless",
            status=502, code="unreadable")
    opened = _open_dir_fd(home_path, create=create)
    dfd = opened[0]
    try:
        try:
            # Exclusive create first, plain nofollow open second.
            # A single O_CREAT|O_NOFOLLOW open loses concurrent
            # first-creates on some kernels (Darwin hands every
            # racer but the winner ENOENT instead of the winner's
            # file), which misreported a healthy race as
            # 'unreadable'. O_EXCL|O_CREAT serializes deterministically
            # (one creator, the rest EEXIST) without ever following a
            # name; the follow-up open keeps O_NOFOLLOW, so a planted
            # symlink still refuses ELOOP below. No extra retries,
            # no sleeps, no behavior change on any other errno.
            cfd = os.open(leaf,
                          os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                          0o600, dir_fd=dfd)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise PreflightError(
                    f"preflight lock unavailable: {leaf}: {exc}",
                    status=502, code="unreadable") from exc
            cfd = None
        else:
            try:
                os.close(cfd)
            except OSError:
                pass
        try:
            fd = os.open(leaf, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dfd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PreflightError(
                    f"preflight lock is a symlink, refusing: {leaf}",
                    status=502, code="unreadable") from exc
            raise PreflightError(
                f"preflight lock unavailable: {leaf}: {exc}",
                status=502, code="unreadable") from exc
        try:
            st = os.fstat(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise PreflightError(
                f"preflight lock stat failed: {leaf}: {exc}",
                status=502, code="unreadable") from exc
        if not _stat.S_ISREG(st.st_mode):
            try:
                os.close(fd)
            except OSError:
                pass
            raise PreflightError(
                f"preflight lock not a regular file: {leaf}",
                status=502, code="unreadable")
    except BaseException:
        try:
            os.close(dfd)
        except OSError:
            pass
        raise
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.close(dfd)
        except OSError:
            pass
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise PreflightError(
                "another live run holds this check; nothing "
                "duplicated. 다음: 끝난 뒤 다시 시도하세요.",
                status=409, code="busy") from exc
        raise PreflightError(f"lock failed: {exc}", status=502,
                             code="unreadable") from exc
    try:
        _assert_leaf_identity(dfd, leaf, fd, st, "preflight lock")
    except BaseException:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.close(dfd)
        except OSError:
            pass
        raise
    try:
        os.close(dfd)
    except OSError:
        pass
    return fd


def _release_nb_lock(fd) -> None:
    """Release a lock from _acquire_nb_lock (best-effort)."""
    if fd is None:
        return
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _busy_refusal(pfid: str) -> PreflightError:
    return PreflightError(
        f"preflight {pfid} probe already running; nothing "
        f"duplicated. 다음: 끝난 뒤 다시 시도하세요.",
        status=409, code="busy")


def _publish_claim(home_path: str, pfid: str) -> str:
    """Atomically claim one probe run (no duplicate spawn).

    Publishes <pfid>.claim.json no-overwrite with our pid+token. A
    conflicting claim whose owner pid is still alive refuses 409
    busy — two clicks never spawn twice. A dead owner is reaped
    only after RE-VERIFYING the exact dead bytes immediately before
    the unlink, inside a bounded retry loop: a concurrent recoverer
    that replaced the dead claim with a live one in the meantime is
    detected (bytes changed / unlink lost the race) and we
    re-evaluate instead of deleting its live claim. Malformed or
    unreadable claims refuse busy immediately and are never
    touched. Returns our claim token (release checks it).
    """
    token = secrets.token_hex(16)
    try:
        pid = os.getpid()
    except OSError:
        pid = 0
    claim = {"schema": PREFLIGHT_SCHEMA, "pfid": pfid, "pid": pid,
             "token": token, "at": _utcnow()}
    last_busy = None
    for _ in range(4):
        opened = _open_dir_fd(home_path, create=False)
        dfd = opened[0]
        try:
            try:
                _write_owned_json(dfd, f"{pfid}.claim.json", claim,
                                  immutable=True,
                                  max_bytes=_CLAIM_MAX_BYTES)
                return token
            except PreflightError as exc:
                if exc.code != "exists-different":
                    raise
            try:
                raw = _read_owned_json(dfd, f"{pfid}.claim.json",
                                       _CLAIM_MAX_BYTES)
            except PreflightError as exc:
                last_busy = _busy_refusal(pfid)
                continue
            try:
                other = _loads_closed(raw)
                other = _check_closed_shape(other, _CLAIM_SPEC,
                                            "claim")
            except PreflightError:
                # Malformed claims never heal: refuse without
                # touching what we cannot prove dead.
                raise _busy_refusal(pfid)
            owner = other.get("pid")
            if not (isinstance(owner, int) and owner > 0
                    and not _pid_alive(owner)):
                raise _busy_refusal(pfid)
            # Re-verify the EXACT dead bytes immediately before the
            # unlink: a concurrent recoverer may have replaced them
            # with a live claim since our read. Any change (or a
            # lost unlink race) restarts the loop instead of
            # deleting a newer live owner's claim.
            try:
                raw2 = _read_owned_json(dfd, f"{pfid}.claim.json",
                                        _CLAIM_MAX_BYTES)
            except PreflightError as exc:
                last_busy = _busy_refusal(pfid)
                continue
            if raw2 != raw:
                last_busy = _busy_refusal(pfid)
                continue
            try:
                os.unlink(f"{pfid}.claim.json", dir_fd=dfd)
            except OSError:
                last_busy = _busy_refusal(pfid)
                continue
            # Unlinked our verified-dead bytes; loop retries the
            # write. A racing writer's file survives untouched: our
            # retry either wins on an empty name or sees its live
            # claim and refuses busy.
            last_busy = _busy_refusal(pfid)
            continue
        finally:
            try:
                os.close(dfd)
            except OSError:
                pass
    raise last_busy if last_busy is not None else _busy_refusal(pfid)


def _release_claim(home_path: str, pfid: str, token: str) -> None:
    """Release our own run claim (token-match, best-effort)."""
    try:
        opened = _open_dir_fd(home_path, create=False)
    except PreflightError:
        return
    dfd = opened[0]
    try:
        try:
            raw = _read_owned_json(dfd, f"{pfid}.claim.json",
                                   _CLAIM_MAX_BYTES)
        except PreflightError:
            return
        try:
            mine = _loads_closed(raw).get("token")
        except PreflightError:
            return
        if mine == token:
            try:
                os.unlink(f"{pfid}.claim.json", dir_fd=dfd)
            except OSError:
                pass
    finally:
        os.close(dfd)


def _cancel_intent(home_path: str, pfid: str) -> bool:
    """True when an explicit cancel intent was recorded.

    Uses the same typed cancel read as every other consumer
    (schema/pfid/home/dir validated). A MISSING flag is absence
    (False); a PRESENT-but-corrupt/unreadable flag RAISES instead
    of silently degrading to absence — proceeding to spawn on top
    of tampered cancel evidence would ignore a possible operator
    cancel. Fail closed, never heal.
    """
    try:
        _load_doc(home_path, pfid, "cancel", _CLAIM_MAX_BYTES)
    except PreflightError as exc:
        if exc.code == "missing":
            return False
        raise PreflightError(
            f"cancel flag unreadable ({exc.code}); refusing rather "
            f"than risking an ignored cancel",
            status=502, code="corrupt") from exc
    return True


def _unknown_channels(detail: str) -> dict:
    """Full channel set, all unknown (probe/observer failure shape)."""
    out = {}
    for name in REQUIRED_CHANNELS:
        out[name] = {"required": REQUIRED_DIRECTION[name],
                     "observed": "unknown", "positive": False,
                     "detail": detail[:PREFLIGHT_DETAIL_MAX_CHARS]}
    return out


def _collect_observations(home_path: str, pfid: str, inputs: dict,
                          transport=None, timeout: int = PREFLIGHT_TIMEOUT_S,
                          cancel_event=None):
    """Gather spawn evidence (spawn boundary, NOT a channel verdict).

    The cancel event is a live threading.Event set by the run's
    cancel watcher (never None in run_preflight); the real exec
    polls it and kills its owned group, so mid-probe cancellation
    stops the actual child instead of only flipping a status file.
    Fake transports receive the same live event object.

    `transport` is an offline-test seam with the SAME tuple shape as
    muse.exec_source_repair(prompt, workdir, timeout, env,
    cancel_event): tests pass a fake; production passes None and the
    real dedicated spawn runs in a fresh owned workspace — admitted
    by stage0 (not by any receipt: this IS the first probe). Without
    a proved stage0 admission this refuses with NO spawn; the refusal
    is returned as evidence, never raised. Returns (spawn, error):
    spawn is {"exit_code", "output_bytes", "usage"} for the admitted
    observer to interpret — rc0 NEVER confirms a channel by itself.
    """
    from . import muse as _muse
    workdir = os.path.join(home_path, PREFLIGHT_DIRNAME,
                           f"{pfid}.work")
    prompt = (
        "Owned capability probe (no target data): reply with one JSON "
        "line {\"probe\": \"preflight\", \"pfid\": \"" + pfid + "\"}. "
        "Do not read, write, or describe any repository.")
    env = {"OPENMAINTY_PREFLIGHT_PROBE": pfid}
    try:
        os.makedirs(workdir, mode=0o700, exist_ok=False)
    except OSError as exc:
        return None, {"error": f"probe workspace unavailable: {exc}"}
    try:
        if transport is not None:
            exit_code, out, usage = transport(
                prompt, workdir, timeout, env, cancel_event)
        else:
            # Owned probe: same home the receipt lives in, trace
            # enabled ONLY for this nonsensitive fixture (never for
            # target jobs). Stage0 admission (trusted auth + native
            # containment) is REQUIRED: without it this refuses with
            # NO spawn — a truthful unknown, never a success. The
            # cancel event reaches the real child: exec polls it and
            # kills its owned group on cancel.
            exit_code, out, usage = _muse.exec_source_repair(
                prompt, workdir, timeout=timeout, env=env,
                cancel_event=cancel_event,
                preflight_home=home_path, admission_kind="stage0",
                trace=True)
    except Exception as exc:  # noqa: BLE001 - observation, not control
        return None, {"error": f"probe spawn failed: {exc}"}
    if not isinstance(usage, dict):
        usage = {"raw_usage": usage}
    spawn = {
        "exit_code": exit_code,
        "output_bytes": len(out.encode("utf-8")) if isinstance(
            out, str) else 0,
        "usage": usage,
    }
    return spawn, None


def _record_spawn_evidence(home_path: str, pfid: str, spawn) -> None:
    """Best-effort immutable spawn evidence (never validity)."""
    if not isinstance(spawn, dict):
        return
    try:
        opened = _open_dir_fd(home_path, create=False)
    except PreflightError:
        return
    dfd = opened[0]
    try:
        doc = {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
               "exit_code": spawn.get("exit_code"),
               "output_bytes": spawn.get("output_bytes"),
               "at": _utcnow()}
        if not isinstance(doc["exit_code"], int) or not isinstance(
                doc["output_bytes"], int):
            return
        try:
            _write_owned_json(dfd, f"{pfid}.spawn.json", doc,
                              immutable=True,
                              max_bytes=_SPAWN_MAX_BYTES)
        except PreflightError:
            pass
    finally:
        os.close(dfd)


def run_preflight(home=None, pfid: str = "", transport=None,
                  observer=None,
                  timeout: int = PREFLIGHT_TIMEOUT_S) -> dict:
    """Execute one preflight probe and persist its terminal outcome.

    Claim-first: an atomic run claim refuses duplicate concurrent
    runs (409 busy, no second spawn); a dead owner's claim is reaped
    once. Status must be created; a recorded cancel intent short-circuits
    to a cancelled terminal without spawning. Otherwise the spawn
    boundary runs, the ADMITTED observer interprets it (default: the
    honest-unknown production observer; tests pass an explicit fake),
    the outcome is VALIDATED BEFORE immutable publication, and the
    status pointer follows. A cancel that landed mid-run reconciles
    the status to cancelled while the outcome stays as evidence.
    Refusals (auth/quota/offline/format/channel/stage0/containment)
    are terminal records, never a dispatch. Raises PreflightError on
    shape/ownership failures; nothing is deleted.
    """
    home_path = _resolve_home(home)
    if not _valid_pfid(pfid):
        raise PreflightError(f"bad preflight id: {pfid!r}",
                             status=400, code="bad-pfid")
    inputs, inputs_raw = _load_doc(home_path, pfid, "input",
                                   PREFLIGHT_INPUT_MAX_BYTES)
    try:
        _load_doc(home_path, pfid, "outcome", PREFLIGHT_OUTCOME_MAX_BYTES)
        raise PreflightError(f"preflight {pfid} already has a terminal "
                             "outcome; re-create instead of re-running",
                             status=409, code="already-terminal")
    except PreflightError as exc:
        if exc.code != "missing":
            raise
    # Per-check run lock (non-blocking): serializes concurrent
    # runners so recovery can never delete a newer live owner's
    # claim. A dead holder's lock is released by the kernel, so no
    # live check is ever blocked by a dead one; a second live run
    # fails fast with 409 busy instead of spawning twice.
    run_lock = _acquire_nb_lock(home_path, f"{pfid}.lock")
    try:
        token = _publish_claim(home_path, pfid)
    except PreflightError:
        _release_nb_lock(run_lock)
        raise
    try:
        status, _raw = _load_doc(home_path, pfid, "status",
                                 PREFLIGHT_STATUS_MAX_BYTES)
        if status.get("state") != "created":
            raise PreflightError(
                f"preflight {pfid} not startable in state "
                f"{status.get('state')}; nothing spawned",
                status=409, code="busy")
        # Pre-spawn cancel evidence is validated BEFORE the status
        # pointer moves to running: a present-but-corrupt flag must
        # refuse with the receipt still "created" and nothing
        # dispatched. The outer finally still releases the owned
        # claim and run lock on this path. Corrupt is never
        # treated as absent, never deleted, never verified.
        if _cancel_intent(home_path, pfid):
            return _write_cancelled_outcome(home_path, pfid, inputs_raw)
        opened = _open_dir_fd(home_path, create=False)
        dfd, dev, ino, hdev, hino = opened
        try:
            _write_owned_json(
                dfd, f"{pfid}.status.json",
                {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
                 "state": "running", "updated_at": _utcnow()},
                immutable=False, max_bytes=PREFLIGHT_STATUS_MAX_BYTES)
        finally:
            os.close(dfd)
        inputs_sha = hashlib.sha256(inputs_raw).hexdigest()
        # Cancellation callback to the owned child: a watcher sets
        # this event the moment the cancel flag lands, and the
        # transport/real exec polls it (real exec kills its owned
        # group on cancel). Fake transports receive the same live
        # event object — never None.
        stop = threading.Event()
        watch_done = threading.Event()

        def _watch_cancel():
            while not watch_done.is_set():
                try:
                    if _cancel_intent(home_path, pfid):
                        stop.set()
                        return
                except PreflightError:
                    # Missing never raises (returns False); any error
                    # here is corrupt/unreadable cancel evidence
                    # mid-run: fail closed by stopping the child
                    # rather than risking an ignored cancel.
                    stop.set()
                    return
                except Exception:  # noqa: BLE001 - keep watching
                    pass
                watch_done.wait(0.2)

        watcher = threading.Thread(target=_watch_cancel, daemon=True)
        watcher.start()
        try:
            spawn, obs_error = _collect_observations(
                home_path, pfid, inputs, transport=transport,
                timeout=timeout, cancel_event=stop)
        finally:
            watch_done.set()
            watcher.join(timeout=10)
            if watcher.is_alive():
                # The cancel observation path is wedged: the
                # post-spawn cancel reconciliation below reads the
                # same evidence, so publishing an outcome whose
                # cancel state cannot be verified would be
                # dishonest. Fail closed (daemon thread reaped at
                # process exit; no live run survives this raise
                # because the claim releases in the outer finally).
                raise PreflightError(
                    "cancel watcher did not stop; cancel evidence "
                    "unverifiable, refusing to publish",
                    status=502, code="unreadable")
        _record_spawn_evidence(home_path, pfid, spawn)
        observe = observer if observer is not None else _default_observer
        if obs_error is not None:
            state = "refused"
            reason = (f"probe collection failed: "
                      f"{obs_error.get('error')}. 다음: 점검 환경을 "
                      f"확인하고 다시 만드세요.")
            code = "probe-failed"
            channels = _unknown_channels(reason)
            auth = _unknown_auth(inputs)
        else:
            try:
                channels, auth = observe(home_path, pfid, inputs, spawn)
            except Exception as exc:  # noqa: BLE001 - observer fault
                reason = (f"probe observer failed: {exc}. 다음: 점검 "
                          f"환경을 확인하고 다시 만드세요.")
                state, code = "refused", "probe-failed"
                channels = _unknown_channels(reason)
                auth = _unknown_auth(inputs)
            else:
                try:
                    channels = _validate_channels(channels,
                                                  "observer.channels")
                    _validate_auth_shape(auth, "observer.auth")
                except PreflightError as exc:
                    reason = (f"probe observer returned malformed "
                              f"verdicts ({exc.code}). 다음: 점검 환경을 "
                              f"확인하고 다시 만드세요.")
                    state, code = "refused", "probe-failed"
                    channels = _unknown_channels(reason)
                    auth = _unknown_auth(inputs)
                else:
                    state, reason, code = evaluate_outcome(
                        inputs, channels, auth)
        outcome = {
            "schema": PREFLIGHT_SCHEMA,
            "pfid": pfid,
            "state": state,
            "inputs_sha256": inputs_sha,
            "channels": channels,
            "auth": auth,
            "error": "" if state == "verified" else reason[:2000],
            "code": "" if state == "verified" else code,
            "reason": reason[:2000],
            "ended_at": _utcnow(),
            "dir": [dev, ino],
            "home": [hdev, hino],
        }
        # Validated BEFORE immutable publication: a verified state is
        # never published without validated proof, and every terminal
        # state round-trips through read validation.
        _validate_outcome_doc(outcome)
        opened2 = _open_dir_fd(home_path, create=False)
        dfd2 = opened2[0]
        try:
            try:
                _write_owned_json(dfd2, f"{pfid}.outcome.json", outcome,
                                  immutable=True,
                                  max_bytes=PREFLIGHT_OUTCOME_MAX_BYTES)
            except PreflightError as exc:
                if exc.code != "exists-different":
                    raise
                # A terminal outcome (e.g. cancelled by an explicit
                # operator action) landed first: honor it instead of
                # clobbering or duplicating.
                try:
                    cur, _cr = _load_doc(
                        home_path, pfid, "status",
                        PREFLIGHT_STATUS_MAX_BYTES)
                except PreflightError:
                    raise exc
                if cur.get("state") == "cancelled":
                    return {"pfid": pfid, "state": "cancelled",
                            "reason": reason, "code": "cancelled"}
                raise
            _write_owned_json(
                dfd2, f"{pfid}.status.json",
                {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
                 "state": state, "updated_at": _utcnow()},
                immutable=False, max_bytes=PREFLIGHT_STATUS_MAX_BYTES)
        finally:
            os.close(dfd2)
        # A cancel that landed mid-run wins the status pointer: the
        # recorded outcome stays as evidence, but the live state
        # honors the operator's cancel instead of advertising the
        # spawn result. Corrupt cancel evidence here refuses (the
        # outcome stays published; only the status pointer write
        # is withheld) rather than advertising a spawn result over
        # possibly-tampered intent.
        if _cancel_intent(home_path, pfid):
            opened3 = _open_dir_fd(home_path, create=False)
            dfd3 = opened3[0]
            try:
                _write_owned_json(
                    dfd3, f"{pfid}.status.json",
                    {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
                     "state": "cancelled", "updated_at": _utcnow()},
                    immutable=False,
                    max_bytes=PREFLIGHT_STATUS_MAX_BYTES)
            finally:
                os.close(dfd3)
            return {"pfid": pfid, "state": "cancelled",
                    "reason": reason, "code": "cancelled"}
        return {"pfid": pfid, "state": state,
                "reason": reason, "code": code}
    finally:
        _release_claim(home_path, pfid, token)
        _release_nb_lock(run_lock)


def _unknown_auth(inputs: dict) -> dict:
    """Honest unknown auth on probe/observer failure."""
    try:
        seen = inputs.get("auth_observed") or {}
        present = bool(seen.get("meta_api_key_present"))
        has_contributor = "contributor_env_present" in seen
        contributor = bool(seen.get("contributor_env_present"))
    except Exception:  # noqa: BLE001 - inputs shape already validated
        present = False
        has_contributor = False
        contributor = False
    out = {"meta_api_key_present": present,
           "credential_source": "unknown",
           "billing_evidence": None,
           "observed_how": "probe/observer failure: no trusted auth "
                           "observation"}
    # Legacy inputs stay legacy-shaped (absent != False).
    if has_contributor:
        out["contributor_env_present"] = contributor
    return out


def _write_cancelled_outcome(home_path: str, pfid: str,
                             inputs_raw: bytes) -> dict:
    """Publish the cancelled terminal (validated, immutable)."""
    opened = _open_dir_fd(home_path, create=False)
    dfd, dev, ino, hdev, hino = opened
    try:
        inputs_sha = hashlib.sha256(inputs_raw).hexdigest()
        reason = ("preflight cancelled by explicit operator action; no "
                  "probe result recorded. 다음: 필요하면 새로 만드세요.")
        outcome = {
            "schema": PREFLIGHT_SCHEMA,
            "pfid": pfid,
            "state": "cancelled",
            "inputs_sha256": inputs_sha,
            "channels": {},
            "auth": _unknown_auth(_loads_closed(inputs_raw)),
            "error": reason,
            "code": "cancelled",
            "reason": reason,
            "ended_at": _utcnow(),
            "dir": [dev, ino],
            "home": [hdev, hino],
        }
        _validate_outcome_doc(outcome)
        _write_owned_json(dfd, f"{pfid}.outcome.json", outcome,
                          immutable=True,
                          max_bytes=PREFLIGHT_OUTCOME_MAX_BYTES)
        _write_owned_json(
            dfd, f"{pfid}.status.json",
            {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
             "state": "cancelled", "updated_at": _utcnow()},
            immutable=False, max_bytes=PREFLIGHT_STATUS_MAX_BYTES)
    finally:
        os.close(dfd)
    return {"pfid": pfid, "state": "cancelled"}


def _default_observer(home_path: str, pfid: str, inputs: dict,
                      spawn, controls=None):
    """Admitted production observer: honest unknowns.

    Reports every channel as unknown/positive False and auth as
    unknown until genuinely supported positive controls exist. The
    spawn evidence (exit code, requested-profile echo) is recorded
    as detail context ONLY — an rc0 spawn with perfect requested
    flags NEVER confirms a channel, because requested flags are not
    observed enforcement and model output is never a positive
    control. Returns (channels, auth) with the closed shapes the
    outcome validator requires.

    controls is an EXPLICIT test-double seam only: production
    callers always leave it None (byte-identical default path),
    and no HTTP/API path may pass one (pinned by test). A
    non-None controls must be {"channels": {...}, "auth": {...}};
    anything else raises and the run refuses probe-failed. The
    returned pair still passes the closed-shape validators, so
    malformed injected observations refuse instead of verifying.
    """
    if controls is not None:
        if not isinstance(controls, dict):
            raise ValueError("controls must be "
                             "{channels, auth}")
        channels = controls.get("channels")
        auth = controls.get("auth")
        if not isinstance(channels, dict) \
                or not isinstance(auth, dict):
            raise ValueError("controls must be "
                             "{channels, auth}")
        return channels, auth
    if not isinstance(spawn, dict):
        spawn = {}
    exit_code = spawn.get("exit_code")
    detail = ("no app-observed positive control for this channel: "
              "only supported startup markers count, and none are "
              "established (spawn exit=%r; requested flags are not "
              "observed enforcement)" % (exit_code,))
    channels = {}
    for name in REQUIRED_CHANNELS:
        channels[name] = {"required": REQUIRED_DIRECTION[name],
                          "observed": "unknown", "positive": False,
                          "detail": detail[:PREFLIGHT_DETAIL_MAX_CHARS]}
    seen_auth = inputs.get("auth_observed") or {}
    auth = {"meta_api_key_present":
            bool(seen_auth.get("meta_api_key_present")),
            "credential_source": "unknown",
            "billing_evidence": None,
            "observed_how": "default observer: no supported trusted "
                            "auth-source mechanism established"}
    # Legacy inputs (key absent) stay legacy-shaped downstream:
    # asserting False for an unobserved create-time fact would be a
    # fabrication. New inputs always carry the bool.
    if "contributor_env_present" in seen_auth:
        auth["contributor_env_present"] = bool(
            seen_auth["contributor_env_present"])
    return channels, auth


def native_containment():
    """Require the admitted native wrapper before any spawn.

    No admitted native wrapper/policy or auth read-exception set has
    been established for the worker child tree (startup hooks, MCP
    servers, app-side Git helpers included): sandbox flags and a
    process group do not contain those. This ALWAYS raises today
    with a specific code and the concrete next evidence needed —
    never a silent pass, never a bypass. A future gate that proves
    the wrapper replaces this refusal with a same-wrapper spawn.
    """
    raise PreflightError(
        "native containment unproved: no admitted wrapper/policy "
        "covers the worker child tree (startup hooks, MCP servers, "
        "app-side Git helpers); restricted flags are requested, not "
        "observed. 다음: 네이티브 격리 래퍼와 인증 읽기 예외가 "
        "확정될 때까지 대기하세요.",
        status=409, code="native-containment-unproved")


def stage0_admission(home=None):
    """Bootstrap admission for the FIRST probe spawn (no receipt yet).

    Separate from the full target receipt: proves trusted auth (no
    META_API_KEY presence, non-unknown credential source from the
    admitted observer path — still unproved) AND native containment
    BEFORE the first model spawn. No generic True, no fixture
    shortcut, no caller-supplied verified dict: every leg re-checks
    live facts. Today every leg refuses with a specific code; that
    refusal IS the honest record, with the next evidence named.
    Returns {"kind": "stage0", ...} when (and only when) every leg
    genuinely passes.
    """
    home_path = _resolve_home(home)
    live = current_identity()
    auth = live.get("auth") or {}
    if auth.get("meta_api_key_present") is True:
        raise PreflightError(
            "stage0 refused: META_API_KEY is present and would take "
            "priority over subscription login. 다음: 키를 제거하세요.",
            status=409, code="api-key-present")
    if not isinstance(auth.get("credential_source"), str) or \
            auth.get("credential_source") in ("unknown", ""):
        raise PreflightError(
            "stage0 refused: no supported trusted credential source "
            "observed (subscription session UNVERIFIED). 다음: 인증 "
            "경로가 확정될 때까지 대기하세요.",
            status=409, code="auth-unestablished")
    native_containment()
    return {"kind": "stage0", "home": home_path, "at": _utcnow(),
            "note": "bootstrap admission only; not target authority"}


def cancel_preflight(home=None, pfid: str = "") -> dict:
    """Terminalize a created/running preflight as cancelled.

    The cancel INTENT flag publishes first (idempotent): a run that
    already holds the claim sees it and reconciles to cancelled
    instead of advertising a spawn result. Then, when no terminal
    outcome exists yet, the cancelled terminal is published
    (validated BEFORE the immutable write). Terminal receipts
    refuse (409); nothing is deleted and no foreign job is touched.
    """
    home_path = _resolve_home(home)
    if not _valid_pfid(pfid):
        raise PreflightError(f"bad preflight id: {pfid!r}",
                             status=400, code="bad-pfid")
    inputs, inputs_raw = _load_doc(home_path, pfid, "input",
                                   PREFLIGHT_INPUT_MAX_BYTES)
    try:
        _load_doc(home_path, pfid, "outcome", PREFLIGHT_OUTCOME_MAX_BYTES)
        raise PreflightError(f"preflight {pfid} already terminal; "
                             "nothing cancelled", status=409,
                             code="already-terminal")
    except PreflightError as exc:
        if exc.code != "missing":
            raise
    opened = _open_dir_fd(home_path, create=False)
    dfd = opened[0]
    try:
        try:
            _write_owned_json(
                dfd, f"{pfid}.cancel.json",
                {"schema": PREFLIGHT_SCHEMA, "pfid": pfid,
                 "at": _utcnow(), "home": [opened[3], opened[4]],
                 "dir": [opened[1], opened[2]]},
                immutable=True, max_bytes=_CLAIM_MAX_BYTES)
        except PreflightError as exc:
            if exc.code not in ("exists-identical", "exists-different"):
                raise
            # A flag already exists: an identical one is our own
            # idempotent retry; a differing one only varies by
            # timestamp — either way intent is recorded, so
            # continue to the terminal write.
    finally:
        os.close(dfd)
    status, _raw = _load_doc(home_path, pfid, "status",
                             PREFLIGHT_STATUS_MAX_BYTES)
    if status.get("state") not in ("created", "running"):
        raise PreflightError(
            f"preflight {pfid} not cancellable in state "
            f"{status.get('state')}", status=409, code="busy")
    return _write_cancelled_outcome(home_path, pfid, inputs_raw)


def latest_verified(home=None):
    """Newest verified receipt whose input pin still matches, else None.

    Returns (pfid, input_doc, outcome_doc). Stale/corrupt/mismatched
    receipts are skipped honestly — never healed, never deleted. A
    truncated scan withholds entirely (returns None): the scanned
    subset is an arbitrary readdir slice, and selecting "newest"
    from it could crown a stale receipt while a newer verified
    one hides beyond the cap. Callers that must distinguish
    truncation from emptiness scan directly.
    """
    home_path = _resolve_home(home)
    pfids, complete = _scan_pfids(home_path)
    if not complete:
        return None
    best = None
    for pfid in pfids:
        try:
            status, _s = _load_doc(home_path, pfid, "status",
                                   PREFLIGHT_STATUS_MAX_BYTES)
            if status.get("state") != "verified":
                continue
            outcome, _o = _load_doc(home_path, pfid, "outcome",
                                    PREFLIGHT_OUTCOME_MAX_BYTES)
            if outcome.get("state") != "verified":
                continue
            inputs, inputs_raw = _load_doc(home_path, pfid, "input",
                                           PREFLIGHT_INPUT_MAX_BYTES)
        except PreflightError:
            continue
        if outcome.get("pfid") != pfid or inputs.get("pfid") != pfid:
            continue
        if outcome.get("inputs_sha256") != hashlib.sha256(
                inputs_raw).hexdigest():
            continue
        ended = str(outcome.get("ended_at") or "")
        if best is None or ended > best[0]:
            best = (ended, pfid, inputs, outcome)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _refuse(message: str, code: str, status: int = 409) -> PreflightError:
    return PreflightError(message, status=status, code=code)


def check_target_authority(home=None, current=None,
                           pfid: str = None) -> dict:
    """Fail-closed gate for target-A (source-worker) startup.

    Loads the newest verified receipt (or the named pfid), then
    requires it to match the SAME live binary/config/auth/policy/
    source/child-env identity: pinned SHA + real path, exact
    recorded config, source FILE SHAs (not just the version),
    child-env key shape, and live auth re-observed (presence +
    credential source, both matching the record and non-unknown).
    The recorded pure verdict re-evaluates to verified. Any
    missing/stale/unknown/corrupt/auth/quota/offline/format/
    containment evidence refuses with a typed code and an actionable
    reason — creating no model/job/worktree mutation. Returns
    {"pfid", "verified_at", "inputs_sha256"}. Offline tests mock
    current_identity; the comparison itself is never bypassed.
    """
    home_path = _resolve_home(home)
    if pfid is not None:
        if not _valid_pfid(pfid):
            raise _refuse(f"bad preflight id: {pfid!r}", "bad-pfid",
                          status=400)
        try:
            status, _s = _load_doc(home_path, pfid, "status",
                                   PREFLIGHT_STATUS_MAX_BYTES)
            outcome, _o = _load_doc(home_path, pfid, "outcome",
                                    PREFLIGHT_OUTCOME_MAX_BYTES)
            inputs, inputs_raw = _load_doc(home_path, pfid, "input",
                                           PREFLIGHT_INPUT_MAX_BYTES)
        except PreflightError as exc:
            if exc.code == "missing":
                raise _refuse(
                    f"no preflight receipt {pfid}: run an isolation "
                    f"check first; target-A refused. 다음: 격리 점검을 "
                    f"먼저 실행하세요.", "no-preflight",
                    status=404) from exc
            raise _refuse(
                f"preflight receipt {pfid} unreadable ({exc.code}): "
                f"{exc.message}. 다음: 점검 기록을 확인하세요.",
                "corrupt", status=502) from exc
        if status.get("state") != "verified" or outcome.get(
                "state") != "verified":
            raise _refuse(
                f"preflight {pfid} is {status.get('state')}: only a "
                f"verified isolation check authorizes target-A. 다음: "
                f"격리 점검을 완료하세요.",
                "stale" if status.get("state") in _TERMINAL_STATES
                else "not-verified")
        found = (pfid, inputs, outcome, inputs_raw)
    else:
        pfids, complete = _scan_pfids(home_path)
        if not complete:
            # The scanned subset is arbitrary: authority is
            # withheld, never guessed from a partial slice.
            raise _refuse(
                "preflight history truncated at scan cap; cannot "
                "prove newest receipt. target-A refused with no "
                "mutation. 다음: 기록 폴더를 확인하세요.",
                "history-truncated", status=502)
        found_latest = latest_verified(home_path)
        if found_latest is None:
            cls = _history_class(home_path, pfids)
            if cls == "tamper":
                raise _refuse(
                    "preflight history tampered (readable status "
                    "with invalid outcome/input): target-A refused "
                    "with no mutation. 다음: 기록 폴더 상태를 "
                    "확인하세요.",
                    "unreadable", status=502)
            if cls == "readable":
                # Readable but unverified history (created/unknown/
                # refused/cancelled): honest nothing-verified, not
                # missing and not corrupt. 404 — the gate cannot
                # reach auth/channel branches without a verified
                # receipt; those reasons live on the persisted
                # outcome records themselves.
                raise _refuse(
                    "no verified preflight: run an isolation check "
                    "first; target-A refused with no mutation. 다음: "
                    "격리 점검을 먼저 실행하세요.",
                    "no-preflight", status=404)
            if _preflight_dir_state(home_path) == "ok":
                # Known directory, zero usable receipts: history was
                # swapped or partially deleted. Never report this as
                # legitimate absence (404 no-preflight).
                raise _refuse(
                    "preflight history missing from a known records "
                    "directory (unreadable): the folder exists but no "
                    "receipt survives; target-A refused with no "
                    "mutation. 다음: 기록 폴더 상태를 확인하세요.",
                    "unreadable", status=502)
            raise _refuse(
                "no verified preflight: run an isolation check first; "
                "target-A refused with no mutation. 다음: 격리 점검을 "
                "먼저 실행하세요.", "no-preflight", status=404)
        pfid, inputs, outcome = found_latest
        _checked, inputs_raw = _load_doc(home_path, pfid, "input",
                                        PREFLIGHT_INPUT_MAX_BYTES)
        found = (pfid, inputs, outcome, inputs_raw)
    pfid, inputs, outcome, inputs_raw = found
    live = current if current is not None else current_identity()
    if not isinstance(live, dict):
        raise _refuse("live identity unreadable; target-A refused. "
                      "다음: 실행 환경을 확인하세요.", "unreadable",
                      status=502)
    rec_bin = inputs.get("binary") or {}
    live_bin = live.get("binary") or {}
    if not isinstance(live_bin, dict) or live_bin.get("status") != "ok":
        detail = (live_bin.get("error") or live_bin.get("status")
                  or "unknown")
        raise _refuse(
            f"pinned binary unavailable now ({detail}); target-A "
            f"refused. 다음: 설치된 바이너리를 확인하세요.",
            "binary-unavailable")
    if live_bin.get("sha256") != rec_bin.get("sha256") or not rec_bin.get(
            "sha256"):
        raise _refuse(
            "pinned binary drifted since the preflight (SHA mismatch); "
            "target-A refused. 다음: 격리 점검을 다시 실행하세요.",
            "binary-drift")
    if live_bin.get("real_path") != rec_bin.get("real_path"):
        raise _refuse(
            "pinned binary resolution changed since the preflight; "
            "target-A refused. 다음: 격리 점검을 다시 실행하세요.",
            "binary-drift")
    live_cfg = live.get("config") if isinstance(live, dict) else None
    rec_cfg = inputs.get("config") or {}
    if not isinstance(live_cfg, dict) or live_cfg.get("status") != \
            "established" or live_cfg.get("status") != rec_cfg.get(
                "status") or list(live_cfg.get("files") or []) != list(
                    rec_cfg.get("files") or []):
        raise _refuse(
            "config identity unestablished or drifted since the "
            "preflight; target-A refused. 다음: 설정 표면이 확정될 "
            "때까지 대기하세요.", "config-unestablished")
    # Same-alive worker binding (slice 297): when the binding
    # exists, an established config only counts on the exact
    # worker it was proved on. A gone, replaced, or drifted
    # worker refuses here; a new check on the live worker
    # re-proves. Absent on both sides keeps the legacy
    # file-based established path; present on only one side
    # is drift. (In production, established arises only via
    # worker observation, which always sets the key.)
    rec_worker = rec_cfg.get("worker")
    live_worker = live_cfg.get("worker")
    if (rec_worker is None) != (live_worker is None):
        raise _refuse(
            "worker binding changed since the preflight; "
            "target-A refused. 다음: 격리 점검을 다시 실행하세요.",
            "config-unestablished")
    if rec_worker is not None:
        if not isinstance(rec_worker, dict) \
                or not isinstance(live_worker, dict):
            raise _refuse(
                "worker binding unreadable; target-A refused. "
                "다음: 격리 점검을 다시 실행하세요.",
                "config-unestablished")
        for key in ("handle", "pid", "sha256"):
            if rec_worker.get(key) != live_worker.get(key):
                raise _refuse(
                    "worker binding drifted since the preflight "
                    "(%s changed); target-A refused. 다음: 격리 "
                    "점검을 다시 실행하세요." % key,
                    "config-unestablished")
    live_src = live.get("source") or {}
    rec_src = inputs.get("source") or {}
    if not isinstance(live_src, dict) or live_src.get("status") != "ok" \
            or live_src.get("app_version") != rec_src.get("app_version") \
            or list(live_src.get("files") or []) != list(
                rec_src.get("files") or []):
        raise _refuse(
            "app source changed since the preflight (version or "
            "managed file SHA mismatch); target-A refused. 다음: 격리 "
            "점검을 다시 실행하세요.", "source-drift")
    live_env = live.get("env") if isinstance(live, dict) else None
    rec_env = inputs.get("env") or {}
    if not isinstance(live_env, dict) or sorted(
            live_env.get("passthrough_keys") or []) != sorted(
            rec_env.get("passthrough_keys") or []) or sorted(
            live_env.get("cache_keys") or []) != sorted(
            rec_env.get("cache_keys") or []) or dict(
            live_env.get("values") or {}) != dict(
            rec_env.get("values") or {}):
        raise _refuse(
            "child-environment identity changed since the preflight "
            "(keys or observed values); "
            "target-A refused. 다음: 격리 점검을 다시 실행하세요.",
            "env-drift")
    live_pol = live.get("policy") if isinstance(live, dict) else None
    if live_pol != REQUIRED_POLICY:
        raise _refuse(
            "live dedicated profile differs from the preflighted "
            "policy; target-A refused. 다음: 전용 프로필을 확인하세요.",
            "bad-policy")
    live_auth = live.get("auth") if isinstance(live, dict) else None
    rec_auth = dict(outcome.get("auth") or {})
    if not isinstance(live_auth, dict):
        raise _refuse(
            "live auth unreadable; target-A refused. 다음: 실행 환경을 "
            "확인하세요.", "auth-unestablished", status=502)
    if live_auth.get("meta_api_key_present") is True:
        raise _refuse(
            "META_API_KEY is present now and would take priority over "
            "subscription login; target-A refused. 다음: 키를 제거하고 "
            "격리 점검을 다시 실행하세요.", "api-key-present")
    for key in ("meta_api_key_present", "credential_source"):
        if live_auth.get(key) != rec_auth.get(key):
            raise _refuse(
                "live auth drifted since the preflight; target-A "
                "refused. 다음: 격리 점검을 다시 실행하세요.",
                "auth-drift")
    if not isinstance(rec_auth.get("credential_source"), str) or \
            rec_auth.get("credential_source") in ("unknown", ""):
        raise _refuse(
            "no supported trusted credential source was ever observed "
            "(subscription session UNVERIFIED): target-A refused "
            "until the auth path is proved. 다음: 인증 경로가 확정될 "
            "때까지 대기하세요.", "auth-unestablished")
    auth = rec_auth
    state, reason, code = evaluate_outcome(
        {"policy": inputs.get("policy"), "config": rec_cfg},
        outcome.get("channels"), auth)
    if state != "verified":
        raise _refuse(
            f"preflight re-evaluation is {state} ({code}): {reason}",
            code or "not-verified")
    if outcome.get("inputs_sha256") != hashlib.sha256(
            inputs_raw).hexdigest():
        raise _refuse(
            "preflight input bytes changed after the outcome was "
            "recorded; refusing a rebound pin. 다음: 격리 점검을 다시 "
            "실행하세요.", "corrupt", status=502)
    return {"pfid": pfid, "verified_at": outcome.get("ended_at"),
            "inputs_sha256": outcome.get("inputs_sha256")}


def _live_states_for_display():
    # Display only: states that read as an in-flight worker.
    # "created" is explicitly NOT running — no worker exists yet.
    # It stays live for dedupe/lock purposes through the separate
    # _TERMINAL_STATES check in create; reporting it as running
    # would advertise a worker that never started.
    return ("running",)


def _preflight_dir_state(home_path: str) -> str:
    """ok | absent | unreadable for the preflight directory itself.

    A missing layout is legitimate absence (nothing ever ran); an
    unreadable one hides history and must not read as empty.
    """
    try:
        opened = _open_dir_fd(home_path, create=False)
    except PreflightError as exc:
        if exc.code == "missing":
            return "absent"
        return "unreadable"
    try:
        os.close(opened[0])
    except OSError:
        pass
    return "ok"


def _order_pfids_by_time(home_path: str, pfids: list) -> list:
    """(sort_key, pfid) list, newest recorded time first.

    sort_key is the terminal outcome's ended_at when present, else
    the status updated_at/created stamp, else "". Ties break by
    pfid for determinism. Random hex order would shuffle history
    across identical timestamps; recorded times are the honest
    order.
    """
    ranked = []
    for pfid in pfids:
        key = ""
        try:
            status, _s = _load_doc(home_path, pfid, "status",
                                   PREFLIGHT_STATUS_MAX_BYTES)
        except PreflightError:
            status = {}
        if isinstance(status, dict):
            key = str(status.get("updated_at")
                      or status.get("created_at") or "")
            if status.get("state") in _TERMINAL_STATES:
                try:
                    outcome, _o = _load_doc(
                        home_path, pfid, "outcome",
                        PREFLIGHT_OUTCOME_MAX_BYTES)
                except PreflightError:
                    outcome = {}
                if isinstance(outcome, dict) and outcome.get(
                        "ended_at"):
                    key = str(outcome["ended_at"])
        ranked.append((key, pfid))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked


def _worker_profile_info():
    """Static description of the contained worker profile.

    Names the subscription-worker profile and its read
    exceptions for the panel (no authority claim: this
    describes the profile the product ships, not an executed
    check). Pure path expansion; an unresolvable account home
    reads as an honest error, never an empty list.
    """
    from . import ncontain as _ncontain
    try:
        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return {"name": "subscription", "exceptions": [],
                "error": "account home unresolvable"}
    try:
        exceptions = _ncontain.subscription_worker_exceptions(
            account_home)
    except _ncontain.ContainmentError as exc:
        return {"name": "subscription", "exceptions": [],
                "error": str(exc)}
    return {"name": "subscription", "exceptions": exceptions}


def describe_preflight(home=None):
    """Read-only projection for the isolation-check panel.

    Never creates directories or records; missing layout reads as
    state "absent" with an actionable reason, while an unreadable
    directory reads as state "unreadable" (never an empty history).
    Readable-but-unverified history (created/unknown/refused/
    cancelled) reads as "not-verified" with code "no-preflight":
    honest nothing-verified, never missing-corrupt. A truncated
    scan reads as "unknown"/"history-truncated" with authority
    withheld, never a newest-claim from a partial slice. A live
    running check shows as running (never hidden as
    not-verified, never success): the probe may still refuse.
    `authority_now` re-runs the gate against LIVE identity so the
    panel tells authorized from stale without starting anything.
    History is bounded, ordered by recorded times (newest first),
    never by random pfid.
    """
    home_path = _resolve_home(home)
    worker_profile = _worker_profile_info()
    if _preflight_dir_state(home_path) == "unreadable":
        return {"state": "unreadable", "pfid": None,
                "reason": "격리 점검 기록을 읽을 수 없습니다 "
                          "(unreadable): 권한을 확인하세요.",
                "code": "unreadable",
                "blocked_by": None,
                "worker_profile": worker_profile,
                "authority_now": {"authorized": False,
                                  "code": "unreadable",
                                  "reason": "기록을 읽을 수 없어 "
                                            "권한을 판단할 수 없습니다."},
                "history": []}
    try:
        pfids, complete = _scan_pfids(home_path)
    except PreflightError as exc:
        return {"state": "unreadable", "pfid": None,
                "reason": "격리 점검 목록을 읽을 수 없습니다 "
                          f"(unreadable): {exc.code}",
                "code": "unreadable",
                "blocked_by": None,
                "worker_profile": worker_profile,
                "authority_now": {"authorized": False,
                                  "code": "unreadable",
                                  "reason": "기록을 읽을 수 없어 "
                                            "권한을 판단할 수 없습니다."},
                "history": []}
    latest = latest_verified(home_path) if pfids else None
    history = []
    for _sort_key, pfid in _order_pfids_by_time(home_path, pfids)[:20]:
        try:
            status, _s = _load_doc(home_path, pfid, "status",
                                   PREFLIGHT_STATUS_MAX_BYTES)
        except PreflightError as exc:
            history.append({"pfid": pfid, "state": "unreadable",
                            "reason": exc.message})
            continue
        entry = {"pfid": pfid, "state": status.get("state")}
        if status.get("state") in _live_states_for_display():
            entry["reason"] = ("probe running; target-A not yet "
                               "authorized — this display alone starts "
                               "nothing. 다음: 끝난 뒤 다시 읽으세요.")
        if status.get("state") in _TERMINAL_STATES:
            try:
                _outcome, _o = _load_doc(home_path, pfid, "outcome",
                                        PREFLIGHT_OUTCOME_MAX_BYTES)
                entry["reason"] = _outcome.get("reason") or ""
                entry["code"] = _outcome.get("code") or ""
            except PreflightError as exc2:
                entry["state"] = "unreadable"
                entry["reason"] = exc2.message
        history.append(entry)
    # Latest terminal record first (history is newest-first): only
    # ITS code decides blocked_by. An older structural refusal
    # behind a newer non-structural one does not block.
    blocked_by = None
    for entry in history:
        if entry.get("state") in _TERMINAL_STATES:
            code = entry.get("code") or ""
            if code in _STRUCTURAL_BLOCK_REASONS:
                blocked_by = code
            break
    try:
        authority = check_target_authority(home_path)
        authority_now = {"authorized": True,
                         "pfid": authority["pfid"],
                         "verified_at": authority["verified_at"]}
    except PreflightError as exc:
        authority_now = {"authorized": False, "code": exc.code,
                         "reason": exc.message}
    if latest is None:
        top_blocked = None
        running = [h for h in history
                   if h.get("state") in _live_states_for_display()]
        hist_cls = _history_class(home_path, pfids)
        if running:
            state = "running"
            reason = ("probe running; target-A not yet authorized. "
                      "다음: 끝난 뒤 다시 읽으세요.")
            code = "running"
            live_pfid = running[0].get("pfid")
        elif not complete:
            # Truncated scan: the visible slice is arbitrary, so
            # no newest/authority claim is made from it. History
            # shows what was scanned; the gate withholds.
            state = "unknown"
            reason = ("격리 점검 기록이 잘렸습니다 "
                      "(history-truncated): 보이는 목록은 일부이며 "
                      "최신 여부를 판단할 수 없습니다.")
            code = "history-truncated"
            live_pfid = None
        elif hist_cls == "tamper":
            state = "unreadable"
            reason = ("격리 점검 기록이 훼손되었습니다 (unreadable): "
                      "읽히는 상태와 어긋난 결과·입력 기록이 있습니다.")
            code = "unreadable"
            live_pfid = None
        elif hist_cls == "readable":
            state = "not-verified"
            if blocked_by is not None:
                reason = _STRUCTURAL_BLOCK_REASONS[blocked_by]
                top_blocked = blocked_by
            else:
                reason = ("latest isolation check is not verified: "
                          "target-A source work stays blocked. 다음: "
                          "격리 점검을 다시 실행하세요.")
            code = "no-preflight"
            live_pfid = None
        elif _preflight_dir_state(home_path) == "ok":
            # Known directory, zero usable records: the receipts
            # that runs would have written are gone (directory
            # swap or partial delete). This is NOT legitimate
            # absence — absence means no directory was ever
            # established. Fail closed so tampered emptiness
            # never reads as a clean slate.
            state = "unreadable"
            reason = ("격리 점검 기록이 비어 있습니다 (unreadable): "
                      "알려진 기록 폴더에 점검 기록이 없습니다. "
                      "폴더가 교체되었을 수 있습니다.")
            code = "unreadable"
            live_pfid = None
        elif not pfids:
            # No directory was ever established: legitimate absence.
            state = "absent"
            reason = ("no verified isolation check: target-A source "
                      "work is blocked until one completes. 다음: 격리 "
                      "점검을 실행하세요.")
            code = "no-preflight"
            live_pfid = None
        else:
            # pfids exist but NONE parse (hist_cls "none"): every
            # record is corrupt. The history entries already carry
            # per-pfid unreadable reasons; the top level must not
            # report a clean nothing-verified either.
            state = "unreadable"
            reason = ("격리 점검 기록을 읽을 수 없습니다 (unreadable): "
                      "기록이 모두 손상되었습니다.")
            code = "unreadable"
            live_pfid = None
        return {"state": state, "pfid": live_pfid, "reason": reason,
                "code": code, "blocked_by": top_blocked,
                "worker_profile": worker_profile,
                "history": history, "channels": {},
                "authority_now": authority_now}
    pfid, inputs, outcome = latest
    channels = {}
    for name in REQUIRED_CHANNELS:
        entry = (outcome.get("channels") or {}).get(name) or {}
        channels[name] = {
            "required": REQUIRED_DIRECTION[name],
            "observed": entry.get("observed") or "unknown",
            "positive": bool(entry.get("positive")),
            "detail": str(entry.get("detail") or "")[:200],
        }
    binary = inputs.get("binary") or {}
    return {
        "state": "verified",
        "pfid": pfid,
        "blocked_by": None,
        "worker_profile": worker_profile,
        "verified_at": outcome.get("ended_at"),
        "inputs_sha256": outcome.get("inputs_sha256"),
        "binary": {"path": binary.get("path"),
                   "sha256": binary.get("sha256")},
        "policy": {"profile": "source-repair", "model": "muse-spark-1.3",
                   "network_mode": "restricted"},
        "config_status": (inputs.get("config") or {}).get("status"),
        "auth": {"meta_api_key_present": bool(
            (outcome.get("auth") or {}).get("meta_api_key_present")),
            "credential_source": str(
                (outcome.get("auth") or {}).get("credential_source")
                or "unknown")},
        "reason": outcome.get("reason") or "",
        "channels": channels,
        "history": history,
        "authority_now": authority_now,
    }


def diagnose_msp_metadata(home=None, binary_path=None, timeout_s=60):
    """One-shot app-owned MSP metadata diagnostic (no receipt).

    Creates a unique fresh 0700 child root UNDER the owned app home
    (the live app home itself is never used as the child HOME, and
    no existing profile/data directory is ever reused), spawns the
    real pinned host there via muse.spawn_msp_host — which always
    builds the ncontain offline wrapper itself — runs the fixed
    initialize -> initialized{} -> four-reads sequence with ONE
    overall deadline, closes the host within a separate bounded
    cleanup budget, and returns a typed in-memory result. Nothing is
    persisted: no preflight record, no receipt, no history entry.
    Nothing here touches current_identity, _default_observer,
    stage0_admission, check_target_authority, exec_source_repair,
    or any target authority — model admission stays fail-closed and
    admits_worker stays False. A positive result requires a fully
    clean close (real EOF on both streams, drained readers, reaped
    child, no kill, no protocol break); anything less is a
    qualified unknown. This is capability QA for the factory
    plumbing only, never worker authorization. Raises
    PreflightError on misuse (bad home/binary args); host outcomes
    return as typed status dicts.
    """
    from . import muse as _muse
    if timeout_s is None or not isinstance(timeout_s, (int, float)) \
            or isinstance(timeout_s, bool) or timeout_s != timeout_s \
            or timeout_s <= 0 or timeout_s == float("inf"):
        raise PreflightError("diagnostic timeout must be a finite "
                             "positive number of seconds",
                             status=400, code="bad-input")
    timeout_s = float(timeout_s)
    home_path = _resolve_home(home)
    try:
        st = os.lstat(home_path)
    except OSError as exc:
        raise PreflightError(f"diagnostic home unreadable: {exc}",
                             status=502, code="unreadable") from exc
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise PreflightError("diagnostic home must be a real "
                             "directory", status=400, code="bad-input")
    if st.st_uid != os.getuid():
        raise PreflightError("diagnostic home is not owned by this "
                             "user", status=502, code="unreadable")
    if _stat.S_IMODE(st.st_mode) != 0o700:
        raise PreflightError("diagnostic home must be mode 0700",
                             status=502, code="unreadable")
    if os.path.realpath(home_path) != os.path.normpath(
            os.path.abspath(home_path)):
        raise PreflightError("diagnostic home path is not canonical",
                             status=400, code="bad-input")
    try:
        child_root = os.path.join(
            home_path, "msp-child-" + secrets.token_hex(8))
        os.mkdir(child_root, 0o700)
        cst = os.lstat(child_root)
    except OSError as exc:
        raise PreflightError(f"diagnostic child root failed: {exc}",
                             status=502, code="unreadable") from exc
    if (_stat.S_ISLNK(cst.st_mode) or not _stat.S_ISDIR(cst.st_mode)
            or cst.st_uid != os.getuid()
            or _stat.S_IMODE(cst.st_mode) != 0o700
            or os.path.realpath(child_root) != child_root):
        raise PreflightError("diagnostic child root identity "
                             "invalid", status=502,
                             code="unreadable")

    def _blank(lane_reason):
        return {"reads": {}, "credential_lane": "unknown",
                "lane_reason": lane_reason, "host": {},
                "cleanup": {"state": "not-started"},
                "admits_worker": False}

    def _refused(code, reason):
        out = _blank("no observation was established")
        out.update({"status": "refused", "code": code,
                    "reason": reason})
        return out

    def _unknown(code, reason, host=None, cleanup=None, reads=None,
                 lane=None, lane_reason=None):
        out = _blank(lane_reason or "no complete observation was "
                                    "established")
        out.update({"status": "unknown", "code": code,
                    "reason": reason,
                    "host": host or {},
                    "cleanup": cleanup or {"state": "not-started"},
                    "reads": reads or {}})
        if lane is not None:
            out["credential_lane"] = lane[0]
            out["lane_reason"] = lane[1]
        return out

    try:
        handle = _muse.spawn_msp_host(child_root, binary_path,
                                      timeout_s=timeout_s)
    except _muse.MspError as exc:
        code = exc.code or "transport"
        if code in ("api-key-present", "binary-unavailable",
                    "bad-input", "containment"):
            return _refused(code, exc.message)
        return _unknown(code, exc.message)
    try:
        live = _muse._MSP_HOSTS.get(handle) or {}
        proc = live.get("proc")
        try:
            pid = proc.pid if proc is not None else None
        except (OSError, ValueError, AttributeError):
            pid = None
        rec_host = {"sha256": (live.get("binary") or {}).get(
            "sha256"), "pid": pid, "nonce": live.get("nonce")}
    except Exception:
        rec_host = {"sha256": None, "pid": None, "nonce": None}
    def _failed_cleanup(reason):
        return {"handle_closed": False, "reason": reason,
                "state": "unknown", "returncode": None,
                "stderr_bytes": 0, "stdout_bytes": 0,
                "stdout_eof": False, "stderr_eof": False,
                "joined_stdout": False, "joined_stderr": False,
                "signalled": False}

    collect_error = None
    reads = None
    try:
        try:
            reads = _muse.collect_msp_metadata(
                handle, timeout=timeout_s,
                app_version=_muse.__version__)
        except _muse.MspError as exc:
            collect_error = exc
        except Exception as exc:
            # Ordinary unexpected failure: sanitized typed unknown.
            # Only the exception TYPE name is recorded — never raw
            # wire bytes, secrets, or a stack trace.
            collect_error = _muse.MspError(
                "diagnostic sequence failed unexpectedly: "
                f"{type(exc).__name__}",
                code="sequence-failed")
    finally:
        # Unconditional: the owned child, readers, and registry are
        # reconciled after ANY collect outcome. Cancel/interrupt
        # still propagates, but only after cleanup ran.
        try:
            cleanup = _muse.msp_close(
                handle, reason="diagnostic-done")
        except _muse.MspError as exc:
            cleanup = _failed_cleanup(exc.message)
        except Exception as exc:
            cleanup = _failed_cleanup(
                f"cleanup failed unexpectedly: "
                f"{type(exc).__name__}")
    if collect_error is not None:
        return _unknown(collect_error.code or "unknown",
                        collect_error.message, host=rec_host,
                        cleanup=cleanup)
    lane, lane_reason = _muse.msp_credential_lane(
        reads.get("account", {}))
    if cleanup.get("state") != "clean":
        return _unknown("cleanup-incomplete",
                        "metadata parsed but the host did not close "
                        "cleanly (real EOF on both streams, drained "
                        "readers, reaped child, no kill, no break "
                        "all required): qualified unknown, not a "
                        "complete observation",
                        host=rec_host, cleanup=cleanup, reads=reads,
                        lane=(lane, lane_reason))
    return {"status": "ok", "code": "metadata-observed",
            "reason": "all four metadata reads parsed on the live "
                      "owned host and the host closed cleanly; "
                      "capability QA only, admits_worker stays False",
            "reads": reads, "credential_lane": lane,
            "lane_reason": lane_reason, "host": rec_host,
            "cleanup": cleanup, "admits_worker": False}
