"""Offline macOS sandbox-exec containment component.

TEST/quiet mechanism only: builds a real sandbox-exec policy for one
validated owned root and one pinned executable, and exposes the exact
wrapper argv the controls run under. Returns wrapper/policy identity
metadata — NEVER a global verified/admission status, and nothing here
authorizes a model worker.

Read isolation is default-deny ((allow default) base, then explicit
deny filters) with a narrow allowlist: the owned child root, the
exact pinned binary, explicit per-policy runtime roots, the OS
directory literal "/" (entry only, never a subtree), system
/Library/Apple + /usr/lib + /usr/share, and the /dev/null|random|
urandom literals. Exact ancestor-directory literals are intended
to permit file-read-METADATA only (realpath resolution; data
intended to stay denied; interaction pending native verification),
while Data is excluded from the generic /System scope alternative
(explicit owned/runtime allowances preserved). No user-home, /tmp-shared,
credential, or auth exception exists. The runtime-root list is
macOS-specific and NOT claimed portable or Meta-ready; Root
acceptance is still pending.

Production preflight (`preflight.native_containment()`,
`stage0_admission`, target authority) keeps refusing full worker
spawns until auth/config AND the actual same-wrapper network and
child-tree channels are genuinely proved. This module does not, and
cannot, clear those gates: its policies deny ALL network (not the
Meta-ready restricted mode), and its provenance is offline-only.
"""
from __future__ import annotations

import hashlib
import os
import pwd
import re
import stat as _stat
import sys

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DENY_ALL_NETWORK = "(deny network*)"
POLICY_NAME = "ncontain-offline.sb"
_POLICY_VERSION = "(version 1)"
_MAX_POLICY_BYTES = 1 << 20
_MAX_BINARY_BYTES = 1 << 30

# Narrow OS read scopes baked into every policy. Deliberately NOT
# an allowlist for interpreters, homes, scratch, or credentials:
# /private, /Library, /Applications, /dev (whole), /opt/homebrew,
# and /Users are all excluded here. Interpreter runtime trees come
# only from the explicit per-policy runtime_read_roots argument.
SYSTEM_READ_SCOPES = (
    "/System",
    "/usr/lib",
    "/usr/share",
    "/Library/Apple/System",
)
DEVICE_LITERALS = (
    "/dev/null",
    "/dev/random",
    "/dev/urandom",
)
# Narrow installed-runtime prefixes. A runtime root is valid only
# when strictly BENEATH one of these (never equal) AND outside the
# policy root and the account home. Whole buckets (/Library,
# /Applications, /opt/homebrew, /), homes, scratch, and fixtures
# are rejected by construction. macOS-only, not portable.
INSTALL_ROOTS = (
    "/opt/homebrew/Cellar",
    "/usr/local/Cellar",
    "/Library/Frameworks/Python.framework",
    "/Library/Developer/CommandLineTools",
    "/Applications/Xcode.app/Contents/Developer",
    "/System/Library/Frameworks",
)

# Contained subscription-worker read exceptions (gate (2), Root
# records root299-root301, experiments muse298-muse300): under
# FULL containment (network denied, owned-only writes, exec pin,
# securityd deny) the pinned worker binds meta / muse-spark-1.3
# with exactly these reads plus the base policy. Each entry is
# (kind, spec, reason): kind is "subpath" or "literal"; a spec
# starting with "~/" joins the account home, absolute specs are
# used verbatim; reason is one line and surfaces in the UI.
SUBSCRIPTION_WORKER_READ_EXCEPTIONS = (
    ("subpath", "~/.config/muse",
     "Muse 설정·로그인 저장소 읽기"),
    ("subpath", "~/Library/Keychains/login.keychain-db",
     "로그인 키체인 파일 읽기"),
    ("subpath", "~/Library/Preferences/com.apple.security.plist",
     "보안 기본설정 읽기"),
    ("subpath", "~/.claude/skills",
     "Claude 스킬 읽기 (세션 시작 요구)"),
    ("subpath", "~/.codex/skills",
     "Codex 스킬 읽기 (세션 시작 요구)"),
    ("literal", "/Library",
     "/Library 항목 읽기 (세션 시작 요구)"),
    ("literal", "/Library/Application Support",
     "/Library/Application Support 목록 읽기 (세션 시작 요구)"),
    ("literal", "~/.claude/CLAUDE.md",
     "Claude 규칙 파일 읽기 (세션 시작 요구)"),
)

# Extra metadata-only literal for the subscription profile: the
# F2 narrowing proved a readdir-only literal on this directory
# suffices (root301), so metadata on the directory itself is
# allowed alongside the read literal. Minimal metadata beyond
# this (ancestor literals of each read path) is confirmed by a
# live check later; do NOT copy the E19 cumulative seed here.
SUBSCRIPTION_WORKER_EXTRA_META_LITERALS = (
    "/Library/Application Support",
)

# D2 (owner decision, gate 2 E28/root425): the ONLY file the
# contained subscription worker may write. The host takes an
# advisory lock on exactly this file during settings/read, so
# the policy allows file-write-data + file-write-create on
# this literal only: no unlink/rename, nothing else in
# ~/.config/muse writable.
SUBSCRIPTION_WORKER_SETTINGS_LOCK = (
    "~/.config/muse/.settings.json.lock")

# E31 narrow network hole (Root steer, gate 2): the ONLY
# network the subscription-canary profile allows. Outbound
# TCP 443 (model transport; the endpoint hostname is unknown
# so no host allowlist can be written honestly) plus the DNS
# resolver socket. Inbound stays denied: (deny network*) is
# kept and these append last-match-wins. No broker: E3-0
# measured 0 broker hits, so the binary ignores the proxy
# env and dials direct.
SUBSCRIPTION_CANARY_NETWORK_LINES = (
    '(allow network-outbound (remote tcp "*:443"))',
    '(allow network-outbound '
    '(literal "/private/var/run/mDNSResponder"))',
)

# Canary read lines, DNS-only (gate 2, slice 295b).
# E35a5 leave-one-out (result 9076182f): removing ONLY
# this line broke DNS (curl exit 6); removing any of the
# other eight V1 lines (/private, /private/var,
# /private/var/run, mDNSResponder read, resolv.conf,
# hosts, services) still resolved. Those eight stay out.
# Slice 295 also carried a TLS-roots group (a trust-roots
# file plus its path metadata, evidence E35a4 V2 for
# curl); E35 ran with it. E36 tests the worker without
# it. Appended after the network lines, canary profile
# only; the plain subscription text never carries them.
SUBSCRIPTION_CANARY_READ_LINES = (
    '(allow file-read-metadata (literal "/var"))',
)


