"""Temp-home fixture helper (M2.15b): restore, never pop.

Dozens of fixtures used to clean up with
`addCleanup(os.environ.pop, "OPENMAINTY_HOME")`, which DELETES the
variable instead of restoring the runner's value — every later test
in the same process then resolved paths.home() to the real
~/.openmainty (order-dependent). This helper records the previous
value, points the variable at the fixture home, and registers a
cleanup that restores the previous value (popping only what this
helper itself added, i.e. when nothing was set before). Nested uses
compose: cleanups run last-in-first-out back to the outer value.
Pure stdlib; no openmainty import, so every test module can use it.

Importing this module also switches the real-home guard on
(OPENMAINTY_TEST_GUARD=1): in `discover -s tests` mode the
tests/__init__.py bootstrap never loads, so the first migrated
module import arms the guard for the whole process instead.
Production never sets it (verified: no launchd plist or CLI code
references it).
"""
import os
import sys

os.environ.setdefault("OPENMAINTY_TEST_GUARD", "1")


def _wait_refresh_idle():
    """Join a running background refresh before HOME is restored.

    M2.16b2: the snapshot writers resolve the home once when they
    start, so a refresh still running at teardown would write
    into the temp home while TemporaryDirectory.cleanup removes
    it (Errno 66). Every fixture registers tmp.cleanup before
    this helper, and cleanups run LIFO — so this wait runs
    before the temp dir is removed. Only when openmainty.serve
    is already imported; never imported just for this. Never
    raises.
    """
    try:
        mod = sys.modules.get("openmainty.serve")
        wait = getattr(mod, "wait_refresh_idle", None)
        if not callable(wait):
            return
        wait(timeout=10.0)
    except Exception:  # noqa: BLE001 - hygiene never fails a test
        pass


def use_temp_home(testcase, path):
    """Point OPENMAINTY_HOME at path for this test; restore after.

    Returns the path as a string. Never raises.
    """
    previous = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(path)

    def _restore():
        _wait_refresh_idle()
        if previous is None:
            os.environ.pop("OPENMAINTY_HOME", None)
        else:
            os.environ["OPENMAINTY_HOME"] = previous

    testcase.addCleanup(_restore)
    return str(path)


def offline_prs(testcase, rows):
    """Opt-in offline boundary for the PR scan source.

    sched scan refreshes open PRs through openmainty.cli._open_prs
    (exact prs.pr_list_argv transport, pinned by test_prs.py). Fixture
    scans with a github profile must never reach real `gh`/network.
    This stubs ONLY _open_prs at the product boundary with the given
    explicit canned rows (pass [] for no open PRs). Restore is
    automatic via addCleanup, LIFO so nested uses compose back to the
    outer value. A wait-before-stop cleanup runs first so a still
    running refresh observes the canned collector while the fixture
    HOME is still active. Collector tests that DO exercise the PR
    transport (test_prs.py argv/shape tests) never use this helper. No
    production branching: pure test mock. _open_issues is
    intentionally left alone (no evidenced denial gap).
    """
    from unittest import mock as _mock
    from openmainty import cli as _cli
    canned = list(rows)
    prs_patch = _mock.patch.object(_cli, "_open_prs", return_value=canned)
    prs_patch.start()
    testcase.addCleanup(prs_patch.stop)
    testcase.addCleanup(_wait_refresh_idle)
    return canned


def offline_qa_registry(testcase, mapping):
    """Opt-in offline boundary for QA registry repo lookups.

    batches._qa_source resolves each dep through _qa_registry_repo,
    which runs `npm view <pkg> repository.url --json` (transport
    boundary pinned by QA-pack tests). Fixture QA collections must
    never reach the real npm registry. This stubs ONLY
    _qa_registry_repo with the given explicit fixture mapping
    {dep: "owner/name"} (pass {} so every lookup resolves to "" and
    the collector records its honest unavailable note). Restore is
    automatic via addCleanup, LIFO so nested uses compose. A
    wait-before-stop cleanup runs first so a still running refresh
    observes the canned collector while the fixture HOME is still
    active. Collector lower-transport tests that DO exercise the npm
    parsing helper keep calling the real function with canned
    subprocess output instead of this helper. No production
    branching: pure test mock.
    """
    from unittest import mock as _mock
    from openmainty import batches as _batches
    table = dict(mapping)

    def _fake(pkg, timeout=60):
        return table.get(str(pkg), "")

    reg_patch = _mock.patch.object(
        _batches, "_qa_registry_repo", side_effect=_fake)
    reg_patch.start()
    testcase.addCleanup(reg_patch.stop)
    testcase.addCleanup(_wait_refresh_idle)
    return table


