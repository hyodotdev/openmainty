"""openmainty CLI: doctor, repo, plan, run, status, resume, answer, deps, watch, chat, serve, app, external."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openmainty import __version__, chat, deps, doctor, external, muse, pcconfig, planner, repos, runner, store  # noqa: E402
from openmainty.util import read_json  # noqa: E402


def _parse_only(value):
    if not value:
        return None
    steps = [s.strip() for s in value.split(",") if s.strip()]
    unknown = [s for s in steps if s not in store.STEP_ORDER]
    if unknown:
        raise SystemExit(f"error: unknown step(s): {', '.join(unknown)} (see: {', '.join(store.STEP_ORDER)})")
    return steps


def _home_note() -> str:
    from openmainty import paths
    return f"(home: {paths.home()})"


# Distinct exit code: the single-worker OS lease is held by another live
# holder, so nothing started — no run created, no resume advanced, no
# attempts consumed. Safe to retry later.
WORKER_BUSY_EXIT = 4


def _acquire_entry_lease():
    """Real OS lease attempt for a manual start/resume entry point.

    `sched` is imported lazily (sched imports cli at module level).
    Returns the owner token, or None when another live holder owns it.
    """
    from openmainty import sched as _sched
    return _sched.acquire_worker_lease()


def _release_entry_lease(owner: str) -> None:
    from openmainty import sched as _sched
    _sched.release_worker_lease(owner)


def cmd_doctor(args) -> int:
    model = getattr(args, "model", None)
    result = doctor.live(model=model) if args.live else doctor.quick()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(doctor.render_human(result))
    return 0


def cmd_repo(args) -> int:
    if args.action == "list":
        names = repos.list_all()
        print("\n".join(names) if names else "(no repos registered)")
        return 0
    if args.action == "add":
        if args.profile:
            profile = read_json(Path(args.profile))
        else:
            profile = {
                "name": args.name,
                "github": args.github,
                "baseBranch": args.base_branch or "main",
                "knowledge": [],
                "checks": [],
                "e2e": [],
                "release": {},
                "announce": {"medium": True, "discord": True, "slack": True},
            }
            if args.check:
                for spec in args.check:
                    name, _, cmd = spec.partition("=")
                    profile["checks"].append({"name": name, "cmd": cmd.split()})
        path = repos.save(profile)
        if args.checkout:
            pc = pcconfig.load()
            pc.setdefault("checkouts", {})[profile["name"]] = str(Path(args.checkout).expanduser().resolve())
            pcconfig.save(pc)
        print(f"registered {profile['name']} -> {path} {_home_note()}")
        return 0
    if args.action == "show":
        profile = repos.load(args.name)
        if args.export:
            print(json.dumps(profile, indent=2))
        else:
            print(json.dumps(profile, indent=2))
        return 0
    if args.action == "link":
        pc = pcconfig.load()
        pc.setdefault("checkouts", {})[args.name] = str(Path(args.checkout).expanduser().resolve())
        pcconfig.save(pc)
        print(f"linked {args.name} -> {pc['checkouts'][args.name]}")
        return 0
    raise SystemExit(f"error: unknown repo action '{args.action}'")


def _issue_body(args) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8")
    return args.body or ""


def _resolve_checkout(profile: dict) -> Path:
    co = pcconfig.checkout_for(profile["name"])
    if not co:
        raise SystemExit(
            f"error: no checkout linked for '{profile['name']}' "
            f"(see: openmainty repo link --name {profile['name']} --checkout PATH)")
    path = Path(co)
    if not path.is_dir():
        raise SystemExit(f"error: checkout not found: {path}")
    return path


def cmd_plan(args) -> int:
    profile = repos.load(args.repo)
    body = _issue_body(args)
    plan = planner.plan_for(profile, args.issue, args.title or "", body)
    print(plan)
    return 0


def _start_run_with(args, executor, run_fields: dict | None = None,
                    raise_quota: bool = False, on_created=None):
    """Start a run; returns (rc, run_id). See `_start_run_with_held`.

    Holds the single-worker OS lease for the whole start. When another
    live holder owns it, nothing is created and `(WORKER_BUSY_EXIT, None)`
    is returned — no run, no claim, no model call.
    """
    owner = _acquire_entry_lease()
    if owner is None:
        print("refusing: another maintenance worker holds the global lock; "
              "no run started")
        return WORKER_BUSY_EXIT, None
    try:
        return _start_run_with_held(args, executor, run_fields=run_fields,
                                    raise_quota=raise_quota,
                                    on_created=on_created)
    finally:
        _release_entry_lease(owner)


def _start_run_with_held(args, executor, run_fields: dict | None = None,
                         raise_quota: bool = False, on_created=None):
    """Start a run; returns (rc, run_id). `run_fields` persists the exact
    scheduler proposal (lane/branch/base/dep/target/queue key) into run.json
    so review can prove which strategy executed — a lane tag in text only
    is not a branch strategy. With raise_quota, a subscription quota hit
    propagates (the scheduler records truthful quota state) instead of
    collapsing into a generic failure code. `on_created(run_id)` fires
    immediately after run.json exists, BEFORE the first worker/model/shell
    step, so a crash mid-run keeps its queue-to-run identity.

    Caller must hold the worker lease (the nested `runner.execute`
    reuses it); see `_start_run_with`."""
    profile = repos.load(args.repo)
    dup = store.active_for(args.repo, args.issue)
    if dup and not args.allow_duplicate:
        print(f"refusing: active local run {dup} already handles {args.repo}#{args.issue} "
              f"(local lock only; use --allow-duplicate to override)")
        return 2, None
    checkout = _resolve_checkout(profile)
    fields = dict(run_fields or {})
    base_branch = fields.get("base_branch") or profile.get("baseBranch", "main")
    # Strict base check: a missing ref must fail here, never silently
    # fall back to HEAD (runner.git_rev keeps its legacy fallback for
    # interactive use). Non-git checkouts (fixture copy path) skip it.
    if (checkout / ".git").exists():
        _, strict_code, strict_err = runner.git_rev_strict(checkout, base_branch)
        if strict_code != 0:
            print(f"error: missing base ref '{base_branch}': "
                  f"{strict_err.strip()[-160:]} (no HEAD fallback)")
            return 1, None
    base, _ = runner.git_rev(checkout, base_branch)
    state = store.create(args.repo, args.issue, args.title or "", base,
                         muse.agent_info(model=getattr(args, "model", None)))
    for key in ("lane", "branch", "base_branch", "queue_key", "dep",
                "target", "scopes", "update_kind", "refresh_base"):
        if fields.get(key) is not None:
            state[key] = fields[key]
    if state.get("branch"):
        state["base_branch"] = base_branch
    store.claim(args.repo, args.issue, state["id"])
    if on_created is not None:
        try:
            on_created(state["id"])
        except Exception:  # noqa: BLE001 - identity persist must not kill run
            pass
    body = _issue_body(args)
    plan = planner.plan_for(profile, args.issue, args.title or "", body,
                            run_ctx=runner.run_context_for(state, profile))
    (store.run_dir(state["id"]) / "plan.md").write_text(plan, encoding="utf-8")
    state["artifacts"]["plan"] = "plan.md"
    store.save(state)
    print(f"run {state['id']} for {args.repo}#{args.issue} { _home_note()}")
    try:
        ok = runner.execute(state, profile, executor, checkout, body=body,
                            only=_parse_only(args.only))
    except runner.QuotaHit:
        if raise_quota:
            raise
        print(f"waiting: subscription quota hit; resume later with: openmainty resume --run {state['id']}")
        return 3, state["id"]
    if ok:
        store.release(args.repo, args.issue, state["id"])
    print(f"run {state['id']}: {'done' if ok else 'stopped (see status)'}")
    return (0 if ok else 1), state["id"]


def _start_run(args, executor) -> int:
    rc, _ = _start_run_with(args, executor)
    return rc


def cmd_run(args, cancel_event=None) -> int:
    return _start_run(args, runner.make_executor(args.executor, model=getattr(args, "model", None),
                                                 cancel_event=cancel_event))


def cmd_status(args) -> int:
    state = store.load(args.run)
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    print(f"run {state['id']}  {state['repo']}#{state['issue']}  {state['title']}")
    print(f"pc={state['pc']}  base={short(state['baseCommit'])}  work={short(state['workCommit'])}"
          f"  wait={state.get('waitReason')}")
    for name in store.STEP_ORDER:
        e = state["steps"][name]
        print(f"  {name:10} {e['status']:8} exit={e['exit_code']}  {e.get('note','')}")
    print(f"artifacts: {json.dumps(state.get('artifacts', {}))}")
    return 0


def short(commit: str) -> str:
    return (commit or "?")[:12]


def cmd_resume(args, cancel_event=None) -> int:
    """Resume a run. Holds the single-worker OS lease for the whole resume.

    When another live holder owns the lease, refuses with
    WORKER_BUSY_EXIT BEFORE any gate pop, waitReason clear, model call,
    or source mutation — the run is byte-identical and safe to retry.
    """
    state = store.load(args.run)
    owner = _acquire_entry_lease()
    if owner is None:
        print(f"refusing: another maintenance worker holds the global lock; "
              f"no work started (run {state['id']} unchanged)")
        return WORKER_BUSY_EXIT
    try:
        return _cmd_resume_held(state, args, cancel_event)
    except runner.WorkerBusy:
        # Unreachable while this call holds the lease; defense in depth.
        print(f"refusing: another maintenance worker holds the global lock; "
              f"no work started (run {state['id']} unchanged)")
        return WORKER_BUSY_EXIT
    finally:
        _release_entry_lease(owner)


def _cmd_resume_held(state: dict, args, cancel_event=None) -> int:
    profile = repos.load(state["repo"])
    checkout = _resolve_checkout(profile)
    # Durable disposition is evaluated BEFORE any waitReason pop: a
    # confirmed duplicate (or unanswered missing-info) never resumes
    # into worker steps, from any entry point.
    gate = runner.resume_gate(state)
    if gate is not None:
        runner.enforce_gate(state, gate)
        state = store.load(state["id"])
        print(f"run {state['id']}: stopped ({state.get('waitReason')})")
        return 1
    if state.get("waitReason"):
        # Neutral resume audit: the entry point is threaded explicitly
        # (scheduler/dashboard/cli) — never infer "human" from a queued
        # run or the executor. A daemon dispatch must never be labelled
        # a human action.
        from openmainty.util import utcnow as _utcnow
        actor = getattr(args, "actor", None) or "cli"
        state.setdefault("interventions", []).append(
            {"at": _utcnow(), "kind": "resume",
             "detail": f"{actor} resumed after: {state['waitReason']}"})
        state["waitReason"] = None
        store.save(state)
    try:
        ok = runner.execute(state, profile,
                            runner.make_executor(args.executor, model=getattr(args, "model", None),
                                                 cancel_event=cancel_event),
                            checkout, only=_parse_only(args.only))
    except runner.QuotaHit:
        print(f"waiting: subscription quota hit; resume later with: openmainty resume --run {state['id']}")
        return 3
    if ok:
        store.release(state["repo"], state["issue"], state["id"])
        # Scheduler propagation (narrow): a FULL successful resume of a
        # scheduler-managed run settles its exact linked queue item —
        # same run, same attempts, no new model calls. Subset resumes,
        # non-scheduler runs, and any guard refusal leave the queue
        # untouched; the run result itself never depends on this.
        if state.get("queue_key") and _parse_only(getattr(args, "only", None)) is None:
            try:
                from openmainty import sched as _sched
                fin = _sched.finalize_finished_run(
                    state.get("repo"), state.get("queue_key"),
                    state["id"], actor="cli-resume")
                if fin.get("ok"):
                    print(f"queue: {fin['key']} -> waiting-review "
                          f"verified-true (same run {state['id']})")
                else:
                    print(f"queue: not finalized ({fin.get('reason')})")
            except Exception as exc:  # noqa: BLE001 - run outcome stands
                print(f"queue: finalize skipped ({type(exc).__name__})")
    print(f"run {state['id']}: {'done' if ok else 'stopped (see status)'}")
    return 0 if ok else 1


def cmd_answer(args) -> int:
    answer = args.answer
    if args.answer_file:
        answer = Path(args.answer_file).read_text(encoding="utf-8")
    state = store.record_answer(args.run, answer or "")
    print(f"answered {args.run}; resume with: openmainty resume --run {args.run}")
    # Best effort: a scheduler-managed run must not strand in
    # waiting-review after its question is answered — queue the SAME run
    # for later safe dispatch. Never fails the saved answer.
    try:
        from openmainty import sched as _sched
        res = _sched.requeue_run_for_dispatch(state.get("repo"), state["id"])
        if res.get("requeued"):
            print(f"queued: run {state['id']} ({res.get('key')}) awaits dispatch")
    except Exception:  # noqa: BLE001 - the durable answer is already saved
        pass
    return 0


def cmd_deps(args) -> int:
    profile = repos.load(args.repo)
    checkout = _resolve_checkout(profile)
    rows = deps.check(profile, checkout)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        if not rows:
            print(f"{args.repo}: no dependencies declared in profile")
        for row in rows:
            if row["error"]:
                print(f"  {row['name']}: ERROR {row['error']}")
            elif row.get("unpublished"):
                print(f"  {row['name']}: not published yet (pin={row['pinned']})")
            elif row["drift"]:
                print(f"  {row['name']}: DRIFT pinned={row['pinned']} latest={row['latest']}")
            else:
                print(f"  {row['name']}: ok ({row['pinned']})")
    drifted = [r for r in rows if r["drift"]]
    if args.check:
        return 1 if (drifted or any(r["error"] for r in rows)) else 0
    return 0


def _open_issues(github: str, limit: int = 20) -> list:
    """Open GitHub issues via gh (repo integration, not a model API).

    Raises on failure (nonzero gh exit or non-JSON output) so the
    scheduler records an honest scan failure and keeps last known data
    instead of reporting healthy `ok (0 open)`. Interactive callers catch
    and print a skip line.
    """
    from openmainty.util import run_cmd
    code, out, err = run_cmd(
        ["gh", "issue", "list", "--repo", github, "--state", "open",
         "--limit", str(limit), "--json", "number,title"])
    if code != 0:
        raise RuntimeError(f"gh issue list failed (exit {code}): {err.strip()[-160:]}")
    try:
        parsed = json.loads(out or "[]")
    except ValueError as exc:
        raise ValueError(f"gh issue list returned non-JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("gh issue list returned non-list JSON")
    return parsed


def _open_prs(github: str, limit: int = 30) -> list:
    """Open GitHub PRs via gh (read-only, like _open_issues).

    Runs exactly prs.pr_list_argv (pinned by test) and returns the
    raw rows; callers sanitize with prs.sanitize_pr. Raises on
    failure so the scheduler keeps last known data instead of
    reporting healthy `ok (0 open)`.
    """
    from openmainty import prs as _prs
    from openmainty.util import run_cmd
    code, out, err = run_cmd(_prs.pr_list_argv(github, limit))
    if code != 0:
        raise RuntimeError(f"gh pr list failed (exit {code}): {err.strip()[-160:]}")
    try:
        parsed = json.loads(out or "[]")
    except ValueError as exc:
        raise ValueError(f"gh pr list returned non-JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("gh pr list returned non-list JSON")
    return parsed


def _github_issue_body(github: str, number) -> str:
    return fetch_issue_content(github, number).get("body") or ""


def fetch_issue_content(github: str, number) -> dict:
    """Read one upstream issue through the supported gh read operation.

    Returns {number, title, body, state, url, fetched_at, provenance} on
    success, or {number, error, fetched_at, provenance} when the issue is
    missing/offline/closed-unreadable. The body is UNTRUSTED work material:
    callers delimit it and never let it change permissions, policy, or
    external destinations. Read-only: never posts, comments, or merges.
    """
    from openmainty.util import run_cmd
    from openmainty.util import utcnow as _now
    provenance = f"gh issue view {github}#{number} (read-only)"
    code, out, err = run_cmd(
        ["gh", "issue", "view", str(number), "--repo", github,
         "--json", "number,title,body,state,url"])
    fetched_at = _now()
    if code != 0:
        return {"number": str(number), "title": "", "body": "",
                "state": "unknown", "url": "",
                "error": f"gh issue view failed (exit {code}): {err.strip()[-160:]}",
                "fetched_at": fetched_at, "provenance": provenance}
    try:
        parsed = json.loads(out or "{}")
    except ValueError as exc:
        return {"number": str(number), "title": "", "body": "",
                "state": "unknown", "url": "",
                "error": f"gh issue view returned non-JSON: {exc}",
                "fetched_at": fetched_at, "provenance": provenance}
    if not isinstance(parsed, dict):
        return {"number": str(number), "title": "", "body": "",
                "state": "unknown", "url": "",
                "error": "gh issue view returned non-object JSON",
                "fetched_at": fetched_at, "provenance": provenance}
    return {"number": str(parsed.get("number", number)),
            "title": str(parsed.get("title", "") or ""),
            "body": str(parsed.get("body", "") or ""),
            "state": str(parsed.get("state", "") or "unknown"),
            "url": str(parsed.get("url", "") or ""),
            "error": None,
            "fetched_at": fetched_at, "provenance": provenance}


def decide_issue(repo: str, issue: str) -> str:
    """start a new run, resume the latest, or skip. Pure local-state rule."""
    prev = store.latest_run(repo, issue)
    if prev is None:
        if chat.reply_session_for(repo, issue) is not None:
            return "skip-draft"
        return "start"
    if prev.get("waitReason"):
        return "skip-waiting"
    if any(e["status"] not in store.TERMINAL for e in prev["steps"].values()):
        return "skip-active"
    if all(e["status"] == "done" for e in prev["steps"].values()):
        return "skip-done"
    return "resume"


def drift_entries(rows: list) -> list:
    """Group drifted deps into ONE run entry (anti-spam: batch, don't spray).

    Returns [(issue_key, title, body)]. Empty when nothing drifted.
    """
    drifted = [r for r in rows if r["drift"]]
    if not drifted:
        return []
    from openmainty.util import utcnow
    lines = "\n".join(f"- {r['name']} ({r['kind']}:{r['id']}): {r['pinned']} -> {r['latest']}"
                      for r in drifted)
    day = utcnow()[:10]
    return [(f"drift-batch-{day}",
             f"chore(deps): bump {len(drifted)} drifted dependenc{'y' if len(drifted) == 1 else 'ies'}",
             f"Dependency drift detected:\n{lines}\n\n"
             "Update pins, mirrors (docs/tables), and lockfiles; verify with the "
             "repo's checks before drafting release notes. Keep this to one run.")]


def cmd_watch(args, cancel_event=None) -> int:
    import argparse as _ap
    profile = repos.load(args.repo)
    checkout = _resolve_checkout(profile)
    code = 0
    # 1. dependency drift -> one batched fix run
    # Stream each failed row as it completes (flushed): sequential
    # registry checks can stall for minutes, and batching errors until
    # the end made watch look wedged with zero output.
    def _stream_error(row):
        if row.get("error"):
            print(f"  {row['name']}: ERROR {row['error']} (skipped)", flush=True)
    rows = deps.check(profile, checkout, progress_fn=_stream_error)
    drifted = [r for r in rows if r["drift"]]
    errors = [r for r in rows if r["error"]]
    if not drifted:
        print(f"{args.repo}: no drift ({len(rows)} checked)")
        code = 0 if not errors else 1
    else:
        code = 0
    import argparse as _ap
    for row in drifted:
        print(f"  {row['name']}: DRIFT pinned={row['pinned']} latest={row['latest']}")
    for issue_key, title, body in drift_entries(rows):
        if not args.start_run:
            continue
        ns = _ap.Namespace(repo=args.repo, issue=issue_key, title=title,
                           body=body, body_file=None,
                           allow_duplicate=False, only=args.only, executor=args.executor,
                           model=getattr(args, "model", None))
        rc = _start_run(ns, runner.make_executor(args.executor, model=getattr(args, "model", None),
                                                 cancel_event=cancel_event))
        code = code or rc
    if drifted and not args.start_run:
        print(f"{len(drifted)} drifted (1 batched run); re-run with --start-run to open it")
    # 2. open issues -> new or resumed runs (full flow, drafts only)
    if not args.no_issues:
        try:
            watch_issues = _open_issues(profile["github"])
        except Exception as exc:  # noqa: BLE001 - gh may be absent/offline
            print(f"  issue scan skipped: {exc}"[:200])
            watch_issues = []
        for item in watch_issues:
            number = str(item["number"])
            decision = decide_issue(args.repo, number)
            print(f"  issue #{number} {item.get('title', '')}: {decision}")
            if decision == "start" and args.start_run:
                ns = _ap.Namespace(
                    repo=args.repo, issue=number, title=item.get("title", ""),
                    body=_github_issue_body(profile["github"], number), body_file=None,
                    allow_duplicate=False, only=None, executor=args.executor)
                code = code or _start_run(ns, runner.make_executor(args.executor, model=getattr(args, "model", None),
                                                                   cancel_event=cancel_event))
            elif decision == "resume" and args.start_run:
                prev = store.latest_run(args.repo, number)
                code = code or cmd_resume(_ap.Namespace(
                    run=prev["id"], executor=args.executor, only=None),
                    cancel_event=cancel_event)
    return code


def cmd_deps_discover(args) -> int:
    from openmainty import manifests
    checkout = _resolve_checkout(repos.load(args.repo))
    disc = manifests.discover(checkout)
    if args.json:
        print(json.dumps(disc, indent=2))
    else:
        print(f"{args.repo}: sources={', '.join(disc['sources']) or '(none)'}")
        for dep in disc["deps"]:
            print(f"  [{dep['manager']}/{dep['scope']}] {dep['name']}: "
                  f"declared={dep['declared']} installed={dep.get('installed')} ({dep['file']})")
        for sdk in disc["sdks"]:
            print(f"  [sdk] {sdk['name']}: {sdk['constraint']}")
        for warn in disc["warnings"]:
            print(f"  note: {warn}")
    return 0


def cmd_sched(args) -> int:
    from openmainty import sched
    action = args.sched_action
    if action not in ("status", "tick", "daemon-tick") and not args.repo:
        raise SystemExit("error: --repo is required for sched " + action)
    if action == "status":
        if args.repo:
            print(json.dumps(sched.load(args.repo), indent=2))
        else:
            out = {name: sched.load(name) for name in repos.list_all()}
            print(json.dumps(out, indent=2))
        return 0
    if action in ("enable", "disable"):
        st = sched.configure(args.repo, enabled=(action == "enable"),
                             interval_s=args.interval or sched.DEFAULT_INTERVAL_S)
        print(json.dumps({"repo": args.repo, "enabled": st["enabled"],
                          "interval_s": st["interval_s"],
                          "next_scan": st.get("next_scan")}, indent=2))
        return 0
    if action == "scan":
        print(json.dumps(sched.scan(args.repo), indent=2, ensure_ascii=False))
        return 0
    if action == "tick":
        print(json.dumps(sched.tick(), indent=2, ensure_ascii=False))
        return 0
    if action == "daemon-tick":
        from openmainty import runner
        ex = runner.make_executor(args.executor,
                                  model=getattr(args, "model", None))
        print(json.dumps(sched.daemon_tick(ex, model=getattr(args, "model", None)),
                         indent=2, ensure_ascii=False))
        return 0
    if action == "auto":
        on = (args.mode or "on").lower() in ("on", "true", "1", "yes")
        st = sched.configure(args.repo, enabled=True,
                             interval_s=args.interval or sched.DEFAULT_INTERVAL_S,
                             execute_enabled=on)
        print(json.dumps({"repo": args.repo, "execute_enabled": st["execute_enabled"],
                          "enabled": st["enabled"],
                          "next_scan": st.get("next_scan")}, indent=2))
        return 0
    if action == "run-due":
        from openmainty import runner
        ex = runner.make_executor(args.executor,
                                  model=getattr(args, "model", None))
        print(json.dumps(sched.run_due(args.repo, ex,
                                       model=getattr(args, "model", None),
                                       key=getattr(args, "key", None)),
                         indent=2, ensure_ascii=False))
        return 0
    if action == "cancel":
        print(json.dumps(sched.cancel(args.repo, args.key), indent=2))
        return 0
    if action == "reconcile":
        for field in ("key", "run", "reason"):
            if not getattr(args, field, None):
                raise SystemExit(
                    f"error: --{field} is required for sched reconcile")
        out = sched.finalize_finished_run(
            args.repo, args.key, args.run, reason=args.reason,
            correct_cancelled=True, actor="cli-reconcile")
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if action == "repair-bindings":
        out = sched.repair_version_bindings(
            args.repo, apply=bool(args.apply),
            fingerprint=getattr(args, "fingerprint", None))
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if action == "set-watch":
        try:
            inputs = json.loads(getattr(args, "inputs", None) or "null")
        except ValueError as exc:
            raise SystemExit(f"error: --inputs must be JSON: {exc}") from exc
        reopen = [s.strip() for s in (getattr(args, "reopen", None) or "").split(",")
                  if s.strip()]
        for field in ("reason", "evidence", "trigger"):
            if not getattr(args, field, None):
                raise SystemExit(f"error: --{field} is required for sched set-watch")
        out = sched.set_external_watch(
            args.repo, args.key, reason=args.reason, evidence=args.evidence,
            trigger=args.trigger, inputs=inputs,
            reopen_steps=tuple(reopen), actor="supervisory-cli")
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    raise SystemExit(f"error: unknown sched action '{action}'")


def cmd_batches(args) -> int:
    """Trusted-local review-batch import/inspection (also behind /api/batch/import)."""
    from openmainty import batches
    action = args.batches_action
    if action == "import":
        if not args.file:
            raise SystemExit("error: --file is required for batches import")
        try:
            with open(args.file, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"error: cannot read import file: {exc}")
        try:
            out = batches.import_batch(payload)
        except batches.BatchError as exc:
            raise SystemExit(f"error: invalid batch: {exc}")
        print(json.dumps({"ok": True, "id": out["id"],
                          "updated_at": out["updated_at"],
                          "primaries": len(out.get("primaries") or [])},
                         indent=2))
        return 0
    if action == "list":
        print(json.dumps([{"id": b["id"], "repo": b.get("repo"),
                           "purpose": b.get("purpose"),
                           "updated_at": b.get("updated_at"),
                           "primaries": len(b.get("primaries") or [])}
                          for b in batches.list_batches()], indent=2))
        return 0
    if action in ("show", "evaluate"):
        if not args.id:
            raise SystemExit(f"error: --id is required for batches {action}")
        try:
            b = batches.load_batch(args.id)
        except batches.BatchError as exc:
            raise SystemExit(f"error: {exc}")
        if action == "show":
            print(json.dumps(b, indent=2, ensure_ascii=False))
            return 0
        print(json.dumps(
            batches.evaluate(b, batches.live_map(batches.closure([b]))),
            indent=2, ensure_ascii=False))
        return 0
    raise SystemExit(f"error: unknown batches action '{action}'")


def cmd_serve(args) -> int:
    from openmainty import serve as _serve
    _serve.serve(args.port)
    return 0


def cmd_demo(args) -> int:
    from openmainty import demo as _demo
    if args.demo_action == "seed":
        if not args.home:
            raise SystemExit("error: --home is required")
        info = _demo.seed(args.home)
        print(f"demo home: {info['home']}")
        for name in info["repos"]:
            print(f"repo: {name}")
        for kind, run_id in info["runs"].items():
            print(f"run ({kind}): {run_id}")
        return 0
    raise SystemExit(f"error: unknown demo action '{args.demo_action}'")


def dashboard_plist_text(launcher, port: int, path_env: str, home) -> str:
    """Pure plist renderer (test 10 asserts on this, never a real plist)."""
    label = "dev.openmainty.dashboard"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array><string>{launcher}</string><string>serve</string><string>--port</string><string>{port}</string></array>
<key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_env}</string></dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{home}/dashboard.log</string>
<key>StandardErrorPath</key><string>{home}/dashboard.log</string>
</dict></plist>
"""


def dashboard_launcher() -> Path:
    """Promoted launcher when a valid promotion exists, else dev.

    Returns the UNRESOLVED app/current/openmainty path on purpose: a
    resolved app/<sha> path would pin the dashboard to one promotion
    and force an owner reinstall after every promote. The launcher's
    own resolve() picks the current tree at each (re)start."""
    from openmainty import promote as _promote
    from openmainty import paths as _paths
    cand = _paths.home() / "app" / "current" / "openmainty"
    if _promote.app_info() and cand.is_file():
        return cand
    return Path(__file__).resolve().parent.parent.parent / "openmainty"


def cmd_app(args) -> int:
    import platform as _platform
    import subprocess as _sp
    if _platform.system() != "Darwin":
        raise SystemExit("error: app install is macOS-only (launchd)")
    label = "dev.openmainty.dashboard"
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if args.action == "install":
        import os as _os
        from openmainty import paths as _paths
        launcher = dashboard_launcher()
        # launchd starts with a minimal PATH (no homebrew); freeze the
        # working PATH of this shell so gh/muse/toolchains resolve.
        # Reinstall after toolchain moves.
        path_dirs = [d for d in _os.environ.get("PATH", "").split(":") if d]
        for d in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            if d not in path_dirs:
                path_dirs.append(d)
        path_env = ":".join(path_dirs)
        content = dashboard_plist_text(launcher, args.port, path_env,
                                       _paths.home())
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(content, encoding="utf-8")
        _sp.run(["launchctl", "bootout", f"gui/{__import__('os').getuid()}/{label}"],
                capture_output=True)
        rc = _sp.run(["launchctl", "bootstrap", f"gui/{__import__('os').getuid()}", str(plist)],
                      capture_output=True, text=True)
        if rc.returncode != 0:
            raise SystemExit(f"error: launchctl bootstrap failed: {rc.stderr.strip()[-200:]}")
        print(f"dashboard installed (login-persistent): http://127.0.0.1:{args.port}/")
        return 0
    if args.action == "uninstall":
        _sp.run(["launchctl", "bootout", f"gui/{__import__('os').getuid()}/{label}"],
                capture_output=True)
        if plist.exists():
            plist.unlink()
        print("dashboard uninstalled")
        return 0
    raise SystemExit(f"error: unknown app action '{args.action}'")


def owner_promote_commands(date_str: str = "") -> str:
    """Runnable one-time plist switch (owner-run only; never Muse/Root).

    Absolute paths throughout: launchd does not expand $HOME inside a
    plist, and PlistBuddy edits the installed copies in place. Plists
    are backed up under ~/Documents (never next to LaunchAgents, never
    ~/Desktop) before any edit."""
    import datetime as _dt
    from openmainty import paths as _paths
    home = Path.home()
    day = date_str or _dt.datetime.now().strftime("%Y%m%d")
    backup = home / "Documents/OpenMainty" / f"launchd-backup-{day}"
    agents = home / "Library/LaunchAgents"
    dash = agents / "dev.openmainty.dashboard.plist"
    maint = agents / "dev.openmainty.maintenance.plist"
    launcher = _paths.home() / "app" / "current" / "openmainty"
    return f"""\
one-time plist switch (owner-run only; never by Muse or Root):
  mkdir -p "{backup}"
  cp "{dash}" "{maint}" "{backup}/"
  /usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 {launcher}" "{dash}"
  /usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 {launcher}" "{maint}"
  plutil -lint "{dash}" && plutil -lint "{maint}"
  plutil -p "{dash}" | head -8
  # pre-checks: bootout kills an in-flight tick, so never switch
  # during the owner's cron window or while a tick holds the worker lock.
  test "$(date -u +%M)" != 13 -a "$(date -u +%M)" != 14 -a "$(date -u +%M)" != 15 || \\
    {{ echo "in owner-cron window (:13-:16); wait and retry"; exit 1; }}
  lsof "{_paths.home()}/sched/.worker.flock" 2>/dev/null | grep -q . && \\
    {{ echo "a tick is running; wait and retry"; exit 1; }}
  launchctl bootout gui/$(id -u)/dev.openmainty.dashboard 2>/dev/null; \\
    launchctl bootstrap gui/$(id -u) "{dash}"
  launchctl bootout gui/$(id -u)/dev.openmainty.maintenance; \\
    launchctl bootstrap gui/$(id -u) "{maint}"
verify:
  curl -s http://127.0.0.1:8901/api/app
  sleep 60  # RunAtLoad ticks immediately after bootstrap; cadence follows.
  grep "daemon-tick start" "{_paths.home()}/maintenance.log" | tail -1
  # exact expected line: "app <sha12> daemon-tick start".
  # the glance footer shows the same sha.
rollback (backups back, then restart both):
  cp "{backup}/dev.openmainty.dashboard.plist" "{backup}/dev.openmainty.maintenance.plist" "{agents}/"
  launchctl bootout gui/$(id -u)/dev.openmainty.dashboard 2>/dev/null; \\
    launchctl bootstrap gui/$(id -u) "{dash}"
  launchctl bootout gui/$(id -u)/dev.openmainty.maintenance; \\
    launchctl bootstrap gui/$(id -u) "{maint}\""""


def cmd_gc(args) -> int:
    from openmainty import gc as _gc
    from openmainty import paths as _paths
    if getattr(args, "apply", False):
        return cmd_gc_apply(args)
    report = _gc.dry_run_report(_paths.home(),
                                older_than_days=args.older_than)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"gc --dry-run ({report['home']}, "
          f"{report['older_than_days']}일 이상) — 삭제 없음")
    by_cat: dict = {}
    for item in report["reclaimable"]:
        by_cat.setdefault(item["category"], []).append(item)
    for cat in ("node_modules", "stale_worktree", "cache", "old_app",
                "tmp_scratch", "stale_tmp"):
        items = by_cat.get(cat, [])
        if not items:
            continue
        size = sum(int(i.get("bytes") or 0) for i in items)
        print(f"[{cat}] {len(items)}건 {size / 1024 ** 3:.1f}GB "
              f"({size} bytes)")
        for item in items[:10]:
            print(f"  {item['path']} "
                  f"({item['bytes'] / 1024 ** 2:.0f}MB) {item['note']}")
        if len(items) > 10:
            print(f"  ... 외 {len(items) - 10}건")
    print(f"합계 {report['total_reclaimable_bytes'] / 1024 ** 3:.1f}GB "
          f"({len(report['reclaimable'])}건)")
    if report["needs_review"]:
        print(f"사람 확인 필요 {len(report['needs_review'])}건 "
              f"(삭제 대상 아님):")
        for item in report["needs_review"][:10]:
            print(f"  {item['path']} {item['note']}")
    print("절대 건드리지 않음: " + ", ".join(report["never"]))
    return 0