class ContainmentError(Exception):
    pass


class UnsupportedOS(ContainmentError):
    pass


def _require_darwin() -> None:
    if sys.platform != "darwin" or not os.path.exists(
            "/System/Library/CoreServices/Finder.app"):
        raise UnsupportedOS(
            "offline containment needs macOS sandbox-exec")
    try:
        st = os.lstat(SANDBOX_EXEC)
    except OSError as exc:
        raise ContainmentError(
            "sandbox-exec unavailable: %s" % exc)
    if not _stat.S_ISREG(st.st_mode):
        raise ContainmentError("sandbox-exec is not a file")


def _sb_literal(value: str) -> str:
    if "\x00" in value:
        raise ContainmentError("NUL byte in sandbox literal")
    return '"%s"' % value.replace("\\", "\\\\").replace(
        '"', '\\"')


def _read_identity(path: str, max_bytes: int):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                 | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not _stat.S_ISREG(before.st_mode):
            raise ContainmentError(
                "not a regular file: %s" % path)
        if before.st_size < 0 or before.st_size > max_bytes:
            raise ContainmentError(
                "size out of bounds: %s" % path)
        digest = hashlib.sha256()
        total = 0
        limit = before.st_size + 1
        while total < limit:
            part = os.read(fd, min(65536, limit - total))
            if not part:
                break
            digest.update(part)
            total += len(part)
        if total != before.st_size:
            raise ContainmentError(
                "changed during read: %s" % path)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_dev, before.st_ino, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns):
            raise ContainmentError(
                "changed during read: %s" % path)
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (
                before.st_dev, before.st_ino):
            raise ContainmentError(
                "replaced during read: %s" % path)
        return digest.hexdigest(), before
    finally:
        os.close(fd)


def pin_executable(path: str, sha256_hex: str,
                   max_bytes: int = _MAX_BINARY_BYTES) -> dict:
    """Pin one direct regular executable by full identity.

    Rejects symlinks (anywhere in the path), non-regular files,
    files without an owner-execute bit, size drift, byte drift,
    and SHA mismatch. Returns the recorded identity; re-check
    before every spawn.
    """
    if not path or not os.path.isabs(path):
        raise ContainmentError("executable must be absolute")
    if "\x00" in path:
        raise ContainmentError("NUL byte in executable path")
    if os.path.realpath(path) != path:
        raise ContainmentError(
            "executable path is not canonical (symlink?): %s"
            % path)
    digest, st = _read_identity(path, max_bytes)
    if not st.st_mode & 0o100:
        raise ContainmentError(
            "executable bit missing: %s" % path)
    if digest != sha256_hex.lower():
        raise ContainmentError(
            "executable SHA mismatch: %s" % path)
    return {"path": path, "real_path": path,
            "sha256": digest, "size": st.st_size,
            "dev": st.st_dev, "ino": st.st_ino,
            "mode": oct(_stat.S_IMODE(st.st_mode)),
            "offline_only": True, "admits_worker": False}


def _check_runtime_roots(roots, root: str) -> list:
    try:
        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError as exc:
        raise ContainmentError("no account home: %s" % exc)
    checked = []
    for candidate in roots or []:
        if not candidate or not os.path.isabs(candidate):
            raise ContainmentError(
                "runtime root must be absolute")
        if "\x00" in candidate:
            raise ContainmentError("NUL byte in runtime root")
        real = os.path.realpath(candidate)
        if real != candidate:
            raise ContainmentError(
                "runtime root not canonical: %r" % candidate)
        try:
            st = os.lstat(real)
        except OSError as exc:
            raise ContainmentError(
                "runtime root unavailable: %r: %s"
                % (candidate, exc))
        if not _stat.S_ISDIR(st.st_mode):
            raise ContainmentError(
                "runtime root not a directory: %r" % candidate)
        for forbidden, label in ((root, "policy root"),
                                 (account_home, "account home")):
            if (real == forbidden
                    or real.startswith(forbidden + os.sep)
                    or forbidden.startswith(real + os.sep)):
                raise ContainmentError(
                    "runtime root must not be %s, its ancestor, "
                    "or its descendant: %r" % (label, candidate))
        if not any(real.startswith(prefix + os.sep)
                   for prefix in INSTALL_ROOTS):
            raise ContainmentError(
                "runtime root must be strictly beneath a known "
                "installed-runtime prefix: %r" % candidate)
        if not os.path.isdir(real):
            raise ContainmentError(
                "runtime root is not an existing dir: %r"
                % candidate)
        checked.append(real)
    return checked


def _ancestor_literals(paths):
    """Strict directory ancestors (up to and including "/") of
    validated canonical paths, for metadata-only reads needed by
    realpath resolution. Exact literals, never subtrees: no parent
    data, no recursive content, no substitute for the validated
    roots themselves.
    """
    seen = []
    for raw in paths:
        cur = raw
        while True:
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            if parent not in seen:
                seen.append(parent)
            if parent == "/":
                break
            cur = parent
    return seen


