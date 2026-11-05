"""Conservative semver/range handling (stdlib only).

Rules that matter for the two registered targets:
- `major == 0` (React Native 0.x): a MINOR change may break compatibility,
  so it must never be treated as safe. Only patch-level moves within the
  same 0.minor are main candidates.
- Pre-release/channel changes always need the `next` lane.
- A `latest` older than declared/installed is a downgrade risk, not an update.
"""
from __future__ import annotations

import re


def parse(version: str):
    """Parse '1.2.3', '0.86.2', '1.0.0-next.1', 'v2.0.0+build' or None."""
    if version is None:
        return None
    s = str(version).strip().lstrip("v=").strip()
    s = s.split("+", 1)[0].strip()
    if not s or s in ("*", "x", "X", "latest"):
        return None
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]([0-9A-Za-z.-]+))?$", s)
    if not m:
        # tolerate '1.2' -> '1.2.0'
        m2 = re.match(r"^(\d+)\.(\d+)(?:[-.]([0-9A-Za-z.-]+))?$", s)
        if not m2:
            return None
        major, minor, pre = int(m2.group(1)), int(m2.group(2)), m2.group(3)
        return (major, minor, 0, _pre_key(pre), bool(pre), s)
    major, minor, patch, pre = (int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), m.group(4))
    return (major, minor, patch, _pre_key(pre), bool(pre), s)


def _pre_key(pre):
    if not pre:
        return ()
    parts = []
    for tok in str(pre).split("."):
        parts.append((0, int(tok)) if tok.isdigit() else (1, tok.lower()))
    return tuple(parts)


def compare(a: str, b: str) -> int:
    """-1/0/1 by semver precedence. Unknown versions -> 0 with fallback note."""
    pa, pb = parse(a), parse(b)
    if pa is None or pb is None:
        sa, sb = (str(a or "").strip(), str(b or "").strip())
        if sa == sb:
            return 0
        return -1 if sa < sb else 1
    ka = (pa[0], pa[1], pa[2], 1 if not pa[4] else 0, pa[3])
    kb = (pb[0], pb[1], pb[2], 1 if not pb[4] else 0, pb[3])
    return -1 if ka < kb else (1 if ka > kb else 0)


def channel_of(version: str) -> str:
    """stable | prerelease-tag | unknown."""
    p = parse(version)
    if p is None:
        return "unknown"
    if p[4]:
        return str(p[5].split("-", 1)[1].split(".", 1)[0].lower())
    return "stable"


def _cmp_tuples(pa, pb) -> int:
    ka = (pa[0], pa[1], pa[2], 1 if not pa[4] else 0, pa[3])
    kb = (pb[0], pb[1], pb[2], 1 if not pb[4] else 0, pb[3])
    return -1 if ka < kb else (1 if ka > kb else 0)


def _satisfies_single(version_t, token: str) -> bool | None:
    """None = unsupported operator (caller treats as unknown, never as match)."""
    token = token.strip()
    if not token or token in ("*", "x", "X", "latest"):
        return True
    m = re.match(r"^(>=|<=|>|<|=|==?|~|\^)?\s*(.+?)\s*$", token)
    if not m:
        return None
    op, ver = m.group(1) or "=", m.group(2)
    if ver in ("*", "x", "X"):
        return True
    # wildcard prefix like 1.2.x / 1.x
    if re.fullmatch(r"\d+(\.\d+)?\.[xX*]", ver):
        base = ver[:-2]
        pv = parse(base + ".0")
        if pv is None:
            return None
        lo = (pv[0], pv[1], 0)
        hi = (pv[0], pv[1] + 1, 0) if "." in base else (pv[0] + 1, 0, 0)
        cur = (version_t[0], version_t[1], version_t[2])
        return lo <= cur < hi
    if re.fullmatch(r"\d+\.[xX*]", ver):
        major = int(ver.split(".")[0])
        return version_t[0] == major
    pv = parse(ver)
    if pv is None:
        return None
    c = _cmp_tuples(version_t, pv)
    if op in ("=", "=="):
        # '=' on a prerelease-less constraint must not match a prerelease
        # of the same numbers (npm semantics).
        if c == 0 and version_t[4] and not pv[4]:
            return False
        return c == 0
    if op == ">":
        return c > 0
    if op == ">=":
        return c >= 0
    if op == "<":
        return c < 0
    if op == "<=":
        return c <= 0
    if op == "^":
        if pv[0] > 0:
            return (version_t[0] == pv[0] and _cmp_tuples(version_t, pv) >= 0)
        if pv[1] > 0:
            return (version_t[0] == 0 and version_t[1] == pv[1]
                    and _cmp_tuples(version_t, pv) >= 0)
        return _cmp_tuples(version_t, pv) == 0
    if op == "~":
        return (version_t[0] == pv[0] and version_t[1] == pv[1]
                and _cmp_tuples(version_t, pv) >= 0)
    return None


