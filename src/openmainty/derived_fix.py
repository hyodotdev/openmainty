"""Private derived source repair, Phase A only (Source23).

App-owned scoped source repair for a blocked typed-issue prepare op:
derive from the validated recovery origin, prepare a deterministic
isolated workspace, run ONE app-owned model repair worker that may
edit ONLY webpack/test sources, and store the validated worker
proposal as ``source_ready_unverified``. Phase A assembles NO model
review (the old blocked-op review contracts cannot consume the new
kind); a future phase needs its own typed source-bound review
constructor. No build, no tests, no tool runner, no registration.

The worker prompt asks for source edits only; the prompt is NOT an
OS-enforced tool ban. Confinement comes from the DEDICATED worker
constructor (``muse.exec_source_repair``: pinned binary re-hashed at
every launch, pinned meta/muse-spark-1.3/contributor-ON, exactly one
``--sandbox-network restricted`` flag, file-only disable flags,
narrowed child env, bounded owned process group, no trace on target
jobs) plus the server-side before/after diff gate below. The generic
``exec_prompt``/``build_cmd`` path stays byte-identical for the older
dependency workers and is never used here. Actual worker receipts
stay unproven until a later execution gate inspects them. The
worker's output is a proposal, never a verified fix. Original
parent/run/input/report records are never rewritten; links are
computed from the new relations.

All actual target work happens later through the rendered app, after
this code is independently accepted/promoted and a later execution
gate fires — never in this authoring turn.
"""
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import threading
from pathlib import Path

from . import batches, issue_recovery as _ir, muse
from . import runner as _runner
from . import paths as _paths
from . import preflight as _preflight


class DerivedFixError(Exception):
    """Typed failure for the derived-fix namespace (cf. ApiError)."""

    def __init__(self, message, status=500, code="error"):
        super().__init__(message)
        self.message = str(message)
        self.http_status = int(status)
        self.code = str(code)


def _require_target_authority() -> None:
    """Fail-closed preflight gate for target-A startup paths.

    Raises DerivedFixError with NO mutation unless a fresh genuine
    preflight bound to the SAME live binary/config/auth/policy/
    source identity verifies. Missing/stale/unknown/corrupt/auth/
    quota/offline/format refusals create no model/job/worktree
    mutation — the attempt stays queued and the operator retries
    after establishing a preflight. The dedicated verified worker
    constructor is the only path this gate opens; the old generic
    exec_prompt(extra=None) after checking a different profile is
    NOT authorized here. Offline tests redirect identity facts
    through the documented fixture env (like the fixture home);
    the comparison itself is never bypassed.
    """
    try:
        _preflight.check_target_authority(None)
    except _preflight.PreflightError as exc:
        raise DerivedFixError(str(exc.message), status=exc.status,
                              code=exc.code or "no-authority") from exc


def _link_loop(exc: OSError) -> bool:
    """True when O_NOFOLLOW refused a symlink (ELOOP).

    A no-follow open that hits a symlink fails with ELOOP instead
    of following it. That is proof of a swapped link on the owned
    path — not a transient I/O failure — so callers refuse with a
    dedicated tamper code instead of a generic unreadable.
    """
    return isinstance(exc, OSError) and exc.errno == errno.ELOOP


def _refused_dir_open(exc: OSError, lstat_path, *, dir_fd=None,
                      symlink_msg: str, notdir_msg: str,
                      unreadable_msg: str) -> DerivedFixError:
    """Narrowly classify a refused no-follow directory open.

    ELOOP proves a symlink. ENOTDIR from ``O_DIRECTORY | O_NOFOLLOW``
    may be a symlink (some kernels report ENOTDIR, not ELOOP, on a
    link) or a plain non-directory — distinguished here with a
    no-follow ``lstat`` that never follows the suspect, so a bare
    errno is never treated as proof of a symlink by itself. Anything
    else, or an unprovable race (the proof lstat fails), stays
    unreadable: EIO and other I/O failures are never rebranded, and
    nothing is created, deleted, rebound, or followed.
    """
    if _link_loop(exc):
        raise DerivedFixError(symlink_msg, status=502,
                              code="ancestor-replaced") from exc
    if exc.errno == errno.ENOTDIR:
        try:
            if dir_fd is None:
                st = os.lstat(lstat_path)
            else:
                st = os.lstat(lstat_path, dir_fd=dir_fd)
        except OSError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise DerivedFixError(symlink_msg, status=502,
                                      code="ancestor-replaced") from exc
            if not stat.S_ISDIR(st.st_mode):
                raise DerivedFixError(notdir_msg, status=502,
                                      code="ancestor-replaced") from exc
    raise DerivedFixError(f"{unreadable_msg}: {exc}",
                          status=502, code="unreadable") from exc


# ---- identity and bounds (own namespace; never the recovery shape) ----
_DERIVED_KIND = "derived-fix"
_DFIX_RE = re.compile(r"^dfix-[0-9a-f]{32}$")
_ATTEMPT_RE = re.compile(r"^da-[0-9a-f]{16}$")
_ORIGIN_INPUT_RE = re.compile(r"^input-[0-9a-f]{16}$")
_ORIGIN_REPORT_RE = re.compile(r"^report-[0-9a-f]{16}$")
_OPID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\+-]{0,200}$")
_HYPOTHESIS_DEFS = {
    # Canonical server-owned technical strategies. The browser
    # sends only the opaque id plus a free operator note; the
    # MEANING travels from this table into the exact sent prompt —
    # the worker never sees the UI and must not infer opaque radio
    # semantics. This is an operator technical experiment, never a
    # fabricated owner policy answer. The UI choice labels render
    # these same literals (pinned by tests on both sides).
    "H-A": "베이스 lib 제외 + 베이스 js/jsx 정규식 유지, "
           "후보 테스트를 이에 맞춰 적용",
    "H-B": "베이스 lib 제외 + 후보 mjs 정규식 적용, "
           "베이스 테스트 유지",
}
_HYPOTHESES = tuple(_HYPOTHESIS_DEFS)
# The ONLY source paths a Phase A worker may modify. Everything else
# — dist/*, *.LICENSE.txt, checkouts outside the derived root — is
# refused, never merged, never committed as a result.
_ALLOWED_SOURCE_PATHS = ("example/webpack.config.js",
                         "src/__tests__/webpack-web.test.ts")
_NOTE_CAP = 2000
_TEXT_CAP = 512
_QUESTIONS_CAP = 64
_WORKER_FILE_CAP = 16384
_WORKER_READ_MAX = 4194304
_WORKER_PROMPT_CAP = 32768
_PROMPT_QUESTIONS = 12
_PROMPT_QUESTION_CHARS = 500
_RAW_HEAD_CAP = 32768
_ERROR_CAP = 500
_REASON_CAP = 200
_WORKER_TIMEOUT_S = 600
# The exact model pinned for actual source-worker dispatch (user
# requires Meta muse-spark-1.3, contributor ON, subscription login
# only — never API-key billing). The artifact declaration MUST name
# this same model; the actual argv/invocation/tooling beyond the
# requested call stays explicitly unverified in this slice.
_WORKER_MODEL = "muse-spark-1.3"
_GIT_TIMEOUT_S = 120
_SCAN_CAP = 256
# Same explicit bound as the recovery review path: one hypothesis may
# be retried only this many times per record, each retry an explicit
# operator action — never an automatic loop.
_MAX_SOURCE_ATTEMPTS = 5
# App-owned automation identity for derived commits. Explicit, never a
# real user or owner, never test-fixture data, applied via `-c` flags
# only — no global git config is written.
_GIT_IDENTITY_NAME = "openmainty-derived-fix"
_GIT_IDENTITY_EMAIL = "openmainty-derived-fix@local"


def new_derived_id() -> str:
    """Fresh derived record id (never a prep-* id)."""
    return "dfix-" + secrets.token_hex(16)


def _lease_owner(op_id: str) -> str:
    return f"derived-fix:{op_id}"


def _home():
    try:
        return _paths.home()
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc


def _wrap_ir(exc: Exception) -> DerivedFixError:
    """Preserve the unchanged recovery validator's typed failure."""
    status = getattr(exc, "http_status", getattr(exc, "status", 500))
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 500
    code = str(getattr(exc, "code", "error") or "error")
    message = str(getattr(exc, "message", None) or exc)
    return DerivedFixError(message, status=status, code=code)


def _checked_derived_file(path, for_write: bool = False):
    """Fail-closed derived-record file: own namespace.

    Same discipline as the review-store root (single safe component,
    resolved path must equal the canonical store path with the SAME
    name, symlinks refused) but independent: it guards
    ``integration_ops/dfix-*.json``, never the recovery store, and
    raises DerivedFixError, never the recovery validator. With
    ``for_write`` the lexical/canonical checks run without requiring
    the file to exist yet; reads always require it.
    """
    if not isinstance(path, (str, Path)):
        raise DerivedFixError("bad derived record path",
                              status=400, code="bad-id")
    try:
        home = _home()
        home_r = home.resolve()
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc
    ops = batches.integration_ops_dir()
    try:
        lex = Path(path).relative_to(ops)
    except ValueError:
        raise DerivedFixError("derived record escapes the ops dir",
                              status=502, code="corrupt") from None
    if len(lex.parts) != 1 or not lex.parts[0].endswith(".json"):
        raise DerivedFixError("derived record is not a dfix file",
                              status=502, code="corrupt")
    stem = lex.parts[0][:-5]
    if not _DFIX_RE.fullmatch(stem):
        raise DerivedFixError("derived record is not a dfix file",
                              status=502, code="corrupt")
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError) as exc:
        raise DerivedFixError(f"unreadable derived store: {exc}",
                              status=502, code="corrupt") from exc
    if resolved != home_r / "integration_ops" / lex.parts[0]:
        raise DerivedFixError("refused symlinked derived store",
                              status=502, code="corrupt")
    if for_write:
        return resolved
    try:
        st = os.lstat(resolved)
    except FileNotFoundError:
        raise DerivedFixError("unknown derived record",
                              status=404,
                              code="unknown-derived") from None
    except OSError as exc:
        raise DerivedFixError(f"unreadable derived store: {exc}",
                              status=502, code="unreadable") from exc
    if stat.S_ISLNK(st.st_mode):
        raise DerivedFixError("refused symlinked derived store",
                              status=502, code="corrupt")
    if not stat.S_ISREG(st.st_mode):
        raise DerivedFixError("derived record is not a regular file",
                              status=502, code="corrupt")
    return resolved


def derived_path(dfix_id: str):
    """Canonical record file for a derived id (no reads)."""
    if not isinstance(dfix_id, str) or not _DFIX_RE.fullmatch(dfix_id):
        raise DerivedFixError(f"bad derived id: {dfix_id!r}",
                              status=400, code="bad-id")
    return batches.integration_ops_dir() / (dfix_id + ".json")


def _expected_work_path(dfix_id: str) -> str:
    return os.path.normpath(str(_home() / "derived" / dfix_id / "work"))


def _canonicalise_workdir(path: str) -> str:
    """Canonical workdir for the repair policy builder.

    The recorded ws path may pass through the symlinked
    app home (live: ~/.openmainty is a symlink to the
    owner's SSD store), while the policy check rightly
    refuses any workdir whose realpath differs. Resolve
    here, at the caller — never loosen the check. Pure
    realpath; the builder still refuses anything that is
    not an existing directory under the canonical home.
    """
    return os.path.realpath(path)


def _secure_work_chain(dfix_id: str) -> str:
    """Prove the task-owned directory chain from the canonical home.

    ``home/derived`` and ``home/derived/<dfix>`` must each exist as
    a real directory — never a symlink, never a file. A symlink at
    either ancestor (including one swapped in mid-run) refuses as a
    typed ancestor-replacement WITHOUT deleting, force-removing, or
    resolving-and-rebinding the suspect path: the workspace and all
    original identities are preserved for inspection. Missing
    ancestors are created (then re-verified); the caller still
    refuses an already-existing ``work`` leaf as busy. The leaf
    symlink walk below this covers ``work`` downward, so together
    the whole chain from the canonical home is task-owned.
    """
    try:
        home_r = str(Path(_home()).resolve())
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc
    ancestors = [os.path.join(home_r, "derived"),
                 os.path.join(home_r, "derived", dfix_id)]
    for ancestor in ancestors:
        try:
            st = os.lstat(ancestor)
        except FileNotFoundError:
            try:
                os.mkdir(ancestor)
            except OSError as exc:
                raise DerivedFixError(
                    f"cannot create derived dir: {exc}",
                    status=502, code="unreadable") from exc
            try:
                st = os.lstat(ancestor)
            except OSError as exc:
                raise DerivedFixError(
                    f"unreadable derived dir: {exc}",
                    status=502, code="unreadable") from exc
        except OSError as exc:
            raise DerivedFixError(f"unreadable derived dir: {exc}",
                                  status=502, code="unreadable") from exc
        if stat.S_ISLNK(st.st_mode):
            raise DerivedFixError(
                "refused: derived ancestor is a symlink "
                f"({ancestor}); suspect workspace preserved, "
                "nothing deleted or rebound",
                status=502, code="ancestor-replaced")
        if not stat.S_ISDIR(st.st_mode):
            raise DerivedFixError(
                f"derived ancestor is not a directory: {ancestor}",
                status=502, code="corrupt")
        if os.path.realpath(ancestor) != ancestor:
            raise DerivedFixError(
                "refused: derived ancestor escapes the canonical "
                f"home ({ancestor}); suspect workspace preserved, "
                "nothing deleted or rebound",
                status=502, code="ancestor-replaced")
    return ancestors[1]


def _reprove_work_chain(dfix_id: str) -> None:
    """Re-prove the owned chain WITHOUT creating or rebinding.

    Read-only counterpart of ``_secure_work_chain`` for every gate
    AFTER preparation (pre/post transport proofs, staging, result
    commit, result reads): both ancestors must still exist as real
    directories resolving to themselves. A mid-run replacement —
    including a swap after a genuine clean prep — refuses as typed
    ancestor-replacement with the suspect path and all original
    identities preserved. Missing ancestors refuse (never mkdir
    here); nothing is deleted, force-removed, or resolved-and-
    rebound.
    """
    try:
        home_r = str(Path(_home()).resolve())
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc
    for ancestor in (os.path.join(home_r, "derived"),
                     os.path.join(home_r, "derived", dfix_id)):
        try:
            st = os.lstat(ancestor)
        except FileNotFoundError:
            raise DerivedFixError(
                f"derived ancestor went missing: {ancestor}; "
                "refusing, nothing deleted or rebound",
                status=502, code="ancestor-replaced") from None
        except OSError as exc:
            raise DerivedFixError(f"unreadable derived dir: {exc}",
                                  status=502, code="unreadable") from exc
        if stat.S_ISLNK(st.st_mode) \
                or not stat.S_ISDIR(st.st_mode) \
                or os.path.realpath(ancestor) != ancestor:
            raise DerivedFixError(
                "refused: derived ancestor replaced mid-run "
                f"({ancestor}); suspect workspace preserved, "
                "nothing deleted or rebound",
                status=502, code="ancestor-replaced")


def _full_input_leaf(attempt_id: str) -> str:
    """Validated sidecar leaf name (no separators possible)."""
    if not _ATTEMPT_RE.fullmatch(attempt_id or ""):
        raise DerivedFixError(f"bad attempt id: {attempt_id!r}",
                              status=400, code="bad-attempt")
    return attempt_id + ".json"