def build_policy_text(root: str, exe_path: str,
                      runtime_read_roots=()) -> str:
    """Sandbox profile: explicit (allow default) base, then
    deny filters — deny-all-network, writes confined to the
    owned root (+/dev/null), reads default-deny except the owned
    root, the exact pinned binary, the explicit runtime roots,
    the OS directory literal "/", the narrow system scopes, and
    the device literals. Only the pinned binary may exec (no
    shell, no script interpreter, no sandbox-exec re-entry).
    Descendants inherit the profile. No credential exceptions,
    no credential reads.

    Two refinements, both PENDING native verification (the echo
    "/" result proves only the directory-entry startup cause):
    (a) the trailing file-read-metadata allow is INTENDED to
    lift metadata (only) for exact ancestor-directory literals
    of the validated root, binary, and runtimes, with data
    reads on those ancestors intended to stay denied; actual
    SBPL rule interaction must come from kernel controls, not
    from assumed text order.
    (b) Data is excluded ONLY from the generic /System scope
    alternative, closing the Data-volume alias as a broad
    read-only scope; separately validated owned-root and runtime
    allowances are preserved regardless of alias identity.
    """
    if not root or not os.path.isabs(root):
        raise ContainmentError("root must be absolute")
    for label, value in (("root", root),
                         ("executable", exe_path)):
        if "\x00" in value:
            raise ContainmentError("NUL byte in %s" % label)
    runtimes = _check_runtime_roots(runtime_read_roots, root)

    def _scope_filter(scope):
        if scope == "/System":
            return ("(require-all (subpath %s) "
                    "(require-not (subpath %s)))"
                    % (_sb_literal(scope),
                       _sb_literal("/System/Volumes/Data")))
        return "(subpath %s)" % _sb_literal(scope)

    allow = " ".join(
        ["(subpath %s)" % _sb_literal(root),
         "(literal %s)" % _sb_literal(exe_path),
         "(literal %s)" % _sb_literal("/")] +
        [_scope_filter(scope)
         for scope in list(runtimes) + list(SYSTEM_READ_SCOPES)]
        + ["(literal %s)" % _sb_literal(dev)
           for dev in DEVICE_LITERALS])
    ancestors = _ancestor_literals([root, exe_path]
                                   + list(runtimes))
    meta_allow = " ".join("(literal %s)" % _sb_literal(a)
                          for a in ancestors)
    lines = [
        _POLICY_VERSION,
        "(allow default)",
        DENY_ALL_NETWORK,
        "(deny mach-lookup (global-name "
        '"com.apple.securityd"))',
        "(deny file-write* (require-all (require-not (subpath %s)) "
        '(require-not (literal "/dev/null"))))'
        % _sb_literal(root),
        "(deny file-read* (require-not (require-any %s)))"
        % allow,
        "(deny process-exec (require-not (literal %s)))"
        % _sb_literal(exe_path),
        "(allow file-read-metadata %s)" % meta_allow,
    ]
    return "\n".join(lines) + "\n"


def _resolve_worker_home(home) -> str:
    """Account home for the subscription profile (fail-closed).

    None resolves from pwd; anything else must be a canonical
    absolute path. Never touches the filesystem beyond the pwd
    lookup.
    """
    if home is None:
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError as exc:
            raise ContainmentError("no account home: %s" % exc)
    if not isinstance(home, str) or not home:
        raise ContainmentError("worker home must be a path")
    if "\x00" in home:
        raise ContainmentError("NUL byte in worker home")
    if not os.path.isabs(home):
        raise ContainmentError("worker home must be absolute")
    if os.path.normpath(home) != home:
        raise ContainmentError("worker home must be canonical")
    return home


def subscription_worker_exceptions(home=None) -> list:
    """Named read exceptions for the UI, expanded for a home.

    Pure path expansion (no reads): returns one
    {"path", "kind", "reason"} dict per entry of
    SUBSCRIPTION_WORKER_READ_EXCEPTIONS. Refuses an
    unresolvable or non-absolute home.
    """
    home = _resolve_worker_home(home)
    out = []
    for kind, spec, reason in SUBSCRIPTION_WORKER_READ_EXCEPTIONS:
        if kind not in ("subpath", "literal"):
            raise ContainmentError(
                "bad exception kind: %r" % (kind,))
        if spec.startswith("~/"):
            path = os.path.normpath(
                os.path.join(home, spec[2:]))
        elif os.path.isabs(spec):
            path = os.path.normpath(spec)
        else:
            raise ContainmentError(
                "bad exception spec: %r" % (spec,))
        out.append({"path": path, "kind": kind,
                    "reason": reason})
    return out


def subscription_canary_exceptions(home=None) -> list:
    """Named exceptions for the canary UI, expanded for a home.

    The subscription list plus one {"path", "kind",
    "reason"} dict per SUBSCRIPTION_CANARY_NETWORK_LINES
    entry (kind "network") and per
    SUBSCRIPTION_CANARY_READ_LINES entry (kind "read"),
    path verbatim: no home expansion — the lines carry no
    home path.
    """
    out = subscription_worker_exceptions(home)
    reasons = {
        SUBSCRIPTION_CANARY_NETWORK_LINES[0]:
            "model transport: outbound TCP 443 only "
            "(no host allowlist is writable honestly)",
        SUBSCRIPTION_CANARY_NETWORK_LINES[1]:
            "DNS resolver socket for the 443 transport",
    }
    for line in SUBSCRIPTION_CANARY_NETWORK_LINES:
        out.append({"path": line, "kind": "network",
                    "reason": reasons[line]})
    read_reasons = {
        SUBSCRIPTION_CANARY_READ_LINES[0]:
            "DNS resolver path (E35a5: only this V1 line "
            "was needed for DNS)",
    }
    for line in SUBSCRIPTION_CANARY_READ_LINES:
        out.append({"path": line, "kind": "read",
                    "reason": read_reasons[line]})
    return out


def _prove_subscription_plus(text: str, base: str,
                             read_clauses: list,
                             meta_entries: list,
                             extra_lines=()) -> None:
    """Prove text is the base policy plus exactly the given
    read clauses, metadata entries, and whole extra lines —
    removal style. Read removal operates on the read-deny
    line only, metadata removal on the metadata line only,
    and each extra line (the D2 write rule) must appear
    exactly once outside those lines; every other line
    (network deny, write confinement, exec pin, securityd
    deny) must be identical.
    """
    def one(lines, prefix, what):
        found = [ln for ln in lines
                 if ln.startswith(prefix)]
        if len(found) != 1:
            raise ContainmentError(
                "subscription proof: %s" % what)
        return found[0]

    tlines = text.splitlines()
    blines = base.splitlines()
    tread = one(tlines, "(deny file-read*", "read not unique")
    oread = one(blines, "(deny file-read*",
                "base read drift")
    rprobe = tread
    for clause in read_clauses:
        if rprobe.count(clause) != 1:
            raise ContainmentError(
                "subscription proof: clause not unique")
        rprobe = rprobe.replace(clause, "", 1)
    if rprobe != oread:
        raise ContainmentError(
            "subscription proof: read not base-plus")
    tmeta = one(tlines, "(allow file-read-metadata",
                "metadata not unique")
    ometa = one(blines, "(allow file-read-metadata",
                "base metadata drift")
    madd = "".join(meta_entries)
    if not madd:
        if tmeta != ometa:
            raise ContainmentError(
                "subscription proof: metadata not base-plus")
    else:
        if tmeta.count(madd) != 1:
            raise ContainmentError(
                "subscription proof: metadata not base-plus")
        if tmeta.replace(madd, "", 1) != ometa:
            raise ContainmentError(
                "subscription proof: metadata not base-plus")
    rest_t = [ln for ln in tlines
              if ln != tread and ln != tmeta]
    rest_b = [ln for ln in blines
              if ln != oread and ln != ometa]
    for extra in extra_lines:
        if rest_t.count(extra) != 1:
            raise ContainmentError(
                "subscription proof: extra not unique")
        rest_t.remove(extra)
    if rest_t != rest_b:
        raise ContainmentError(
            "subscription proof: extra diff")


