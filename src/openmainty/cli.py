"""openmainty CLI: doctor, repo, plan, run, status, resume, external."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openmainty import __version__, deps, doctor, external, muse, pcconfig, planner, repos, runner, store  # noqa: E402
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


def _start_run(args, executor) -> int:
    profile = repos.load(args.repo)
    dup = store.active_for(args.repo, args.issue)
    if dup and not args.allow_duplicate:
        print(f"refusing: active local run {dup} already handles {args.repo}#{args.issue} "
              f"(local lock only; use --allow-duplicate to override)")
        return 2
    checkout = _resolve_checkout(profile)
    base, _ = runner.git_rev(checkout, profile.get("baseBranch", "main"))
    state = store.create(args.repo, args.issue, args.title or "", base,
                         muse.agent_info(model=getattr(args, "model", None)))
    store.claim(args.repo, args.issue, state["id"])
    body = _issue_body(args)
    plan = planner.plan_for(profile, args.issue, args.title or "", body)
    (store.run_dir(state["id"]) / "plan.md").write_text(plan, encoding="utf-8")
    state["artifacts"]["plan"] = "plan.md"
    store.save(state)
    print(f"run {state['id']} for {args.repo}#{args.issue} { _home_note()}")
    try:
        ok = runner.execute(state, profile, executor, checkout, body=body,
                            only=_parse_only(args.only))
    except runner.QuotaHit:
        print(f"waiting: subscription quota hit; resume later with: openmainty resume --run {state['id']}")
        return 3
    if ok:
        store.release(args.repo, args.issue, state["id"])
    print(f"run {state['id']}: {'done' if ok else 'stopped (see status)'}")
    return 0 if ok else 1


def cmd_run(args) -> int:
    return _start_run(args, runner.make_executor(args.executor, model=getattr(args, "model", None)))


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


def cmd_resume(args) -> int:
    state = store.load(args.run)
    profile = repos.load(state["repo"])
    checkout = _resolve_checkout(profile)
    if state.get("waitReason"):
        from openmainty.util import utcnow as _utcnow
        state.setdefault("interventions", []).append(
            {"at": _utcnow(), "kind": "human-resume",
             "detail": f"resumed after: {state['waitReason']}"})
        state["waitReason"] = None
        store.save(state)
    try:
        ok = runner.execute(state, profile,
                            runner.make_executor(args.executor, model=getattr(args, "model", None)),
                            checkout, only=_parse_only(args.only))
    except runner.QuotaHit:
        print(f"waiting: subscription quota hit; resume later with: openmainty resume --run {state['id']}")
        return 3
    if ok:
        store.release(state["repo"], state["issue"], state["id"])
    print(f"run {state['id']}: {'done' if ok else 'stopped (see status)'}")
    return 0 if ok else 1


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
    """Open GitHub issues via gh (repo integration, not a model API)."""
    from openmainty.util import run_cmd
    code, out, err = run_cmd(
        ["gh", "issue", "list", "--repo", github, "--state", "open",
         "--limit", str(limit), "--json", "number,title"])
    if code != 0:
        print(f"  issue scan skipped: gh failed ({err.strip()[-120:]})")
        return []
    try:
        return json.loads(out or "[]")
    except ValueError:
        print("  issue scan skipped: gh returned non-JSON")
        return []


def _issue_body(github: str, number) -> str:
    from openmainty.util import run_cmd
    code, out, _ = run_cmd(
        ["gh", "issue", "view", str(number), "--repo", github,
         "--json", "body", "--jq", ".body"])
    return out if code == 0 else ""


def decide_issue(repo: str, issue: str) -> str:
    """start a new run, resume the latest, or skip. Pure local-state rule."""
    prev = store.latest_run(repo, issue)
    if prev is None:
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


def cmd_watch(args) -> int:
    import argparse as _ap
    profile = repos.load(args.repo)
    checkout = _resolve_checkout(profile)
    code = 0
    # 1. dependency drift -> one batched fix run
    rows = deps.check(profile, checkout)
    drifted = [r for r in rows if r["drift"]]
    errors = [r for r in rows if r["error"]]
    for row in errors:
        print(f"  {row['name']}: ERROR {row['error']} (skipped)")
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
        rc = _start_run(ns, runner.make_executor(args.executor, model=getattr(args, "model", None)))
        code = code or rc
    if drifted and not args.start_run:
        print(f"{len(drifted)} drifted (1 batched run); re-run with --start-run to open it")
    # 2. open issues -> new or resumed runs (full flow, drafts only)
    if not args.no_issues:
        for item in _open_issues(profile["github"]):
            number = str(item["number"])
            decision = decide_issue(args.repo, number)
            print(f"  issue #{number} {item.get('title', '')}: {decision}")
            if decision == "start" and args.start_run:
                ns = _ap.Namespace(
                    repo=args.repo, issue=number, title=item.get("title", ""),
                    body=_issue_body(profile["github"], number), body_file=None,
                    allow_duplicate=False, only=None, executor=args.executor)
                code = code or _start_run(ns, runner.make_executor(args.executor, model=getattr(args, "model", None)))
            elif decision == "resume" and args.start_run:
                prev = store.latest_run(args.repo, number)
                code = code or cmd_resume(_ap.Namespace(
                    run=prev["id"], executor=args.executor, only=None))
    return code


def cmd_serve(args) -> int:
    from openmainty import serve as _serve
    _serve.serve(args.port)
    return 0


def cmd_app(args) -> int:
    import platform as _platform
    import subprocess as _sp
    if _platform.system() != "Darwin":
        raise SystemExit("error: app install is macOS-only (launchd)")
    label = "dev.openmainty.dashboard"
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if args.action == "install":
        from openmainty import paths as _paths
        launcher = Path(__file__).resolve().parent.parent.parent / "openmainty"
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array><string>{launcher}</string><string>serve</string><string>--port</string><string>{args.port}</string></array>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>{_paths.home()}/dashboard.log</string>
<key>StandardErrorPath</key><string>{_paths.home()}/dashboard.log</string>
</dict></plist>
"""
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