def cmd_gc_apply(args) -> int:
    """Owner-only delete path (CLI only — no API, no button).

    Requires --category plus --confirm-bytes equal to the FRESH
    category total; anything else refuses with the exact total and
    deletes nothing. Prints the total, every deleted path and every
    skip with its reason, then records one timeline event.
    """
    from openmainty import events as _events
    from openmainty import gc as _gc
    from openmainty import paths as _paths
    from openmainty import repos as _repos
    if not getattr(args, "category", None):
        print("error: --apply에는 --category "
              "<node_modules|cache|old_app|tmp_scratch|stale_tmp> 필요",
              file=sys.stderr)
        return 2
    if getattr(args, "confirm_bytes", None) is None:
        try:
            fresh = _gc.dry_run_report(
                _paths.home(), older_than_days=args.older_than)
        except Exception:  # noqa: BLE001 - total unknown, say so
            fresh = {}
        total = sum(int(i.get("bytes") or 0)
                    for i in (fresh.get("reclaimable") or [])
                    if i.get("category") == args.category)
        print(f"error: --confirm-bytes {total} 필요 "
              f"({args.category} 새로 계산 합계). 삭제 안 함",
              file=sys.stderr)
        return 2
    try:
        receipt = _gc.apply_category(
            _paths.home(), args.category, args.confirm_bytes,
            tmp_root=getattr(args, "tmp_root", None))
    except _gc.ApplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gb = receipt["freed_bytes"] / 1024 ** 3
    print(f"정리 완료 [{receipt['category']}] "
          f"{len(receipt['deleted'])}건 {gb:.1f}GB 확보")
    for d in receipt["deleted"]:
        print(f"  삭제 {d['path']} ({d['bytes'] / 1024 ** 2:.0f}MB)")
    for s in receipt["skipped"]:
        print(f"  건너뜀 {s['path']} — {s['reason']}")
    title = (f"정리 {len(receipt['deleted'])}건 {gb:.1f}GB 확보 "
             f"({receipt['category']})")
    try:
        for name in _repos.list_all() or []:
            _events.append(name, "app-gc", "", title, "", actor="owner")
    except Exception:  # noqa: BLE001 - event never fails cleanup
        pass
    return 0