def build_subscription_worker_policy_text(root: str,
                                          exe_path: str,
                                          home=None) -> str:
    """Contained subscription-worker profile: product A text
    plus exactly the F2 named read exception (gate (2),
    root301) and the one D2 settings-lock write rule (gate
    (2), root425).

    Reads: the SUBSCRIPTION_WORKER_READ_EXCEPTIONS clauses.
    Metadata: the ancestor literals of each read path plus
    SUBSCRIPTION_WORKER_EXTRA_META_LITERALS, restricted to
    entries not already present in the base text. Write: one
    appended allow line, file-write-data + file-write-create
    on the SUBSCRIPTION_WORKER_SETTINGS_LOCK literal only
    (advisory lock for settings/read; no unlink/rename,
    nothing else writable). Network deny, the write-deny
    line itself, the exec pin, and the securityd deny stay
    byte-identical to the base policy. The result is proven
    base-plus by removal; anything else raises
    ContainmentError. Nothing here authorizes a model worker.
    """
    if not root or not os.path.isabs(root):
        raise ContainmentError("root must be absolute")
    for label, value in (("root", root),
                         ("executable", exe_path)):
        if "\x00" in value:
            raise ContainmentError("NUL byte in %s" % label)
    exceptions = subscription_worker_exceptions(home)
    real_root = os.path.realpath(root)
    for entry in exceptions:
        real_entry = os.path.realpath(entry["path"])
        if real_entry == real_root or real_entry.startswith(
                real_root + os.sep):
            raise ContainmentError(
                "worker exception must not resolve under the "
                "owned root: %s" % entry["path"])
    base = build_policy_text(root, exe_path)
    read_clauses = []
    for entry in exceptions:
        if entry["kind"] == "subpath":
            read_clauses.append(
                "(subpath %s) " % _sb_literal(entry["path"]))
        else:
            read_clauses.append(
                "(literal %s) " % _sb_literal(entry["path"]))
    anchor = "(deny file-read* (require-not (require-any "
    if base.count(anchor) != 1:
        raise ContainmentError("subscription proof: no anchor")
    readlines = [ln for ln in base.splitlines()
                 if ln.startswith("(deny file-read*")]
    if len(readlines) != 1:
        raise ContainmentError(
            "subscription proof: base read not unique")
    readline = readlines[0]
    for clause in read_clauses:
        if clause.strip() in readline:
            raise ContainmentError(
                "subscription proof: clause present")
    text = base.replace(anchor, anchor + "".join(read_clauses),
                        1)
    wanted_meta = _ancestor_literals(
        [entry["path"] for entry in exceptions])
    for extra in SUBSCRIPTION_WORKER_EXTRA_META_LITERALS:
        if extra not in wanted_meta:
            wanted_meta.append(extra)
    metalines = [ln for ln in text.splitlines()
                 if ln.startswith("(allow file-read-metadata")]
    if len(metalines) != 1:
        raise ContainmentError(
            "subscription proof: base metadata not unique")
    metaline = metalines[0]
    if not metaline.endswith(")"):
        raise ContainmentError(
            "subscription proof: base metadata shape")
    meta_entries = []
    for path in wanted_meta:
        entry = " (literal %s)" % _sb_literal(path)
        if entry not in metaline:
            meta_entries.append(entry)
    text = text.replace(metaline,
                        metaline[:-1] + "".join(meta_entries)
                        + ")", 1)
    account_home = _resolve_worker_home(home)
    lock_path = os.path.normpath(os.path.join(
        account_home, SUBSCRIPTION_WORKER_SETTINGS_LOCK[2:]))
    real_lock = os.path.realpath(lock_path)
    if real_lock == real_root or real_lock.startswith(
            real_root + os.sep):
        raise ContainmentError(
            "worker exception must not resolve under the "
            "owned root: %s" % lock_path)
    write_rule = ("(allow file-write-data file-write-create "
                  "(literal %s))" % _sb_literal(lock_path))
    if write_rule in text:
        raise ContainmentError(
            "subscription proof: write rule present")
    if not text.endswith("\n"):
        raise ContainmentError(
            "subscription proof: base text shape")
    text = text + write_rule + "\n"
    _prove_subscription_plus(text, base, read_clauses,
                             meta_entries, [write_rule])
    return text


def _prove_canary_plus(text: str, sub: str) -> None:
    """Prove text is the subscription text plus exactly the
    canary network lines plus exactly the canary read
    lines — removal style. Each line must be absent from
    the subscription text and present exactly once in the
    canary text, network lines first, then read lines;
    everything else must be identical.
    """
    expected = list(SUBSCRIPTION_CANARY_NETWORK_LINES) \
        + list(SUBSCRIPTION_CANARY_READ_LINES)
    for line in expected:
        if line in sub:
            raise ContainmentError(
                "canary proof: canary line already present")
    tlines = text.splitlines()
    slines = sub.splitlines()
    if tlines != slines + expected:
        raise ContainmentError("canary proof: extra diff")
    for line in expected:
        if tlines.count(line) != 1:
            raise ContainmentError(
                "canary proof: canary line not unique")


def build_subscription_canary_policy_text(root: str,
                                          exe_path: str,
                                          home=None) -> str:
    """Canary profile: subscription text plus exactly the two
    E31 network lines plus exactly the 295 read lines for
    DNS + TLS roots (gate (2)).

    Appended at the END (SBPL last-match-wins) with
    (deny network*) kept: outbound TCP 443 plus the DNS
    resolver socket, then the resolver-path and TLS-root
    reads the E35a4/E35a5 curl probes measured; inbound
    stays denied. File-write, file-read, exec pin, and
    securityd deny are identical to the subscription text
    — proven by removal; anything else raises
    ContainmentError. Nothing here authorizes a model
    worker: the single canary turn is gated in muse.py, and
    the "subscription" profile stays network-denied.
    """
    sub = build_subscription_worker_policy_text(
        root, exe_path, home)
    if not sub.endswith("\n"):
        raise ContainmentError("canary proof: base text shape")
    text = sub + "\n".join(
        list(SUBSCRIPTION_CANARY_NETWORK_LINES)
        + list(SUBSCRIPTION_CANARY_READ_LINES)) + "\n"
    _prove_canary_plus(text, sub)
    return text