def cmd_external(args) -> int:
    state = store.load(args.run)
    if args.dry_run or not args.approve:
        print(json.dumps(external.preview(state), indent=2))
        return 0
    print(json.dumps(external.apply(state, approval_file=args.approve), indent=2))
    return 0


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

    dp = sub.add_parser("deps", help="check dependency drift vs public registries")
    dp.add_argument("--repo", required=True)
    dp.add_argument("--json", action="store_true")
    dp.add_argument("--check", action="store_true", help="exit 1 on drift/error (for cron)")
    dp.set_defaults(fn=cmd_deps)

    wt = sub.add_parser("watch", help="check drift and issues; optionally open runs")
    wt.add_argument("--repo", required=True)
    wt.add_argument("--start-run", action="store_true", help="open/resume runs (spends quota)")
    wt.add_argument("--no-issues", action="store_true", help="skip the open-issue scan")
    wt.add_argument("--executor", default="muse", choices=["muse", "echo", "fake"])
    wt.add_argument("--model", default=None, help="muse model id (default: muse-spark-1.3)")
    wt.add_argument("--only", default=None)
    wt.set_defaults(fn=cmd_watch)

    sv = sub.add_parser("serve", help="run the read-only local dashboard")
    sv.add_argument("--port", type=int, default=8901)
    sv.set_defaults(fn=cmd_serve)

    ap = sub.add_parser("app", help="install/uninstall the login-persistent dashboard (macOS)")
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("--port", type=int, default=8901)
    ap.set_defaults(fn=cmd_app)

    ex = sub.add_parser("external", help="preview (dry-run) external actions")
    ex.add_argument("--run", required=True)
    ex.add_argument("--dry-run", action="store_true", default=True)
    ex.add_argument("--approve", default=None, help="approval file (v1 still only prepares payloads)")
    ex.set_defaults(fn=cmd_external)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