def offline_muse_version(testcase, version):
    """Opt-in offline boundary for model CLI version metadata.

    Usage/agent-info stamping calls openmainty.muse.version()
    (runner usage, exec_prompt usage, agent_info) even on
    FakeExecutor/offline flows; the raw call shells out to
    `muse --version` on every invocation (no cache). Fixture flows
    that assert behavior — not the version string — must never reach
    a real model CLI. This stubs ONLY muse.version with the given
    explicit sentinel string (pass e.g. "fixture-muse-0.0.0").
    Restore is automatic via addCleanup, LIFO so nested uses compose
    back to the outer value. A wait-before-stop cleanup runs first so
    a still running refresh observes the canned metadata while the
    fixture HOME is still active. Transport tests that DO exercise
    version/CLI shape (build_cmd/usage parsing) never use this helper.
    No production branching: pure test mock.
    """
    from unittest import mock as _mock
    from openmainty import muse as _muse
    sentinel = str(version)
    ver_patch = _mock.patch.object(_muse, "version", return_value=sentinel)
    ver_patch.start()
    testcase.addCleanup(ver_patch.stop)
    testcase.addCleanup(_wait_refresh_idle)
    return sentinel


def offline_muse_binary(testcase, path="/fixture/muse"):
    """Opt-in offline boundary for model CLI presence probing.

    chat.subscription_error() calls openmainty.muse.which(), whose raw
    implementation runs `shutil.which("muse")` against the host PATH.
    Fixture flows that assert gate behavior — not real CLI discovery —
    must never depend on whether a `muse` binary happens to be
    installed on the host. This stubs ONLY muse.which with the given
    explicit fixture path (pass e.g. "/fixture/muse"; the stub is never
    invoked). Restore is automatic via addCleanup, LIFO so nested uses
    compose back to the outer value. A wait-before-stop cleanup runs
    first so a still running refresh observes the canned presence while
    the fixture HOME is still active. Tests that DO exercise the real
    missing-binary 403 explicitly re-patch muse.which to None instead
    of using this helper. No production branching: pure test mock.
    """
    from unittest import mock as _mock
    from openmainty import muse as _muse
    fixture = str(path)
    which_patch = _mock.patch.object(_muse, "which", return_value=fixture)
    which_patch.start()
    testcase.addCleanup(which_patch.stop)
    testcase.addCleanup(_wait_refresh_idle)
    return fixture


def offline_service_status(testcase, status):
    """Opt-in offline boundary for maintenance-service discovery.

    serve overview/status paths read openmainty.serve.
    _maintenance_service, whose real implementation runs
    `launchctl list <label>`. Fixture flows that assert snapshot rows
    — not service discovery — must never reach the real scheduler.
    This stubs ONLY _maintenance_service with the given explicit owned
    row (pass e.g. {"installed": True, "running": False, "pid": None,
    "note": "fixture offline; not live service observation"}). The row
    is defensively deep-copied at setup and a fresh copy is returned
    per call, so caller or consumer mutation cannot leak between calls.
    Restore is automatic via addCleanup, LIFO so nested uses compose
    back to the outer value. A wait-before-stop cleanup runs first so
    a still running refresh observes the canned status while the
    fixture HOME is still active. Service-discovery transport tests
    never use this helper. No production branching: pure test mock.
    """
    import copy as _copy
    from unittest import mock as _mock
    from openmainty import serve as _serve
    owned = _copy.deepcopy(dict(status))

    def _fake(*_args, **_kwargs):
        return _copy.deepcopy(owned)

    svc_patch = _mock.patch.object(
        _serve, "_maintenance_service", side_effect=_fake)
    svc_patch.start()
    testcase.addCleanup(svc_patch.stop)
    testcase.addCleanup(_wait_refresh_idle)
    return _copy.deepcopy(owned)


def offline_discussions(testcase):
    """Neutral offline boundary for the Discussions scan source.

    sched.scan refreshes the Discussions list snapshot and label
    census on the same cadence; fixture repos must never reach real
    `gh`/network (a hung gh would hold the scan for ~20s per call).
    This stubs the two Discussions collectors at the product
    boundary with deterministic empty data — no items, no labels,
    no error — so existing scan assertions keep their meaning.
    Restore is automatic via addCleanup. Collector tests that DO
    exercise the network boundary (test_discussions.py) mock one
    level lower (_gh_graphql/_gh_rest) with canned payloads instead
    of using this helper. No production branching: pure test mock.
    """
    from unittest import mock as _mock
    from openmainty import discussions as _disc
    scan_patch = _mock.patch.object(
        _disc, "scan_source",
        return_value={"items": [],
                      "status": {"state": "ok", "checked_at": None,
                                 "error": None, "disabled": False,
                                 "partial": False}})
    labels_patch = _mock.patch.object(
        _disc, "fetch_labels",
        return_value={"schema": 1, "kind": "label-census",
                      "labels": [], "labels_complete": True,
                      "pages": 1})
    scan_patch.start()
    labels_patch.start()
    testcase.addCleanup(labels_patch.stop)
    testcase.addCleanup(scan_patch.stop)