def _prove_broker_plus(text: str, sub: str, hole: str) -> None:
    """Prove text is the subscription text plus exactly the
    one broker egress line — removal style. The line must
    be absent from the subscription text and present
    exactly once, appended at the end; "(deny network*)"
    must be kept; "*:443" must be absent; everything else
    must be identical.
    """
    if hole in sub:
        raise ContainmentError(
            "broker proof: hole already present")
    tlines = text.splitlines()
    slines = sub.splitlines()
    if tlines != slines + [hole]:
        raise ContainmentError("broker proof: extra diff")
    if tlines.count(hole) != 1:
        raise ContainmentError(
            "broker proof: hole not unique")
    if "(deny network*)" not in sub:
        raise ContainmentError(
            "broker proof: network deny lost")
    if "*:443" in text:
        raise ContainmentError(
            "broker proof: 443 line forbidden")


def _check_repair_workdir(workdir, home_real: str) -> str:
    """Canonical validated repair worktree path, or ContainmentError.

    The derived worktree for one operation only: it must be an
    existing directory, canonical (realpath equal to the passed
    path, so no symlink in any component can redirect it
    outside what the caller passed), never the filesystem
    root or the account home, and must neither contain nor
    sit inside the account's ~/.muse, ~/.config/muse or
    ~/.local/share/muse. Returns the validated path.
    """
    if not isinstance(workdir, str) or not workdir:
        raise ContainmentError("repair workdir must be a path")
    if "\x00" in workdir:
        raise ContainmentError("NUL byte in repair workdir")
    if not os.path.isabs(workdir):
        raise ContainmentError(
            "repair workdir must be absolute")
    try:
        real = os.path.realpath(workdir)
    except OSError as exc:
        raise ContainmentError(
            "repair workdir unresolvable: %s" % exc)
    if real != workdir:
        raise ContainmentError(
            "repair workdir resolves outside the passed "
            "path (symlink?): %s" % workdir)
    try:
        st = os.lstat(real)
    except OSError as exc:
        raise ContainmentError(
            "repair workdir unavailable: %s" % exc)
    if not _stat.S_ISDIR(st.st_mode):
        raise ContainmentError(
            "repair workdir is not a directory")
    if real == "/":
        raise ContainmentError(
            "repair workdir must not be the filesystem root")
    if real == home_real:
        raise ContainmentError(
            "repair workdir must not be the account home")
    for leaf in (".muse", ".config/muse",
                 ".local/share/muse"):
        guarded = os.path.join(home_real, leaf)
        if real == guarded or real.startswith(
                guarded + os.sep):
            raise ContainmentError(
                "repair workdir must not sit inside %s"
                % guarded)
        if guarded.startswith(real + os.sep):
            raise ContainmentError(
                "repair workdir must not contain %s"
                % guarded)
    return real


def _check_broker_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ContainmentError(
            "broker port must be an integer 1024-65535")
    if port < 1024 or port > 65535:
        raise ContainmentError(
            "broker port must be an integer 1024-65535")
    return port


def build_subscription_broker_policy_text(root: str,
                                          exe_path: str,
                                          home,
                                          port: int) -> str:
    """Broker profile: subscription text plus exactly one
    loopback egress line for the given broker port (gate
    (2), slice 296).

    E38 showed the host accepts
    -c endpoint_transport.proxy="http://127.0.0.1:PORT"
    (trace transport=proxy) and reaches the proxy with DNS
    denied — so this profile carries NO *:443 line and NO
    DNS lines: the only way out is the loopback broker,
    which enforces the host allowlist itself. Appended at
    the END (SBPL last-match-wins) with (deny network*)
    kept. Proven by removal; anything else raises
    ContainmentError. The "subscription" and
    "subscription-canary" texts are untouched by this
    builder.
    """
    port = _check_broker_port(port)
    sub = build_subscription_worker_policy_text(
        root, exe_path, home)
    if not sub.endswith("\n"):
        raise ContainmentError("broker proof: base text shape")
    hole = ('(allow network-outbound (remote tcp '
            '"localhost:%d"))' % port)
    text = sub + hole + "\n"
    _prove_broker_plus(text, sub, hole)
    return text


# Reviewed read-exception set (gate (2)): the repair policy must
# contain exactly these named read exceptions and no others. This
# literal pins the reviewed set INDEPENDENTLY of
# SUBSCRIPTION_WORKER_READ_EXCEPTIONS: editing that tuple without
# updating this literal fails the build. Text-only proof, not
# live enforcement: it shows the built text carries the reviewed
# set, not that the running child is confined (see preflight).
_REVIEWED_READ_EXCEPTION_PATHS = (
    ("subpath", "~/.config/muse"),
    ("subpath", "~/Library/Keychains/login.keychain-db"),
    ("subpath", "~/Library/Preferences/com.apple.security.plist"),
    ("subpath", "~/.claude/skills"),
    ("subpath", "~/.codex/skills"),
    ("literal", "/Library"),
    ("literal", "/Library/Application Support"),
    ("literal", "~/.claude/CLAUDE.md"),
)


def _expand_reviewed_exception(home_real, kind, spec):
    """Expand one reviewed spec with the same rules the builder
    uses for subscription_worker_exceptions: a "~/" spec is
    joined to the home whatever the kind (the builder does not
    check kind before joining); an absolute spec is
    normalized; anything else refuses."""
    if spec.startswith("~/"):
        return (kind, os.path.normpath(
            os.path.join(home_real, spec[2:])))
    if os.path.isabs(spec):
        return (kind, os.path.normpath(spec))
    raise ContainmentError(
        "bad reviewed exception spec: %r" % (spec,))