def _range_mentions_prerelease(text: str) -> bool:
    """True when the range itself names a prerelease (npm: opt-in to prereleases)."""
    for group in text.split("||"):
        for part in group.strip().split():
            m = re.match(r"^(>=|<=|>|<|=|==?|~|\^)?\s*(.+?)\s*$", part)
            if m and parse(m.group(2)) is not None and parse(m.group(2))[4]:
                return True
    return False


def floor_of_range(range_str: str) -> str | None:
    """Lowest concrete version a range admits, or None when unsupported.

    Used only as a conservative comparison base when no installed lock
    exists: `^6.0.0` -> `6.0.0`, `>=0.19.0 <0.21.0` -> `0.19.0`.
    Never invents: path/workspace/git/url specs and `*` return None.
    """
    text = (range_str or "").strip()
    if not text or text in ("*", "x", "X", "latest"):
        return None
    if re.match(r"^(file|link|workspace|portal|git\+|git:|http|github:|npm:)", text):
        return None
    if text.startswith("{") or "sdk:" in text or "/" in text.split()[0]:
        return None
    floors = []
    for group in text.split("||"):
        parts = [p for p in group.strip().split() if p]
        if not parts:
            continue
        lo = None
        for part in parts:
            m = re.match(r"^(>=|<=|>|<|=|==?|~|\^)?\s*(.+?)\s*$", part)
            if not m:
                return None
            op, ver = m.group(1) or "=", m.group(2)
            if ver in ("*", "x", "X"):
                continue
            if re.fullmatch(r"\d+(\.\d+)?\.[xX*]", ver) or re.fullmatch(r"\d+\.[xX*]", ver):
                base = ver[:-2]
                pv = parse(base + ".0")
                if pv is None:
                    return None
                lo = pv if lo is None or _cmp_tuples(pv, lo) > 0 else lo
                continue
            pv = parse(ver)
            if pv is None:
                return None
            if op in ("=", "==", ">=", "~", "^", ">"):
                # `>` admits floor+epsilon, but the floor is still the
                # conservative base for update_kind (never claims newer).
                lo = pv if lo is None or _cmp_tuples(pv, lo) > 0 else lo
        if lo is not None:
            floors.append(lo)
    if not floors:
        return None
    best = floors[0]
    for cand in floors[1:]:
        if _cmp_tuples(cand, best) < 0:
            best = cand
    return f"{best[0]}.{best[1]}.{best[2]}"


def satisfies(range_str: str, version: str) -> bool | None:
    """True/False, or None when the range uses unsupported syntax.

    None must be rendered as 'unsupported/unavailable', never as drift-ok.

    npm semantics: a stable range never admits a prerelease version unless
    the range itself names a prerelease of the same numbers.
    """
    vt = parse(version)
    if vt is None:
        return None
    text = (range_str or "").strip()
    if not text or text in ("*", "latest"):
        return True
    if vt[4] and not _range_mentions_prerelease(text):
        return False
    for group in text.split("||"):
        parts = [p for p in group.strip().split() if p]
        if not parts:
            continue
        results = [_satisfies_single(vt, p) for p in parts]
        if all(r is True for r in results):
            return True
        if any(r is False for r in results):
            continue
        return None  # some part unsupported -> unknown
    return False


def update_kind(current: str, latest: str) -> str:
    """none | patch | minor | major | rn-minor | prerelease | downgrade | unknown."""
    if not current or not latest:
        return "unknown"
    pc0, pl0 = parse(current), parse(latest)
    if pc0 is None or pl0 is None:
        if str(current).strip() == str(latest).strip():
            return "none"
        return "unknown"
    c = compare(current, latest)
    if c == 0:
        # same precedence but different channel text (e.g. rc vs stable)
        if channel_of(current) != channel_of(latest):
            return "prerelease"
        return "none"
    if c > 0:
        return "downgrade"
    pc, pl = parse(current), parse(latest)
    if pc is None or pl is None:
        return "unknown"
    if pl[4] or channel_of(latest) != "stable":
        return "prerelease"
    if pc[0] == 0 or pl[0] == 0:
        if (pc[0], pc[1]) != (pl[0], pl[1]):
            # RN 0.x minor (or 0->1) move: breaking until proven otherwise.
            if pc[0] == 0 and pl[0] == 0 and pc[1] != pl[1]:
                return "rn-minor"
            return "major"
        return "patch"
    if pc[0] != pl[0]:
        return "major"
    if pc[1] != pl[1]:
        return "minor"
    return "patch"


def safe_lane(kind: str) -> str:
    """Conservative branch lane: only pure patch goes to main.

    `none` (already at latest) also rests on main; everything else —
    including 0.x minor moves — needs the next lane.
    """
    if kind in ("patch", "none"):
        return "main"
    return "next"