def _open_full_input_dir(dfix_id: str, *, create: bool) -> int:
    """Open the sidecar container as a pinned directory descriptor.

    The chain is reproved read-only first; then home → derived →
    dfix → full-input is walked down with ``O_NOFOLLOW |
    O_DIRECTORY`` descriptors, each ``fstat``-verified as a real
    directory. A symlink at any level fails instead of being
    followed; all later leaf operations use the returned descriptor
    and can never escape it, be rebound, or traverse a planted link.
    Callers must ``os.close`` the descriptor. ``create=True`` creates
    only the final ``full-input`` level (owned ``os.mkdir``); reads
    never create anything.
    """
    if not _DFIX_RE.fullmatch(dfix_id or ""):
        raise DerivedFixError(f"bad derived id: {dfix_id!r}",
                              status=400, code="bad-id")
    _reprove_work_chain(dfix_id)
    try:
        home_r = str(Path(_home()).resolve())
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc

    def _open_dir(path: str, dir_fd=None) -> int:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY
                         | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if _link_loop(exc):
                raise DerivedFixError(
                    "refused: full-input ancestor is a symlink; "
                    "suspect layout preserved",
                    status=502, code="ancestor-replaced") from exc
            raise DerivedFixError(
                f"unreadable full-input ancestor: {exc}",
                status=502, code="ancestor-replaced") from exc
        try:
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise DerivedFixError(
                f"unreadable full-input ancestor: {exc}",
                status=502, code="ancestor-replaced") from exc
        if not is_dir:
            try:
                os.close(fd)
            except OSError:
                pass
            raise DerivedFixError(
                "refused: full-input ancestor is not a directory; "
                "suspect layout preserved",
                status=502, code="ancestor-replaced")
        return fd

    dfd = _open_dir(home_r)
    try:
        for level in ("derived", dfix_id):
            nxt = _open_dir(level, dir_fd=dfd)
            os.close(dfd)
            dfd = nxt
        if create:
            try:
                os.mkdir("full-input", 0o700, dir_fd=dfd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise DerivedFixError(
                    f"cannot create full-input dir: {exc}",
                    status=502, code="unreadable") from exc
        # The final level distinguishes a missing container (typed
        # missing-required, nothing created) from an unreadable or
        # replaced one (ancestor-replaced, suspect preserved).
        try:
            cfd = os.open("full-input", os.O_RDONLY | os.O_DIRECTORY
                          | os.O_NOFOLLOW, dir_fd=dfd)
        except FileNotFoundError as exc:
            raise DerivedFixError(
                "full-input container is missing; nothing created",
                status=502, code="missing-required") from exc
        except OSError as exc:
            if _link_loop(exc):
                raise DerivedFixError(
                    "refused: full-input container is a symlink; "
                    "suspect layout preserved",
                    status=502, code="ancestor-replaced") from exc
            raise DerivedFixError(
                f"unreadable full-input ancestor: {exc}",
                status=502, code="ancestor-replaced") from exc
        try:
            is_dir = stat.S_ISDIR(os.fstat(cfd).st_mode)
        except OSError as exc:
            try:
                os.close(cfd)
            except OSError:
                pass
            raise DerivedFixError(
                "refused: full-input container is not a directory; "
                "suspect layout preserved",
                status=502, code="ancestor-replaced") from exc
        if not is_dir:
            try:
                os.close(cfd)
            except OSError:
                pass
            raise DerivedFixError(
                "refused: full-input container is not a directory; "
                "suspect layout preserved",
                status=502, code="ancestor-replaced")
        return cfd
    finally:
        try:
            os.close(dfd)
        except OSError:
            pass


def _read_full_input_bytes(dfd: int, name: str) -> bytes:
    """Bounded no-follow read of one sidecar leaf (never a symlink).

    The descriptor is opened ``O_NONBLOCK`` so a swapped-in FIFO or
    device can never block the open; anything that is not a regular
    file refuses promptly with no leak. The size is proven by
    ``fstat`` BEFORE any byte is read; anything over the cap refuses
    without reading. The final NAME is then stat'ed directory-
    relative with no-follow and must still name the same device and
    inode as the open descriptor — a replacement between open and
    read (even a same-size swap the open fd cannot observe) refuses
    instead of being accepted. Exactly the proven size is then read
    and one extra byte is demanded to be EOF: early EOF and
    post-fstat growth both refuse, so a grown file can never pass as
    its pinned prefix. A closing ``fstat`` plus a closing name stat
    must agree with the open identity on device, inode, size, and
    mtime — a replaced or resized leaf refuses instead of being
    accepted as the immutable bytes. ``O_NOFOLLOW`` makes a
    swapped-in symlink fail instead of being followed — no outside
    read, ever.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=dfd)
    except FileNotFoundError as exc:
        raise DerivedFixError(
            "full input is required but missing",
            status=502, code="missing-required") from exc
    except OSError as exc:
        if _link_loop(exc):
            raise DerivedFixError(
                "refused: full-input leaf is a symlink; "
                "suspect artifact preserved, nothing followed",
                status=502, code="corrupt") from exc
        raise DerivedFixError(f"unreadable full input: {exc}",
                              status=502, code="unreadable") from exc
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise DerivedFixError(
                f"unreadable full input: initial stat failed: {exc}",
                status=502, code="unreadable") from exc
        if not stat.S_ISREG(st.st_mode):
            raise DerivedFixError(
                "refused: full-input leaf is not a regular file",
                status=502, code="corrupt")
        if st.st_size < 0 or st.st_size > _FULL_INPUT_CAP:
            raise DerivedFixError(
                "full input exceeds the raw artifact cap; refusing "
                "without reading",
                status=502, code="over-budget")
        try:
            link = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError as exc:
            raise DerivedFixError(
                "unreadable full input: "
                f"name stat failed: {exc}",
                status=502, code="unreadable") from exc
        if (link.st_dev, link.st_ino) != (st.st_dev, st.st_ino):
            raise DerivedFixError(
                "full input was replaced during open; refusing",
                status=502, code="corrupt")
        ident = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        chunks = []
        remaining = st.st_size
        while remaining > 0:
            try:
                piece = os.read(fd, min(remaining, 65536))
            except OSError as exc:
                raise DerivedFixError(
                    f"unreadable full input: {exc}",
                    status=502, code="unreadable") from exc
            if not piece:
                raise DerivedFixError(
                    "full input shrank during read; refusing a "
                    "partial prefix as the immutable bytes",
                    status=502, code="corrupt")
            chunks.append(piece)
            remaining -= len(piece)
        try:
            tail = os.read(fd, 1)
        except OSError as exc:
            raise DerivedFixError(
                f"unreadable full input: {exc}",
                status=502, code="unreadable") from exc
        if tail:
            raise DerivedFixError(
                "full input grew during read; refusing the pinned "
                "prefix as the immutable bytes",
                status=502, code="corrupt")
        try:
            st2 = os.fstat(fd)
            link2 = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except OSError as exc:
            raise DerivedFixError(
                f"unreadable full input: closing stat failed: {exc}",
                status=502, code="unreadable") from exc
        if (st2.st_dev, st2.st_ino, st2.st_size, st2.st_mtime_ns) \
                != ident \
                or (link2.st_dev, link2.st_ino) != (st.st_dev,
                                                   st.st_ino):
            raise DerivedFixError(
                "full input was replaced during read; refusing",
                status=502, code="corrupt")
        return b"".join(chunks)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _serialize_full_input(obj: dict) -> bytes:
    """Canonical bytes for the sidecar: exact format pinned here."""
    try:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise DerivedFixError(
            f"full input is not serializable: {exc}",
            status=502, code="corrupt") from exc
    try:
        raw = text.encode("utf-8") + b"\n"
    except UnicodeEncodeError as exc:
        # Lone surrogates (e.g. from escaped JSON halves) are not
        # valid UTF-8 and can never be dispatched: typed refusal,
        # never a 500 and never silent replacement.
        raise DerivedFixError(
            f"full input is not encodable UTF-8: {exc}",
            status=502, code="corrupt") from exc
    if len(raw) > _FULL_INPUT_CAP:
        raise DerivedFixError(
            f"full input exceeds {_FULL_INPUT_CAP} bytes; refusing",
            status=502, code="over-budget")
    return raw


def _loads_strict(raw: bytes) -> dict:
    """Parse JSON refusing duplicate keys (never silent last-wins)."""
    try:
        text = raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise DerivedFixError(f"full input is not UTF-8: {exc}",
                              status=502, code="corrupt") from exc

    def _no_dupes(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise DerivedFixError(
                    f"full input has a duplicate key: {key!r}",
                    status=502, code="corrupt")
            out[key] = value
        return out

    try:
        obj = json.loads(text, object_pairs_hook=_no_dupes)
    except DerivedFixError:
        raise
    except (ValueError, TypeError) as exc:
        raise DerivedFixError(f"full input is not JSON: {exc}",
                              status=502, code="corrupt") from exc
    if not isinstance(obj, dict):
        raise DerivedFixError("full input is not an object",
                              status=502, code="corrupt")
    return obj


def _validate_full_input(obj: dict, dfix_id: str, attempt_id: str,
                         attempt: dict) -> dict:
    """Closed-shape check of a parsed sidecar against its pin.

    Every int uses explicit type (bool never passes); every key set
    is exact; every digest is 40/64-hex in its own domain. The pin
    lives in the INDEPENDENT stored attempt (``full_input``), never
    trusted from inside the file it hashes.
    """
    if set(obj.keys()) != _FULL_INPUT_KEYS:
        raise DerivedFixError("full input keys malformed",
                              status=502, code="corrupt")
    if type(obj.get("input_schema")) is not int \
            or obj["input_schema"] != _FULL_INPUT_VERSION:
        raise DerivedFixError("full input schema malformed",
                              status=502, code="corrupt")
    if obj.get("dfix") != dfix_id or obj.get("attempt_id") != attempt_id:
        raise DerivedFixError("full input names the wrong attempt",
                              status=502, code="corrupt")
    # Op binding is enforced by the caller against the record
    # (describe path) and in _verify_full_input below; here only
    # shape is enforced.
    if not isinstance(obj.get("op_id"), str) or not obj["op_id"]:
        raise DerivedFixError("full input op binding malformed",
                              status=502, code="corrupt")
    text = obj.get("prompt_text")
    if not isinstance(text, str) or not text:
        raise DerivedFixError("full input prompt text malformed",
                              status=502, code="corrupt")
    try:
        prompt_bytes = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        # Parsed JSON may carry lone surrogates (\ud800 escapes);
        # they are not valid UTF-8 and can never match a dispatched
        # prompt: typed corrupt, never a 500.
        raise DerivedFixError(
            f"full input prompt is not encodable UTF-8: {exc}",
            status=502, code="corrupt") from exc
    if type(obj.get("prompt_bytes")) is not int \
            or obj["prompt_bytes"] != len(prompt_bytes) \
            or not 1 <= obj["prompt_bytes"] <= _WORKER_PROMPT_CAP:
        raise DerivedFixError("full input prompt size malformed",
                              status=502, code="corrupt")
    if not _is_hex(obj.get("prompt_sha256"), 64):
        raise DerivedFixError("full input prompt digest malformed",
                              status=502, code="corrupt")
    profile = obj.get("profile")
    if not isinstance(profile, dict) \
            or set(profile.keys()) != _FULL_INPUT_PROFILE_KEYS:
        raise DerivedFixError("full input profile malformed",
                              status=502, code="corrupt")
    # Every profile value is pinned exactly as the writer records it:
    # no arbitrary contributor strings, no invented digests or
    # capacities, no numbers where the writer emits text. Anything
    # the app did not actually observe stays the explicit honest
    # marker the writer emits (requested / not-collected / unknown).
    if profile.get("provider") != "meta" \
            or profile.get("model") != _WORKER_MODEL \
            or profile.get("contributor") != "on" \
            or profile.get("subscription") != "login-session" \
            or profile.get("argv_state") != "requested" \
            or profile.get("argv_note") != _FULL_INPUT_ARGV_NOTE \
            or profile.get("binary_sha256") != "not-collected" \
            or profile.get("toolset_doc_sha256") != "not-collected" \
            or profile.get("toolset_doc_version") != "not-collected" \
            or profile.get("capacity") != "unknown":
        raise DerivedFixError("full input profile malformed",
                              status=502, code="corrupt")
    if type(profile.get("timeout_s")) is not int \
            or profile["timeout_s"] != _WORKER_TIMEOUT_S:
        raise DerivedFixError("full input profile malformed",
                              status=502, code="corrupt")
    if not isinstance(profile.get("workdir"), str) \
            or not profile["workdir"]:
        raise DerivedFixError("full input profile malformed",
                              status=502, code="corrupt")
    extra = profile.get("extra")
    if extra is not None and (
            not isinstance(extra, list)
            or any(type(x) is not str for x in extra)):
        raise DerivedFixError("full input profile malformed",
                              status=502, code="corrupt")
    refs = obj.get("source_refs")
    if not isinstance(refs, dict) \
            or set(refs.keys()) != _FULL_INPUT_REF_KEYS \
            or any(not _is_hex(refs.get(k), 40)
                   for k in _FULL_INPUT_REF_KEYS):
        raise DerivedFixError("full input source refs malformed",
                              status=502, code="corrupt")
    evs = obj.get("evidence")
    # Exactly the builder's windows: one per allowed path per pinned
    # ref (3 x len(_ALLOWED_SOURCE_PATHS)) with unique exhaustive
    # (path, ref_kind) coverage — no more, no fewer, no duplicates,
    # no unknown ref kinds, no unbound digests, no numbers-as-bool.
    # sent/sent_bytes/sent_sha256 describe the EXACT fitted body in
    # the prompt (possibly an honest absent/oversize description or
    # a truncated fragment); omitted_reason is the closed honest
    # label for anything not fully delivered.
    if not isinstance(evs, list) \
            or len(evs) != 3 * len(_ALLOWED_SOURCE_PATHS):
        raise DerivedFixError("full input evidence malformed",
                              status=502, code="corrupt")
    seen = set()
    for ev in evs:
        if not isinstance(ev, dict) \
                or set(ev.keys()) != _FULL_INPUT_EVIDENCE_KEYS:
            raise DerivedFixError("full input evidence malformed",
                                  status=502, code="corrupt")
        if ev.get("path") not in _ALLOWED_SOURCE_PATHS \
                or ev.get("ref_kind") not in _FULL_INPUT_REF_KINDS \
                or not _is_hex(ev.get("ref"), 40) \
                or ev.get("status") not in _FULL_INPUT_EVIDENCE_STATUSES \
                or (ev.get("path"), ev.get("ref_kind")) in seen:
            raise DerivedFixError("full input evidence malformed",
                                  status=502, code="corrupt")
        seen.add((ev.get("path"), ev.get("ref_kind")))
        sent_bytes = ev.get("sent_bytes")
        sent_sha = ev.get("sent_sha256")
        if type(ev.get("sent")) is not bool \
                or type(ev.get("partial")) is not bool \
                or type(sent_bytes) is not int \
                or not 0 <= sent_bytes <= _WORKER_PROMPT_CAP \
                or ev.get("sent") != (sent_bytes > 0) \
                or (sent_sha is None) != (sent_bytes == 0) \
                or (sent_sha is not None
                    and not _is_hex(sent_sha, 64)):
            raise DerivedFixError("full input evidence malformed",
                                  status=502, code="corrupt")
        reason = ev.get("omitted_reason")
        status = ev.get("status")
        if status == "absent":
            # Absent and oversize windows are never truncated by the
            # fitter (their description passes through whole), so a
            # partial flag there is a lie.
            if reason != "absent-at-ref" \
                    or ev.get("partial") is not False:
                raise DerivedFixError(
                    "full input evidence malformed",
                    status=502, code="corrupt")
        elif status == "too-large":
            if reason != "blob-over-file-cap" \
                    or ev.get("partial") is not False:
                raise DerivedFixError(
                    "full input evidence malformed",
                    status=502, code="corrupt")
        elif reason not in (None, "over-global-budget") \
                or ev.get("partial") != (reason is not None):
            raise DerivedFixError("full input evidence malformed",
                                  status=502, code="corrupt")
        size = ev.get("size")
        if status == "absent":
            if size is not None:
                raise DerivedFixError(
                    "full input evidence malformed",
                    status=502, code="corrupt")
        elif type(size) is not int or size < 0:
            raise DerivedFixError("full input evidence malformed",
                                  status=502, code="corrupt")
    if seen != {(p, k) for p in _ALLOWED_SOURCE_PATHS
                for k in _FULL_INPUT_REF_KINDS}:
        raise DerivedFixError("full input evidence malformed",
                              status=502, code="corrupt")
    questions = obj.get("questions")
    # sent/omitted partition the total exactly (at most the prompt
    # cap of shown questions); clipped/clipped_bytes count the shown
    # questions shortened to the per-question display cap, with the
    # exact omitted byte count — a "sent" question is never claimed
    # fully delivered when only its prefix is in the prompt.
    if not isinstance(questions, dict) \
            or set(questions.keys()) != _FULL_INPUT_QUESTIONS_KEYS:
        raise DerivedFixError("full input questions malformed",
                              status=502, code="corrupt")
    for key in ("total", "sent", "omitted", "clipped",
                "clipped_bytes"):
        if type(questions.get(key)) is not int \
                or questions[key] < 0:
            raise DerivedFixError("full input questions malformed",
                                  status=502, code="corrupt")
    total = questions["total"]
    sent_q = questions["sent"]
    if sent_q > _PROMPT_QUESTIONS \
            or questions["omitted"] != total - sent_q \
            or (total <= _PROMPT_QUESTIONS
                and (sent_q != total or questions["omitted"] != 0)) \
            or (total > _PROMPT_QUESTIONS
                and (sent_q != _PROMPT_QUESTIONS
                     or questions["omitted"] != total
                     - _PROMPT_QUESTIONS)) \
            or questions["clipped"] > sent_q \
            or (questions["clipped"] == 0) \
            != (questions["clipped_bytes"] == 0):
        raise DerivedFixError("full input questions malformed",
                              status=502, code="corrupt")
    for key in ("conflicts", "original_touched"):
        val = obj.get(key)
        if not isinstance(val, list) or len(val) > _FULL_INPUT_LIST_CAP \
                or any(type(x) is not str for x in val):
            raise DerivedFixError(
                f"full input {key} malformed",
                status=502, code="corrupt")
    return obj


def _publish_full_input(dfix_id: str, attempt: dict, prompt: str,
                        info: dict, ws: dict, rec: dict) -> dict:
    """Atomically publish the immutable input sidecar, no overwrite.

    Canonical bytes are written to a unique temp (O_EXCL|O_NOFOLLOW,
    fsync) then `os.link`-published: the link fails atomically when
    the final already exists. Identical existing bytes confirm the
    stored artifact (never a re-dispatch excuse); differing bytes
    refuse and the old artifact survives. Returns the pin
    ``{"sha256", "bytes"}`` over the exact published file bytes.
    """
    if not isinstance(prompt, str) or not prompt:
        raise DerivedFixError("full input prompt is not text",
                              status=502, code="corrupt")
    try:
        prompt_bytes = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DerivedFixError(
            f"full input prompt is not encodable UTF-8: {exc}",
            status=502, code="corrupt") from exc
    if not 1 <= len(prompt_bytes) <= _WORKER_PROMPT_CAP:
        raise DerivedFixError("full input prompt over budget; refusing",
                              status=409, code="over-budget")
    if not isinstance(attempt.get("id"), str):
        raise DerivedFixError("full input attempt has no id",
                              status=502, code="corrupt")
    leaf = _full_input_leaf(attempt["id"])
    cfd = _open_full_input_dir(dfix_id, create=True)
    try:
        obj = {
        "input_schema": _FULL_INPUT_VERSION,
        "dfix": dfix_id,
        "attempt_id": attempt["id"],
        "op_id": rec.get("derived_from"),
        "prompt_text": prompt,
        "prompt_bytes": len(prompt_bytes),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "profile": {
            "provider": "meta",
            "model": _WORKER_MODEL,
            "contributor": "on",
            "subscription": "login-session",
            "timeout_s": _WORKER_TIMEOUT_S,
            "extra": None,
            "workdir": ws.get("path"),
            "argv_state": "requested",
            "argv_note": _FULL_INPUT_ARGV_NOTE,
            "binary_sha256": "not-collected",
            "toolset_doc_sha256": "not-collected",
            "toolset_doc_version": "not-collected",
            "capacity": "unknown",
        },
        "source_refs": {
            "candidate_head": rec["candidate"]["head"],
            "base_head": ws.get("base_head"),
            "prep_head": ws.get("prep_head"),
            "prep_tree": ws.get("prep_tree"),
        },
        "evidence": info.get("evidence", []),
        "questions": info.get("questions", {}),
        "conflicts": info.get("conflicts", []),
        "original_touched": info.get("original_touched", []),
    }
        raw = _serialize_full_input(obj)
        uniq = f"tmp.{os.getpid()}.{threading.get_ident()}"
        tmp = None
        for counter in range(8):
            cand = f"{uniq}.{counter}.tmp"
            try:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o600, dir_fd=cfd)
            except FileExistsError:
                continue
            except OSError as exc:
                raise DerivedFixError(
                    f"full-input temp failed: {exc}",
                    status=502, code="unreadable") from exc
            tmp = cand
            break
        if tmp is None:
            raise DerivedFixError("full-input temp unavailable",
                                  status=502, code="unreadable")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            try:
                os.unlink(tmp, dir_fd=cfd)
            except OSError:
                pass
            raise DerivedFixError(
                f"full-input write failed: {exc}",
                status=502, code="unreadable") from exc
        try:
            os.link(tmp, leaf, src_dir_fd=cfd, dst_dir_fd=cfd)
        except FileExistsError:
            # The final already exists: re-read it through the pinned
            # container with no-follow + fstat + size-first proof, so
            # a swapped-in symlink or oversized final refuses instead
            # of being followed into an outside read. Identical bytes
            # confirm the stored artifact (never a dispatch excuse);
            # differing bytes refuse, old bytes untouched.
            try:
                existing = _read_full_input_bytes(cfd, leaf)
            finally:
                try:
                    os.unlink(tmp, dir_fd=cfd)
                except OSError:
                    pass
            if existing != raw:
                raise DerivedFixError(
                    "full input already stored with different bytes; "
                    "refusing, existing artifact preserved",
                    status=502, code="duplicate-conflict")
        except OSError as exc:
            try:
                os.unlink(tmp, dir_fd=cfd)
            except OSError:
                pass
            raise DerivedFixError(
                f"full-input publish failed: {exc}",
                status=502, code="unreadable") from exc
        else:
            try:
                os.unlink(tmp, dir_fd=cfd)
            except OSError:
                pass
        return {"sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw)}
    finally:
        try:
            os.close(cfd)
        except OSError:
            pass


def _verify_full_input(dfix_id: str, attempt_id: str):
    """Re-read and fully bind a published sidecar (read-only).

    Reads through the pinned container descriptor with no-follow +
    fstat + size-first proof (a swapped symlink/ancestor refuses
    instead of being followed), parses with duplicate-key refusal,
    checks the closed schema, then binds the exact file bytes to the
    INDEPENDENT recorded attempt pin and the embedded prompt to the
    attempt's ``prompt_sha256``. Coordinated edits and metadata-only
    edits both fail against the pin. Creates nothing.
    """
    leaf = _full_input_leaf(attempt_id)
    cfd = _open_full_input_dir(dfix_id, create=False)
    try:
        raw = _read_full_input_bytes(cfd, leaf)
    finally:
        try:
            os.close(cfd)
        except OSError:
            pass
    # Read-only record access: the public GET must never create the
    # integration dir as a side effect on an absent/corrupt layout.
    rec = _read_record_ro(dfix_id)
    matches = [a for a in rec.get("source_attempts") or []
               if isinstance(a, dict) and a.get("id") == attempt_id]
    if len(matches) != 1:
        raise DerivedFixError("full input names no unique attempt",
                              status=502, code="corrupt")
    attempt = matches[0]
    pin = attempt.get("full_input")
    if pin is None:
        raise DerivedFixError(
            "full input was never published for this attempt",
            status=502, code="missing-required")
    # The pin is closed-shape too: a non-dict, an unknown key, a
    # non-hex digest, or a non-int/bool/out-of-range byte count never
    # validates — bool is not an int here and the count is strictly
    # positive and bounded by the same raw-artifact cap.
    if not isinstance(pin, dict) or set(pin.keys()) != _PIN_KEYS \
            or not _is_hex(pin.get("sha256"), 64) \
            or type(pin.get("bytes")) is not int \
            or not 1 <= pin["bytes"] <= _FULL_INPUT_CAP:
        raise DerivedFixError(
            "full input pin malformed",
            status=502, code="corrupt")
    if hashlib.sha256(raw).hexdigest() != pin["sha256"] \
            or len(raw) != pin["bytes"]:
        raise DerivedFixError(
            "full input bytes do not match the recorded pin",
            status=502, code="corrupt")
    obj = _validate_full_input(_loads_strict(raw), dfix_id,
                               attempt_id, attempt)
    if obj["op_id"] != rec.get("derived_from"):
        raise DerivedFixError("full input names the wrong parent op",
                              status=502, code="corrupt")
    if obj["prompt_sha256"] != attempt.get("prompt_sha256"):
        raise DerivedFixError(
            "full input prompt is not the attempted prompt",
            status=502, code="corrupt")
    if hashlib.sha256(
            obj["prompt_text"].encode("utf-8")).hexdigest() != \
            attempt.get("prompt_sha256"):
        raise DerivedFixError(
            "full input prompt bytes do not match the attempt",
            status=502, code="corrupt")
    return obj


def describe_full_input(op_id: str, dfix_id: str,
                        attempt_id: str) -> dict:
    """Read-only full-input view bound to one parent op (never writes).

    Legacy attempts (pin key absent) report ``legacy-not-recorded``;
    new attempts with a null pin report ``not-yet-dispatched`` while
    live or ``pre-input-failure`` once terminal. A present pin with a
    missing file is ``missing-required``; any mismatch is ``corrupt``.
    Only a fully bound artifact reports ``stored`` with its text.
    """
    # Parent-id validation is pure: the same character class the
    # frozen op-file helper enforces, checked here WITHOUT calling
    # it — that helper creates the integration dir as a side effect,
    # and a read-only view must never create directories. Binding is
    # proven from the record itself below; a missing layout simply
    # fails closed as unreadable, creating nothing.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,200}",
                        op_id or ""):
        raise DerivedFixError(f"bad parent op id: {op_id!r}",
                              status=404, code="bad-op")
    # Read-only record access: this GET creates no directories, even
    # on an absent/corrupt layout.
    rec = _read_record_ro(dfix_id)
    if rec.get("derived_from") != op_id:
        raise DerivedFixError("derived record is not bound to this op",
                              status=409, code="foreign-op")
    matches = [a for a in rec.get("source_attempts") or []
               if isinstance(a, dict) and a.get("id") == attempt_id]
    if len(matches) != 1:
        raise DerivedFixError("no unique attempt for this input",
                              status=404, code="unknown-attempt")
    attempt = matches[0]
    if "full_input" not in attempt:
        return {"dfix": dfix_id, "attempt_id": attempt_id,
                "status": "legacy-not-recorded"}
    if attempt.get("full_input") is None:
        if attempt.get("status") in _LIVE_ATTEMPT:
            return {"dfix": dfix_id, "attempt_id": attempt_id,
                    "status": "not-yet-dispatched"}
        return {"dfix": dfix_id, "attempt_id": attempt_id,
                "status": "pre-input-failure",
                "error": attempt.get("error") or ""}
    obj = _verify_full_input(dfix_id, attempt_id)
    # Omitted means honestly not fully delivered, labeled by reason —
    # never inferred from availability: an absent or oversize window
    # contributes its description bytes (sent) while its code is
    # omitted for the recorded reason; a fitted-truncated window is
    # partial with its delivered fragment SHA.
    return {"dfix": dfix_id, "attempt_id": attempt_id,
            "status": "stored",
            "prompt_sha256": obj["prompt_sha256"],
            "prompt_bytes": obj["prompt_bytes"],
            "pin_sha256": attempt["full_input"]["sha256"],
            "pin_bytes": attempt["full_input"]["bytes"],
            "source_refs": obj["source_refs"],
            "omitted": [{"path": e.get("path"),
                         "ref_kind": e.get("ref_kind"),
                         "reason": e.get("omitted_reason"),
                         "sent_bytes": e.get("sent_bytes")}
                        for e in obj["evidence"]
                        if e.get("omitted_reason") is not None],
            "evidence": [{"path": e.get("path"),
                          "ref_kind": e.get("ref_kind"),
                          "status": e.get("status"),
                          "sent": e.get("sent"),
                          "partial": e.get("partial"),
                          "sent_bytes": e.get("sent_bytes"),
                          "omitted_reason": e.get("omitted_reason")}
                         for e in obj["evidence"]],
            "profile": obj["profile"],
            "questions": obj["questions"],
            "conflicts": obj["conflicts"],
            "original_touched": obj["original_touched"],
            "text": obj["prompt_text"]}


def _read_record(dfix_id: str):
    doc_path = _checked_derived_file(derived_path(dfix_id))
    try:
        raw = doc_path.read_bytes()
    except OSError as exc:
        raise DerivedFixError(f"unreadable derived record: {exc}",
                              status=502, code="unreadable") from exc
    try:
        rec = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise DerivedFixError(f"derived record is not JSON: {exc}",
                              status=502, code="corrupt") from exc
    return _validate_derived_record(rec)


def _open_record_dir():
    """Pin home/integration_ops as descriptors (read-only, no mkdir).

    Both levels are opened ``O_NOFOLLOW | O_DIRECTORY`` and
    fstat-verified as real directories: a swapped-in symlink, file,
    or FIFO at either ancestor refuses INSTEAD of being followed,
    and no later leaf operation can escape the proven container.
    A missing integration_ops dir is an unknown record (nothing to
    read, nothing created); any other failure is unreadable or a
    replaced ancestor. Callers close both descriptors.
    """
    try:
        home_r = str(Path(_home()).resolve())
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc
    try:
        hfd = os.open(home_r, os.O_RDONLY | os.O_DIRECTORY
                      | os.O_NOFOLLOW)
    except OSError as exc:
        raise _refused_dir_open(
            exc, home_r,
            symlink_msg=("refused: app home is a symlink; "
                         "suspect layout preserved"),
            notdir_msg="refused: app home is not a directory",
            unreadable_msg="unreadable app home") from exc
    try:
        home_is_dir = stat.S_ISDIR(os.fstat(hfd).st_mode)
    except OSError as exc:
        try:
            os.close(hfd)
        except OSError:
            pass
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc
    if not home_is_dir:
        try:
            os.close(hfd)
        except OSError:
            pass
        raise DerivedFixError(
            "refused: app home is not a directory",
            status=502, code="ancestor-replaced")
    try:
        try:
            ofd = os.open("integration_ops", os.O_RDONLY
                          | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=hfd)
        except FileNotFoundError as exc:
            raise DerivedFixError("unknown derived record",
                                  status=404,
                                  code="unknown-derived") from exc
        except OSError as exc:
            raise _refused_dir_open(
                exc, "integration_ops", dir_fd=hfd,
                symlink_msg=("refused: integration store is a "
                             "symlink; suspect layout preserved"),
                notdir_msg=("refused: integration store is not a "
                            "directory; suspect layout preserved"),
                unreadable_msg="unreadable derived store") from exc
        try:
            is_dir = stat.S_ISDIR(os.fstat(ofd).st_mode)
        except OSError as exc:
            try:
                os.close(ofd)
            except OSError:
                pass
            raise DerivedFixError(
                f"unreadable derived store: {exc}",
                status=502, code="unreadable") from exc
        if not is_dir:
            try:
                os.close(ofd)
            except OSError:
                pass
            raise DerivedFixError(
                "refused: integration store is not a directory; "
                "suspect layout preserved",
                status=502, code="ancestor-replaced")
        return hfd, ofd
    except DerivedFixError:
        try:
            os.close(hfd)
        except OSError:
            pass
        raise


def _read_record_ro(dfix_id: str):
    """Read-only record access for the full-input GET (never mkdir).

    ``derived_path`` reaches ``batches.integration_ops_dir()``, which
    creates the integration dir as a side effect — unacceptable on a
    read-only view of an absent/corrupt layout. Instead the
    home/integration_ops chain is pinned with no-follow directory
    descriptors (a swapped ancestor refuses instead of being
    followed), and the leaf is opened ``O_NONBLOCK`` so a planted
    FIFO or device can never block the open. The size is proven by
    fstat BEFORE any byte is read and bounded by a reasoned record
    cap (far above the honest maximum: at most five attempts each
    carrying bounded raw heads and metadata, well under a megabyte);
    anything larger refuses without reading. The final name is
    stat'ed directory-relative with no-follow and must still name
    the open device and inode, at open and again at close — a
    same-size replacement the open descriptor cannot observe still
    refuses. Exactly the proven size is read, one extra byte must
    be EOF, and a closing fstat must show the same device, inode,
    size, and mtime — growth, early EOF, and replacement all
    refuse. Decoding is strict UTF-8 and duplicate JSON keys refuse,
    exactly like the sidecar loader;
    the UNCHANGED record validator runs on the parsed object. A
    missing record or layout fails closed and creates nothing.
    """
    if not isinstance(dfix_id, str) or not _DFIX_RE.fullmatch(dfix_id):
        raise DerivedFixError(f"bad derived id: {dfix_id!r}",
                              status=400, code="bad-id")
    # The anchored dfix hex id cannot hold separators, so the leaf
    # name can never escape the pinned container.
    leaf = dfix_id + ".json"
    hfd, ofd = _open_record_dir()
    try:
        try:
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_NONBLOCK, dir_fd=ofd)
        except FileNotFoundError as exc:
            raise DerivedFixError("unknown derived record",
                                  status=404,
                                  code="unknown-derived") from exc
        except OSError as exc:
            if _link_loop(exc):
                raise DerivedFixError(
                    "refused: derived record leaf is a symlink; "
                    "suspect store preserved, nothing followed",
                    status=502, code="corrupt") from exc
            raise DerivedFixError(
                f"unreadable derived store: {exc}",
                status=502, code="unreadable") from exc
        try:
            try:
                st = os.fstat(fd)
            except OSError as exc:
                raise DerivedFixError(
                    "unreadable derived record: "
                    f"initial stat failed: {exc}",
                    status=502, code="unreadable") from exc
            if not stat.S_ISREG(st.st_mode):
                raise DerivedFixError(
                    "derived record is not a regular file",
                    status=502, code="corrupt")
            if st.st_size < 0 or st.st_size > _RECORD_READ_CAP:
                raise DerivedFixError(
                    "derived record exceeds the read cap; refusing "
                    "without reading",
                    status=502, code="over-budget")
            try:
                link = os.stat(leaf, dir_fd=ofd,
                               follow_symlinks=False)
            except OSError as exc:
                raise DerivedFixError(
                    "unreadable derived record: "
                    f"name stat failed: {exc}",
                    status=502, code="unreadable") from exc
            if (link.st_dev, link.st_ino) != (st.st_dev, st.st_ino):
                raise DerivedFixError(
                    "derived record was replaced during open; "
                    "refusing",
                    status=502, code="corrupt")
            ident = (st.st_dev, st.st_ino, st.st_size,
                     st.st_mtime_ns)
            chunks = []
            remaining = st.st_size
            while remaining > 0:
                try:
                    piece = os.read(fd, min(remaining, 65536))
                except OSError as exc:
                    raise DerivedFixError(
                        f"unreadable derived record: {exc}",
                        status=502, code="unreadable") from exc
                if not piece:
                    raise DerivedFixError(
                        "derived record shrank during read; refusing "
                        "a partial prefix",
                        status=502, code="corrupt")
                chunks.append(piece)
                remaining -= len(piece)
            try:
                tail = os.read(fd, 1)
            except OSError as exc:
                raise DerivedFixError(
                    f"unreadable derived record: {exc}",
                    status=502, code="unreadable") from exc
            if tail:
                raise DerivedFixError(
                    "derived record grew during read; refusing",
                    status=502, code="corrupt")
            try:
                st2 = os.fstat(fd)
                link2 = os.stat(leaf, dir_fd=ofd,
                                follow_symlinks=False)
            except OSError as exc:
                raise DerivedFixError(
                    "unreadable derived record: "
                    f"closing stat failed: {exc}",
                    status=502, code="unreadable") from exc
            if (st2.st_dev, st2.st_ino, st2.st_size,
                    st2.st_mtime_ns) != ident \
                    or (link2.st_dev, link2.st_ino) != (st.st_dev,
                                                       st.st_ino):
                raise DerivedFixError(
                    "derived record was replaced during read; "
                    "refusing",
                    status=502, code="corrupt")
            raw = b"".join(chunks)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    finally:
        for dir_fd in (ofd, hfd):
            try:
                os.close(dir_fd)
            except OSError:
                pass
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DerivedFixError(
            f"derived record is not UTF-8: {exc}",
            status=502, code="corrupt") from exc

    def _no_dupes(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise DerivedFixError(
                    f"derived record has a duplicate key: {key!r}",
                    status=502, code="corrupt")
            out[key] = value
        return out

    try:
        rec = json.loads(text, object_pairs_hook=_no_dupes)
    except DerivedFixError:
        raise
    except ValueError as exc:
        raise DerivedFixError(f"derived record is not JSON: {exc}",
                              status=502, code="corrupt") from exc
    if not isinstance(rec, dict):
        raise DerivedFixError("derived record is not an object",
                              status=502, code="corrupt")
    return _validate_derived_record(rec)


def _write_record(rec: dict) -> None:
    """Atomically replace ONLY our own derived record.

    Validates exact shape first, then writes a uniquely-named temp
    file in the same directory and renames it over the record
    (same supported local pattern as the negative-record writer:
    unique temp + os.replace). A failed write leaves the old record
    byte-identical and readable — history is never half-written —
    and removes its own temp best-effort. Concurrent writers still
    serialize on the caller-held op exclusion, not here.
    """
    _validate_derived_record(rec, for_write=True)
    doc_path = derived_path(rec["op_id"])
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    _checked_derived_file(doc_path, for_write=True)
    payload = json.dumps(rec, ensure_ascii=False,
                         sort_keys=True).encode("utf-8")
    # Own fresh exclusive temp: O_CREAT|O_EXCL|O_NOFOLLOW never
    # follows a pre-existing symlink and never reuses a predictable
    # name — a planted temp symlink is met with FileExistsError,
    # never traversed. The failed write leaves the existing record
    # and all artifacts byte-identical and removes only its own temp
    # best-effort. A bounded counter suffix keeps concurrent writers
    # in this process from colliding.
    base = doc_path.name + ".%d.%d" % (os.getpid(),
                                       threading.get_ident())
    for counter in range(8):
        tmp = doc_path.with_name(f"{base}.{counter}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            continue
        except OSError as exc:
            raise DerivedFixError(
                f"derived record temp failed: {exc}",
                status=502, code="unreadable") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise DerivedFixError(
                f"derived record write failed: {exc}",
                status=502, code="unreadable") from exc
        try:
            os.replace(tmp, doc_path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise DerivedFixError(
                f"derived record replace failed: {exc}",
                status=502, code="unreadable") from exc
        return
    raise DerivedFixError("derived record temp unavailable",
                          status=502, code="unreadable")


_RECORD_KEYS = {"schema", "kind", "op", "op_id", "derived_from",
                "lease_owner", "origin", "candidate", "base",
                "hypothesis", "workspace", "source_attempts",
                "latest_attempt", "job", "status", "error", "created_at",
                "updated_at"}
_ORIGIN_KEYS = {"input", "report", "input_sha256", "report_sha256",
                "fingerprint", "base_commit", "questions"}
# Actual parent descriptor keys only: candidate carries
# branch/fingerprint/head/worktree, base carries
# batch/checkout/fingerprint/head/lane. Nothing else is persisted —
# no repo/run_id/lane copies, no worktree/branch on base.
_CAND_KEYS = {"branch", "fingerprint", "head", "worktree"}
_BASE_KEYS = {"batch", "checkout", "fingerprint", "head", "lane"}
_HYP_KEYS = {"id", "note"}
_WS_KEYS = {"path", "head", "fingerprint", "clean", "base_head",
            "prep_tree", "prep_head", "applied", "conflict_reset",
            "retained_base", "allowed_changed_paths", "result_head",
            "result_tree", "scope"}
# 299b: new records carry the prep-time .git identity ("git").
# Legacy records lack it (never rewritten); they validate but run
# unbound (hardened flags/env, discovery-based paths).
_NEW_WS_KEYS = _WS_KEYS | {"git"}
_GIT_REC_KEYS = {"kind", "gitdir", "pointer_hex", "config_sha256",
                 "hooks"}
_SCOPE_KEYS = {"conflicts", "original_touched", "touched"}
_APPLIED_KEYS = {"path", "bytes", "blob_sha256"}
_RESET_KEYS = {"path", "base_blob", "base_blob_sha256",
               "absent_at_base", "size", "reason"}
_RETAINED_KEYS = {"path", "base_blob", "base_blob_sha256",
                  "absent_at_base", "size", "note"}
_ATTEMPT_KEYS = {"id", "hypothesis_id", "status", "prompt_sha256",
                 "worker_report", "verified_paths", "job", "error",
                 "log_bytes", "truncated", "raw_head",
                 "raw_head_sha256", "raw_total_chars",
                 "raw_total_bytes", "started_at", "ended_at"}
# New closed attempt key: the full-input pin. Old stored attempts lack
# it (legacy, never rewritten); new attempts always carry it (null
# until the prompt is composed, dict after). Both keysets validate.
_NEW_ATTEMPT_KEYS = _ATTEMPT_KEYS | {"full_input"}
_PIN_KEYS = {"sha256", "bytes"}
# Canonical sidecar serialization: exact bytes are pinned, so the
# format is fixed here, not inferred at read time.
_FULL_INPUT_VERSION = 1
_FULL_INPUT_KEYS = {"input_schema", "dfix", "attempt_id", "op_id",
                    "prompt_text", "prompt_bytes", "prompt_sha256",
                    "profile", "source_refs", "evidence", "questions",
                    "conflicts", "original_touched"}
_FULL_INPUT_PROFILE_KEYS = {"provider", "model", "contributor",
                            "subscription", "timeout_s", "extra",
                            "workdir", "argv_state", "argv_note",
                            "binary_sha256", "toolset_doc_sha256",
                            "toolset_doc_version", "capacity"}
_FULL_INPUT_REF_KEYS = {"candidate_head", "base_head", "prep_head",
                        "prep_tree"}
_FULL_INPUT_EVIDENCE_KEYS = {"path", "ref_kind", "ref", "status",
                             "size", "sent", "partial",
                             "sent_bytes", "sent_sha256",
                             "omitted_reason"}
_FULL_INPUT_EVIDENCE_STATUSES = ("ok", "too-large", "absent")
# Closed omission vocabulary: the only honest reasons a window's
# code is not fully in the prompt. None means fully delivered.
_FULL_INPUT_OMITTED_REASONS = ("absent-at-ref", "blob-over-file-cap",
                               "over-global-budget")
_FULL_INPUT_REF_KINDS = ("base", "candidate", "prepared")
# Raw-artifact cap: the full 32KiB UTF-8 prompt domain must always fit.
# With ensure_ascii=False, multibyte passes through unexpanded; only
# ASCII controls/quotes/backslashes expand, at most 6 bytes per byte
# (\u00XX). Worst case: 32768 x 6 = 196608 prompt bytes + metadata
# (refs/profile/evidence/questions/conflicts — tens of KB at most).
# 262144 covers that worst case with margin and stays bounded; larger
# refuses loudly instead of trimming or silently dropping evidence.
_FULL_INPUT_CAP = 262144
# The exact argv note the writer records: argv is assembled inside the
# model call, so only the requested call is declared here and the
# actual invocation stays explicitly unverified.
_FULL_INPUT_ARGV_NOTE = (
    "argv is assembled inside muse.exec_prompt; "
    "only the requested call is recorded here, "
    "actual invocation is unverified")
_FULL_INPUT_QUESTIONS_KEYS = {"total", "sent", "omitted",
                              "clipped", "clipped_bytes"}
_FULL_INPUT_LIST_CAP = 4096
# Reasoned bound for read-only record reads: an honest record holds
# at most five attempts, each with bounded raw heads (32KiB text),
# bounded errors/notes, a small pinned workspace, and origin
# metadata — well under a megabyte in every real case. 4 MiB is more
# than 4x headroom while still refusing absurd or hostile sizes
# without reading them. This is a read-gate only; it changes no
# validator, identity, or write path.
_RECORD_READ_CAP = 4194304
_REPORT_KEYS = {"status", "changed_paths", "per_file_sha256",
                "diff_stat", "resolutions", "notes"}
_RESOLUTION_KEYS = {"path", "conflict_id", "choice"}
_JOB_KEYS = {"id", "kind", "label"}
_LATEST_KEYS = {"id", "status"}
_STATUSES = ("started", "source-running", "source-ready-unverified",
             "source-error", "refused", "stale", "cancelled")
_TERMINAL_OK = ("source-ready-unverified",)
_TERMINAL_BAD = ("source-error", "refused", "stale", "cancelled")
_LIVE_ATTEMPT = ("queued", "started", "source-running")


def _is_hex(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(
        c in "0123456789abcdef" for c in value)


def _clean_rel(path) -> str:
    """A clean repo-relative path, else refuse."""
    if not isinstance(path, str) or not path or len(path) > 256 \
            or path.startswith("/") or "\x00" in path:
        raise DerivedFixError(f"refused path shape: {path!r}",
                              status=502, code="corrupt")
    parts = path.split("/")
    if not all(parts) or ".." in parts:
        raise DerivedFixError(f"refused escaping path: {path!r}",
                              status=502, code="corrupt")
    return path


def _validate_derived_record(rec: dict, for_write: bool = False) -> dict:
    """Exact-shape validation for the derived record (both ends).

    Unknown keys, kinds, versions, or statuses never migrate or heal:
    they refuse as corrupt. History (source_attempts) is append-only;
    only forward status transitions keep their meaning. The schema
    check is explicit-int equality (``type(x) is int and x == 1``):
    ``True`` (bool), ``1.0`` (float), and unknown versions refuse —
    without relying on identity comparison.
    """
    _ = for_write
    if not isinstance(rec, dict) or set(rec.keys()) != _RECORD_KEYS:
        raise DerivedFixError("derived record keys malformed",
                              status=502, code="corrupt")
    schema = rec.get("schema")
    if type(schema) is not int or schema != 1 \
            or rec.get("kind") != _DERIVED_KIND \
            or rec.get("op") != _DERIVED_KIND:
        raise DerivedFixError("derived record kind malformed",
                              status=502, code="corrupt")
    if not _DFIX_RE.fullmatch(str(rec.get("op_id") or "")):
        raise DerivedFixError("derived record id malformed",
                              status=502, code="corrupt")
    parent_op = rec.get("derived_from")
    if not isinstance(parent_op, str) or not _OPID_RE.fullmatch(parent_op):
        raise DerivedFixError("derived parent link malformed",
                              status=502, code="corrupt")
    if rec.get("lease_owner") != _lease_owner(parent_op):
        raise DerivedFixError("derived lease owner malformed",
                              status=502, code="corrupt")
    origin = rec.get("origin")
    if not isinstance(origin, dict) or set(origin.keys()) != _ORIGIN_KEYS:
        raise DerivedFixError("derived origin malformed",
                              status=502, code="corrupt")
    if not _ORIGIN_INPUT_RE.fullmatch(str(origin.get("input") or "")):
        raise DerivedFixError("derived origin input name malformed",
                              status=502, code="corrupt")
    if not _ORIGIN_REPORT_RE.fullmatch(str(origin.get("report") or "")):
        raise DerivedFixError("derived origin report name malformed",
                              status=502, code="corrupt")
    for key in ("input_sha256", "report_sha256"):
        if not _is_hex(origin.get(key), 64):
            raise DerivedFixError("derived origin digest malformed",
                                  status=502, code="corrupt")
    fp = origin.get("fingerprint")
    if not _is_hex(fp, 64):
        raise DerivedFixError("derived origin fingerprint malformed",
                              status=502, code="corrupt")
    if not _is_hex(origin.get("base_commit"), 40):
        raise DerivedFixError("derived origin base commit malformed",
                              status=502, code="corrupt")
    # Path-ID containment: the names must name the fingerprinted input.
    if origin["input"] != f"input-{fp[:16]}" \
            or origin["report"] != f"report-{fp[:16]}":
        raise DerivedFixError("derived origin names do not match "
                              "their fingerprint",
                              status=502, code="corrupt")
    questions = origin.get("questions")
    if not isinstance(questions, list) or len(questions) > _QUESTIONS_CAP \
            or any(not isinstance(q, str) or not q or len(q) > _TEXT_CAP
                   for q in questions):
        raise DerivedFixError("derived origin questions malformed",
                              status=502, code="corrupt")
    cand = rec.get("candidate")
    if not isinstance(cand, dict) or set(cand.keys()) != _CAND_KEYS:
        raise DerivedFixError("derived candidate ref malformed",
                              status=502, code="corrupt")
    if not isinstance(cand.get("branch"), str) or not cand["branch"] \
            or len(cand["branch"]) > 256:
        raise DerivedFixError("derived candidate branch malformed",
                              status=502, code="corrupt")
    if not _is_hex(cand.get("fingerprint"), 64):
        raise DerivedFixError("derived candidate fingerprint malformed",
                              status=502, code="corrupt")
    if not _is_hex(cand.get("head"), 40):
        raise DerivedFixError("derived candidate head malformed",
                              status=502, code="corrupt")
    if not isinstance(cand.get("worktree"), str) or not cand["worktree"] \
            or len(cand["worktree"]) > 1024:
        raise DerivedFixError("derived candidate worktree malformed",
                              status=502, code="corrupt")
    base = rec.get("base")
    if not isinstance(base, dict) or set(base.keys()) != _BASE_KEYS:
        raise DerivedFixError("derived base ref malformed",
                              status=502, code="corrupt")
    for key in ("batch", "lane"):
        if not isinstance(base.get(key), str) or not base[key] \
                or len(base[key]) > 256:
            raise DerivedFixError(f"derived base {key} malformed",
                                  status=502, code="corrupt")
    if not _is_hex(base.get("fingerprint"), 64):
        raise DerivedFixError("derived base fingerprint malformed",
                              status=502, code="corrupt")
    if not _is_hex(base.get("head"), 40):
        raise DerivedFixError("derived base head malformed",
                              status=502, code="corrupt")
    if not isinstance(base.get("checkout"), str) or not base["checkout"] \
            or len(base["checkout"]) > 1024:
        raise DerivedFixError("derived base checkout malformed",
                              status=502, code="corrupt")
    hyp = rec.get("hypothesis")
    if not isinstance(hyp, dict) or set(hyp.keys()) != _HYP_KEYS \
            or hyp.get("id") not in _HYPOTHESES \
            or not isinstance(hyp.get("note"), str) \
            or not hyp["note"] or len(hyp["note"]) > _NOTE_CAP:
        raise DerivedFixError("derived hypothesis malformed",
                              status=502, code="corrupt")
    ws = rec.get("workspace")
    if ws is not None:
        _validate_workspace(ws, rec["op_id"])
    attempts = rec.get("source_attempts")
    if not isinstance(attempts, list):
        raise DerivedFixError("derived attempts malformed",
                              status=502, code="corrupt")
    for attempt in attempts:
        _validate_attempt(attempt)
    # Attempt ids are unique and at most one attempt may be live:
    # history is append-only and a second live attempt is never
    # minted while one lives.
    seen_ids = [a.get("id") for a in attempts]
    if len(set(seen_ids)) != len(seen_ids):
        raise DerivedFixError("derived attempt ids not unique",
                              status=502, code="corrupt")
    if sum(1 for a in attempts
           if a.get("status") in _LIVE_ATTEMPT) > 1:
        raise DerivedFixError("derived record has two live attempts",
                              status=502, code="corrupt")
    latest = rec.get("latest_attempt")
    if latest is not None:
        if not isinstance(latest, dict) \
                or set(latest.keys()) != _LATEST_KEYS \
                or not _ATTEMPT_RE.fullmatch(str(latest.get("id") or "")) \
                or latest.get("status") not in _STATUSES + ("queued",):
            raise DerivedFixError("derived latest pointer malformed",
                                  status=502, code="corrupt")
        # The pointer is bound to a UNIQUE actual attempt: same id,
        # same status, same hypothesis. A dangling or ambiguous
        # pointer is corruption, never a record.
        matches = [a for a in attempts
                   if a.get("id") == latest.get("id")]
        if len(matches) != 1:
            raise DerivedFixError("derived latest names no unique "
                                  "attempt",
                                  status=502, code="corrupt")
        if matches[0].get("status") != latest.get("status"):
            raise DerivedFixError("derived latest status mismatch",
                                  status=502, code="corrupt")
        if matches[0].get("hypothesis_id") != (
                rec.get("hypothesis") or {}).get("id"):
            raise DerivedFixError("derived latest hypothesis mismatch",
                                  status=502, code="corrupt")
        if latest.get("status") == "source-ready-unverified":
            ws_ready = rec.get("workspace") or {}
            if ws_ready.get("result_head") is None \
                    or ws_ready.get("result_tree") is None:
                raise DerivedFixError("ready latest without a "
                                      "recorded result",
                                      status=502, code="corrupt")
    job = rec.get("job")
    if job is not None:
        # The stored link names an ACTUAL job: the derived-fix kind
        # and a label that is one of the record's own attempt ids.
        # While the latest attempt lives the label must name it —
        # a stale link from an older attempt never owns the run.
        if not isinstance(job, dict) or set(job.keys()) != _JOB_KEYS \
                or not isinstance(job.get("id"), str) or not job["id"] \
                or job.get("kind") != _DERIVED_KIND \
                or not _ATTEMPT_RE.fullmatch(str(job.get("label")
                                                 or "")) \
                or job.get("label") not in seen_ids:
            raise DerivedFixError("derived job link malformed",
                                  status=502, code="corrupt")
        if latest is not None \
                and latest.get("status") in _LIVE_ATTEMPT \
                and job.get("label") != latest.get("id"):
            raise DerivedFixError("derived job link does not name the "
                                  "live attempt",
                                  status=502, code="corrupt")
    if rec.get("status") not in _STATUSES:
        raise DerivedFixError("derived status malformed",
                              status=502, code="corrupt")
    # Recorded status must mirror the latest attempt: open records
    # read "started"; terminal records repeat their terminal status.
    # A preparation refusal with no attempt yet is a valid record —
    # it fabricates no attempt, transport, or job. But a NONEMPTY
    # history without its unique latest binding is corruption, never
    # an open record: the pointer invariant holds from the first
    # stored attempt.
    if latest is None and attempts:
        raise DerivedFixError("derived attempts without a latest "
                              "pointer",
                              status=502, code="corrupt")
    latest_status = latest.get("status") if latest else None
    if rec.get("status") == "stale":
        # A recorded result that no longer verifies: the latest
        # terminal attempt stays untouched, only the record is stale.
        if latest_status not in _TERMINAL_OK:
            raise DerivedFixError("stale record without a recorded "
                                  "result",
                                  status=502, code="corrupt")
    elif latest_status in _TERMINAL_OK + _TERMINAL_BAD:
        if rec.get("status") != latest_status:
            raise DerivedFixError("derived status does not mirror its "
                                  "terminal attempt",
                                  status=502, code="corrupt")
    elif latest_status is None:
        if rec.get("status") not in ("started", "refused"):
            raise DerivedFixError("derived open record must read "
                                  "started or refused",
                                  status=502, code="corrupt")
    elif rec.get("status") != "started":
        raise DerivedFixError("derived open record must read started",
                              status=502, code="corrupt")
    if not isinstance(rec.get("error"), str) \
            or len(rec["error"]) > _ERROR_CAP:
        raise DerivedFixError("derived error malformed",
                              status=502, code="corrupt")
    for key in ("created_at", "updated_at"):
        if not isinstance(rec.get(key), str) or not rec[key]:
            raise DerivedFixError("derived timestamp malformed",
                                  status=502, code="corrupt")
    return rec


def _validate_git_record(rec) -> None:
    if not isinstance(rec, dict) or set(rec.keys()) != _GIT_REC_KEYS:
        raise DerivedFixError("derived git record malformed",
                              status=502, code="corrupt")
    if rec.get("kind") not in ("file", "dir"):
        raise DerivedFixError("derived git record malformed",
                              status=502, code="corrupt")
    if not isinstance(rec.get("gitdir"), str) or not os.path.isabs(
            rec["gitdir"]):
        raise DerivedFixError("derived git record malformed",
                              status=502, code="corrupt")
    if not isinstance(rec.get("pointer_hex"), str):
        raise DerivedFixError("derived git record malformed",
                              status=502, code="corrupt")
    try:
        bytes.fromhex(rec["pointer_hex"])
    except ValueError:
        raise DerivedFixError("derived git record malformed",
                              status=502, code="corrupt") from None
    if not isinstance(rec.get("config_sha256"), str) or \
            not isinstance(rec.get("hooks"), list) or \
            any(not isinstance(h, str) for h in rec["hooks"]):
        raise DerivedFixError("derived git record malformed",
                              status=502, code="corrupt")


def _validate_workspace(ws: dict, dfix_id: str) -> dict:
    if not isinstance(ws, dict) or set(ws.keys()) not in (
            _WS_KEYS, _NEW_WS_KEYS):
        raise DerivedFixError("derived workspace malformed",
                              status=502, code="corrupt")
    if "git" in ws:
        _validate_git_record(ws["git"])
    # The work path is canonical: exactly home/derived/<dfix>/work.
    # Anything else (sibling dirs, symlinked roots, other records) is
    # corruption, never a workspace.
    if not isinstance(ws.get("path"), str) or os.path.normpath(
            ws["path"]) != _expected_work_path(dfix_id):
        raise DerivedFixError("derived workspace path malformed",
                              status=502, code="corrupt")
    for key in ("head", "base_head", "prep_tree", "prep_head"):
        if not _is_hex(ws.get(key), 40):
            raise DerivedFixError("derived workspace pin malformed",
                                  status=502, code="corrupt")
    for key in ("result_head", "result_tree"):
        value = ws.get(key)
        if value is not None and not _is_hex(value, 40):
            raise DerivedFixError("derived workspace result malformed",
                                  status=502, code="corrupt")
    if (ws.get("result_head") is None) != (ws.get("result_tree") is None):
        raise DerivedFixError("derived workspace result half-written",
                              status=502, code="corrupt")
    if not _is_hex(ws.get("fingerprint"), 64):
        raise DerivedFixError("derived workspace fingerprint malformed",
                              status=502, code="corrupt")
    if ws.get("clean") is not True:
        raise DerivedFixError("derived workspace clean flag malformed",
                              status=502, code="corrupt")
    applied = ws.get("applied")
    if not isinstance(applied, list):
        raise DerivedFixError("derived workspace applied malformed",
                              status=502, code="corrupt")
    for entry in applied:
        if not isinstance(entry, dict) \
                or set(entry.keys()) != _APPLIED_KEYS \
                or entry.get("path") not in _ALLOWED_SOURCE_PATHS \
                or not isinstance(entry.get("bytes"), int) \
                or entry["bytes"] < 0 \
                or not _is_hex(entry.get("blob_sha256"), 64):
            raise DerivedFixError("derived workspace applied malformed",
                                  status=502, code="corrupt")
    for key, want in (("conflict_reset", _RESET_KEYS),
                      ("retained_base", _RETAINED_KEYS)):
        entries = ws.get(key)
        if not isinstance(entries, list):
            raise DerivedFixError("derived workspace prep malformed",
                                  status=502, code="corrupt")
        for entry in entries:
            if not isinstance(entry, dict) \
                    or set(entry.keys()) != want:
                raise DerivedFixError("derived workspace prep entry "
                                      "malformed",
                                      status=502, code="corrupt")
            _clean_rel(entry.get("path"))
            blob, sha = entry.get("base_blob"), entry.get(
                "base_blob_sha256")
            absent = entry.get("absent_at_base")
            size = entry.get("size")
            if not isinstance(absent, bool):
                raise DerivedFixError("derived workspace prep entry "
                                      "malformed",
                                      status=502, code="corrupt")
            if absent:
                # Genuine absence carries no bytes identity at all.
                if blob is not None or sha is not None \
                        or size is not None:
                    raise DerivedFixError("derived workspace prep entry "
                                          "malformed",
                                          status=502, code="corrupt")
            elif not _is_hex(blob, 40) \
                    or not isinstance(size, int) \
                    or isinstance(size, bool) or size < 0:
                raise DerivedFixError("derived workspace prep entry "
                                      "malformed",
                                      status=502, code="corrupt")
            elif size > _WORKER_READ_MAX:
                # Honest oversized identity: OID + size, never an
                # invented raw SHA for bytes never fully read.
                if sha is not None:
                    raise DerivedFixError("derived workspace prep entry "
                                          "malformed",
                                          status=502, code="corrupt")
            elif not _is_hex(sha, 64):
                raise DerivedFixError("derived workspace prep entry "
                                      "malformed",
                                      status=502, code="corrupt")
            if not isinstance(entry.get("reason" if key == "conflict_reset"
                                        else "note"), str) \
                    or not entry.get("reason" if key == "conflict_reset"
                                     else "note") \
                    or len(entry["reason" if key == "conflict_reset"
                                  else "note"]) > _REASON_CAP:
                raise DerivedFixError("derived workspace prep entry "
                                      "malformed",
                                      status=502, code="corrupt")
    if ws.get("allowed_changed_paths") != list(_ALLOWED_SOURCE_PATHS):
        raise DerivedFixError("derived allowlist malformed",
                              status=502, code="corrupt")
    scope = ws.get("scope")
    if not isinstance(scope, dict) or set(scope.keys()) != _SCOPE_KEYS:
        raise DerivedFixError("derived workspace scope malformed",
                              status=502, code="corrupt")
    for key in ("conflicts", "original_touched", "touched"):
        if not isinstance(scope.get(key), list) or any(
                not isinstance(p, str) for p in scope[key]):
            raise DerivedFixError("derived workspace scope malformed",
                                  status=502, code="corrupt")
        for path in scope[key]:
            _clean_rel(path)
    if not scope["conflicts"] or not scope["touched"]:
        raise DerivedFixError("derived workspace scope empty",
                              status=502, code="corrupt")
    if set(scope["conflicts"]) - set(scope["touched"]):
        raise DerivedFixError("derived scope claims conflicts outside "
                              "the current diff",
                              status=502, code="corrupt")
    return ws


def _validate_attempt(attempt: dict) -> dict:
    if not isinstance(attempt, dict) \
            or set(attempt.keys()) not in (
                _ATTEMPT_KEYS, _NEW_ATTEMPT_KEYS):
        raise DerivedFixError("derived attempt malformed",
                              status=502, code="corrupt")
    # The pin is closed and exact; an absent key is a legacy attempt
    # (read as no pin, never rewritten here). Bool is never an int.
    if "full_input" in attempt:
        pin = attempt["full_input"]
        if pin is not None:
            if not isinstance(pin, dict) \
                    or set(pin.keys()) != _PIN_KEYS:
                raise DerivedFixError(
                    "derived full-input pin malformed",
                    status=502, code="corrupt")
            if not _is_hex(pin.get("sha256"), 64) \
                    or type(pin.get("bytes")) is not int \
                    or pin["bytes"] < 0:
                raise DerivedFixError(
                    "derived full-input pin malformed",
                    status=502, code="corrupt")
    if not _ATTEMPT_RE.fullmatch(str(attempt.get("id") or "")):
        raise DerivedFixError("derived attempt id malformed",
                              status=502, code="corrupt")
    if attempt.get("hypothesis_id") not in _HYPOTHESES:
        raise DerivedFixError("derived attempt hypothesis malformed",
                              status=502, code="corrupt")
    if attempt.get("status") not in _STATUSES + ("queued",):
        raise DerivedFixError("derived attempt status malformed",
                              status=502, code="corrupt")
    prompt_sha = attempt.get("prompt_sha256")
    if prompt_sha is not None and not _is_hex(prompt_sha, 64):
        raise DerivedFixError("derived attempt prompt pin malformed",
                              status=502, code="corrupt")
    report = attempt.get("worker_report")
    if report is not None:
        _validate_worker_report(report)
    verified = attempt.get("verified_paths")
    if verified is not None:
        if not isinstance(verified, dict) or not verified or any(
                k not in _ALLOWED_SOURCE_PATHS or not _is_hex(v, 64)
                for k, v in verified.items()):
            raise DerivedFixError("derived verified paths malformed",
                                  status=502, code="corrupt")
    owned = attempt.get("job")
    if owned is not None:
        # Historical owned-job metadata: the link that owned THIS
        # attempt while it ran (kind + label naming this attempt
        # id), preserved on the terminal attempt after current
        # ownership moves on. Never a foreign kind or label.
        if not isinstance(owned, dict) \
                or set(owned.keys()) != _JOB_KEYS \
                or not isinstance(owned.get("id"), str) \
                or not owned["id"] \
                or owned.get("kind") != _DERIVED_KIND \
                or owned.get("label") != attempt.get("id"):
            raise DerivedFixError("derived attempt job malformed",
                                  status=502, code="corrupt")
    if not isinstance(attempt.get("error"), str) \
            or len(attempt["error"]) > _ERROR_CAP:
        raise DerivedFixError("derived attempt error malformed",
                              status=502, code="corrupt")
    for key in ("log_bytes", "raw_total_chars", "raw_total_bytes"):
        if not isinstance(attempt.get(key), int) or attempt[key] < 0:
            raise DerivedFixError("derived attempt log meta malformed",
                                  status=502, code="corrupt")
    if attempt.get("truncated") is not True \
            and attempt.get("truncated") is not False:
        raise DerivedFixError("derived attempt log meta malformed",
                              status=502, code="corrupt")
    raw_head = attempt.get("raw_head")
    if raw_head is not None:
        if not isinstance(raw_head, str) or len(raw_head) > _RAW_HEAD_CAP:
            raise DerivedFixError("derived attempt raw head malformed",
                                  status=502, code="corrupt")
        if not _is_hex(attempt.get("raw_head_sha256"), 64):
            raise DerivedFixError("derived attempt raw head pin malformed",
                                  status=502, code="corrupt")
    elif attempt.get("raw_head_sha256") is not None:
        raise DerivedFixError("derived attempt raw head pin malformed",
                              status=502, code="corrupt")
    if not isinstance(attempt.get("started_at"), str) \
            or not attempt["started_at"]:
        raise DerivedFixError("derived attempt timestamp malformed",
                              status=502, code="corrupt")
    if not isinstance(attempt.get("ended_at"), str):
        raise DerivedFixError("derived attempt timestamp malformed",
                              status=502, code="corrupt")
    return attempt


def _validate_worker_report(report: dict) -> dict:
    """Worker proposal shape (proposal, never proof).

    The app recomputes every hash from workspace bytes after this
    passes; a shape-valid report with mismatched hashes still
    refuses at verification. Changed paths must already be inside the
    allowlist here — an out-of-scope claim is an invalid report, not
    something the caller re-checks later.
    """
    if not isinstance(report, dict) or set(report.keys()) != _REPORT_KEYS:
        raise DerivedFixError("worker report keys malformed",
                              status=502, code="invalid-report")
    if report.get("status") != "proposal":
        raise DerivedFixError("worker report status malformed",
                              status=502, code="invalid-report")
    changed = report.get("changed_paths")
    if not isinstance(changed, list) or not changed or len(changed) > 8 \
            or any(p not in _ALLOWED_SOURCE_PATHS for p in changed) \
            or len(set(changed)) != len(changed):
        raise DerivedFixError("worker report paths malformed",
                              status=502, code="invalid-report")
    hashes = report.get("per_file_sha256")
    if not isinstance(hashes, dict) or set(hashes.keys()) != set(changed) \
            or any(not _is_hex(v, 64) for v in hashes.values()):
        raise DerivedFixError("worker report hashes malformed",
                              status=502, code="invalid-report")
    diff = report.get("diff_stat")
    if not isinstance(diff, dict) \
            or set(diff.keys()) != {"files", "insertions", "deletions"} \
            or not isinstance(diff.get("files"), int) \
            or diff["files"] != len(changed) \
            or not isinstance(diff.get("insertions"), int) \
            or diff["insertions"] < 0 \
            or not isinstance(diff.get("deletions"), int) \
            or diff["deletions"] < 0:
        raise DerivedFixError("worker report diff malformed",
                              status=502, code="invalid-report")
    resolutions = report.get("resolutions")
    if not isinstance(resolutions, list) or len(resolutions) > 8:
        raise DerivedFixError("worker report resolutions malformed",
                              status=502, code="invalid-report")
    for entry in resolutions:
        if not isinstance(entry, dict) \
                or set(entry.keys()) != _RESOLUTION_KEYS \
                or entry.get("path") not in changed \
                or not isinstance(entry.get("conflict_id"), str) \
                or not entry["conflict_id"] \
                or len(entry["conflict_id"]) > 256 \
                or entry.get("choice") not in _HYPOTHESES:
            raise DerivedFixError("worker report resolutions malformed",
                                  status=502, code="invalid-report")
    if not isinstance(report.get("notes"), str) \
            or len(report["notes"]) > _NOTE_CAP:
        raise DerivedFixError("worker report notes malformed",
                              status=502, code="invalid-report")
    return report


def _git(repo_dir: str, args: list, timeout: int = _GIT_TIMEOUT_S,
         *, git_bound=None):
    """Run git in a repo dir; failures are typed, output bounded.

    Always over the hardened path: -c overrides plus the scrubbed
    env (system/global/XDG config off), in the unbound branch too.
    Bound (worker-touched tree): re-prove the .git pointer against
    the prep record first and pin --git-dir/--work-tree from it,
    never from discovery. A changed pointer refuses
    git-pointer-changed before any spawn. Because global config
    is off everywhere here, `git worktree add` no longer sees a
    global LFS filter.
    """
    if git_bound is None:
        argv = _runner.hardened_git_argv(
            ["-C", str(repo_dir)] + list(args))
    else:
        try:
            paths = _runner.reprove_git_pointer(repo_dir, git_bound)
        except _runner.SourceUnreadable as exc:
            raise DerivedFixError(
                "worktree git identity changed since prepare",
                status=502, code="git-pointer-changed") from exc
        argv = _runner.hardened_git_argv(
            list(args), gitdir=paths["gitdir"],
            worktree=paths["worktree"])
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=_runner.hardened_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DerivedFixError(f"git unavailable: {exc}",
                              status=502, code="unreadable") from exc
    return proc


def _git_ok(repo_dir: str, args: list, *, git_bound=None):
    proc = _git(repo_dir, args, git_bound=git_bound)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:200]
        raise DerivedFixError(
            f"git {' '.join(args)} failed: {detail}",
            status=502, code="unreadable")
    return proc.stdout


def _git_identity_args() -> list:
    return ["-c", f"user.name={_GIT_IDENTITY_NAME}",
            "-c", f"user.email={_GIT_IDENTITY_EMAIL}"]


def _pinned_commit(repo_dir: str, head: str, label: str) -> None:
    """The recorded pin must resolve to itself, live, right now.

    ``rev-list -n 1`` prints the commit the pin names; anything else
    (abbreviation drift, a moved ref, an unresolvable object) means the
    recorded bytes are not what this repo holds — fail closed, never
    fall back to branch names or worktree state.
    """
    if not _is_hex(head, 40):
        raise DerivedFixError(f"parent {label} head malformed",
                              status=409, code="bad-parent")
    out = _git_ok(repo_dir, ["rev-list", "-n", "1", head])
    if out.decode("utf-8", "replace").strip() != head:
        raise DerivedFixError(f"parent {label} pin does not resolve "
                              "to the recorded commit here",
                              status=409, code="bad-parent")


def _assert_path_no_symlinks(work: str, rel: str) -> None:
    """No component of rel may be a symlink (leaf may be dir/file).

    lstat-walks from the work root without following anything: a
    symlink anywhere on the path — including an ancestor the worker
    swapped mid-run — refuses before any read, hash, stage, or add.
    Resolving the target first would lose exactly this identity.
    """
    for part in rel.split("/"):
        _clean_rel(part)
    node = Path(work)
    try:
        work_st = os.lstat(node)
    except OSError as exc:
        raise DerivedFixError(f"unreadable workspace: {exc}",
                              status=502, code="unreadable") from exc
    if stat.S_ISLNK(work_st.st_mode):
        raise DerivedFixError("refused symlinked workspace root",
                              status=502, code="corrupt")
    for part in rel.split("/"):
        node = node / part
        try:
            st = os.lstat(node)
        except FileNotFoundError:
            raise DerivedFixError(f"missing workspace path: {rel}",
                                  status=404,
                                  code="unknown-file") from None
        except OSError as exc:
            raise DerivedFixError(f"unreadable workspace path: {exc}",
                                  status=502, code="unreadable") from exc
        if stat.S_ISLNK(st.st_mode):
            raise DerivedFixError(f"refused symlink on path: {rel}",
                                  status=502, code="corrupt")


def _assert_no_symlink_components(work: str, rel: str) -> None:
    """Like the path walk, but the leaf must be a regular file."""
    _assert_path_no_symlinks(work, rel)
    st = os.lstat(Path(work, *rel.split("/")))
    if not stat.S_ISREG(st.st_mode):
        raise DerivedFixError(f"not a regular file: {rel}",
                              status=502, code="corrupt")


def _read_workspace_bytes(work: str, rel: str) -> bytes:
    """Read a worktree file after the symlink-ancestor walk."""
    _assert_no_symlink_components(work, rel)
    node = Path(work, *rel.split("/"))
    try:
        size = os.lstat(node).st_size
    except OSError as exc:
        raise DerivedFixError(f"unreadable workspace file: {exc}",
                              status=502, code="unreadable") from exc
    if size > _WORKER_READ_MAX:
        raise DerivedFixError(f"workspace file too large to bind: {rel}",
                              status=502, code="too-large")
    try:
        return node.read_bytes()
    except OSError as exc:
        raise DerivedFixError(f"unreadable workspace file: {exc}",
                              status=502, code="unreadable") from exc


def _read_workspace_text(work: str, rel: str):
    """(text, sha256, lossy): bounded decode of a walked file."""
    raw = _read_workspace_bytes(work, rel)
    try:
        text = raw.decode("utf-8")
        return text, hashlib.sha256(raw).hexdigest(), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), hashlib.sha256(
            raw).hexdigest(), True


def _snapshot_worktree(path: str, *, git_bound=None):
    """Real worktree descriptor via the shared snapshot helper.

    Returns {head, fingerprint, clean} as observed — never hardcoded.
    """
    snap = batches._snapshot(str(path), git_bound=git_bound)
    if not isinstance(snap, dict) or not snap.get("head") \
            or not snap.get("fingerprint"):
        raise DerivedFixError("worktree snapshot unavailable",
                              status=502, code="unreadable")
    return {"head": snap["head"],
            "fingerprint": snap["fingerprint"],
            "clean": bool(snap.get("clean"))}


def _read_parent_op(op_id: str):
    """Read the blocked prepare op this derivation binds (no writes).

    Only the binding fields are consumed; nothing is relabeled and
    the parent record is never modified here. A blocked prepare must
    carry a non-empty recorded conflict list — without actual
    conflicts there is nothing to derive, and an absent list is a
    scope failure, not an empty scope.
    """
    try:
        path = batches.op_file(op_id)
    except Exception as exc:
        raise DerivedFixError(f"bad parent op id: {op_id!r}",
                              status=400, code="bad-op") from exc
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise DerivedFixError(f"unknown parent op: {op_id}",
                              status=404, code="unknown-op") from None
    except OSError as exc:
        raise DerivedFixError(f"unreadable parent op: {exc}",
                              status=502, code="unreadable") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise DerivedFixError("parent op is not a regular file",
                              status=502, code="corrupt")
    try:
        rec = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise DerivedFixError(f"unreadable parent op: {exc}",
                              status=502, code="unreadable") from exc
    if not isinstance(rec, dict) or rec.get("op") != "prepare" \
            or rec.get("status") != "blocked":
        # Healthy empty for a non-blocked prepare (no derivation
        # allowed); structural breakage of a blocked prepare stays
        # bad-parent/corrupt and never hides here.
        raise DerivedFixError("parent op is not a blocked prepare",
                              status=409, code="not-blocked")
    for key in ("candidate", "base"):
        if not isinstance(rec.get(key), dict):
            raise DerivedFixError("parent binding malformed",
                                  status=409, code="bad-parent")
    conflicts = (rec.get("result") or {}).get("conflicted_paths") \
        if isinstance(rec.get("result"), dict) else None
    if not isinstance(conflicts, list) or not conflicts:
        raise DerivedFixError("parent op records no conflicts; nothing "
                              "to derive",
                              status=409, code="bad-parent")
    for path in conflicts:
        _clean_rel(path)
    return rec, sorted(set(conflicts))


def _check_eligibility(parent_conflicts: list, cur: dict) -> None:
    """Reasoned Phase A capability eligibility (creation-time gate).

    Refuses unrelated cases BEFORE any derived record, git worktree,
    or transport, grounded in the actual repair capability and the
    live source/conflict/report contract: (1) the current review
    must conclude needs_changes — that verdict IS the repair
    request, whether or not it carries question text; (2) at least
    one recorded conflict must lie in the Phase A source scope — an
    unrelated repository/conflict case (calendar files, dist
    bundles, manifest-only drift) has nothing this worker may
    legally fix. No question is invented or required: a valid
    needs_changes verdict over eligible code conflicts already
    identifies the repair. Reads only the live origin and parent
    binding — no hardcoded run, fixture, or repo names. A calendar
    case is refused here, never offered a Nitro webpack experiment.
    """
    if cur.get("report_status") != "needs_changes":
        raise DerivedFixError(
            "current review does not ask for source changes "
            f"({cur.get('report_status')}); Phase A repairs only "
            "needs_changes reviews",
            status=409, code="not-eligible")
    if not (set(parent_conflicts or ()) & set(_ALLOWED_SOURCE_PATHS)):
        raise DerivedFixError(
            "no recorded conflict in Phase A source scope "
            f"{list(_ALLOWED_SOURCE_PATHS)}; this case needs a "
            "different worker",
            status=409, code="not-eligible")


def _origin_record_bytes(root: Path, name: str) -> bytes:
    """Single read of one origin record (lstat-guarded, no follows)."""
    target = root / f"{name}.json"
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        raise DerivedFixError(f"origin record is gone: {name}",
                              status=502, code="corrupt") from None
    except OSError as exc:
        raise DerivedFixError(f"unreadable origin record: {exc}",
                              status=502, code="unreadable") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise DerivedFixError("origin record is not a regular file",
                              status=502, code="corrupt")
    try:
        return target.read_bytes()
    except OSError as exc:
        raise DerivedFixError(f"unreadable origin record: {exc}",
                              status=502, code="unreadable") from exc


def _current_valid_origin(op_id: str) -> dict:
    """The CURRENT valid review origin, proved by unchanged validators.

    Reads the real latest pointer and stored input through the
    UNCHANGED recovery validators (never a reimplementation): name
    shape, stored identity, and the full latest -> attempt -> input ->
    report chain. Each origin record is read ONCE — the same bytes
    are parsed, validated, and hashed. Then the unchanged full live
    binding/current proof (``load_blocked_op``: queue/run/profile,
    registered batch, current base checkout snapshot) must still
    attribute the SAME run id and base commit as the stored input;
    a moved run or rebased base is stale, never rebound. Finally the
    unchanged full replay (``_current_fp``) replays the stored sent
    source against the current live binding and must reproduce the
    stored fingerprint — ids alone never prove currency. Returns
    names, raw bytes, SHAs, fingerprint, validated questions,
    recorded conflicts, the parsed original touched list, the
    validated base commit, run id, and the live binding. Anything
    short of this is 409 (no current origin / stale) or 502
    (corrupt).
    """
    try:
        root = _ir.rec_root(op_id)
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    try:
        latest = _ir._read_record(root, "latest.json")
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    if latest is None:
        raise DerivedFixError("no review recorded for op",
                              status=409, code="no-current-origin")
    if not isinstance(latest, dict) or latest.get("status") != "done":
        raise DerivedFixError("no completed current review for op",
                              status=409, code="no-current-origin")
    input_name = latest.get("input")
    report_name = latest.get("report")
    if not isinstance(input_name, str) \
            or not _ORIGIN_INPUT_RE.fullmatch(input_name):
        raise DerivedFixError("latest input link malformed",
                              status=502, code="corrupt")
    if not isinstance(report_name, str) \
            or not _ORIGIN_REPORT_RE.fullmatch(report_name):
        raise DerivedFixError("latest report link malformed",
                              status=502, code="corrupt")
    in_raw, in_doc = _read_origin_once(root, input_name)
    rep_raw, rep_doc = _read_origin_once(root, report_name)
    try:
        ident = _ir._validate_stored_identity(op_id, in_doc)
        report = _ir._validate_report_chain(root, op_id, latest, in_doc)
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    fp = ident["fingerprint"]
    if input_name != f"input-{fp[:16]}" \
            or report_name != f"report-{fp[:16]}":
        raise DerivedFixError("origin names do not match their "
                              "fingerprint",
                              status=502, code="corrupt")
    if not isinstance(rep_doc, dict) or rep_doc.get("fingerprint") != fp:
        raise DerivedFixError("origin report no longer matches",
                              status=502, code="corrupt")
    questions = report.get("questions") if isinstance(report, dict) else None
    if not isinstance(questions, list) or len(questions) > _QUESTIONS_CAP \
            or any(not isinstance(q, str) or not q or len(q) > _TEXT_CAP
                   for q in questions):
        raise DerivedFixError("origin questions malformed",
                              status=502, code="corrupt")
    report_status = report.get("status") if isinstance(report, dict) \
        else None
    if not isinstance(report_status, str) or not report_status:
        raise DerivedFixError("origin report status malformed",
                              status=502, code="corrupt")
    conflicts = in_doc.get("conflicted_paths")
    if not isinstance(conflicts, list) or not conflicts:
        raise DerivedFixError("origin records no conflicts",
                              status=502, code="corrupt")
    for path in conflicts:
        _clean_rel(path)
    original_touched = _ir.parse_original_touched(
        in_doc.get("op_error") or "")
    if not isinstance(original_touched, list):
        original_touched = []
    base_commit = in_doc.get("base_commit")
    if not _is_hex(base_commit, 40):
        raise DerivedFixError("origin base commit malformed",
                              status=502, code="corrupt")
    run_id = in_doc.get("run", {}).get("id") \
        if isinstance(in_doc.get("run"), dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise DerivedFixError("origin run link malformed",
                              status=502, code="corrupt")
    try:
        live = _ir.load_blocked_op(op_id)
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    if live.get("base_commit") != base_commit:
        raise DerivedFixError("live base commit moved since the review; "
                              "refresh and resubmit",
                              status=409, code="stale-origin")
    if not isinstance(live.get("run"), dict) \
            or live["run"].get("id") != run_id:
        raise DerivedFixError("live run moved since the review; "
                              "refresh and resubmit",
                              status=409, code="stale-origin")
    # The unchanged full replay binds raw parent/run revisions and
    # the original sent source: the stored sent ranges/scope/prompt
    # are replayed against the CURRENT live binding and must
    # reproduce the stored fingerprint. A valid parent/run revision
    # under the same ids can still leave an OLD completed report
    # accepted as current — ids alone do not prove it. Any drift in
    # run bytes, base identity, or recorded refs yields a different
    # value here: stale, never rebound or healed. This is the exact
    # call the review's own execute path uses.
    try:
        now_fp = _ir._current_fp(live, in_doc)
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    except (KeyError, TypeError, AttributeError) as exc:
        raise DerivedFixError("live binding shape unreadable",
                              status=502, code="corrupt") from exc
    if now_fp != fp:
        raise DerivedFixError("stored sent source no longer replays "
                              "the stored fingerprint; an older "
                              "completed review is not current — "
                              "refresh and resubmit",
                              status=409, code="stale-origin")
    return {"input": input_name, "report": report_name,
            "fingerprint": fp, "questions": list(questions),
            "report_status": report_status,
            "conflicts": sorted(set(conflicts)),
            "original_touched": [p for p in original_touched
                                 if isinstance(p, str)],
            "base_commit": base_commit, "run_id": run_id,
            "input_raw": in_raw, "report_raw": rep_raw,
            "input_sha256": hashlib.sha256(in_raw).hexdigest(),
            "report_sha256": hashlib.sha256(rep_raw).hexdigest(),
            "live": live}


def _prove_live_binding(rec: dict, cur: dict) -> None:
    """The record must still match the validated live binding.

    Compares ALL FOUR original candidate descriptor fields
    (branch/fingerprint/head/worktree) and all five base fields to
    the CURRENT validated descriptors from ``load_blocked_op``, the
    recorded base commit/run to the live ones, and the latest pointer
    to the exact bound origin names. A different identical linked
    worktree or branch is not the originally verified source
    identity. Any move is 409 stale — never healed, never rebound.
    """
    live = cur.get("live") or {}
    live_cand = live.get("candidate") or {}
    live_base = live.get("base") or {}
    rec_cand, rec_base = rec.get("candidate") or {}, rec.get("base") or {}
    for key in ("branch", "fingerprint", "head", "worktree"):
        if live_cand.get(key) != rec_cand.get(key):
            raise DerivedFixError("live candidate moved since the "
                                  "record was bound",
                                  status=409, code="stale-origin")
    for key in ("batch", "checkout", "head", "fingerprint", "lane"):
        if live_base.get(key) != rec_base.get(key):
            raise DerivedFixError("live base moved since the record "
                                  "was bound",
                                  status=409, code="stale-origin")
    if cur.get("base_commit") != rec["origin"].get("base_commit"):
        raise DerivedFixError("live base commit moved since the record "
                              "was bound",
                              status=409, code="stale-origin")
    op_id = rec["derived_from"]
    try:
        root = _ir.rec_root(op_id)
        latest = _ir._read_record(root, "latest.json")
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    if not isinstance(latest, dict) or latest.get("status") != "done" \
            or latest.get("input") != rec["origin"]["input"] \
            or latest.get("report") != rec["origin"]["report"]:
        raise DerivedFixError("review latest moved since the record "
                              "was bound; refresh and resubmit",
                              status=409, code="stale-origin")


def _reprove_origin(rec: dict) -> dict:
    """Re-prove the recorded origin from live sources.

    Used before ANY git or transport step and after transport: the
    full current-origin proof (bytes, chain, live binding) must
    still validate AND still equal the recorded names, SHAs,
    fingerprint, and base commit. Returns the fresh proof.
    """
    cur = _current_valid_origin(rec["derived_from"])
    origin = rec["origin"]
    if cur["input"] != origin["input"] \
            or cur["report"] != origin["report"] \
            or cur["input_sha256"] != origin["input_sha256"] \
            or cur["report_sha256"] != origin["report_sha256"] \
            or cur["fingerprint"] != origin["fingerprint"] \
            or cur["base_commit"] != origin.get("base_commit"):
        raise DerivedFixError("origin changed since the record was "
                              "bound; refresh and resubmit",
                              status=409, code="stale-origin")
    _prove_live_binding(rec, cur)
    return cur


def _validate_hypothesis(hypothesis) -> dict:
    if not isinstance(hypothesis, dict):
        raise DerivedFixError("hypothesis must be an object",
                              status=400, code="bad-hypothesis")
    hid = hypothesis.get("id")
    note = hypothesis.get("note", "")
    if hid not in _HYPOTHESES:
        raise DerivedFixError("hypothesis id must be H-A or H-B",
                              status=400, code="bad-hypothesis")
    if not isinstance(note, str) or not note.strip() \
            or len(note) > _NOTE_CAP:
        raise DerivedFixError("hypothesis note malformed",
                              status=400, code="bad-hypothesis")
    return {"id": hid, "note": note}


def _utcnow() -> str:
    import datetime as _dt
    return _dt.datetime.now(
        _dt.timezone.utc).isoformat(timespec="seconds")


def _note_sha(note: str) -> str:
    return hashlib.sha256(note.encode("utf-8")).hexdigest()


def _scan_derived(op_id: str):
    """All derived records bound to one parent (bounded, read-only).

    Corrupt records raise — they are never silently skipped into an
    empty view. Only a file that vanishes between the glob and the
    read (unknown-derived race) is skipped; a non-matching stem in
    our own namespace is itself corruption.
    """
    try:
        ops = batches.integration_ops_dir()
    except OSError as exc:
        raise DerivedFixError(f"unreadable app home: {exc}",
                              status=502, code="unreadable") from exc
    try:
        names = sorted(p.name for p in ops.glob("dfix-*.json"))
    except OSError as exc:
        raise DerivedFixError(f"unreadable ops dir: {exc}",
                              status=502, code="unreadable") from exc
    if len(names) > _SCAN_CAP:
        raise DerivedFixError("too many derived records",
                              status=500, code="busy")
    out = []
    for name in names:
        stem = name[:-5] if name.endswith(".json") else name
        if not _DFIX_RE.fullmatch(stem):
            raise DerivedFixError(f"foreign file in derived namespace: "
                                  f"{name}",
                                  status=502, code="corrupt")
        try:
            rec = _read_record(stem)
        except DerivedFixError as exc:
            if exc.code == "unknown-derived":
                continue
            raise
        if rec.get("derived_from") == op_id:
            out.append(rec)
    return out


def _same_key(rec: dict, exp_in: str, exp_rep: str, hyp: dict,
              note_sha: str) -> bool:
    origin = rec.get("origin") or {}
    hyp_rec = rec.get("hypothesis") or {}
    return origin.get("input_sha256") == exp_in \
        and origin.get("report_sha256") == exp_rep \
        and hyp_rec.get("id") == hyp["id"] \
        and _note_sha(hyp_rec.get("note") or "") == note_sha


def _live_attempts(rec: dict):
    return [a for a in rec.get("source_attempts") or []
            if a.get("status") in _LIVE_ATTEMPT]


def _attached_job_link(rec: dict, live: list, job: dict,
                       live_jobs, op_id: str) -> bool:
    """True only for the actual owned running job.

    Both sides must agree: the STORED link carries the derived-fix
    kind and names the one live attempt under our lease owner, AND
    the REAL dashboard entry for that id is running with the same
    kind and the same attempt label. Id membership alone never
    attaches, and a self-claimed persisted link without a matching
    live table entry never attaches. At most one live attempt may
    exist (the store refuses a second while one lives). Anything
    else reconciles instead.
    """
    if not isinstance(job, dict) or not live or len(live) != 1:
        return False
    if job.get("kind") != _DERIVED_KIND:
        return False
    if job.get("label") != live[0].get("id"):
        return False
    if rec.get("lease_owner") != _lease_owner(op_id):
        return False
    entry = live_jobs.get(job.get("id")) \
        if isinstance(live_jobs, dict) else None
    if not isinstance(entry, dict):
        return False
    if entry.get("status") != "running":
        return False
    if entry.get("kind") != _DERIVED_KIND:
        return False
    if entry.get("label") != live[0].get("id"):
        return False
    return True


def peek_derived_attach(op_id: str, origin_input: str,
                        origin_report: str, expected_shas: dict,
                        hypothesis: dict, live_jobs):
    """Lock-free attach fast-path (read-only, advisory).

    Validates the same same-key material as creation (400 on
    malformed), then scans WITHOUT the lease or op lock: when the
    identical record already carries the one live attempt with its
    actual owned running job — proved against the REAL live job
    table, not id membership — the caller returns attached with the
    established union (started false, reused true, attached true)
    before acquiring the global lease. Anything else — unknown key,
    no live attempt, unowned job — returns None and the
    authoritative create path decides under the lease (it
    re-verifies, reconciles, or 409s). A stale advisory attach (job
    died between peek and poll) renders unknown on the next read;
    attach never mints work. Shape errors propagate so malformed
    posts fail fast without the lease.
    """
    hyp = _validate_hypothesis(hypothesis)
    if not isinstance(origin_input, str) \
            or not _ORIGIN_INPUT_RE.fullmatch(origin_input):
        raise DerivedFixError("origin input name malformed",
                              status=400, code="bad-origin")
    if not isinstance(origin_report, str) \
            or not _ORIGIN_REPORT_RE.fullmatch(origin_report):
        raise DerivedFixError("origin report name malformed",
                              status=400, code="bad-origin")
    if not isinstance(expected_shas, dict):
        raise DerivedFixError("expected origin revision required",
                              status=400, code="bad-revision")
    exp_in = expected_shas.get("input_sha256")
    exp_rep = expected_shas.get("report_sha256")
    for label, value in (("input_sha256", exp_in),
                         ("report_sha256", exp_rep)):
        if not isinstance(value, str) or not _is_hex(value, 64):
            raise DerivedFixError(
                f"expected {label} must be a 64-hex SHA-256",
                status=400, code="bad-revision")
    try:
        note_sha = _note_sha(hyp["note"])
        live = live_jobs if isinstance(live_jobs, dict) else {}
        for rec in _scan_derived(op_id):
            if not _same_key(rec, exp_in, exp_rep, hyp, note_sha):
                continue
            attempts = _live_attempts(rec)
            if not attempts:
                return None
            job = rec.get("job") or {}
            if _attached_job_link(rec, attempts, job, live,
                                  op_id):
                return {"reused": True, "attached": True,
                        "dfix": rec["op_id"], "job": rec.get("job"),
                        "reason": "identical derive already running; "
                                  "attached, no duplicate"}
            return None
    except DerivedFixError as exc:
        if exc.http_status == 400:
            raise
        return None
    return None


def create_derived_fix(op_id: str, origin_input: str, origin_report: str,
                       expected_shas: dict, hypothesis: dict,
                       live_jobs=None, retry_of=None) -> dict:
    """Create (or reuse/attach/reconcile/retry) a private derived record.

    CALLER MUST HOLD the global worker lease AND the parent-op
    exclusion (this function takes no lock itself: the exclusion is
    non-reentrant, so taking it here would deadlock against the
    holder). Validates the blocked parent and the EXACT current
    origin — names, raw SHAs, fingerprint, and the unchanged
    recovery chain — before ANY new record, git, or transport step.
    Same origin+hypothesis+note bytes never mint a second record:
    terminal-OK reuses (after result re-verification), live jobs
    attach (only with a live owned job proof) or reconcile, and
    terminal-BAD requires an explicit ``retry_of`` naming the same
    record (bounded attempts, no auto-retry). Changed origin or
    hypothesis starts a new record under the same explicit UI action.
    """
    hyp = _validate_hypothesis(hypothesis)
    if not isinstance(origin_input, str) \
            or not _ORIGIN_INPUT_RE.fullmatch(origin_input):
        raise DerivedFixError("origin input name malformed",
                              status=400, code="bad-origin")
    if not isinstance(origin_report, str) \
            or not _ORIGIN_REPORT_RE.fullmatch(origin_report):
        raise DerivedFixError("origin report name malformed",
                              status=400, code="bad-origin")
    if not isinstance(expected_shas, dict):
        raise DerivedFixError("expected origin revision required",
                              status=400, code="bad-revision")
    exp_in = expected_shas.get("input_sha256")
    exp_rep = expected_shas.get("report_sha256")
    for label, value in (("input_sha256", exp_in),
                         ("report_sha256", exp_rep)):
        if not isinstance(value, str) or not _is_hex(value, 64):
            raise DerivedFixError(
                f"expected {label} must be a 64-hex SHA-256",
                status=400, code="bad-revision")
    if retry_of is not None and (
            not isinstance(retry_of, str)
            or not _DFIX_RE.fullmatch(retry_of)):
        raise DerivedFixError("retry_of must name a derived record",
                              status=400, code="bad-retry")
    parent, parent_conflicts = _read_parent_op(op_id)
    cur = _current_valid_origin(op_id)
    if set(parent_conflicts) != set(cur["conflicts"]):
        raise DerivedFixError("op conflict scope moved since the review; "
                              "refresh and resubmit",
                              status=409, code="stale-origin")
    if cur["input"] != origin_input or cur["report"] != origin_report:
        raise DerivedFixError("displayed origin is no longer current; "
                              "refresh and resubmit",
                              status=409, code="stale-origin")
    if cur["input_sha256"] != exp_in or cur["report_sha256"] != exp_rep:
        raise DerivedFixError("origin bytes changed since display; "
                              "refresh and resubmit",
                              status=409, code="stale-origin")
    # Eligibility before any derived record, git, or transport: an
    # unrelated case is refused here, never recorded.
    _check_eligibility(parent_conflicts, cur)
    # The attach proof reads the REAL live job table (kind/label/
    # status per entry), never bare id membership.
    table = live_jobs if isinstance(live_jobs, dict) else {}
    note_sha = _note_sha(hyp["note"])
    for rec in _scan_derived(op_id):
        if not _same_key(rec, exp_in, exp_rep, hyp, note_sha):
            continue
        if rec["status"] in _TERMINAL_OK:
            if _verify_result_identity(rec):
                return {"reused": True, "attached": False,
                        "dfix": rec["op_id"],
                        "reason": "identical derived result already "
                                  "recorded; no new run",
                        "job": rec.get("job")}
            mark_derived_stale(rec["op_id"],
                               "stored result no longer verifies "
                               "against its workspace")
            raise DerivedFixError(
                "stored result failed re-verification and is marked "
                f"stale ({rec['op_id']}); vary the hypothesis note "
                "to start a fresh record",
                status=409, code="stale-result")
        if rec["status"] == "stale":
            raise DerivedFixError(
                f"derived record is stale ({rec['op_id']}); vary the "
                "hypothesis note to start a fresh record",
                status=409, code="stale-result")
        live = _live_attempts(rec)
        if live:
            job = rec.get("job") or {}
            # Attach only to the ACTUAL owned job: stored kind is
            # derived-fix, the stored label names the one live
            # attempt, the REAL table entry for that id is running
            # with the same kind and label, and the lease owner is
            # ours. Anything else is not attachable — reconcile,
            # never attach-blind.
            if _attached_job_link(rec, live, job, table,
                                  op_id):
                return {"reused": True, "attached": True,
                        "dfix": rec["op_id"],
                        "reason": "identical derive already running; "
                                  "attached, no duplicate",
                        "job": rec.get("job")}
            for attempt in live:
                _finish_attempt(rec, dict(attempt), "source-error",
                                "owner job not live; reconciled by "
                                "explicit start — no auto-resume", None)
            return {"reused": False, "attached": False,
                    "dfix": rec["op_id"], "reconciled": True,
                    "reason": "previous owner job not live; old attempt "
                              "closed as a preserved terminal, fresh "
                              "attempt follows on explicit start"}
        if retry_of == rec["op_id"]:
            if len(rec.get("source_attempts") or []) \
                    >= _MAX_SOURCE_ATTEMPTS:
                raise DerivedFixError(
                    "too many source attempts on this record; vary the "
                    "hypothesis note to start a fresh record",
                    status=409, code="too-many-attempts")
            return {"reused": False, "attached": False,
                    "dfix": rec["op_id"], "retried": True,
                    "reason": "explicit retry on the same record; a "
                              "fresh attempt follows on explicit start"}
        if rec["status"] in _TERMINAL_BAD:
            if retry_of is not None:
                raise DerivedFixError(
                    "retry_of does not name this identical failed "
                    f"record ({rec['op_id']})",
                    status=409, code="bad-retry")
            raise DerivedFixError(
                "identical derive already failed on record "
                f"{rec['op_id']}; post retry_of with that id for an "
                "explicit bounded retry, or vary the hypothesis note",
                status=409, code="retry-required",
                )
        # Open record, identical key, no live attempt: continue it —
        # never mint a duplicate record for the same bytes.
        return {"reused": False, "attached": False,
                "dfix": rec["op_id"], "reconciled": True,
                "reason": "identical derive already open; continuing "
                          "on the same record"}
    if retry_of is not None:
        raise DerivedFixError("retry_of names no identical failed record",
                              status=409, code="bad-retry")
    dfix_id = new_derived_id()
    now = _utcnow()
    cand = parent.get("candidate") or {}
    base = parent.get("base") or {}
    rec = {
        "schema": 1, "kind": _DERIVED_KIND, "op": _DERIVED_KIND,
        "op_id": dfix_id, "derived_from": op_id,
        "lease_owner": _lease_owner(op_id),
        "origin": {"input": origin_input, "report": origin_report,
                   "input_sha256": exp_in, "report_sha256": exp_rep,
                   "fingerprint": cur["fingerprint"],
                   "base_commit": cur["base_commit"],
                   "questions": list(cur["questions"])},
        "candidate": {"branch": str(cand.get("branch") or ""),
                      "fingerprint": str(cand.get("fingerprint") or ""),
                      "head": str(cand.get("head") or ""),
                      "worktree": str(cand.get("worktree") or "")},
        "base": {"batch": str(base.get("batch") or ""),
                 "checkout": str(base.get("checkout") or ""),
                 "fingerprint": str(base.get("fingerprint") or ""),
                 "head": str(base.get("head") or ""),
                 "lane": str(base.get("lane") or "")},
        "hypothesis": hyp, "workspace": None, "source_attempts": [],
        "latest_attempt": None, "job": None, "status": "started",
        "error": "", "created_at": now, "updated_at": now,
    }
    _write_record(rec)
    return {"reused": False, "attached": False, "dfix": dfix_id,
            "reason": "derived fix record created", "job": None}


def _latest_success_attempt(rec: dict):
    """The latest successful attempt, strictly bound, or None.

    Returns the single attempt whose id and status equal the latest
    pointer, whose status is the success terminal, and whose
    hypothesis is the record's — with a non-empty verified map.
    Anything else (no pointer, mismatch, wrong hypothesis, empty
    pins) is not a reusable result.
    """
    latest = rec.get("latest_attempt") or {}
    if not latest.get("id") \
            or latest.get("status") != "source-ready-unverified":
        return None
    matches = [a for a in rec.get("source_attempts") or []
               if a.get("id") == latest.get("id")]
    if len(matches) != 1:
        return None
    attempt = matches[0]
    if attempt.get("status") != latest.get("status"):
        return None
    if attempt.get("hypothesis_id") != (rec.get("hypothesis") or {}).get(
            "id"):
        return None
    verified = attempt.get("verified_paths")
    if not isinstance(verified, dict) or not verified:
        return None
    return attempt


def _verify_result_identity(rec: dict) -> bool:
    """True only when the stored result still proves itself live.

    Resolves the EXACT latest successful attempt (never the
    workspace, whose closed schema carries no verified paths) and
    binds its verified map: the result commit/tree must resolve in
    the workspace repo, the live snapshot must equal the recorded
    result head and fingerprint on a clean tree, and every verified
    file must re-hash to the attempt's recorded value. Genuine reuse
    returns the same record without transport; altered bytes fail.
    Never raises: the caller marks stale on False.
    """
    try:
        _reprove_work_chain(rec["op_id"])
        attempt = _latest_success_attempt(rec)
        if attempt is None:
            return False
        verified = attempt.get("verified_paths") or {}
        if any(p not in _ALLOWED_SOURCE_PATHS or not _is_hex(v, 64)
               for p, v in verified.items()):
            return False
        ws = rec.get("workspace") or {}
        work = ws.get("path")
        if not work or not ws.get("result_head") \
                or not ws.get("result_tree"):
            return False
        bound = ws.get("git")
        out = _git(work, ["rev-parse",
                          f"{ws['result_head']}^{{commit}}"],
                   git_bound=bound)
        if out.returncode != 0:
            return False
        out = _git(work, ["rev-parse",
                          f"{ws['result_head']}^{{tree}}"],
                   git_bound=bound)
        if out.returncode != 0 or out.stdout.decode(
                "utf-8", "replace").strip() != ws["result_tree"]:
            return False
        snap = _snapshot_worktree(work, git_bound=bound)
        # A recorded result settles clean at the result head; a dirty
        # tree, a moved head, or a changed fingerprint is not the
        # recorded result — stale, never rebound.
        if snap["head"] != ws["result_head"] \
                or snap["fingerprint"] != ws["fingerprint"] \
                or snap["clean"] is not True:
            return False
        for path, want in verified.items():
            raw = _read_workspace_bytes(work, path)
            if hashlib.sha256(raw).hexdigest() != want:
                return False
    except DerivedFixError:
        return False
    except OSError:
        return False
    return True


def mark_derived_stale(dfix_id: str, reason: str) -> None:
    """Mark a terminal-OK record stale (result no longer verifies)."""
    rec = _read_record(dfix_id)
    if rec.get("status") not in _TERMINAL_OK:
        raise DerivedFixError("only a recorded result can go stale",
                              status=409, code="busy")
    rec = dict(rec)
    rec["status"] = "stale"
    rec["error"] = str(reason or "stale")[:_ERROR_CAP]
    rec["updated_at"] = _utcnow()
    _write_record(rec)


def _git_common_dir(checkout: str, label: str) -> str:
    """The repo identity shared by linked worktrees (read-only).

    ``--show-toplevel`` returns each worktree root, so comparing
    toplevels rejects the real case: candidate and base are distinct
    linked worktrees of the SAME common Git dir. Identity is the
    resolved common dir; the recorded checkout stays the read-only
    command cwd.
    """
    if not isinstance(checkout, str) or not checkout:
        raise DerivedFixError(f"parent {label} checkout missing",
                              status=409, code="bad-parent")
    proc = _git(checkout, ["rev-parse", "--git-common-dir"])
    if proc.returncode != 0:
        raise DerivedFixError(f"parent {label} is not a git checkout",
                              status=409, code="bad-parent")
    raw = proc.stdout.decode("utf-8", "replace").strip()
    if not raw:
        raise DerivedFixError(f"parent {label} has no git common dir",
                              status=409, code="bad-parent")
    full = raw if os.path.isabs(raw) else os.path.normpath(
        os.path.join(checkout, raw))
    try:
        return os.path.realpath(full)
    except OSError as exc:
        raise DerivedFixError(f"unreadable git common dir: {exc}",
                              status=502, code="unreadable") from exc


def _read_origin_once(root: Path, name: str):
    """Single guarded read of one origin record: (raw bytes, doc).

    The SAME bytes are parsed and hashed — never parse revision A
    then bind a reread B.
    """
    raw = _origin_record_bytes(root, name)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise DerivedFixError(f"origin record is not JSON: {name}",
                              status=502, code="corrupt") from exc
    if not isinstance(doc, dict):
        raise DerivedFixError(f"origin record is not an object: {name}",
                              status=502, code="corrupt")
    return raw, doc


def _touched_paths(repo_dir: str, base_head: str, cand_head: str):
    out = _git_ok(repo_dir, ["diff", "--name-only", "-z", base_head,
                             cand_head])
    parts = [p for p in out.decode("utf-8", "replace").split("\x00")
             if p]
    if len(parts) > 64:
        raise DerivedFixError("candidate touches too many paths",
                              status=409, code="bad-parent")
    for path in parts:
        _clean_rel(path)
    return sorted(parts)


def _candidate_patch_paths(repo_dir: str, base_commit: str,
                           cand_head: str):
    """The ORIGINAL origin->candidate patch endpoints.

    Validated stored input base_commit -> candidate head: the
    candidate's own introduced change. Base..candidate replays the
    candidate onto base-side evolution (reversing base-only
    improvements); merge-base is an inferred arbitrary endpoint and
    is never used.
    """
    if not _is_hex(base_commit, 40):
        raise DerivedFixError("origin base commit malformed",
                              status=502, code="corrupt")
    _pinned_commit(repo_dir, base_commit, "origin-base")
    out = _git_ok(repo_dir, ["diff", "--name-only", "-z", base_commit,
                             cand_head])
    parts = [p for p in out.decode("utf-8", "replace").split("\x00")
             if p]
    for path in parts:
        _clean_rel(path)
    return sorted(parts)


def _candidate_diff(repo_dir: str, old_head: str, new_head: str,
                    path: str) -> bytes:
    """Diff bytes between two explicit pins for one path (read-only)."""
    proc = _git(repo_dir,
                ["diff", "--no-color", "--no-ext-diff", "--src-prefix=a/",
                 "--dst-prefix=b/", old_head, new_head, "--", path])
    if proc.returncode != 0:
        raise DerivedFixError(f"cannot diff {path}",
                              status=502, code="unreadable")
    return proc.stdout


def _tree_entry_state(repo_dir: str, head: str, path: str, *,
                      git_bound=None) -> str:
    """present|absent from a proven-readable pinned tree (fail-closed).

    Reuses the UNCHANGED original absence proof: the commit must
    resolve, then ``ls-tree`` must read the tree. An unreadable
    tree, a missing commit, or Git IO failure raises unreadable —
    never a silent absence. A missing or corrupt tree/blob behind a
    present commit can no longer be mislabeled absent.
    """
    if not _ir._ref_readable(repo_dir, head, git_bound=git_bound):
        raise DerivedFixError("pinned commit unreadable",
                              status=502, code="unreadable")
    try:
        absent = _ir._path_absent(repo_dir, head, path,
                                  git_bound=git_bound)
    except _ir.IssueRecoveryError as exc:
        raise _wrap_ir(exc) from exc
    return "absent" if absent else "present"


def _pinned_blob_oid(repo_dir: str, head: str, path: str) -> str:
    """The blob OID for a present pinned path (fail-closed).

    The entry must be a blob object: a tree, commit (submodule), or
    unreadable entry is never treated as file bytes.
    """
    typ = _git(repo_dir, ["cat-file", "-t", f"{head}:{path}"])
    if typ.returncode != 0 or typ.stdout.decode(
            "utf-8", "replace").strip() != "blob":
        raise DerivedFixError(f"pinned path is not a file blob: {path}",
                              status=502, code="unreadable")
    return _git_ok(repo_dir, ["rev-parse", f"{head}:{path}"]).decode(
        "utf-8", "replace").strip()


def _base_blob_info(repo_dir: str, base_head: str, path: str):
    """(oid|None, sha256|None, absent, size|None): base bytes evidence.

    Absence is proved from the READABLE pinned tree (see
    ``_tree_entry_state``); a missing/corrupt tree or blob fails
    closed. An oversized blob keeps OID+size identity but never
    claims a computed raw SHA — the workspace schema carries the
    size so nonabsent entries stay provable without an invented
    digest.
    """
    if _tree_entry_state(repo_dir, base_head, path) == "absent":
        return None, None, True, None
    oid = _pinned_blob_oid(repo_dir, base_head, path)
    size_raw = _git_ok(repo_dir, ["cat-file", "-s",
                                  f"{base_head}:{path}"]).decode(
                                      "utf-8", "replace").strip()
    try:
        size = int(size_raw)
    except ValueError:
        raise DerivedFixError("pinned base blob unreadable",
                              status=502, code="unreadable") from None
    if size > _WORKER_READ_MAX:
        return oid, None, False, size
    raw = _git_ok(repo_dir, ["show", f"{base_head}:{path}"])
    return oid, hashlib.sha256(raw).hexdigest(), False, size


def _stage_only(work: str, paths, *, git_bound=None) -> None:
    """Stage EXACTLY these validated paths (nothing else, ever)."""
    ordered = sorted(set(paths))
    for path in ordered:
        _clean_rel(path)
    if not ordered:
        return
    proc = _git(work, ["add", "--"] + ordered, git_bound=git_bound)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:200]
        raise DerivedFixError(f"staging failed: {detail}",
                              status=502, code="unreadable")
    out = _git_ok(work, ["diff", "--cached", "--name-only", "-z"],
                    git_bound=git_bound)
    staged = sorted(p for p in out.decode("utf-8", "replace").split(
        "\x00") if p)
    if staged != ordered:
        raise DerivedFixError("index does not match the approved set; "
                              "nothing committed",
                              status=502, code="corrupt")


def _commit_work(work: str, dfix_id: str, message: str,
                 allow_empty: bool = False, *,
                 git_bound=None) -> str:
    args = _git_identity_args() + ["commit", "-qm", message]
    if allow_empty:
        args.append("--allow-empty")
    proc = _git(work, args, git_bound=git_bound)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:200]
        raise DerivedFixError(f"commit failed: {detail}",
                              status=502, code="unreadable")
    return _git_ok(work, ["rev-parse", "HEAD"],
                   git_bound=git_bound).decode(
        "utf-8", "replace").strip()


def prepare_repair_workspace(dfix_id: str) -> dict:
    """Deterministically prepare the isolated repair workspace.

    CALLER MUST HOLD the parent-op exclusion. Re-proves the recorded
    origin, resolves both recorded pins live in the SAME repo, adds a
    detached worktree at the pinned base, then applies ONLY
    cleanly-applying candidate hunks for allowed non-conflict paths
    (staged explicitly), resets every other touched path to the base
    blob with recorded SHAs, and commits the prepared tree detached —
    yielding an immutable prep head. The preparation snapshot is the
    observed dirty-then-clean pair, never a hardcoded flag. On
    failure the partial workspace is PRESERVED and its path is
    reported: nothing here deletes worktrees, original checkouts,
    branches, or indexes.
    """
    rec = _read_record(dfix_id)
    if rec.get("status") == "started":
        pass
    elif rec.get("status") in _TERMINAL_BAD \
            and rec.get("workspace") is None:
        pass
    else:
        raise DerivedFixError("workspace already prepared",
                              status=409, code="busy")
    _reprove_origin(rec)
    base_head = rec["base"]["head"]
    cand_head = rec["candidate"]["head"]
    # Candidate and base are distinct linked worktrees of the SAME
    # common Git dir in the real case — compare common dirs, never
    # worktree toplevels. All pinned reads use the protected
    # candidate checkout as the read-only command cwd.
    common_cand = _git_common_dir(rec["candidate"]["worktree"],
                                  "candidate")
    common_base = _git_common_dir(rec["base"]["checkout"], "base")
    if common_cand != common_base:
        raise DerivedFixError("candidate/base checkouts span repos; "
                              "same-repo pins only",
                              status=409, code="bad-parent")
    repo_dir = rec["candidate"]["worktree"]
    _pinned_commit(repo_dir, base_head, "base")
    _pinned_commit(repo_dir, cand_head, "candidate")
    # The whole chain home/derived/<dfix> is proved task-owned
    # BEFORE mkdir/git-worktree/read/stage: a replaced ancestor
    # refuses here with the suspect path preserved.
    _secure_work_chain(dfix_id)
    work = _expected_work_path(dfix_id)
    try:
        st = os.lstat(work)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise DerivedFixError(f"unreadable derived dir: {exc}",
                              status=502, code="unreadable") from exc
    if st is not None:
        raise DerivedFixError("derived workspace already exists",
                              status=409, code="busy")
    proc = _git(repo_dir, ["worktree", "add", "--detach", work,
                           base_head])
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:200]
        raise DerivedFixError(f"worktree add failed: {detail}",
                              status=502, code="unreadable")
    try:
        return _prepare_in_worktree(rec, repo_dir, work, base_head,
                                    cand_head)
    except DerivedFixError as exc:
        raise DerivedFixError(
            f"{exc.message} (partial workspace preserved at {work}; "
            "nothing deleted)",
            status=exc.http_status, code=exc.code) from None


def _git_patch(work: str, patch: bytes, check_only: bool,
               *, git_bound=None):
    """Run git apply [--check] with explicit stdin bytes (no inherit).

    Prep-time only, but over the same hardened path: bound calls
    re-prove the pointer and pin git-dir/work-tree from the record.
    """
    verb = ["apply", "--check", "-"] if check_only else [
        "apply", "-"]
    if git_bound is None:
        argv = _runner.hardened_git_argv(["-C", str(work)] + verb)
    else:
        try:
            paths = _runner.reprove_git_pointer(work, git_bound)
        except _runner.SourceUnreadable as exc:
            raise DerivedFixError(
                "worktree git identity changed since prepare",
                status=502, code="git-pointer-changed") from exc
        argv = _runner.hardened_git_argv(
            verb, gitdir=paths["gitdir"],
            worktree=paths["worktree"])
    try:
        proc = subprocess.run(argv, input=patch,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=_GIT_TIMEOUT_S,
                              env=_runner.hardened_git_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DerivedFixError(f"git unavailable: {exc}",
                              status=502, code="unreadable") from exc
    return proc


def _prepare_in_worktree(rec, repo_dir, work, base_head, cand_head):
    touched = _touched_paths(repo_dir, base_head, cand_head)
    # The recorded conflict set comes from the validated origin; the
    # live base..candidate diff must still contain every one of them,
    # or the world moved under the record — fail closed.
    try:
        op_id = rec["derived_from"]
        root = _ir.rec_root(op_id)
        in_raw = _origin_record_bytes(root, rec["origin"]["input"])
        in_doc = json.loads(in_raw.decode("utf-8"))
        origin_conflicts = set(in_doc.get("conflicted_paths") or [])
        original_touched = [
            p for p in _ir.parse_original_touched(
                in_doc.get("op_error") or "") if isinstance(p, str)]
    except DerivedFixError:
        raise
    except (ValueError, _ir.IssueRecoveryError) as exc:
        raise DerivedFixError("origin conflict scope unreadable",
                              status=502, code="unreadable") from exc
    if not origin_conflicts:
        raise DerivedFixError("origin records no conflicts",
                              status=502, code="corrupt")
    if origin_conflicts - set(touched):
        raise DerivedFixError("recorded conflicts left the current "
                              "base..candidate diff; scope drift",
                              status=409, code="scope-drift")
    base_commit = rec["origin"]["base_commit"]
    patch_paths = set(_candidate_patch_paths(repo_dir, base_commit,
                                             cand_head))
    applied, conflict_reset, retained = [], [], []
    for path in touched:
        in_scope = path in _ALLOWED_SOURCE_PATHS
        conflicted = path in origin_conflicts
        oid, sha, absent, size = _base_blob_info(repo_dir, base_head,
                                                     path)
        if in_scope and not conflicted and path not in patch_paths:
            # Base-side evolution only: the candidate never
            # introduced this path. Keep the pinned base bytes —
            # replaying the current base..candidate delta here would
            # reverse a base-only improvement.
            retained.append({"path": path, "base_blob": oid,
                             "base_blob_sha256": sha,
                             "absent_at_base": absent, "size": size,
                             "note": "base-side evolution; kept at "
                                     "pinned base (not "
                                     "candidate-introduced)"})
            continue
        if in_scope and not conflicted:
            # Apply the ORIGINAL candidate patch (validated base
            # commit -> candidate head), preserving base evolution.
            patch = _candidate_diff(repo_dir, base_commit, cand_head,
                                    path)
            if not patch.strip():
                retained.append({"path": path, "base_blob": oid,
                                 "base_blob_sha256": sha,
                                 "absent_at_base": absent, "size": size,
                                 "note": "no content delta; kept at "
                                         "pinned base"})
                continue
            probe = _git_patch(work, patch, check_only=True)
            if probe.returncode != 0:
                detail = probe.stderr.decode(
                    "utf-8", "replace").strip()[:200]
                conflict_reset.append({
                    "path": path, "base_blob": oid,
                    "base_blob_sha256": sha, "absent_at_base": absent,
                    "size": size,
                    "reason": "does not apply cleanly (not a recorded "
                              f"conflict): {detail}"})
                continue
            apply = _git_patch(work, patch, check_only=False)
            if apply.returncode != 0:
                detail = apply.stderr.decode(
                    "utf-8", "replace").strip()[:200]
                conflict_reset.append({
                    "path": path, "base_blob": oid,
                    "base_blob_sha256": sha, "absent_at_base": absent,
                    "size": size,
                    "reason": f"apply failed: {detail}"})
                continue
            raw = _read_workspace_bytes(work, path)
            applied.append({"path": path, "bytes": len(raw),
                            "blob_sha256": hashlib.sha256(
                                raw).hexdigest()})
        elif conflicted:
            reason = ("recorded conflict; left at base for the worker"
                      if in_scope else
                      "recorded conflict outside worker scope; left at "
                      "pinned base")
            conflict_reset.append({"path": path, "base_blob": oid,
                                   "base_blob_sha256": sha,
                                   "absent_at_base": absent,
                                   "size": size,
                                   "reason": reason})
        else:
            if path.startswith("example/dist/") \
                    or path.endswith(".LICENSE.txt"):
                note = ("kept at pinned base; regenerated by a future "
                        "build step, never hand-edited here")
            else:
                note = ("kept at pinned base; out of Phase A scope")
            retained.append({"path": path, "base_blob": oid,
                             "base_blob_sha256": sha,
                             "absent_at_base": absent, "size": size,
                             "note": note})
    accounted = ({e["path"] for e in applied}
                 | {e["path"] for e in conflict_reset}
                 | {e["path"] for e in retained})
    if accounted != set(touched):
        raise DerivedFixError("prep manifest does not account the full "
                              "touched set; nothing committed",
                              status=502, code="corrupt")
    if {e["path"] for e in applied} - patch_paths:
        raise DerivedFixError("prep applied a non-candidate path; "
                              "nothing committed",
                              status=502, code="corrupt")
    if not applied and not conflict_reset:
        raise DerivedFixError("no effective preparation: nothing for "
                              "Phase A to repair",
                              status=409, code="nothing-to-derive")
    _stage_only(work, [e["path"] for e in applied])
    prep_tree = _git_ok(work, ["write-tree"]).decode(
        "utf-8", "replace").strip()
    snap_dirty = _snapshot_worktree(work)
    prep_head = _commit_work(work, rec["op_id"],
                             f"derived {rec['op_id']} prepared",
                             allow_empty=not applied)
    tree_at_head = _git_ok(work, ["rev-parse",
                                  f"{prep_head}^{{tree}}"]).decode(
                                      "utf-8", "replace").strip()
    if tree_at_head != prep_tree:
        raise DerivedFixError("prep commit tree mismatch; nothing "
                              "recorded",
                              status=502, code="corrupt")
    snap_clean = _snapshot_worktree(work)
    if snap_clean["head"] != prep_head or snap_clean["clean"] is not True:
        raise DerivedFixError("prepared worktree did not settle clean "
                              "at the prep head",
                              status=502, code="corrupt")
    scope = {"conflicts": sorted(origin_conflicts),
             "original_touched": sorted(original_touched),
             "touched": list(touched)}
    rec = dict(rec)
    rec["workspace"] = {
        "path": work, "head": prep_head,
        "fingerprint": snap_clean["fingerprint"], "clean": True,
        "base_head": base_head, "prep_tree": prep_tree,
        "prep_head": prep_head, "applied": applied,
        "conflict_reset": conflict_reset, "retained_base": retained,
        "allowed_changed_paths": list(_ALLOWED_SOURCE_PATHS),
        "result_head": None, "result_tree": None, "scope": scope,
        "git": _runner.record_git_pointer(work),
    }
    rec["updated_at"] = _utcnow()
    _write_record(rec)
    return {"prep_head": prep_head, "prep_tree": prep_tree,
            "applied": applied, "conflict_reset": conflict_reset,
            "retained_base": retained, "scope": scope,
            "dirty_snapshot": snap_dirty, "clean_snapshot": snap_clean}


def _verify_prep_identity(rec: dict) -> dict:
    """The prepared workspace must still be itself: same repo, same
    prep head/tree/fingerprint, observed clean, and every applied
    file still hashing to its manifest blob."""
    ws = rec.get("workspace")
    if not isinstance(ws, dict) or not ws.get("prep_head"):
        raise DerivedFixError("workspace not prepared",
                              status=409, code="busy")
    work = ws["path"]
    out = _git(work, ["rev-parse", "HEAD"])
    if out.returncode != 0 or out.stdout.decode(
            "utf-8", "replace").strip() != ws["prep_head"]:
        raise DerivedFixError("prepared workspace HEAD moved",
                              status=409, code="workspace-changed")
    out = _git(work, ["rev-parse", f"{ws['prep_head']}^{{tree}}"])
    if out.returncode != 0 or out.stdout.decode(
            "utf-8", "replace").strip() != ws["prep_tree"]:
        raise DerivedFixError("prepared workspace tree moved",
                              status=409, code="workspace-changed")
    snap = _snapshot_worktree(work)
    if snap["head"] != ws["prep_head"] \
            or snap["fingerprint"] != ws["fingerprint"] \
            or snap["clean"] is not True:
        raise DerivedFixError("prepared workspace drifted",
                              status=409, code="workspace-changed")
    for entry in ws.get("applied") or []:
        raw = _read_workspace_bytes(work, entry["path"])
        if hashlib.sha256(raw).hexdigest() != entry["blob_sha256"]:
            raise DerivedFixError("prepared file changed under the "
                                  "record",
                                  status=409,
                                  code="workspace-changed")
    return ws


def _pinned_evidence(repo_dir: str, head: str, ref_kind: str,
                     path: str) -> dict:
    """One evidence window from a PINNED commit (never the worktree).

    Returns {path, ref_kind, ref, blob_oid|None, size|None,
    raw_sha256|None, status, text|None, omitted_bytes, lossy}. A path
    missing from the READABLE pinned tree is genuine absence with the
    pin as evidence; an unreadable commit/blob fails closed and is
    never labeled absent; an oversized blob keeps size+OID identity
    but never claims a computed raw SHA.
    """
    try:
        state = _tree_entry_state(repo_dir, head, path)
    except DerivedFixError as exc:
        raise DerivedFixError(f"pinned {ref_kind} tree unreadable: "
                              f"{exc.message}",
                              status=exc.http_status,
                              code=exc.code) from None
    base = {"path": path, "ref_kind": ref_kind, "ref": head,
            "blob_oid": None, "size": None, "raw_sha256": None,
            "status": "absent", "text": None, "omitted_bytes": 0,
            "lossy": False}
    if state == "absent":
        return base
    try:
        oid = _pinned_blob_oid(repo_dir, head, path)
    except DerivedFixError as exc:
        raise DerivedFixError(f"pinned {ref_kind} blob unreadable: "
                              f"{exc.message}",
                              status=502,
                              code="unreadable") from None
    size_raw = _git_ok(repo_dir, ["cat-file", "-s",
                                  f"{head}:{path}"]).decode(
                                      "utf-8", "replace").strip()
    try:
        size = int(size_raw)
    except ValueError:
        raise DerivedFixError(f"pinned {ref_kind} blob unreadable",
                              status=502, code="unreadable") from None
    base["blob_oid"] = oid
    base["size"] = size
    if size > _WORKER_FILE_CAP:
        base["status"] = "too-large"
        base["omitted_bytes"] = size
        return base
    raw = _git_ok(repo_dir, ["show", f"{head}:{path}"])
    base["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        base["text"] = raw.decode("utf-8")
    except UnicodeDecodeError:
        base["text"] = raw.decode("utf-8", "replace")
        base["lossy"] = True
    base["status"] = "ok"
    return base


def _short12(value) -> str:
    return str(value or "")[:12] or "없음"


def _trunc_marker(omitted: int) -> str:
    return f"\n(…{omitted}바이트 생략; 잘린 뒤 내용은 모름…)"


def _render_evidence_head(ev: dict) -> str:
    """The immutable identity line: never truncated, never dropped.

    Path, ref kind, FULL pinned commit, FULL blob OID, FULL raw
    SHA-256, and byte size survive every budget — only the body
    below is truncatable. The declaration link is only sound when
    these bytes are actually sent: the model cannot derive the
    SHA-256 of an untransmitted manifest from a prefix. Six such
    headers are bounded (~1.5KB total) and fit the global contract.
    Missing digests (absent/unread/oversized windows) render as
    explicit 없음 markers — never a prefix that pretends identity.
    """
    ref = ev.get("ref") if _is_hex(ev.get("ref"), 40) else None
    blob = ev.get("blob_oid") if _is_hex(ev.get("blob_oid"), 40) \
        else None
    raw = ev.get("raw_sha256") if _is_hex(ev.get("raw_sha256"), 64) \
        else None
    size = ev.get("size")
    return (f"--- evidence {ev['path']} @ {ev['ref_kind']} "
            f"(ref {ref or '없음(미수집)'}, "
            f"blob {blob or '없음(미수집)'}, "
            f"raw-sha256 {raw or '없음(미주장)'}, "
            f"원본 {size if isinstance(size, int) else '?'}B) ---")


def _render_evidence_body(ev: dict) -> str:
    """The truncatable payload for one evidence window."""
    if ev["status"] == "ok":
        body = ev["text"] or ""
        if ev["lossy"]:
            body += "\n(참고: 원본 바이트 중 UTF-8이 아닌 부분이 대체 문자로 " \
                "표시됨)"
        return body
    if ev["status"] == "absent":
        return f"(없음: 고정 커밋 {_short12(ev['ref'])}의 트리에 없음이 확인됨)"
    if ev["status"] == "too-large":
        return f"(생략: {ev['size']}B가 전송 한도를 초과 — 내용 미전송, " \
            "원본 해시 미주장)"
    return "(읽기 불가: 고정 커밋에서 읽지 못함 — 부재가 아님)"


def _render_evidence(ev: dict) -> str:
    """The full canonical render (head + body, untruncated)."""
    return _render_evidence_head(ev) + "\n" + _render_evidence_body(ev)


def _fit_evidence_blocks(evs: list, budget: int):
    """Fit evidence blocks to a byte budget — bounded, single pass.

    Returns (block texts, used bytes) where each block is its
    immutable head (FULL pinned commit, blob, and raw SHA-256 —
    never prefixes) plus its (possibly truncated) body. Heads,
    delimiters, and state lines are reserved first and never cut;
    only clean ``ok`` text bodies shrink, each to an equal share,
    with omissions computed ONCE from the original body bytes
    (byte units throughout, never mixed with chars). Lossy
    (non-UTF-8) windows never truncate: they pass through only when
    they fit, with their explicit warning label, and otherwise raise
    an honest over-budget refusal — no raw omission is ever claimed
    from transformed display text. A marker length depends only on
    the omitted digit count, which grows slower than the bytes freed
    — so one deterministic adjustment always fits, and anything
    unrepresentable (zero/tiny budget, oversized fixed part) raises
    a typed over-budget refusal instead of looping, re-counting
    markers, or cutting headers. The operator note and questions
    live in the fixed prompt head, never in this truncatable
    payload — they are never silently dropped here. Never returns
    more than ``budget`` bytes.
    """
    heads = [_render_evidence_head(ev) for ev in evs]
    bodies = [_render_evidence_body(ev) for ev in evs]
    ok_idx = [i for i, ev in enumerate(evs)
              if ev.get("status") == "ok"]
    seps = max(len(evs) - 1, 0)
    fixed = sum(len(h.encode("utf-8")) + 1 for h in heads) \
        + seps \
        + sum(len(bodies[i].encode("utf-8"))
              for i in range(len(evs)) if i not in ok_idx)
    if fixed > budget:
        raise DerivedFixError("worker prompt over budget; refusing",
                              status=409, code="over-budget")
    share = (budget - fixed) // len(ok_idx) if ok_idx else 0
    fitted = []
    for i, ev in enumerate(evs):
        if i not in ok_idx:
            fitted.append(heads[i] + "\n" + bodies[i])
            continue
        if ev.get("lossy") is True:
            # A lossy window's sent text is replacement-transformed:
            # its rendered bytes are NOT the original raw bytes, so
            # no omission count computed from the display text may
            # stand in for raw bytes. A fitting lossy body passes
            # through with its explicit warning label; an
            # over-share lossy body refuses honestly instead of
            # claiming raw omission counts from transformed text.
            # Full raw identity (SHA + byte size) stays in the head.
            if len(bodies[i].encode("utf-8")) <= share:
                fitted.append(heads[i] + "\n" + bodies[i])
                continue
            raise DerivedFixError("worker prompt over budget: "
                                  "lossy evidence window does not fit "
                                  "and cannot be honestly truncated",
                                  status=409, code="over-budget")
        raw = bodies[i].encode("utf-8")
        if len(raw) <= share:
            fitted.append(heads[i] + "\n" + bodies[i])
            continue
        # Worst-case marker uses the full body length for digits,
        # so the first cut provably leaves room for its marker;
        # one adjustment then fits exactly (digits grow slower
        # than the bytes freed).
        keep = share - len(_trunc_marker(len(raw)).encode("utf-8"))
        if keep < 0:
            raise DerivedFixError("worker prompt over budget; "
                                  "refusing",
                                  status=409, code="over-budget")
        kept = raw[:keep].decode("utf-8", "ignore")
        omitted = len(raw) - len(kept.encode("utf-8"))
        text = kept + _trunc_marker(omitted)
        overflow = len(text.encode("utf-8")) - share
        if overflow > 0:
            # Unreachable by construction (digits grow slower than
            # the bytes freed), kept as a typed backstop instead
            # of a second guessing loop.
            keep -= overflow
            if keep < 0:
                raise DerivedFixError("worker prompt over budget; "
                                      "refusing",
                                      status=409, code="over-budget")
            kept = raw[:keep].decode("utf-8", "ignore")
            omitted = len(raw) - len(kept.encode("utf-8"))
            text = kept + _trunc_marker(omitted)
            if len(text.encode("utf-8")) > share:
                raise DerivedFixError("worker prompt over budget; "
                                      "refusing",
                                      status=409, code="over-budget")
        fitted.append(heads[i] + "\n" + text)
    used = sum(len(b.encode("utf-8")) for b in fitted) + seps
    if used > budget:
        raise DerivedFixError("worker prompt over budget; refusing",
                              status=409, code="over-budget")
    return fitted, used


def _build_worker_prompt(rec: dict, repo_dir: str,
                         cur: dict) -> tuple:
    """Render the bounded source-repair prompt (server-side only).

    Every evidence window is read from an IMMUTABLE pinned commit —
    base, candidate, and the prepared prep head — never from the live
    working file twice under different labels. Each window carries
    its path/ref-kind/ref/blob/raw-SHA/size identity; omissions are
    explicit with reasons, never silent. The final UTF-8 byte total,
    including headers, schema, and omission lines, must fit the
    global budget or preparation refuses. The prompt asks for source
    edits only; it is NOT an OS-enforced tool ban — confinement
    comes from the managed worker sandbox plus the post-run diff
    gate. No build/test/install/network or public action is
    authorized by this prompt.

    Returns ``(prompt, info)``: the prompt bytes are exactly what the
    worker receives; ``info`` is truthful selected/excluded/partial/
    omitted metadata built from the SAME locals (never a re-derived
    copy) for the immutable input sidecar.
    """
    ws = rec["workspace"]
    hyp = rec["hypothesis"]
    origin = rec["origin"]
    lines = [
        "읽기 전용 조사가 아니라 제한된 소스 수정을 요청한다. 아래 "
        "규칙을 어긴 출력은 무효다.",
        "허용 변경 경로(정확히 이 목록만): "
        + ", ".join(_ALLOWED_SOURCE_PATHS),
        "금지: dist/*, *.LICENSE.txt, 위 목록 밖 모든 경로, 작업 영역 "
        "밖 쓰기, 빌드·테스트·설치·네트워크·외부 전송.",
        "이 프롬프트는 도구 금지가 아니다: 실제 격리는 앱 작업자의 "
        "관리 파일시스템 샌드박스와 서버의 전후 diff 검증이 맡는다.",
        f"가설: {hyp['id']} — {hyp['note']}",
        "선택된 운용자 기술 가설 정의(서버 소유 — 브라우저 입력이 "
        "아님; 소유자 정책 답변이 아님):",
        _HYPOTHESIS_DEFS.get(hyp["id"], "(알 수 없는 가설)") + " "
        "(운용자 기술 실험 — 별도 검증 단계가 필요)",
        f"원본: 입력 {origin['input']} · 보고서 {origin['report']} · "
        f"지문 {origin['fingerprint'][:16]}… · "
        f"입력SHA {origin['input_sha256'][:12]}… · "
        f"보고서SHA {origin['report_sha256'][:12]}…",
        f"기록된 충돌 {len(cur['conflicts'])}건: "
        + ", ".join(cur["conflicts"]),
        f"원본 후보 변경(기록 파싱) "
        f"{len(cur['original_touched'])}건" + (
            ": " + ", ".join(cur["original_touched"])
            if cur["original_touched"] else " (파싱된 목록 없음)"),
        "충돌 범위(베이스에 고정됨; 아래 base 해시와 다르면 중단):",
    ]
    for entry in ws.get("conflict_reset") or []:
        lines.append(f"- {entry.get('path')}: "
                     f"{'베이스에 없음(확인됨)' if entry.get('absent_at_base') else 'base blob ' + _short12(entry.get('base_blob'))} · "
                     f"{entry.get('reason')}")
    for entry in ws.get("applied") or []:
        lines.append(f"- {entry.get('path')}: 후보 변경 적용됨 "
                     f"({entry.get('bytes')}B, sha "
                     f"{_short12(entry.get('blob_sha256'))})")
    for entry in ws.get("retained_base") or []:
        lines.append(f"- {entry.get('path')}: 베이스 유지 · "
                     f"{entry.get('note')}")
    shown_qs = cur["questions"][:_PROMPT_QUESTIONS]
    lines.append(f"원본 질문 {len(cur['questions'])}건 중 "
                 f"{len(shown_qs)}건 전송"
                 + (f" (외 {len(cur['questions']) - len(shown_qs)}건 미전송)"
                    if len(cur["questions"]) > len(shown_qs) else "")
                 + ":")
    for i, q in enumerate(shown_qs):
        text = q if len(q) <= _PROMPT_QUESTION_CHARS else (
            q[:_PROMPT_QUESTION_CHARS]
            + f"…({len(q.encode('utf-8')) - len(q[:_PROMPT_QUESTION_CHARS].encode('utf-8'))}바이트 생략)")
        lines.append(f"Q{i + 1}. {text}")
    fixed_head = "\n".join(lines) + "\n"
    schema_tail = (
        "출력 계약(정확히 이 JSON 하나만, 다른 텍스트 금지): "
        '{"status": "proposal", "changed_paths": [...], '
        '"per_file_sha256": {path: sha256}, '
        '"diff_stat": {"files": n, "insertions": n, "deletions": n}, '
        '"resolutions": [{"path": p, "conflict_id": c, "choice": '
        '"H-A"|"H-B"}], "notes": "…"} — conflict_id는 위에 기록된 '
        "충돌 경로 중 하나여야 하며, changed_paths는 허용 목록 안의 "
        "실제 변경 파일과 정확히 일치해야 한다.")
    evs = []
    for path in _ALLOWED_SOURCE_PATHS:
        for label, head in (("base", ws["base_head"]),
                            ("candidate", rec["candidate"]["head"]),
                            ("prepared", ws["prep_head"])):
            evs.append(_pinned_evidence(repo_dir, head, label, path))
    # Blocks join with n-1 separators plus one framing newline.
    framing = len(evs)
    fixed_len = len((fixed_head + schema_tail).encode("utf-8"))
    budget = _WORKER_PROMPT_CAP - fixed_len - framing
    fitted, _used = _fit_evidence_blocks(evs, budget)
    prompt = fixed_head + "\n".join(fitted) + "\n" + schema_tail
    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) > _WORKER_PROMPT_CAP:
        raise DerivedFixError("worker prompt over budget; refusing",
                              status=409, code="over-budget")
    # Per-window provenance comes from the EXACT fitted block placed
    # in the prompt — never from availability alone. `sent` means the
    # window contributed body bytes; the bytes and their SHA are the
    # fitted body itself (code, possibly truncated, or the honest
    # absent/oversize description). A fitting that shrinks a readable
    # body to empty, a clipped question, and an untransmitted window
    # are each labeled with what actually happened — never "all
    # delivered" from an `ok` status, never invented counts.
    evidence = []
    for i, ev in enumerate(evs):
        head = _render_evidence_head(ev)
        full = head + "\n" + _render_evidence_body(ev)
        if not fitted[i].startswith(head + "\n"):
            raise DerivedFixError(
                "evidence fit diverged from its immutable head; "
                "refusing rather than misbinding provenance",
                status=502, code="corrupt")
        body = fitted[i][len(head) + 1:]
        body_bytes = body.encode("utf-8")
        status = ev.get("status")
        partial = fitted[i] != full
        if status == "ok":
            reason = "over-global-budget" if partial else None
        elif status == "too-large":
            reason = "blob-over-file-cap"
        else:
            reason = "absent-at-ref"
        evidence.append(
            {"path": ev.get("path"), "ref_kind": ev.get("ref_kind"),
             "ref": ev.get("ref"), "status": status,
             "size": ev.get("size"),
             "sent": len(body_bytes) > 0,
             "partial": partial,
             "sent_bytes": len(body_bytes),
             "sent_sha256": hashlib.sha256(body_bytes).hexdigest()
             if body_bytes else None,
             "omitted_reason": reason})
    clipped = 0
    clipped_bytes = 0
    for q in shown_qs:
        if len(q) > _PROMPT_QUESTION_CHARS:
            clipped += 1
            clipped_bytes += len(q.encode("utf-8")) - len(
                q[:_PROMPT_QUESTION_CHARS].encode("utf-8"))
    info = {
        "evidence": evidence,
        "questions": {"total": len(cur.get("questions") or []),
                      "sent": len(shown_qs),
                      "omitted": len(cur.get("questions") or [])
                      - len(shown_qs),
                      "clipped": clipped,
                      "clipped_bytes": clipped_bytes},
        "conflicts": list(cur.get("conflicts") or []),
        "original_touched": list(cur.get("original_touched") or []),
        "prompt_bytes": len(prompt_bytes),
    }
    return prompt, info


def _scrubbed_env() -> dict:
    """Worker env: scrub billing keys, then layer the per-run cache.

    Mirrors the review worker convention: META_API_KEY removed so
    subscription login stays the billing path; the toolchain cache
    env is added by exec_prompt itself from the workdir.
    """
    return {k: v for k, v in os.environ.items()
            if k != "META_API_KEY"}


def _raw_meta(raw_stream) -> dict:
    """TOTAL vs SAVED raw accounting for one transport output.

    None (transport never ran) stays None with zero totals — distinct
    from an actually extracted empty string. Bounded head, explicit
    byte counts, truncation flag, and a SHA binding the saved head.
    """
    if raw_stream is None:
        return {"raw_head": None, "raw_head_sha256": None,
                "raw_total_chars": 0, "raw_total_bytes": 0,
                "log_bytes": 0, "truncated": False}
    text = raw_stream if isinstance(raw_stream, str) else str(raw_stream)
    total_chars = len(text)
    total_bytes = len(text.encode("utf-8"))
    saved = text[:_RAW_HEAD_CAP]
    saved_bytes = len(saved.encode("utf-8"))
    return {"raw_head": saved,
            "raw_head_sha256": hashlib.sha256(
                saved.encode("utf-8")).hexdigest(),
            "raw_total_chars": total_chars,
            "raw_total_bytes": total_bytes,
            "log_bytes": saved_bytes,
            "truncated": total_chars > len(saved)}


def _parse_worker_output(raw_stream) -> dict:
    """Strict JSON parse of the worker terminal text (proposal only).

    The transport returns a raw JSONL event stream, never plain
    report JSON: the terminal text is extracted with the UNCHANGED
    shared helper, then strictly parsed. rc0 without terminal text
    is a failure, never a success.
    """
    terminal = batches._terminal_report(raw_stream or "")
    if not terminal or not terminal.strip():
        raise DerivedFixError("worker produced no terminal text: "
                              "rc0 is not success",
                              status=502, code="source-error")
    try:
        doc = json.loads(terminal.strip())
    except ValueError:
        raise DerivedFixError(
            "worker terminal is not strict JSON: "
            + terminal.strip()[:200],
            status=502, code="invalid-report")
    return _validate_worker_report(doc)


def _pre_transport_proof(rec: dict, ws: dict) -> dict:
    """Fresh pre-transport proofs: origin bytes, chain, live binding
    (including the unchanged full sent-source replay), prep identity,
    pins. The recorded candidate checkout is the read-only command
    cwd (same common repo as base, proved at prepare time). Returns
    the repo dir and the fresh origin proof for the prompt build."""
    cur = _reprove_origin(rec)
    _reprove_work_chain(rec["op_id"])
    _verify_prep_identity(rec)
    repo_dir = rec["candidate"]["worktree"]
    _pinned_commit(repo_dir, rec["base"]["head"], "base")
    _pinned_commit(repo_dir, rec["candidate"]["head"], "candidate")
    return {"repo_dir": repo_dir, "cur": cur}


def _post_transport_proof(rec: dict) -> None:
    """Origin/parent/prep/binding unchanged by the transport window.

    Beyond the origin re-proof: the workspace must still sit
    EXACTLY at the prep head (a worker commit or checkout moves
    HEAD and refuses), detached (an on-branch state refuses), with
    an empty index (anything the worker staged refuses). Proving
    this BEFORE staging and committing means a worker-side git
    action can never slip into the app's result commit.
    """
    _reprove_origin(rec)
    _reprove_work_chain(rec["op_id"])
    ws = rec.get("workspace") or {}
    work = ws.get("path")
    prep_head = ws.get("prep_head") or ""
    out = _git(work, ["rev-parse", "HEAD"])
    head = out.stdout.decode("utf-8", "replace").strip() \
        if out.returncode == 0 else ""
    if not head:
        raise DerivedFixError("workspace repo unreadable after "
                              "transport",
                              status=502, code="unreadable")
    if head != prep_head:
        raise DerivedFixError("workspace HEAD moved during transport "
                              "(worker commit or checkout); workspace "
                              "quarantined, nothing staged",
                              status=502, code="workspace-changed")
    _pinned_commit(work, prep_head, "prep")
    detached = _git(work, ["symbolic-ref", "-q", "HEAD"])
    if detached.returncode == 0:
        raise DerivedFixError("workspace left on a branch during "
                              "transport; workspace quarantined, "
                              "nothing staged",
                              status=502, code="workspace-changed")
    staged = _git_ok(work, ["diff", "--cached", "--name-only", "-z"])
    if staged.decode("utf-8", "replace").strip("\x00"):
        raise DerivedFixError("workspace index not empty after "
                              "transport (worker staged files); "
                              "workspace quarantined, nothing staged",
                              status=502, code="workspace-changed")


def _real_diff_stat(work: str, prep_head: str, *, git_bound=None):
    """(files, insertions, deletions) of the real staged diff.

    Parsed from ``git diff prep_head --numstat`` AFTER the backend
    stages exactly the approved set — staged plus worktree changes
    vs the prep head. Binary entries (``-`` counts) and unparseable
    lines refuse as unverifiable, never as proven counts.
    """
    out = _git_ok(work, ["diff", "--numstat", prep_head, "--", "."],
                    git_bound=git_bound)
    files = insertions = deletions = 0
    for line in out.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise DerivedFixError("unparseable diff stat",
                                  status=502, code="corrupt")
        if parts[0] == "-" or parts[1] == "-":
            raise DerivedFixError("unverifiable binary diff",
                                  status=502, code="corrupt")
        try:
            adds, dels = int(parts[0]), int(parts[1])
        except ValueError:
            raise DerivedFixError("unparseable diff stat",
                                  status=502, code="corrupt") from None
        if adds < 0 or dels < 0:
            raise DerivedFixError("unparseable diff stat",
                                  status=502, code="corrupt")
        files += 1
        insertions += adds
        deletions += dels
    return files, insertions, deletions


def _finish_explained(rec: dict, attempt: dict, status: str,
                      exc: DerivedFixError, job_id, meta,
                      tail: str) -> None:
    """Terminal failure binding the machine-readable code in text.

    The stored error keeps the human message plus the exact typed
    code (bounded alnum in brackets) so oracles and operators can
    distinguish stale-origin from hash-mismatch from diff-mismatch
    without guessing.
    """
    _finish_attempt(rec, attempt, status,
                    f"{exc.message} [{exc.code}]; {tail}", job_id,
                    meta)


def _worktree_changes(work: str, prep_head: str, *, git_bound=None):
    """(changed, new): typed before/after detection vs the prep head.

    Uses --raw name-status so deletions (D), renames (R), and type
    changes (T) are typed refusals — never silently merged, never
    staged. Mode-only M entries (identical bytes) refuse as
    mode-only changes. Untracked files are split: cache-prefix files
    stay out of the commit (symlink-walked for escapes), allowed new
    files join the backend-staged set, everything else refuses.
    """
    out = _git_ok(work, ["diff", "--raw", "-z", prep_head, "--", "."],
                    git_bound=git_bound)
    tokens = out.decode("utf-8", "replace").split("\x00")
    changed = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not tok or not tok.startswith(":"):
            continue
        parts = tok.split(" ")
        if len(parts) < 5:
            raise DerivedFixError("unparseable diff status",
                                  status=502, code="corrupt")
        status = parts[4][:1]
        if i >= len(tokens):
            break
        old_path = tokens[i]
        i += 1
        if status == "R":
            if i >= len(tokens):
                break
            new_path = tokens[i]
            i += 1
            raise DerivedFixError(
                f"refused renamed path: {old_path} -> {new_path}",
                status=502, code="renamed-path")
        path = old_path
        if status == "D":
            raise DerivedFixError(f"refused deleted path: {path}",
                                  status=502, code="deleted-path")
        if status == "T":
            raise DerivedFixError(f"refused type change: {path}",
                                  status=502, code="type-change")
        if status == "U":
            raise DerivedFixError(f"refused unmerged path: {path}",
                                  status=502, code="corrupt")
        if status not in ("A", "M"):
            raise DerivedFixError(f"refused unexpected change: {path}",
                                  status=502, code="corrupt")
        _clean_rel(path)
        changed.append(path)
    for path in changed:
        _assert_no_symlink_components(work, path)
    # Mode-only M: identical bytes to the prep blob refuses.
    for path in list(changed):
        proc = _git(work, ["show", f"{prep_head}:{path}"],
                    git_bound=git_bound)
        if proc.returncode == 0:
            if proc.stdout == _read_workspace_bytes(work, path):
                raise DerivedFixError(
                    f"refused mode-only change: {path}",
                    status=502, code="mode-only-change")
    status_out = _git_ok(work, ["status", "--porcelain=v1", "-uall",
                                "-z"],
                            git_bound=git_bound)
    new_files = []
    for line in status_out.decode("utf-8", "replace").split("\x00"):
        if not line.startswith("??"):
            continue
        rel = line[2:].strip().strip('"')
        if not rel:
            continue
        if rel == ".openmainty-cache" or rel.startswith(
                ".openmainty-cache/"):
            # Cache activity never enters the commit, but a symlink
            # inside the cache prefix could redirect a later read —
            # refuse symlink components anywhere on the path.
            _assert_path_no_symlinks(work, rel.rstrip("/"))
            continue
        _clean_rel(rel)
        if rel not in _ALLOWED_SOURCE_PATHS:
            raise DerivedFixError(
                f"worker left untracked file outside scope: {rel}; "
                "workspace quarantined, nothing merged",
                status=502, code="out-of-scope")
        # A tracked path shadowed by an untracked twin refuses; the
        # tree proof fails closed so an unreadable prep tree never
        # lets a shadow through as "untracked".
        if _tree_entry_state(work, prep_head, rel,
                             git_bound=git_bound) == "present":
            raise DerivedFixError(
                f"refused shadowed tracked path: {rel}",
                status=502, code="corrupt")
        _assert_no_symlink_components(work, rel)
        new_files.append(rel)
    return sorted(set(changed)), sorted(set(new_files))


def _assert_scope(work: str, changed, new_files) -> None:
    for path in list(changed) + list(new_files):
        if path not in _ALLOWED_SOURCE_PATHS:
            raise DerivedFixError(
                f"worker touched out-of-scope path: {path}; "
                "workspace quarantined, nothing merged",
                status=502, code="out-of-scope")
        _assert_no_symlink_components(work, path)


def _hash_workspace_files(work: str, changed) -> dict:
    verified = {}
    for path in changed:
        raw = _read_workspace_bytes(work, path)
        verified[path] = hashlib.sha256(raw).hexdigest()
    return verified


def run_repair_worker(dfix_id: str, attempt_id: str, job_id=None,
                      cancel_event=None) -> int:
    """Run ONE app-owned source-repair model step (worker thread).

    CALLER MUST HOLD the parent-op exclusion. The preflight
    authority gate runs BEFORE any status write: without a fresh
    genuine preflight bound to the SAME live identity the attempt
    stays queued — no status change, no model/job/worktree mutation.
    Then re-proves origin, parent, and prep identity, persists the
    exact sent prompt SHA, and runs the worker through the DEDICATED
    verified constructor (never the generic exec_prompt) under the
    restricted network profile with the workdir pinned to the
    derived root. After transport it re-proves the world did not
    move, recomputes the prompt to detect manifest tampering, and
    verifies: non-empty diff, typed change classes, allowlist-only
    changes, app-recomputed hashes matching the proposal, result
    commit detached. Success records ``source_ready_unverified`` — a
    validated proposal, never a verified fix. Returns 0 on
    terminal-ready, 1 on terminal failure, 130 when the operator
    cancelled (caller maps it like the review path).
    """
    rec = _read_record(dfix_id)
    attempt = next((a for a in rec["source_attempts"]
                    if a.get("id") == attempt_id), None)
    if attempt is None:
        raise DerivedFixError("unknown source attempt",
                              status=404, code="unknown-attempt")
    if attempt.get("status") not in ("queued", "started"):
        raise DerivedFixError("attempt already terminal",
                              status=409, code="busy")
    _require_target_authority()
    ws = rec.get("workspace")
    if not isinstance(ws, dict) or not ws.get("prep_head"):
        raise DerivedFixError("workspace not prepared",
                              status=409, code="busy")
    attempt = dict(attempt)
    attempt["status"] = "source-running"
    _store_attempt(rec, attempt)
    try:
        proof = _pre_transport_proof(rec, ws)
        repo_dir = proof["repo_dir"]
        cur = proof["cur"]
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "refused", exc, job_id,
                          _raw_meta(None), "nothing started")
        return 1
    try:
        prompt, build_info = _build_worker_prompt(rec, repo_dir, cur)
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "refused", exc, job_id,
                          _raw_meta(None), "nothing started")
        return 1
    # The immutable input is published BEFORE any dispatch and the
    # pin is recorded with the prompt hash in one store; the recorded
    # bytes are then re-verified. A persistence failure at ANY of
    # these three steps (publish, pin store, pre-verify) closes the
    # attempt as a truthful terminal with NO transport — never a
    # phantom source-running/queued record. A failed pin store still
    # attempts the terminal write best-effort (the orphan artifact
    # stays on disk as failure evidence); if storage itself remains
    # unwritable the explicit unreadable error propagates instead of
    # a fake terminal. `prompt` stays the original str object end to
    # end: UTF-8 equality, never a rebuilt payload.
    try:
        pin = _publish_full_input(dfix_id, attempt, prompt,
                                  build_info, ws, rec)
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        attempt = dict(attempt)
        attempt["prompt_sha256"] = prompt_sha
        attempt["full_input"] = pin
        _store_attempt(_read_record(dfix_id), attempt)
        _verify_full_input(dfix_id, attempt["id"])
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          _raw_meta(None), "workspace preserved")
        return 1
    try:
        home_str = str(_home())
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "refused", exc, job_id,
                          _raw_meta(None), "nothing started")
        return 1
    try:
        # Dedicated verified constructor ONLY: pinned binary, pinned
        # model/profile, restricted network, narrowed child env, no
        # trace on this target job. The generic exec_prompt path is
        # NOT used here even though it stays byte-identical for the
        # older dependency workers.
        exit_code, raw_stream, _usage = muse.exec_source_repair(
            prompt, _canonicalise_workdir(ws["path"]),
            timeout=_WORKER_TIMEOUT_S,
            cancel_event=cancel_event, trace=False,
            preflight_home=home_str, admission_kind="target")
    except Exception as exc:
        name = type(exc).__name__
        if cancel_event is not None and cancel_event.is_set():
            _finish_attempt(rec, attempt, "cancelled",
                            "operator cancelled the repair worker",
                            job_id, _raw_meta(None))
            return 130
        _finish_attempt(rec, attempt, "source-error",
                        f"worker crashed ({name})", job_id,
                        _raw_meta(None))
        return 1
    meta = _raw_meta(raw_stream)
    if cancel_event is not None and cancel_event.is_set():
        _finish_attempt(rec, attempt, "cancelled",
                        "operator cancelled the repair worker", job_id,
                        meta)
        return 130
    if exit_code == 124:
        _finish_attempt(rec, attempt, "source-error",
                        "worker timed out; workspace preserved", job_id,
                        meta)
        return 1
    if exit_code != 0:
        _finish_attempt(rec, attempt, "source-error",
                        f"worker exit {exit_code}; workspace preserved",
                        job_id, meta)
        return 1
    # The published input is re-verified after transport and before
    # any result is accepted: moved bytes refuse, never record.
    try:
        _verify_full_input(dfix_id, attempt["id"])
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "workspace preserved")
        return 1
    try:
        report = _parse_worker_output(raw_stream)
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "workspace preserved")
        return 1
    try:
        _post_transport_proof(rec)
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "workspace preserved")
        return 1
    # The prompt must recompute byte-identical: any manifest or prep
    # tampering between send and finish refuses, never records.
    try:
        fresh = _read_record(dfix_id)
        prompt2, _build_info2 = _build_worker_prompt(fresh, repo_dir, cur)
        if hashlib.sha256(prompt2.encode("utf-8")).hexdigest() != \
                prompt_sha:
            raise DerivedFixError("sent prompt no longer recomputes; "
                                  "manifest or prep changed during the "
                                  "run",
                                  status=502, code="prompt-drift")
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "workspace preserved")
        return 1
    bound = ws.get("git")
    try:
        changed, new_files = _worktree_changes(ws["path"],
                                               ws["prep_head"],
                                               git_bound=bound)
        _assert_scope(ws["path"], changed, new_files)
        if not changed and not new_files:
            raise DerivedFixError(
                "worker produced no effective change; unverified, "
                "never done",
                status=502, code="no-change")
        # The claimed changed set must EQUAL the backend-observed
        # effective change (tracked A/M plus backend-staged new
        # files): an extra claimed path, a missing real change, or
        # an unchanged path dressed as changed all refuse. Shape
        # (keys == changed) is not enough — set identity is.
        effective = set(changed) | set(new_files)
        if set(report.get("changed_paths") or []) != effective:
            raise DerivedFixError(
                "worker changed_paths do not equal the observed "
                "workspace change; workspace quarantined",
                status=502, code="changed-mismatch")
        verified = _hash_workspace_files(ws["path"], changed)
        for rel in new_files:
            raw = _read_workspace_bytes(ws["path"], rel)
            verified[rel] = hashlib.sha256(raw).hexdigest()
        claimed = report.get("per_file_sha256") or {}
        for path in verified:
            if claimed.get(path) != verified.get(path):
                raise DerivedFixError(
                    f"worker hash mismatch for {path}: proposal not "
                    "trusted; workspace quarantined",
                    status=502, code="hash-mismatch")
        scope_conflicts = set(
            (ws.get("scope") or {}).get("conflicts") or [])
        for entry in report.get("resolutions") or []:
            if entry.get("conflict_id") not in scope_conflicts:
                raise DerivedFixError(
                    "worker resolution references an unrecorded "
                    "conflict",
                    status=502, code="invalid-resolution")
            # The resolution choice is the ATTEMPT's hypothesis, not
            # any free hypothesis token: a valid report resolves the
            # question this attempt was started for.
            if entry.get("choice") != attempt.get("hypothesis_id"):
                raise DerivedFixError(
                    "worker resolution does not answer this "
                    "attempt's hypothesis",
                    status=502, code="invalid-resolution")
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "refused", exc, job_id, meta,
                          "nothing committed")
        return 1
    try:
        _reprove_work_chain(dfix_id)
        _stage_only(ws["path"], list(verified), git_bound=bound)
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "nothing committed")
        return 1
    # The claimed diff stat is verified against the REAL staged
    # diff (tracked edits plus backend-staged new files): file,
    # insertion, and deletion counts must match exactly, or the
    # proposal refuses before any commit. Binary/unparseable diff
    # output refuses as unverifiable, never as proven.
    try:
        files_n, ins_n, dels_n = _real_diff_stat(ws["path"],
                                                ws["prep_head"],
                                                git_bound=bound)
        diff = report.get("diff_stat") or {}
        if files_n != diff.get("files") \
                or ins_n != diff.get("insertions") \
                or dels_n != diff.get("deletions"):
            raise DerivedFixError(
                "worker diff_stat does not match the real staged "
                f"diff ({files_n}/{ins_n}/{dels_n}); workspace "
                "quarantined, nothing committed",
                status=502, code="diff-mismatch")
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "refused", exc, job_id, meta,
                          "nothing committed")
        return 1
    tree = _git_ok(ws["path"], ["write-tree"],
                   git_bound=bound).decode(
        "utf-8", "replace").strip()
    try:
        _reprove_work_chain(dfix_id)
        head = _commit_work(ws["path"], dfix_id,
                            f"derived {dfix_id} repaired",
                            git_bound=bound)
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "nothing recorded")
        return 1
    try:
        tree_at_head = _git_ok(ws["path"], ["rev-parse",
                                             f"{head}^{{tree}}"],
                                    git_bound=bound).decode(
                                                 "utf-8",
                                                 "replace").strip()
        if tree_at_head != tree:
            raise DerivedFixError("result commit tree mismatch",
                                  status=502, code="corrupt")
        out = _git_ok(ws["path"], ["ls-tree", "-r", "--name-only",
                                   head],
                      git_bound=bound)
        committed = out.decode("utf-8", "replace").splitlines()
        if any(p == ".openmainty-cache" or p.startswith(
                ".openmainty-cache/") for p in committed):
            raise DerivedFixError("result commit contains cache files",
                                  status=502, code="corrupt")
        snap = _snapshot_worktree(ws["path"], git_bound=bound)
        if snap["head"] != head or snap["clean"] is not True:
            raise DerivedFixError("result worktree did not settle "
                                  "clean at the result head",
                                  status=502, code="corrupt")
        rec2 = _read_record(dfix_id)
        ws2 = dict(rec2["workspace"])
        ws2["result_head"] = head
        ws2["result_tree"] = tree
        ws2["head"] = head
        ws2["fingerprint"] = snap["fingerprint"]
        attempt2 = dict(attempt)
        attempt2["status"] = "source-ready-unverified"
        attempt2["worker_report"] = report
        attempt2["verified_paths"] = verified
        # The owned link stays on the successful attempt as
        # history; current ownership clears on the next attempt.
        attempt2["job"] = dict(rec2["job"]) \
            if isinstance(rec2.get("job"), dict) else None
        attempt2["error"] = ""
        attempt2["ended_at"] = _utcnow()
        for key in ("log_bytes", "truncated", "raw_head",
                    "raw_head_sha256", "raw_total_chars",
                    "raw_total_bytes"):
            attempt2[key] = meta[key]
        _store_attempt(rec2, attempt2,
                       record_status="source-ready-unverified",
                       workspace=ws2)
        check = _read_record(dfix_id)
        if check.get("workspace") != ws2:
            raise DerivedFixError("stored workspace mismatch after "
                                  "write",
                                  status=502, code="corrupt")
    except DerivedFixError as exc:
        _finish_explained(rec, attempt, "source-error", exc, job_id,
                          meta, "nothing recorded")
        return 1
    return 0


_ATTEMPT_TRANSITIONS = {
    None: ("queued",),
    "queued": ("started", "source-running", "source-error", "refused",
               "stale", "cancelled"),
    "started": ("source-running", "source-error", "refused", "stale",
                "cancelled"),
    # A running worker may reach the only valid forward success:
    # source-ready-unverified (a validated proposal, never a verified
    # fix). Without this edge every successful worker 409s after its
    # result commit. Terminal history and negative transitions are
    # unchanged: terminals only repeat themselves, never move.
    "source-running": ("source-ready-unverified", "source-error",
                       "refused", "stale", "cancelled"),
    "source-ready-unverified": ("source-ready-unverified",),
    "source-error": ("source-error",),
    "refused": ("refused",),
    "stale": ("stale",),
    "cancelled": ("cancelled",),
}


def _store_attempt(rec: dict, attempt: dict,
                   record_status: str = None,
                   workspace: dict = None) -> None:
    """Persist one attempt (append-only history, forward transitions).

    History entries are never edited in place except for queued ->
    running -> terminal transitions of the same attempt id; no entry
    is ever deleted, and original parent records are untouched.
    ``workspace``, when given, REPLACES the stored workspace after
    exact validation — the caller-side dict is never silently
    dropped. An identical rewrite is always accepted (idempotent
    persist); any status change must move forward through the map.
    """
    _validate_attempt(attempt)
    if workspace is not None:
        _validate_workspace(workspace, rec["op_id"])
    fresh = _read_record(rec["op_id"])
    attempts = [dict(a) for a in fresh["source_attempts"]]
    for i, existing in enumerate(attempts):
        if existing.get("id") == attempt.get("id"):
            old = existing.get("status")
            new = attempt.get("status")
            # An identical rewrite is always accepted (idempotent
            # persist, e.g. recording the sent prompt pin mid-run);
            # anything else must move forward through the map.
            if new != old \
                    and new not in _ATTEMPT_TRANSITIONS.get(old, ()):
                raise DerivedFixError(
                    "illegal attempt transition",
                    status=409, code="busy")
            attempts[i] = dict(attempt)
            break
    else:
        if attempt.get("status") not in _ATTEMPT_TRANSITIONS[None]:
            raise DerivedFixError("new attempt must start queued",
                                  status=409, code="busy")
        attempts.append(dict(attempt))
        # A new queued attempt starts unowned: the previous terminal
        # attempt keeps its own job metadata in history, but current
        # ownership transitions to no job BEFORE the new attempt is
        # bound (its job link is noted explicitly at startup). This
        # keeps the live-label binding exact across retries and
        # reconciliations instead of rejecting on a stale label.
        fresh = dict(fresh)
        fresh["job"] = None
    fresh = dict(fresh)
    fresh["source_attempts"] = attempts
    fresh["latest_attempt"] = {"id": attempt.get("id"),
                               "status": attempt.get("status")}
    if workspace is not None:
        fresh["workspace"] = dict(workspace)
    if record_status is not None:
        if record_status not in _STATUSES:
            raise DerivedFixError("derived status malformed",
                                  status=502, code="corrupt")
        fresh["status"] = record_status
    else:
        fresh["status"] = "started" if attempt.get("status") in (
            "queued", "started", "source-running") else fresh["status"]
    fresh["updated_at"] = _utcnow()
    _write_record(fresh)


def _finish_attempt(rec: dict, attempt: dict, status: str, error: str,
                    _job_id=None, raw_meta: dict = None) -> None:
    meta = raw_meta or _raw_meta(None)
    attempt = dict(attempt)
    attempt["status"] = status
    attempt["error"] = str(error or "")[:_ERROR_CAP]
    # Preserve the owned-job metadata on the closing attempt: the
    # link that owned this run stays with its history after current
    # ownership moves on. Read fresh (the caller-side record predates
    # the job link); an unreadable record keeps job None rather than
    # inventing history.
    try:
        current_job = _read_record(rec["op_id"]).get("job")
    except DerivedFixError:
        current_job = None
    attempt["job"] = dict(current_job) \
        if isinstance(current_job, dict) else None
    attempt["ended_at"] = _utcnow()
    for key in ("log_bytes", "truncated", "raw_head",
                "raw_head_sha256", "raw_total_chars",
                "raw_total_bytes"):
        attempt[key] = meta[key]
    _store_attempt(rec, attempt, record_status=status)


def start_source_attempt(dfix_id: str) -> dict:
    """Create one queued source attempt (explicit UI action only).

    CALLER MUST HOLD the parent-op exclusion and the global lease.
    Exactly one hypothesis runs per attempt; a second attempt
    requires a new explicit start call — never automatic. Bounded:
    a record holds at most _MAX_SOURCE_ATTEMPTS attempts.
    """
    rec = _read_record(dfix_id)
    if rec.get("status") == "stale":
        raise DerivedFixError("stale record cannot restart; vary the "
                              "hypothesis note to start a fresh record",
                              status=409, code="stale-result")
    if rec.get("status") not in ("started",) + _TERMINAL_BAD:
        raise DerivedFixError("record not startable in its state",
                              status=409, code="busy")
    live = _live_attempts(rec)
    if live:
        raise DerivedFixError("an attempt is already live",
                              status=409, code="busy")
    if len(rec.get("source_attempts") or []) >= _MAX_SOURCE_ATTEMPTS:
        raise DerivedFixError("too many source attempts on this record",
                              status=409, code="too-many-attempts")
    attempt = {"id": "da-" + secrets.token_hex(8),
               "hypothesis_id": rec["hypothesis"]["id"],
               "status": "queued", "prompt_sha256": None,
               "full_input": None,
               "worker_report": None, "verified_paths": None,
               "job": None,
               "error": "", "log_bytes": 0, "truncated": False,
               "raw_head": None, "raw_head_sha256": None,
               "raw_total_chars": 0, "raw_total_bytes": 0,
               "started_at": _utcnow(), "ended_at": ""}
    _store_attempt(rec, attempt)
    return {"attempt_id": attempt["id"]}


def start_derived_run(dfix_id: str) -> dict:
    """Prepare the workspace if needed, then open one source attempt.

    CALLER MUST HOLD the global worker lease and the parent-op
    exclusion. Exactly one attempt runs per explicit call; nothing
    here auto-retries or auto-resumes. A carried workspace is
    re-verified, never trusted: drift refuses and marks the record.
    Workspace preparation is skipped only when the record already
    carries a validated prepared workspace.

    The preflight authority gate runs FIRST: without a fresh
    genuine preflight bound to the SAME live identity, nothing is
    prepared or attempted — no model/job/worktree mutation.
    """
    _require_target_authority()
    rec = _read_record(dfix_id)
    if rec.get("workspace") is None:
        # Explicit retry on a record whose prep never succeeded:
        # prepare a fresh owned workspace now (old evidence, if
        # any, is a preserved refusal without a workspace).
        prepare_repair_workspace(dfix_id)
    else:
        # A clean carried workspace is reused honestly. A dirty one
        # on an explicit retry is a TYPED non-retriable refusal —
        # never an overwrite, reset, or rebind of failed evidence —
        # with the supported fresh-derivation action named.
        is_retry = rec.get("status") in _TERMINAL_BAD
        try:
            # The chain is re-proved BEFORE any carried-workspace
            # Git read: a swapped home/derived/<dfix> ancestor must
            # refuse here, never be followed into prep HEAD, index,
            # or staging checks. Refusal preserves the suspect
            # workspace and all original identities.
            _reprove_work_chain(dfix_id)
            _verify_prep_identity(rec)
        except DerivedFixError as exc:
            if is_retry and exc.code == "workspace-changed":
                raise DerivedFixError(
                    f"{exc.message} Explicit retry cannot reuse this "
                    "dirty workspace: the failed files, logs, and pins "
                    "stay preserved as evidence. Start a fresh "
                    "derivation with a new hypothesis note (the same "
                    "origin is allowed); do not edit, reset, or rebind "
                    "this workspace.",
                    status=409,
                    code="retry-workspace-dirty") from None
            try:
                refuse_derived(dfix_id, str(exc.message))
            except DerivedFixError:
                pass
            raise
    att = start_source_attempt(dfix_id)
    return {"dfix": dfix_id, "attempt_id": att["attempt_id"]}


def mark_derived_startup_failed(dfix_id: str, attempt_id: str,
                                reason: str) -> None:
    """Close a queued derived attempt that never executed (own ns).

    Used when the dashboard job slot (or op lock) is unavailable
    after the record was created: the queued attempt becomes a
    preserved terminal error, never a phantom running record. This
    is intentionally NOT issue_recovery.mark_startup_failed, which
    writes recovery attempts/latest — namespaces must not cross.
    """
    if not isinstance(attempt_id, str) or not attempt_id:
        raise DerivedFixError(f"bad attempt id: {attempt_id!r}",
                              status=400, code="bad-attempt")
    rec = _read_record(dfix_id)
    attempt = next((a for a in rec["source_attempts"]
                    if a.get("id") == attempt_id), None)
    if attempt is None:
        raise DerivedFixError("unknown source attempt",
                              status=404, code="unknown-attempt")
    if attempt.get("status") != "queued":
        raise DerivedFixError("only a queued attempt can fail startup",
                              status=409, code="not-queued")
    _finish_attempt(rec, dict(attempt), "source-error",
                    str(reason or "startup failed")[:_ERROR_CAP], None,
                    _raw_meta(None))


def note_job(dfix_id: str, job: dict) -> None:
    """Record the live job link (actual kind/attempt identity).

    The link must name a real ``derived-fix`` job whose label is the
    record's CURRENT live attempt id: ``_job_new(kind="derived-fix",
    label=attempt_id)`` is the only actual startup path, so a mocked
    constant label is never ownership. A missing live attempt, a
    foreign kind, or a label that does not name that attempt refuses
    loudly — the serve layer must fail startup explicitly, never
    swallow this into a jobless "running" record that can neither
    cancel nor attach.
    """
    if not isinstance(job, dict) or not job.get("id"):
        raise DerivedFixError("bad job link", status=500,
                              code="corrupt")
    if job.get("kind") != _DERIVED_KIND:
        raise DerivedFixError("foreign job kind cannot own a derived "
                              "attempt",
                              status=500, code="corrupt")
    rec = _read_record(dfix_id)
    latest = rec.get("latest_attempt") or {}
    if latest.get("status") not in _LIVE_ATTEMPT or not latest.get("id"):
        raise DerivedFixError("no live attempt to own the job",
                              status=409, code="busy")
    if job.get("label") != latest.get("id"):
        raise DerivedFixError("job label does not name the live "
                              "attempt",
                              status=500, code="corrupt")
    rec = dict(rec)
    rec["job"] = {"id": str(job["id"]), "kind": _DERIVED_KIND,
                  "label": str(job["label"])}
    rec["updated_at"] = _utcnow()
    _write_record(rec)


def snapshot_derived(dfix_id: str, job_live: bool = None) -> dict:
    """Read-only projection for UI/API (never writes, never mutates).

    A ``stale`` record always projects ``stale`` with its
    administrative reason, even while its historical ready attempt
    is retained. Otherwise, an attempt that reads
    ``source-running`` — or a ``queued`` attempt whose job link
    names a dead owner — while its owner job is not live renders as
    ``unknown (owner job not live)``; an explicit ``job_live=False``
    never reads as idle, and an absent link (``job_live=None``)
    never reads as unknown either. The top-level error is the
    stale administrative reason for stale records, else the
    current failed attempt's reason (resolved from history by the
    pointer id/status, never from the pointer itself), else the
    stored record reason. Persisting a terminal costs an explicit
    start/reconciliation under the lease, never this read path. Raw
    stream content is retained in the record but not projected;
    totals and pins are.
    """
    rec = _read_record(dfix_id)
    latest = rec.get("latest_attempt") or {}
    status = latest.get("status") or rec.get("status")
    rendered = status
    # The pointer carries id/status only: the full attempt is the
    # UNIQUE bound entry in history (validator-enforced by id, then
    # status). Never expand the pointer schema, never guess from
    # array position — an unresolvable pointer yields no cause.
    full = None
    if latest:
        bound = [a for a in rec.get("source_attempts") or []
                 if isinstance(a, dict)
                 and a.get("id") == latest.get("id")
                 and a.get("status") == latest.get("status")]
        full = bound[0] if len(bound) == 1 else None
    current_failed = full is not None \
        and full.get("status") in _TERMINAL_BAD
    # Precedence: the current stale administrative verdict wins with
    # its reason; otherwise the CURRENT failed attempt's real cause
    # wins even over an older stored record reason; the stored record
    # reason is only the fallback when no current failed cause
    # exists. Nothing is mirrored or rewritten here.
    if rec.get("status") == "stale":
        rendered = "stale"
        top_error = rec.get("error") or ""
    elif current_failed:
        top_error = full.get("error") or ""
    else:
        top_error = rec.get("error") or ""
    if rendered != "stale" and status in ("source-running", "queued") \
            and job_live is False:
        rendered = "unknown"
    files = []
    for attempt in rec["source_attempts"]:
        # Pin-level input state only (no file I/O here): the viewer
        # route performs the verified read. Absent pin = legacy
        # record; null pin = not yet composed (live) or failed
        # before composition (terminal); present pin = stored,
        # content verified on read.
        if "full_input" not in attempt:
            input_state = "legacy-not-recorded"
        elif attempt.get("full_input") is None:
            if attempt.get("status") in _LIVE_ATTEMPT:
                input_state = "not-yet-dispatched"
            else:
                input_state = "pre-input-failure"
        else:
            input_state = "stored-unverified"
        files.append({"id": attempt.get("id"),
                      "hypothesis_id": attempt.get("hypothesis_id"),
                      "status": attempt.get("status"),
                      "prompt_sha256": (
                          attempt.get("prompt_sha256") or "")[:12] or None,
                      "full_input_state": input_state,
                      "error": attempt.get("error"),
                      "verified_paths": attempt.get("verified_paths"),
                      "log_bytes": attempt.get("log_bytes"),
                      "truncated": bool(attempt.get("truncated")),
                      "raw_total_chars": attempt.get("raw_total_chars"),
                      "raw_total_bytes": attempt.get("raw_total_bytes"),
                      "ended_at": attempt.get("ended_at")})
    ws = rec.get("workspace") or {}
    scope = ws.get("scope") or {}
    return {"dfix": dfix_id, "derived_from": rec.get("derived_from"),
            "status": rendered, "recorded_status": rec.get("status"),
            "hypothesis": rec.get("hypothesis"),
            "origin": {"input": rec["origin"]["input"],
                       "report": rec["origin"]["report"],
                       "fingerprint": rec["origin"]["fingerprint"],
                       "base_commit": rec["origin"].get("base_commit")},
            "workspace": {"clean": ws.get("clean"),
                          "prep_head": ws.get("prep_head"),
                          "result_head": ws.get("result_head"),
                          "applied": ws.get("applied"),
                          "conflict_reset": ws.get("conflict_reset"),
                          "retained_base": ws.get("retained_base"),
                          "scope": {"conflicts": scope.get("conflicts"),
                                    "original_touched": scope.get(
                                        "original_touched"),
                                    "touched": scope.get("touched")}},
            "attempts": files,
            "latest_attempt": latest.get("id"),
            "job": rec.get("job"),
            "error": top_error,
            "updated_at": rec.get("updated_at")}


def describe_derived(op_id: str, job_live_ids=None):
    """Read-only status projection for one parent op (never writes).

    Derived is allowed only for a blocked parent WITH a currently
    valid review origin; the origin names and SHAs served here are
    the exact revision the submit path must echo back. Corrupt or
    unreadable parent/review/derived records raise typed failures —
    never an empty view that hides corruption. ``job_live_ids`` is
    an optional set of live job ids supplied by the caller (the
    serve layer owns the job table); running attempts whose job is
    absent render `unknown`, never mutated here.
    """
    try:
        batches.op_file(op_id)
    except Exception as exc:
        raise DerivedFixError(f"bad parent op id: {op_id!r}",
                              status=400, code="bad-op") from exc
    try:
        parent, _conflicts = _read_parent_op(op_id)
    except DerivedFixError as exc:
        if exc.code == "not-blocked":
            parent = None
        else:
            raise
    _ = parent
    live = set(job_live_ids or [])
    origin = None
    origin_shas = None
    if parent is not None:
        try:
            cur = _current_valid_origin(op_id)
        except DerivedFixError as exc:
            if exc.code == "no-current-origin":
                cur = None
            else:
                raise
        if cur is not None:
            origin = {"input": cur["input"], "report": cur["report"],
                      "fingerprint": cur["fingerprint"],
                      "base_commit": cur["base_commit"],
                      "questions": list(cur["questions"])}
            origin_shas = {"input_sha256": cur["input_sha256"],
                           "report_sha256": cur["report_sha256"]}
    items = []
    for rec in _scan_derived(op_id):
        job = rec.get("job") or {}
        job_live = (job.get("id") in live) if job.get("id") else None
        items.append(snapshot_derived(rec["op_id"], job_live=job_live))
    from . import issue_recovery as _ir
    try:
        owner_decisions = _ir.list_owner_decisions(op_id)
    except _ir.IssueRecoveryError as exc:
        if exc.code in ("bad-op", "unknown-op"):
            owner_decisions = []
        else:
            raise
    return {"op": op_id,
            "derived_allowed": bool(parent is not None
                                    and origin is not None),
            "origin": origin,
            "origin_shas": origin_shas,
            "owner_decisions": owner_decisions,
            "derived": items}


def refuse_derived(dfix_id: str, message: str) -> None:
    """Mark a non-terminal record refused (preparation/worker gates).

    Never rewrites terminal history; records the bounded reason. A
    preparation refusal with no attempt yet is valid (no attempt,
    transport, or job is fabricated). A record whose latest attempt
    is still live is NOT rewritten — refusing it would corrupt the
    status mirror; the caller reconciles the live attempt first and
    propagates the original failure instead.
    """
    rec = _read_record(dfix_id)
    if rec.get("status") in _TERMINAL_OK + _TERMINAL_BAD:
        return
    latest = rec.get("latest_attempt") or {}
    if latest.get("status") in _LIVE_ATTEMPT:
        raise DerivedFixError("cannot refuse a record with a live "
                              "attempt; reconcile it first",
                              status=409, code="busy")
    rec = dict(rec)
    rec["status"] = "refused"
    rec["error"] = str(message or "refused")[:_ERROR_CAP]
    rec["updated_at"] = _utcnow()
    _write_record(rec)