def _prove_repair_read_exceptions(exceptions, home_real,
                                 text) -> None:
    """Prove the built text carries exactly the reviewed set.

    `exceptions` is the {"path", "kind", "reason"} dict list the
    builder derived from SUBSCRIPTION_WORKER_READ_EXCEPTIONS; its
    (kind, path) pairs must match the reviewed literal exactly
    (same pairs, no more, no fewer). A dropped exception
    (silent narrowing) or an added one (silent widening) raises
    ContainmentError. Each reviewed pair must then appear as its
    exact allow clause exactly once in the single read-deny
    line of the returned text. Text-only proof, not live
    enforcement: it shows the built text carries the reviewed
    set, not that the running child is confined (the 1878
    refusal and preflight stay unchanged).
    """
    reviewed = set(
        _expand_reviewed_exception(home_real, kind, spec)
        for kind, spec in _REVIEWED_READ_EXCEPTION_PATHS)
    if len(_REVIEWED_READ_EXCEPTION_PATHS) != len(reviewed):
        raise ContainmentError(
            "repair proof: reviewed exception set is ambiguous")
    got = set((entry["kind"], entry["path"])
              for entry in exceptions)
    if got != reviewed:
        raise ContainmentError(
            "repair proof: read exceptions differ from the "
            "reviewed set")
    readlines = [ln for ln in text.splitlines()
                 if ln.startswith("(deny file-read*")]
    if len(readlines) != 1:
        raise ContainmentError(
            "repair proof: base read line not unique")
    readline = readlines[0]
    for kind, path in sorted(reviewed):
        clause = "(%s %s)" % (kind, _sb_literal(path))
        found = readline.count(clause)
        if found == 0:
            raise ContainmentError(
                "repair proof: reviewed exception missing from "
                "policy text: %s %s" % (kind, path))
        if found != 1:
            raise ContainmentError(
                "repair proof: reviewed exception duplicated in "
                "policy text: %s %s" % (kind, path))


def _prove_repair_plus(text: str, sub: str,
                       extra_lines) -> None:
    """Prove text is the broker text plus exactly the repair
    lines — removal style. Each line must be absent from the
    broker text and present exactly once, appended at the
    end in order; "(deny network*)" must be kept; "*:443"
    must be absent; everything else must be identical.
    """
    extra_lines = list(extra_lines)
    if len(extra_lines) not in (2, 3):
        raise ContainmentError(
            "repair proof: two or three lines, got %d"
            % len(extra_lines))
    if not extra_lines[-1].startswith("(deny file-write*"):
        raise ContainmentError(
            "repair proof: .git deny must be last")
    for line in extra_lines:
        if line in sub:
            raise ContainmentError(
                "repair proof: line already present")
    tlines = text.splitlines()
    slines = sub.splitlines()
    if tlines != slines + extra_lines:
        raise ContainmentError("repair proof: extra diff")
    for line in extra_lines:
        if tlines.count(line) != 1:
            raise ContainmentError(
                "repair proof: line not unique")
    if "(deny network*)" not in sub:
        raise ContainmentError(
            "repair proof: network deny lost")
    if "*:443" in text:
        raise ContainmentError(
            "repair proof: 443 line forbidden")


def _repair_git_deny_line(work: str) -> str:
    """Deny writes to any .git path under the workdir (299b).

    One regex rule, built RAW: the #"..." literal is read
    raw by the kernel, so _sb_literal doubling would ask
    for a literal backslash and deny nothing. Covers the
    top pointer/dir itself, anything under it, and nested
    sub/.git paths, in any case ([gG][iI][tT]: on
    case-insensitive volumes .GIT aliases .git). Plain
    names like .gitignore or sub.git never match. The
    worker has no reason to write any .git path; the
    kernel test and the E45 controls prove the deny wins.
    """
    for char in work:
        if char == '"' or char == "\\" or ord(char) < 0x20 \
                or char == "\x7f":
            raise ContainmentError(
                "repair workdir not safe for a raw regex "
                "literal")
    pattern = "^%s/(.*/)?\\.[gG][iI][tT](/.*)?$" % re.escape(
        work)
    return '(deny file-write* (regex #"%s"))' % pattern


def _repair_extra_lines(work: str, root: str) -> list:
    """The exact lines the repair builder appends (order fixed).

    Write rule, read rule when the workdir sits outside the
    owned root, then the .git write deny LAST so it wins over
    the workdir allow.
    """
    write_line = ('(allow file-write* (subpath %s))'
                  % _sb_literal(work))
    real_root = os.path.realpath(root)
    if work == real_root or work.startswith(
            real_root + os.sep):
        extra = [write_line]
    else:
        extra = [write_line,
                 '(allow file-read* (subpath %s))'
                 % _sb_literal(work)]
    extra.append(_repair_git_deny_line(work))
    return extra


def build_repair_broker_policy_text(root: str,
                                    exe_path: str,
                                    home,
                                    port: int,
                                    workdir) -> str:
    """Repair profile: broker text plus exactly the write rule
    for one derived worktree (gate (2), slice 298).

    The write rule is always added; the read rule only when
    the base text does not already allow the workdir (i.e.
    it sits outside the owned root). Both are appended at
    the END (SBPL last-match-wins) with (deny network*)
    kept, followed by the .git write deny (299b), which wins
    over the workdir allow. No other path becomes writable.
    Proven by removal; anything else raises ContainmentError.
    The "subscription", "subscription-canary" and
    "subscription-broker" texts are untouched by this builder.
    """
    sub = build_subscription_broker_policy_text(
        root, exe_path, home, port)
    home_real = os.path.realpath(
        _resolve_worker_home(home))
    work = _check_repair_workdir(workdir, home_real)
    if not sub.endswith("\n"):
        raise ContainmentError(
            "repair proof: base text shape")
    extra = _repair_extra_lines(work, root)
    text = sub + "".join(
        ln + "\n" for ln in extra)
    _prove_repair_plus(text, sub, extra)
    _prove_repair_read_exceptions(
        subscription_worker_exceptions(home_real), home_real,
        text)
    return text


def _check_owned_dir(root: str) -> os.stat_result:
    if "\x00" in root:
        raise ContainmentError("NUL byte in root")
    try:
        live = os.lstat(root)
    except OSError as exc:
        raise ContainmentError(
            "root unavailable: %s" % exc)
    if not _stat.S_ISDIR(live.st_mode):
        raise ContainmentError("root is not a directory")
    if live.st_uid != os.getuid():
        raise ContainmentError("root not owned by current uid")
    if os.path.realpath(root) != root:
        raise ContainmentError(
            "root is not canonical (symlink?): %s" % root)
    return live


def _repinned(exe_pin: dict) -> dict:
    """Re-pin the executable from scratch (canonical parents,
    regular nofollow identity, owner-execute bit, full SHA) and
    refuse any drift from the recorded identity. Returns the
    refreshed identity (never silently rebinds).
    """
    path = (exe_pin or {}).get("path")
    if not path:
        raise ContainmentError("valid executable pin required")
    fresh = pin_executable(path, (exe_pin or {}).get("sha256")
                           or "")
    for key in ("sha256", "size", "dev", "ino", "mode"):
        if fresh.get(key) != exe_pin.get(key):
            raise ContainmentError(
                "executable drifted (%s): %s" % (key, path))
    return fresh