def cmd_attest(args) -> int:
    """Human attestation of one batch gate (owner or root).

    M24b-F2: only a human at a terminal may attest — a non-TTY
    stdin refuses, and the batch id must be typed back. This stops
    an agent shell from attesting silently. (The API keeps its own
    confirm:true gate.)
    """
    from openmainty import batches as _batches
    if not sys.stdin.isatty():
        print("attest 거부: 대화형 확인이 필요합니다 — 사람이 직접 "
              "실행하세요")
        return 2
    try:
        typed = input(f"확인하려면 묶음 id를 입력하세요 "
                      f"[{args.batch}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("attest 취소됨")
        return 2
    if typed != args.batch:
        print("attest 거부: 묶음 id가 다릅니다 — 사람을 확인해야 함")
        return 2
    log_text = ""
    if getattr(args, "log_file", ""):
        try:
            with open(args.log_file, "rb") as fh:
                log_text = fh.read().decode("utf-8", "ignore")
        except OSError as exc:
            print(f"attest 거부: 로그 파일을 읽을 수 없음: {exc}")
            return 2
    try:
        if getattr(args, "receipt", ""):
            out = _batches.attest_gate(
                args.batch, args.gate, args.attestor, args.note,
                "receipt", receipt_sha=args.receipt)
        elif log_text:
            out = _batches.attest_gate(
                args.batch, args.gate, args.attestor, args.note,
                "direct", log_text=log_text)
        else:
            out = _batches.attest_gate(
                args.batch, args.gate, args.attestor, args.note,
                "na", na_reason=args.na_reason or "")
    except _batches.BatchError as exc:
        print(f"attest 거부: {exc}")
        return 2
    print(f"확인 기록: {out['batch']} {out['gate']} "
          f"({out['attestor']}, {out['mode']})")
    return 0


def cmd_metrics(args) -> int:
    """Honest autonomy metrics (M3.4a, read-only).

    Regenerates every number from this OPENMAINTY_HOME: no cache,
    no network, no writes. Batch hold/waiting state is always
    current; --days windows only the flow counts."""
    from openmainty import metrics as _metrics
    lang = str(getattr(args, "lang", "ko") or "ko")
    if lang.lower() not in ("ko", "en"):
        print(f"metrics 거부: --lang은 ko 또는 en ({lang})")
        return 2
    days = getattr(args, "days", None)
    if days is not None and days < 1:
        print("metrics 거부: --days는 1 이상")
        return 2
    data = _metrics.collect(days=days)
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(_metrics.render_text(data, lang=lang), end="")
    return 0


def cmd_hold(args) -> int:
    """Owner/root hold ("보류") on a batch.

    Same human rule as attest (M24b-F2): only a human at a
    terminal may hold or release — a non-TTY stdin refuses, and
    the batch id must be typed back, so an agent shell can never
    hold silently. Agents (Muse) must never hold.
    """
    from openmainty import batches as _batches
    if not sys.stdin.isatty():
        print("보류 거부: 대화형 확인이 필요합니다 — 사람이 직접 "
              "실행하세요")
        return 2
    try:
        typed = input(f"확인하려면 묶음 id를 입력하세요 "
                      f"[{args.batch}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("보류 취소됨")
        return 2
    if typed != args.batch:
        print("보류 거부: 묶음 id가 다릅니다 — 사람을 확인해야 함")
        return 2
    try:
        out = _batches.hold_batch(
            args.batch, not getattr(args, "release", False),
            args.attestor, reason=getattr(args, "reason", "") or "")
    except _batches.BatchError as exc:
        print(f"보류 거부: {exc}")
        return 2
    if out["hold"]:
        print(f"보류 설정: {out['batch']} ({out['by']}) — "
              f"{out['reason']}")
    else:
        print(f"보류 해제: {out['batch']} ({out['by']}) — 게이트 복원됨")
    return 0


def cmd_release(args) -> int:
    """Owner self-update: test, promote, kickstart, verify."""
    from openmainty import release as _release
    try:
        if getattr(args, "rollback", False):
            out = _release.rollback(to=args.to or "", yes=args.yes)
            print(f"롤백 {out['sha'][:12]} 완료 "
                  f"(이전 {out['previous'][:12] or '없음'} → 앱 확인됨)"
                  if out.get("verified") and not out.get("warning")
                  else f"롤백 {out['sha'][:12]} — "
                  f"{out.get('warning') or '확인 필요'}")
            return 0 if out.get("verified") else 1
        out = _release.release(dry_run=args.dry_run)
        if out.get("dry_run"):
            print("출시 계획 (삭제·변경 없음):")
            for step in out["steps"]:
                print(f"  - {step}")
            return 0
        tail = ""
        if out.get("py39_skipped"):
            tail = f" · {out['py39_skipped']}"
        elif out.get("py39") is not None:
            tail = f" · Py3.9 {out['py39']}건 OK"
        if out.get("verified") and not out.get("warning"):
            print(f"출시 {out['sha'][:12]} 완료 "
                  f"(FULL {out['full']}건 OK{tail} · 앱 확인됨)")
            return 0
        print(f"출시 {out['sha'][:12]} — "
              f"{out.get('warning') or '확인 필요'}")
        return 1
    except _release.ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _plist_argv(path) -> list:
    """Full ProgramArguments of a plist, or [] when unreadable.

    Read-only (plistlib parse, no writes).
    """
    try:
        import plistlib
        data = plistlib.loads(Path(path).read_bytes())
    except Exception:  # noqa: BLE001 - unreadable means "not switched"
        return []
    return [str(a) for a in (data.get("ProgramArguments") or [])]


def _plist_argv0(path) -> str:
    """ProgramArguments[0] of a plist, or "" when unreadable.

    Read-only (plistlib parse, no writes): decides whether the
    owner's launchd already runs the promoted copy.
    """
    argv = _plist_argv(path)
    return argv[0] if argv else ""


def _plist_runs_current(path, launcher) -> bool:
    """True when the plist already runs the promoted copy.

    Direct (argv[0] is the launcher) or via the owner's /bin/bash
    run-agent.sh wrapper whose script execs app/current/openmainty.
    Read-only on the plist and the wrapper; anything unreadable or
    unrecognized means "not switched".
    """
    import os as _os
    argv = _plist_argv(path)
    if not argv:
        return False
    if argv[0] == str(launcher):
        return True
    if argv[0] not in ("bash", "/bin/bash") or len(argv) < 2:
        return False
    if _os.path.basename(argv[1]) != "run-agent.sh":
        return False
    try:
        text = Path(argv[1]).read_text(encoding="utf-8",
                                       errors="replace")
    except Exception:  # noqa: BLE001 - unreadable means "not switched"
        return False
    return "app/current/openmainty" in text


def promote_followup_text(dash, maint, launcher, uid: int) -> str:
    """Post-promote owner text: the full one-time plist switch only
    while a plist still points elsewhere; otherwise a one-line
    dashboard restart. Both plists must run app/current/openmainty —
    directly or via the /bin/bash run-agent.sh wrapper — to skip the
    switch block. Read-only on the plists and the wrapper."""
    if _plist_runs_current(dash, launcher) \
            and _plist_runs_current(maint, launcher):
        return ("launchd가 이미 승격 사본을 실행 중 — 대시보드만 "
                "재시작하세요: "
                f"launchctl kickstart -k gui/{uid}/dev.openmainty.dashboard")
    return owner_promote_commands()


def promote_followup_for_owner(uid: int) -> str:
    """Production wiring: real plist paths and the promoted launcher."""
    from openmainty import paths as _paths
    launcher = str(_paths.home() / "app" / "current" / "openmainty")
    agents = Path.home() / "Library/LaunchAgents"
    return promote_followup_text(
        agents / "dev.openmainty.dashboard.plist",
        agents / "dev.openmainty.maintenance.plist",
        launcher, uid)


def cmd_promote(args) -> int:
    import datetime as _dt
    from openmainty import promote as _promote
    at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if args.rollback is not None:
            out = _promote.rollback(args.rollback or "", at=at)
            print(json.dumps({"ok": True, **out}, indent=2))
            return 0
        if args.prune:
            out = _promote.prune(keep=args.keep, assume_yes=args.yes, at=at)
            print(json.dumps({"ok": True, **out}, indent=2))
            return 0
        if not args.manifest:
            raise SystemExit("error: promote needs --manifest <Root independent-run JSON>")
        try:
            manifest = json.loads(Path(args.manifest).read_text(
                encoding="utf-8"))
        except OSError as exc:
            print(f"error: cannot read manifest: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"error: manifest is not JSON: {exc}", file=sys.stderr)
            return 2
        out = _promote.promote(manifest, at=at)
        print(json.dumps({"ok": True, **out}, indent=2))
        import os as _os
        print(promote_followup_for_owner(_os.getuid()))
        return 0
    except _promote.PromoteError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2


def cmd_external(args) -> int:
    state = store.load(args.run)
    if args.dry_run or not args.approve:
        print(json.dumps(external.preview(state), indent=2))
        return 0
    print(json.dumps(external.apply(state, approval_file=args.approve), indent=2))
    return 0


def _chat_repl(session, profile, executor, workdir) -> int:
    print(f"chat {session['id']} ({session['repo']}#{session.get('issue') or '-'}). "
          f"/quit to end, /show for history.")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/show":
            print(chat.tail_text(session["id"], n=20, chars=8000) or "(empty)")
            continue
        if line.startswith("/"):
            print("commands: /quit /show")
            continue
        try:
            turn = chat.send(session, profile, line, executor, workdir)
        except runner.QuotaHit:
            print(f"waiting: subscription quota hit; resume later with: "
                  f"openmainty chat send --session {session['id']} --message \"...\"")
            return 3
        print(f"muse> {turn['text']}")
    from openmainty.util import utcnow as _utcnow
    session["ended_at"] = _utcnow()
    chat.save(session)
    print(f"session saved: {session['id']}")
    return 0


def cmd_chat(args) -> int:
    if args.action == "list":
        sessions = chat.list_sessions(limit=args.limit)
        if not sessions:
            print("(no chat sessions)")
            return 0
        for s in sessions:
            print(f"{s['id']}  {s.get('repo')}#{s.get('issue')}  "
                  f"turns={s.get('turns', 0)}  wait={s.get('waitReason')}  "
                  f"{s.get('title', '')}")
        return 0
    if args.action == "show":
        session = chat.load(args.session)
        if args.json:
            print(json.dumps({"session": session,
                              "transcript": chat.read_transcript(session["id"])},
                             indent=2, ensure_ascii=False))
            return 0
        print(chat.export_markdown(session))
        return 0
    if args.action == "export":
        session = chat.load(args.session)
        text = chat.export_markdown(session)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"exported {session['id']} -> {args.out}")
        else:
            print(text)
        return 0
    if args.action == "delete":
        session = chat.load(args.session)
        checkout = None
        try:
            checkout = _resolve_checkout(repos.load(session["repo"]))
        except SystemExit:
            pass
        chat.delete(session["id"], checkout)
        print(f"deleted {session['id']}")
        return 0
    # start / send spend subscription quota: subscriber-only gate first.
    billing = chat.require_subscription()
    if args.action == "start":
        profile = repos.load(args.repo)
        checkout = _resolve_checkout(profile)
        session = chat.create(args.repo, args.issue, args.title or "",
                              muse.agent_info(model=getattr(args, "model", None)))
        workdir = chat.ensure_workdir(session, checkout, profile.get("baseBranch", "main"))
        executor = runner.Executor(model=getattr(args, "model", None))
        print(f"chat {session['id']} for {args.repo}#{args.issue or '-'} "
              f"({_home_note()}, {billing})")
        if not args.message:
            return _chat_repl(session, profile, executor, workdir)
        try:
            turn = chat.send(session, profile, args.message, executor, workdir)
        except runner.QuotaHit:
            print(f"waiting: subscription quota hit; resume later with: "
                  f"openmainty chat send --session {session['id']} --message \"...\"")
            return 3
        print(turn["text"])
        return 0
    if args.action == "send":
        session = chat.load(args.session)
        profile = repos.load(session["repo"])
        checkout = _resolve_checkout(profile)
        workdir = chat.ensure_workdir(session, checkout, profile.get("baseBranch", "main"))
        executor = runner.Executor(model=getattr(args, "model", None))
        try:
            turn = chat.send(session, profile, args.message, executor, workdir)
        except runner.QuotaHit:
            print(f"waiting: subscription quota hit; resume later with: "
                  f"openmainty chat send --session {session['id']} --message \"...\"")
            return 3
        print(turn["text"])
        return 0
    raise SystemExit(f"error: unknown chat action '{args.action}'")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openmainty", description="Your AI maintainer for open source.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check env, auth, tools, devices")
    d.add_argument("--live", action="store_true", help="prove subscription headless run (spends quota)")
    d.add_argument("--json", action="store_true")
    d.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    d.set_defaults(fn=cmd_doctor)

    r = sub.add_parser("repo", help="register/link repos")
    rsub = r.add_subparsers(dest="action", required=True)
    rl = rsub.add_parser("list")
    rl.set_defaults(fn=cmd_repo)
    ra = rsub.add_parser("add", help="register a repo (portable profile)")
    ra.add_argument("--name")
    ra.add_argument("--github")
    ra.add_argument("--base-branch", default="main")
    ra.add_argument("--checkout", help="PC-local checkout path (stored in pc.json, not the profile)")
    ra.add_argument("--check", action="append", default=[], help="name=cmd (repeatable)")
    ra.add_argument("--profile", help="import portable profile JSON file")
    ra.set_defaults(fn=cmd_repo)
    rs = rsub.add_parser("show")
    rs.add_argument("--name", required=True)
    rs.add_argument("--export", action="store_true")
    rs.set_defaults(fn=cmd_repo)
    rk = rsub.add_parser("link", help="link a repo to a PC-local checkout")
    rk.add_argument("--name", required=True)
    rk.add_argument("--checkout", required=True)
    rk.set_defaults(fn=cmd_repo)

    pl = sub.add_parser("plan", help="print the stepwise verification plan")
    pl.add_argument("--repo", required=True)
    pl.add_argument("--issue", required=True)
    pl.add_argument("--title", default="")
    pl.add_argument("--body", default="")
    pl.add_argument("--body-file")
    pl.set_defaults(fn=cmd_plan)

    rn = sub.add_parser("run", help="handle one repo+issue end to end (local)")
    rn.add_argument("--repo", required=True)
    rn.add_argument("--issue", required=True)
    rn.add_argument("--title", default="")
    rn.add_argument("--body", default="")
    rn.add_argument("--body-file")
    rn.add_argument("--executor", default="muse", choices=["muse", "echo", "fake"])
    rn.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    rn.add_argument("--allow-duplicate", action="store_true")
    rn.add_argument("--only", default=None, help="comma-separated subset of steps (default: all)")
    rn.set_defaults(fn=cmd_run)

    st = sub.add_parser("status", help="show run state")
    st.add_argument("--run", required=True)
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    rs2 = sub.add_parser("resume", help="resume an interrupted run")
    rs2.add_argument("--run", required=True)
    rs2.add_argument("--executor", default="muse", choices=["muse", "echo", "fake"])
    rs2.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    rs2.add_argument("--only", default=None, help="comma-separated subset of steps (default: all)")
    rs2.set_defaults(fn=cmd_resume)

    an = sub.add_parser("answer", help="answer a run's question (then resume)")
    an.add_argument("--run", required=True)
    an.add_argument("--answer", default=None)
    an.add_argument("--answer-file", default=None)
    an.set_defaults(fn=cmd_answer)

    dp = sub.add_parser("deps", help="check dependency drift vs public registries")
    dp.add_argument("--repo", required=True)
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--check", action="store_true", help="exit 1 on drift/error (for cron)")
    dp.set_defaults(fn=cmd_deps)

    dd = sub.add_parser("deps-discover", help="show real manifest dependencies (no registry)")
    dd.add_argument("--repo", required=True)
    dd.add_argument("--json", action="store_true")
    dd.set_defaults(fn=cmd_deps_discover)

    sc = sub.add_parser("sched", help="persistent maintenance scheduler per repo")
    sc.add_argument("sched_action",
                    choices=["status", "enable", "disable", "scan", "tick",
                             "daemon-tick", "auto", "run-due", "cancel",
                             "set-watch", "reconcile", "repair-bindings"])
    sc.add_argument("--apply", action="store_true",
                    help="repair-bindings: perform the planned repair under "
                         "lock (default dry-run)")
    sc.add_argument("--fingerprint", default=None,
                    help="repair-bindings --apply: CAS against the dry-run "
                         "fingerprint")
    sc.add_argument("--repo", default=None)
    sc.add_argument("--reason", default=None,
                    help="set-watch: why no change is the settled outcome; "
                         "reconcile: required correction reason")
    sc.add_argument("--evidence", default=None,
                    help="set-watch: concrete observed evidence")
    sc.add_argument("--trigger", default=None,
                    help="set-watch: what upstream change reopens this")
    sc.add_argument("--inputs", default=None,
                    help="set-watch: baseline watched inputs as JSON")
    sc.add_argument("--reopen", default=None,
                    help="set-watch: comma-separated steps to reopen to pending")
    sc.add_argument("--interval", type=int, default=1800,
                    help="scan interval seconds for enable (default 1800)")
    sc.add_argument("--mode", default="on",
                    help="auto execute mode: on|off (default on)")
    sc.add_argument("--key", default=None, help="queue key for cancel/run-due/reconcile")
    sc.add_argument("--run", default=None,
                    help="reconcile: exact run id linked to --key")
    sc.add_argument("--executor", default="fake", choices=["muse", "echo", "fake"])
    sc.add_argument("--model", default=None)
    sc.set_defaults(fn=cmd_sched)

    wt = sub.add_parser("watch", help="check drift and issues; optionally open runs")
    wt.add_argument("--repo", required=True)
    wt.add_argument("--start-run", action="store_true", help="open/resume runs (spends quota)")
    wt.add_argument("--no-issues", action="store_true", help="skip the open-issue scan")
    wt.add_argument("--executor", default="muse", choices=["muse", "echo", "fake"])
    wt.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    wt.add_argument("--only", default=None)
    wt.set_defaults(fn=cmd_watch)

    bt = sub.add_parser("batches", help="review batches: import/list/show (trusted local only)")
    bt.add_argument("batches_action", choices=["import", "list", "show", "evaluate"])
    bt.add_argument("--file", default=None,
                    help="import: batch payload JSON file")
    bt.add_argument("--id", default=None,
                    help="show/evaluate: batch id")
    bt.set_defaults(fn=cmd_batches)

    sv = sub.add_parser("serve", help="run the read-only local dashboard")
    sv.add_argument("--port", type=int, default=8901)
    sv.set_defaults(fn=cmd_serve)

    dm = sub.add_parser("demo", help="offline demo helpers (synthetic data only)")
    dsub = dm.add_subparsers(dest="demo_action", required=True)
    ds = dsub.add_parser("seed", help="seed an empty dir as a demo home (refuses non-empty)")
    ds.add_argument("--home", required=True)
    ds.set_defaults(fn=cmd_demo)

    ap = sub.add_parser("app", help="install/uninstall the login-persistent dashboard (macOS)")
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("--port", type=int, default=8901)
    ap.set_defaults(fn=cmd_app)

    pm = sub.add_parser("promote", help="promote a frozen tree to ~/.openmainty/app/ (Root-run)")
    pm.add_argument("--manifest", default=None, help="Root independent-run JSON with sources_sha256")
    pm.add_argument("--rollback", nargs="?", const="", default=None, help="repoint current (optional sha)")
    pm.add_argument("--prune", action="store_true", help="delete old promoted trees (needs --yes)")
    pm.add_argument("--yes", action="store_true", help="confirm prune")
    pm.add_argument("--keep", type=int, default=3, help="promoted trees to keep")
    pm.set_defaults(fn=cmd_promote)

    gc = sub.add_parser("gc", help="report regenerable weight (dry-run default; --apply deletes one category)")
    _gc_mode = gc.add_mutually_exclusive_group(required=True)
    _gc_mode.add_argument("--dry-run", action="store_true", help="print the report; deletes nothing")
    _gc_mode.add_argument("--apply", action="store_true", help="owner only: delete one category (needs --category + --confirm-bytes)")
    gc.add_argument("--category", default=None, help="node_modules|cache|old_app|tmp_scratch|stale_tmp (stale_worktree stays report-only)")
    gc.add_argument("--confirm-bytes", type=int, default=None, help="must equal the fresh category total")
    gc.add_argument("--older-than", type=int, default=7, help="stale-worktree age in days")
    gc.add_argument("--json", action="store_true", help="emit the raw report JSON")
    gc.set_defaults(fn=cmd_gc)

    at = sub.add_parser("attest", help="human attestation of one batch gate (owner or root)")
    at.add_argument("--batch", required=True, help="batch id")
    at.add_argument("--gate", required=True, help="gate key from the batch policy")
    at.add_argument("--as", dest="attestor", required=True, help="attestor name: owner, root, or a name (never empty, never worker)")
    at.add_argument("--note", required=True, help="one line: what was checked")
    mode = at.add_mutually_exclusive_group(required=True)
    mode.add_argument("--receipt", default="", help="worker receipt sha to review (receipt mode)")
    mode.add_argument("--log-file", default="", help="log file to store as evidence (direct mode)")
    mode.add_argument("--na", default="", dest="na_reason", help="reason the gate does not apply (N/A mode)")
    at.set_defaults(fn=cmd_attest)

    mt = sub.add_parser("metrics", help="honest autonomy metrics from this home (read-only; regenerates every number)")
    mt.add_argument("--json", action="store_true", help="emit the raw report JSON")
    mt.add_argument("--days", type=int, default=None, help="window flow counts to the last N days (batch hold/waiting stays current)")
    mt.add_argument("--lang", default="ko", help="ko (default) or en")
    mt.set_defaults(fn=cmd_metrics)

    hd = sub.add_parser("hold", help="owner/root hold (or release) on a batch")
    hd.add_argument("--batch", required=True, help="batch id")
    hd.add_argument("--as", dest="attestor", required=True, help="attestor name: owner or root (never worker)")
    hd.add_argument("--reason", default="", help="one line: why it waits (required to hold)")
    hd.add_argument("--release", action="store_true", help="release the hold and restore the gates")
    hd.set_defaults(fn=cmd_hold)

    rl = sub.add_parser("release", help="owner self-update: test, promote, kickstart, verify (refuse-first)")
    rl.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    rl.add_argument("--rollback", action="store_true", help="repoint current at the previous (or --to) sha (needs --yes)")
    rl.add_argument("--to", default="", help="promotion sha to roll back to (default: previous)")
    rl.add_argument("--yes", action="store_true", help="confirm the dashboard restart")
    rl.set_defaults(fn=cmd_release)

    ex = sub.add_parser("external", help="preview (dry-run) external actions")
    ex.add_argument("--run", required=True)
    ex.add_argument("--dry-run", action="store_true", default=True)
    ex.add_argument("--approve", default=None, help="approval file (v1 still only prepares payloads)")
    ex.set_defaults(fn=cmd_external)

    ct = sub.add_parser("chat", help="talk to the subscribed worker inside openmainty (sessions saved)")
    csub = ct.add_subparsers(dest="action", required=True)
    cs = csub.add_parser("start", help="start a session (REPL when --message is absent)")
    cs.add_argument("--repo", required=True)
    cs.add_argument("--issue", default="")
    cs.add_argument("--title", default="")
    cs.add_argument("--message", default=None)
    cs.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    cs.set_defaults(fn=cmd_chat)
    cd = csub.add_parser("send", help="send one message to a session")
    cd.add_argument("--session", required=True)
    cd.add_argument("--message", required=True)
    cd.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    cd.set_defaults(fn=cmd_chat)
    cl = csub.add_parser("list", help="list saved sessions")
    cl.add_argument("--limit", type=int, default=20)
    cl.set_defaults(fn=cmd_chat)
    cw = csub.add_parser("show", help="show a session transcript")
    cw.add_argument("--session", required=True)
    cw.add_argument("--json", action="store_true")
    cw.set_defaults(fn=cmd_chat)
    ce = csub.add_parser("export", help="export transcript as markdown (feeds run --body-file)")
    ce.add_argument("--session", required=True)
    ce.add_argument("--out", default=None)
    ce.set_defaults(fn=cmd_chat)
    cx = csub.add_parser("delete", help="delete a saved session")
    cx.add_argument("--session", required=True)
    cx.set_defaults(fn=cmd_chat)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