def create_offline_policy(root: str, exe_pin: dict,
                          runtime_read_roots=()) -> dict:
    """Write the offline policy into the owned root. The pinned
    executable is re-read and drift-checked first; the recorded
    root/runtime values are validated, never trusted blindly.
    Returns wrapper/policy identity metadata (offline-only).
    """
    _require_darwin()
    if not isinstance(exe_pin, dict):
        raise ContainmentError("valid executable pin required")
    if exe_pin.get("admits_worker"):
        raise ContainmentError(
            "a worker-admitting pin is never valid here")
    fresh_pin = _repinned(exe_pin)
    st = _check_owned_dir(root)
    text = build_policy_text(root, fresh_pin["path"],
                             runtime_read_roots)
    raw = text.encode("utf-8")
    if len(raw) > _MAX_POLICY_BYTES:
        raise ContainmentError("policy too large")
    policy_path = os.path.join(root, POLICY_NAME)
    if os.path.lexists(policy_path):
        raise ContainmentError(
            "policy already exists; refusing to overwrite")
    fd = os.open(policy_path,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise ContainmentError("short write of policy")
    finally:
        os.close(fd)
    digest, pst = _read_identity(policy_path,
                                _MAX_POLICY_BYTES)
    if digest != hashlib.sha256(raw).hexdigest():
        raise ContainmentError("policy changed after write")
    return {"policy_path": policy_path,
            "policy_sha256": digest,
            "policy_size": pst.st_size,
            "policy_dev": pst.st_dev,
            "policy_ino": pst.st_ino,
            "root": root, "root_dev": st.st_dev,
            "root_ino": st.st_ino,
            "executable": fresh_pin,
            "sandbox_exec": SANDBOX_EXEC,
            "network": "deny-all (offline-only, not Meta-ready)",
            "offline_only": True, "admits_worker": False}


def create_subscription_worker_policy(root: str, exe_pin: dict,
                                      home=None) -> dict:
    """Write the subscription-worker policy into the owned root.

    Same write discipline as create_offline_policy (re-pinned
    executable, validated owned root, exclusive create, post-write
    identity recheck), but the text is
    build_subscription_worker_policy_text: product A plus exactly
    the F2 named read exception. A worker-admitting pin is never
    valid here, and the returned identity still carries
    admits_worker False: nothing here authorizes a model worker.
    """
    _require_darwin()
    if not isinstance(exe_pin, dict):
        raise ContainmentError("valid executable pin required")
    if exe_pin.get("admits_worker"):
        raise ContainmentError(
            "a worker-admitting pin is never valid here")
    fresh_pin = _repinned(exe_pin)
    st = _check_owned_dir(root)
    text = build_subscription_worker_policy_text(
        root, fresh_pin["path"], home)
    raw = text.encode("utf-8")
    if len(raw) > _MAX_POLICY_BYTES:
        raise ContainmentError("policy too large")
    policy_path = os.path.join(root, POLICY_NAME)
    if os.path.lexists(policy_path):
        raise ContainmentError(
            "policy already exists; refusing to overwrite")
    fd = os.open(policy_path,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise ContainmentError("short write of policy")
    finally:
        os.close(fd)
    digest, pst = _read_identity(policy_path,
                                _MAX_POLICY_BYTES)
    if digest != hashlib.sha256(raw).hexdigest():
        raise ContainmentError("policy changed after write")
    return {"policy_path": policy_path,
            "policy_sha256": digest,
            "policy_size": pst.st_size,
            "policy_dev": pst.st_dev,
            "policy_ino": pst.st_ino,
            "root": root, "root_dev": st.st_dev,
            "root_ino": st.st_ino,
            "executable": fresh_pin,
            "sandbox_exec": SANDBOX_EXEC,
            "network": "deny-all (subscription-worker, "
                       "offline-only, not Meta-ready)",
            "profile": "subscription",
            "offline_only": True, "admits_worker": False}


def create_subscription_canary_policy(root: str, exe_pin: dict,
                                      home=None) -> dict:
    """Write the subscription-canary policy into the owned root.

    Same write discipline as create_subscription_worker_policy,
    but the text is build_subscription_canary_policy_text:
    subscription text plus exactly the two E31 network lines.
    The returned identity says "canary" with offline_only
    False and an honest network label; admits_worker stays
    False because general worker admission is never granted
    here — the single canary turn is gated in muse.py
    (profile + prior canary admission + single use).
    """
    _require_darwin()
    if not isinstance(exe_pin, dict):
        raise ContainmentError("valid executable pin required")
    if exe_pin.get("admits_worker"):
        raise ContainmentError(
            "a worker-admitting pin is never valid here")
    fresh_pin = _repinned(exe_pin)
    st = _check_owned_dir(root)
    text = build_subscription_canary_policy_text(
        root, fresh_pin["path"], home)
    raw = text.encode("utf-8")
    if len(raw) > _MAX_POLICY_BYTES:
        raise ContainmentError("policy too large")
    policy_path = os.path.join(root, POLICY_NAME)
    if os.path.lexists(policy_path):
        raise ContainmentError(
            "policy already exists; refusing to overwrite")
    fd = os.open(policy_path,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise ContainmentError("short write of policy")
    finally:
        os.close(fd)
    digest, pst = _read_identity(policy_path,
                                _MAX_POLICY_BYTES)
    if digest != hashlib.sha256(raw).hexdigest():
        raise ContainmentError("policy changed after write")
    return {"policy_path": policy_path,
            "policy_sha256": digest,
            "policy_size": pst.st_size,
            "policy_dev": pst.st_dev,
            "policy_ino": pst.st_ino,
            "root": root, "root_dev": st.st_dev,
            "root_ino": st.st_ino,
            "executable": fresh_pin,
            "sandbox_exec": SANDBOX_EXEC,
            "network": "443-outbound + DNS-resolver only "
                       "(canary, not Meta-ready)",
            "profile": "canary",
            "offline_only": False, "admits_worker": False}


def create_subscription_broker_policy(root: str, exe_pin: dict,
                                      home, port: int) -> dict:
    """Write the subscription-broker policy into the owned root.

    Same write discipline as create_subscription_canary_policy
    (re-pinned executable, validated owned root, exclusive
    create, post-write identity recheck), but the text is
    build_subscription_broker_policy_text: subscription text
    plus exactly the one loopback egress line for the given
    broker port. The returned identity says "broker" with an
    honest network label; admits_worker stays False because
    general worker admission is never granted here — the
    single brokered turn is gated in muse.py exactly like
    the canary turn (profile + prior admission + single use).
    """
    _require_darwin()
    if not isinstance(exe_pin, dict):
        raise ContainmentError("valid executable pin required")
    if exe_pin.get("admits_worker"):
        raise ContainmentError(
            "a worker-admitting pin is never valid here")
    fresh_pin = _repinned(exe_pin)
    st = _check_owned_dir(root)
    text = build_subscription_broker_policy_text(
        root, fresh_pin["path"], home, port)
    raw = text.encode("utf-8")
    if len(raw) > _MAX_POLICY_BYTES:
        raise ContainmentError("policy too large")
    policy_path = os.path.join(root, POLICY_NAME)
    if os.path.lexists(policy_path):
        raise ContainmentError(
            "policy already exists; refusing to overwrite")
    fd = os.open(policy_path,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise ContainmentError("short write of policy")
    finally:
        os.close(fd)
    digest, pst = _read_identity(policy_path,
                                _MAX_POLICY_BYTES)
    if digest != hashlib.sha256(raw).hexdigest():
        raise ContainmentError("policy changed after write")
    return {"policy_path": policy_path,
            "policy_sha256": digest,
            "policy_size": pst.st_size,
            "policy_dev": pst.st_dev,
            "policy_ino": pst.st_ino,
            "root": root, "root_dev": st.st_dev,
            "root_ino": st.st_ino,
            "executable": fresh_pin,
            "sandbox_exec": SANDBOX_EXEC,
            "network": "loopback broker only "
                       "(broker, not Meta-ready)",
            "profile": "broker",
            "offline_only": False, "admits_worker": False}


def create_repair_broker_policy(root: str, exe_pin: dict,
                                home, port: int,
                                workdir) -> dict:
    """Write the repair-broker policy into the owned root.

    Same write discipline as
    create_subscription_broker_policy (re-pinned
    executable, validated owned root, exclusive create,
    post-write identity recheck), but the text is
    build_repair_broker_policy_text: broker text plus
    exactly the write rule for one derived worktree (and
    the read rule only when the base text does not
    already allow it). admits_worker stays False: each
    repair attempt proves itself in muse.py instead.
    """
    _require_darwin()
    if not isinstance(exe_pin, dict):
        raise ContainmentError("valid executable pin required")
    if exe_pin.get("admits_worker"):
        raise ContainmentError(
            "a worker-admitting pin is never valid here")
    fresh_pin = _repinned(exe_pin)
    st = _check_owned_dir(root)
    text = build_repair_broker_policy_text(
        root, fresh_pin["path"], home, port, workdir)
    raw = text.encode("utf-8")
    if len(raw) > _MAX_POLICY_BYTES:
        raise ContainmentError("policy too large")
    policy_path = os.path.join(root, POLICY_NAME)
    if os.path.lexists(policy_path):
        raise ContainmentError(
            "policy already exists; refusing to overwrite")
    fd = os.open(policy_path,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(fd, raw) != len(raw):
            raise ContainmentError("short write of policy")
    finally:
        os.close(fd)
    digest, pst = _read_identity(policy_path,
                                _MAX_POLICY_BYTES)
    if digest != hashlib.sha256(raw).hexdigest():
        raise ContainmentError("policy changed after write")
    return {"policy_path": policy_path,
            "policy_sha256": digest,
            "policy_size": pst.st_size,
            "policy_dev": pst.st_dev,
            "policy_ino": pst.st_ino,
            "root": root, "root_dev": st.st_dev,
            "root_ino": st.st_ino,
            "executable": fresh_pin,
            "sandbox_exec": SANDBOX_EXEC,
            "network": "loopback broker only "
                       "(repair, not Meta-ready)",
            "profile": "repair-broker",
            "offline_only": False, "admits_worker": False}


def wrapper_argv(policy: dict, exe_path: str) -> list:
    """Exact wrapper invocation the controls run under:
    [sandbox-exec, -f, policy, executable]. Revalidates the
    recorded root identity, the policy file (regular, nofollow,
    SHA/mode/size/name-inode), and the full executable pin
    (SHA/dev/ino/size/mode plus executable bit) before
    constructing the argv — never path-only, never silent
    rebinding. No shell, no launchd, no model-child escape.

    This Mac refuses nested sandbox-exec init, so test code must
    never exec this argv itself; the reviewed harness applies it
    natively exactly once per product child.
    """
    _require_darwin()
    if not isinstance(policy, dict):
        raise ContainmentError("policy identity required")
    root = policy.get("root")
    try:
        live = os.lstat(root)
    except (OSError, TypeError) as exc:
        raise ContainmentError(
            "recorded root unavailable: %s" % exc)
    if (live.st_dev, live.st_ino) != (
            policy.get("root_dev"), policy.get("root_ino")):
        raise ContainmentError("recorded root identity drifted")
    if not _stat.S_ISDIR(live.st_mode):
        raise ContainmentError("recorded root not a directory")
    if live.st_uid != os.getuid():
        raise ContainmentError("recorded root not owned")
    if os.path.realpath(root) != root:
        raise ContainmentError("recorded root rebound")
    policy_path = policy.get("policy_path")
    if not policy_path:
        raise ContainmentError("policy identity required")
    if policy_path != os.path.join(root, POLICY_NAME):
        raise ContainmentError(
            "policy path must be the owned root policy file")
    digest, pst = _read_identity(policy_path,
                                _MAX_POLICY_BYTES)
    if digest != policy.get("policy_sha256"):
        raise ContainmentError("policy bytes drifted")
    if pst.st_size != policy.get("policy_size"):
        raise ContainmentError("policy size drifted")
    if (pst.st_dev, pst.st_ino) != (
            policy.get("policy_dev"), policy.get("policy_ino")):
        raise ContainmentError("policy file replaced")
    if _stat.S_IMODE(pst.st_mode) != 0o600:
        raise ContainmentError("policy mode drifted")
    if pst.st_uid != os.getuid():
        raise ContainmentError("policy owner drifted")
    if exe_path != (policy.get("executable") or {}).get("path"):
        raise ContainmentError(
            "argv executable must equal the pinned path")
    _repinned(policy.get("executable"))
    return [SANDBOX_EXEC, "-f", policy_path, exe_path]
