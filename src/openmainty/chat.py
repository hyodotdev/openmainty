"""In-program conversations with the subscribed Muse worker.

`openmainty chat` keeps maintenance talk inside OpenMainty: every turn runs
through the subscription worker (`muse exec`, contributor on) and the full
transcript is saved under <home>/sessions/<id>/, so a later run can reuse
it via `chat export` + `run --body-file`.

Subscriber-only: chat refuses API-key billing. A set META_API_KEY, or a
missing `muse` CLI (no subscription login possible), means no chat. There
is deliberately no --executor flag here: echo/fake exist only on
run/resume/watch for cost-free verification, and tests inject them at the
module level.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path

from . import muse, runner
from . import paths
from . import store
from .util import pc_id, read_json, run_cmd, utcnow, write_json


def new_session_id() -> str:
    return "chat-" + secrets.token_hex(4)


# F1: session ids are exactly what new_session_id makes — anything
# else (traversal, absolute, empty) is refused before any path join
# (Path / "/abs" == "/abs" would otherwise escape the sessions dir).
_SESSION_ID_RE = re.compile(r"\Achat-[0-9a-f]{8}\Z")


def check_session_id(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not _SESSION_ID_RE.match(sid):
        raise SystemExit(f"error: invalid chat session id {sid!r}")
    return sid


def session_dir(session_id: str) -> Path:
    return paths.sessions_dir() / check_session_id(session_id)


def session_file(session_id: str) -> Path:
    return session_dir(session_id) / "session.json"


def transcript_file(session_id: str) -> Path:
    return session_dir(session_id) / "transcript.jsonl"


def subscription_error():
    """None when subscriber access is allowed, else the reason."""
    if muse.which() is None:
        return ("chat needs the `muse` CLI with a subscription login "
                "(`muse login`); CLI not found.")
    if os.environ.get("META_API_KEY"):
        return ("chat is subscription-only, but META_API_KEY is set "
                "(API-key billing takes priority over login). "
                "Unset it and use `muse login` instead.")
    return None


def require_subscription() -> str:
    """Enforce subscriber-only access. Returns the billing path description."""
    err = subscription_error()
    if err:
        raise SystemExit(f"error: {err}")
    return muse.billing_path()


def create(repo: str, issue="", title="", worker=None) -> dict:
    session = {
        "schema": 1,
        "id": new_session_id(),
        "repo": repo,
        "issue": str(issue or ""),
        "title": title or "",
        "pc": pc_id(),
        "worker": worker or {},
        "created_at": utcnow(),
        "turns": 0,
        "waitReason": None,
        "workdir_mode": None,
    }
    session_dir(session["id"]).mkdir(parents=True, exist_ok=True)
    save(session)
    return session


def load(session_id: str) -> dict:
    data = read_json(session_file(session_id), default=None)
    if data is None:
        raise SystemExit(f"error: unknown chat session '{session_id}'")
    return data


def save(session: dict) -> None:
    write_json(session_file(session["id"]), session)


def delete(session_id: str, checkout=None) -> dict:
    """Remove a session dir; unregister its git worktree first when possible."""
    sid = check_session_id(session_id)
    session = load(sid)
    if str(session.get("id") or "") != sid:
        raise SystemExit(
            f"error: chat session id mismatch (requested {sid!r})")
    sdir = session_dir(sid)
    # Containment before any rmtree: the resolved target must stay
    # inside the sessions dir, even if a stored id ever surprises us.
    try:
        base = paths.sessions_dir().resolve()
        target = sdir.resolve()
        target.relative_to(base)
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"error: chat session dir escapes its home ({exc})") from exc
    work = sdir / "work"
    if checkout is not None and work.is_dir():
        code, _, _ = run_cmd(
            ["git", "-C", str(checkout), "worktree", "remove", "--force", str(work)])
        if code != 0:
            run_cmd(["git", "-C", str(checkout), "worktree", "prune"])
    shutil.rmtree(target, ignore_errors=True)
    return session


def read_transcript(session_id: str) -> list:
    path = transcript_file(session_id)
    if not path.is_file():
        return []
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            turns.append(json.loads(line))
        except ValueError:
            continue
    return turns


def append_turn(session: dict, role: str, text: str, usage=None) -> dict:
    turn = {"at": utcnow(), "role": role, "text": text}
    if usage is not None:
        turn["usage"] = usage
    path = transcript_file(session["id"])
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
    session["turns"] = session.get("turns", 0) + 1
    save(session)
    return turn


STEP_KO = {"triage": "분류", "reproduce": "재현", "analyze": "분석", "fix": "수정",
           "review": "리뷰", "e2e": "검증", "release": "초안", "announce": "알림"}
STATUS_KO = {"done": "완료", "failed": "실패", "blocked": "중단", "running": "진행 중",
             "pending": "대기"}


def _note_ko(note: str) -> str:
    n = (note or "").strip()
    if n == "muse worker exit 0 with expected markers":
        return "정상 완료"
    if n.startswith("reviewer found no VALID findings"):
        return "지적 없음"
    if n.startswith("fake executor exit 0"):
        return "통과(모의)"
    if "check(s) passed" in n:
        return "검사 통과"
    if n.startswith("drafts written"):
        return "초안 작성됨(미게시)"
    if n.startswith("worker failed"):
        return "작업자 실패(" + n[len("worker failed"):].strip(" ()") + ")"
    if n.startswith("asked:"):
        return "질문: " + n[len("asked:"):].strip()
    if n.startswith("fix step changed no files"):
        return "변경 없음"
    if n.startswith("no source change: settled as external-watch"):
        return "외부 대기(변경 없음 · 확정)"
    if n.startswith("apply exited 0 but VALID findings remain unaddressed"):
        return "리뷰 지적 미반영"
    if n.startswith("environment failure:"):
        return "환경 실패(코드 문제 아님)"
    return n


def session_for_run(run_id: str):
    for s in list_sessions(limit=500):
        if s.get("run_id") == run_id:
            return s
    return None


def _diffstat_files(rdir: Path, step: str = "fix") -> tuple:
    """(changed file list, total line) from a recorded diffstat, or ([], "")."""
    p = rdir / f"{step}.diffstat.txt"
    if not p.is_file():
        return [], ""
    text = p.read_text(encoding="utf-8")
    files = [l.split("|")[0].strip() for l in text.splitlines() if "|" in l]
    total = next((l.strip() for l in text.splitlines()
                  if re.search(r"\d+ files? changed", l)), "")
    return files, total


def _step_detail(state: dict, step: str) -> str:
    """One substantive line about what a step did (files, verdict, drafts)."""
    rdir = store.run_dir(state["id"])
    if step in ("fix", "review"):
        files, total = _diffstat_files(rdir, step)
        if files:
            head = ", ".join(files[:4]) + (" 외" if len(files) > 4 else "")
            return f"{head} ({total})" if total else head
    if step in ("triage", "reproduce", "analyze"):
        p = rdir / f"{step}.log.txt"
        if p.is_file():
            lines = [l.strip() for l in
                     runner.human_text(p.read_text(encoding="utf-8")).splitlines()
                     if l.strip()][:2]
            if lines:
                return " / ".join(lines)[:220]
    if step in ("release", "announce"):
        names = [v for v in (state.get("artifacts") or {}).values() if v]
        if names:
            return "초안: " + ", ".join(sorted(set(names))[:6])
    return ""


def sync_run_session(state: dict):
    """Mirror run progress into a repo-grouped chat session (quota-free).

    Called after every executed step and at terminal states. Idempotent:
    only newly finished steps and new waits append turns.
    """
    steps = state.get("steps") or {}
    session = session_for_run(state["id"])
    if session is None:
        session = create(state["repo"], state.get("issue", ""),
                         f"유지보수 {(state.get('title') or '').strip()}".strip())
        session["run_id"] = state["id"]
        session["synced_steps"] = {}
        save(session)
        append_turn(session, "assistant",
                    f"유지보수 시작 — 실행 {state['id']}\n단계별로 진행 상황을 여기에 기록합니다.")
    synced = session.get("synced_steps") or {}
    lines = []
    for step in store.STEP_ORDER:
        entry = steps.get(step) or {}
        status = entry.get("status")
        if status in ("done", "failed", "blocked") and synced.get(step) != status:
            note = _note_ko(entry.get("note"))
            detail = _step_detail(state, step) if status == "done" else ""
            lines.append(f"**{STEP_KO.get(step, step)}** {STATUS_KO.get(status, status)}"
                         + (f" — {detail[:220]}" if detail else
                            (f" — {note[:160]}" if note else "")))
            synced[step] = status
    wait = state.get("waitReason") or ""
    if wait.startswith("question:") and synced.get("wait") != wait:
        lines.append("**질문** " + wait[len("question:"):].strip()
                     + "\n→ 실행 기록에서 답변하면 이어서 진행합니다.")
        synced["wait"] = wait
    if all((steps.get(s) or {}).get("status") == "done" for s in store.STEP_ORDER) \
            and not synced.get("wrapped"):
        lines.append("유지보수 완료 — 초안은 실행 기록에서 확인하세요.")
        synced["wrapped"] = True
    if lines:
        append_turn(session, "assistant", "\n\n".join(lines))
    session["synced_steps"] = synced
    save(session)
    return session


DRAFT_KIND_KO = {"pr.md": "PR 본문", "release_notes.md": "릴리스 노트",
                 "plan.md": "계획", "medium.md": "공지(Medium)",
                 "discord.md": "공지(Discord)", "slack.md": "공지(Slack)"}
ISSUE_REF_RE = re.compile(r"#(\d+)")


def run_summary(session: dict):
    """At-a-glance card for run-backed sessions. Quota-free: local files only."""
    run_id = session.get("run_id")
    if not run_id:
        return None
    try:
        state = store.load(run_id)
    except SystemExit:
        return None
    steps = state.get("steps") or {}
    st = lambda s: (steps.get(s) or {}).get("status")  # noqa: E731
    wait = state.get("waitReason") or ""
    if wait.startswith("question:"):
        status = "waiting"
    elif all(st(s) == "done" for s in store.STEP_ORDER):
        status = "done"
    elif any(st(s) == "failed" for s in store.STEP_ORDER):
        status = "failed"
    elif any(st(s) == "running" for s in store.STEP_ORDER):
        status = "active"
    else:
        status = "partial"
    rdir = store.run_dir(run_id)
    files, total = _diffstat_files(rdir)
    drafts = []
    names = sorted(set((state.get("artifacts") or {}).values()),
                   key=lambda n: ({"pr.md": 0, "release_notes.md": 1,
                                   "plan.md": 2}.get(n, 3), n))
    for name in names:
        if name and (rdir / name).is_file():
            drafts.append({"name": name, "kind": DRAFT_KIND_KO.get(name, "초안")})
    repo = session.get("repo") or state.get("repo") or ""
    self_issue = str(session.get("issue") or state.get("issue") or "")
    seen = {self_issue}
    related = []

    def scan(text, via):
        for num in ISSUE_REF_RE.findall(text or ""):
            if num in seen:
                continue
            seen.add(num)
            related.append({"issue": num, "via": via})

    for step in ("triage", "analyze"):
        p = rdir / f"{step}.log.txt"
        if p.is_file():
            scan(runner.human_text(p.read_text(encoding="utf-8")), STEP_KO[step])
    for d in drafts:
        try:
            scan((rdir / d["name"]).read_text(encoding="utf-8"), "초안")
        except OSError:
            pass
    if self_issue:
        for p in sorted(paths.home().glob(f"drafts/*-{self_issue}.md")):
            try:
                scan(p.read_text(encoding="utf-8"), "답글 초안")
            except OSError:
                pass
    peers = [s for s in list_sessions(limit=500) if s.get("repo") == repo]
    _link_peers(peers, related)
    github = _repo_github(repo)
    e2e = steps.get("e2e") or {}
    return {
        "run_id": run_id, "file_run": run_id, "issue": self_issue,
        "title": state.get("title") or session.get("title") or "",
        "status": status,
        "question": wait[len("question:"):].strip() if status == "waiting" else "",
        "verdict": _step_detail(state, "triage") or _step_detail(state, "analyze"),
        "files": files[:12], "files_total": total,
        "checks": _note_ko(e2e.get("note")) if st("e2e") in store.TERMINAL else "",
        "drafts": drafts, "related": related[:6],
        "siblings": [{"id": s["id"], "title": s.get("title", "")} for s in peers
                     if str(s.get("issue")) == self_issue and s["id"] != session["id"]],
        "github": github, "base": (state.get("baseCommit") or state.get("base") or "")[:12],
    }


def _link_peers(peers: list, related: list) -> None:
    by_issue = {}
    for s in peers:
        if s.get("issue"):
            by_issue.setdefault(str(s["issue"]), s)
    for r in related:
        peer = by_issue.get(r["issue"])
        r["session_id"] = peer["id"] if peer else None
        r["title"] = (peer.get("title") or "") if peer else ""


def _repo_github(repo: str) -> str:
    try:
        from . import repos
        return repos.load(repo).get("github", "")
    except SystemExit:
        return ""


def reply_summary(session: dict):
    """At-a-glance card for reply-draft sessions (no run). Quota-free."""
    rel = session.get("draft_path")
    if not rel:
        return None
    p = paths.home() / "drafts" / rel
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    self_issue = str(session.get("issue") or "")
    seen = {self_issue}
    related = []
    for num in ISSUE_REF_RE.findall(text):
        if num not in seen:
            seen.add(num)
            related.append({"issue": num, "via": "답글 초안"})
    repo = session.get("repo") or ""
    _link_peers([s for s in list_sessions(limit=500) if s.get("repo") == repo],
                related)
    return {
        "run_id": "", "file_run": "_drafts", "issue": self_issue,
        "title": session.get("title") or "", "status": "draft",
        "question": "", "verdict": "이슈 답글 초안 — 게시 승인 대기 중(미게시)",
        "files": [], "files_total": "", "checks": "",
        "drafts": [{"name": rel, "kind": "답글 초안"}],
        "related": related[:6], "siblings": [],
        "github": _repo_github(repo), "base": "",
    }


def session_card(session: dict):
    """Summary card for run-backed or reply-draft sessions, else None."""
    if session.get("run_id"):
        return run_summary(session)
    if session.get("draft_path"):
        return reply_summary(session)
    return None


def reply_session_for(repo: str, issue: str):
    """Latest reply-draft session for repo+issue, or None."""
    return next((s for s in list_sessions(limit=500)
                 if s.get("repo") == repo and str(s.get("issue")) == str(issue)
                 and s.get("draft_path") and not s.get("run_id")), None)


def sync_reply_session(repo: str, issue: str, title: str, draft_rel: str):
    """Mirror a reply draft file into a repo-grouped session (quota-free).

    Idempotent: re-syncs only when the draft file changed.
    """
    import hashlib
    text = (paths.home() / "drafts" / draft_rel).read_text(encoding="utf-8")
    digest = hashlib.sha1(text.encode()).hexdigest()[:12]
    session = reply_session_for(repo, issue)
    if session is None:
        session = create(repo, issue, title)
    if session.get("draft_hash") == digest and session.get("draft_path") == draft_rel:
        return session
    try:
        transcript_file(session["id"]).unlink()
    except OSError:
        pass
    session["turns"] = 0
    session["draft_path"] = draft_rel
    session["draft_hash"] = digest
    save(session)
    append_turn(session, "assistant", "답글 초안 — 게시 승인 대기 중(미게시)\n\n" + text)
    return session


def tail_text(session_id: str, n: int = 6, chars: int = 4000) -> str:
    turns = read_transcript(session_id)[-n:]
    lines = [f"{t.get('role', '?')}: {(t.get('text') or '')[:1200]}" for t in turns]
    return "\n".join(lines)[-chars:]


def ensure_workdir(session: dict, checkout: Path, base_branch: str) -> Path:
    """Isolated workspace for chat turns; the linked checkout is never written."""
    work = session_dir(session["id"]) / "work"
    if work.exists():
        return work
    mode = "copy"
    if (checkout / ".git").is_dir():
        base, _ = runner.git_rev(checkout, base_branch)
        # M2.9b: hook-free like every other app git call — a
        # post-checkout hook would run outside any sandbox.
        code, _, _ = run_cmd(
            ["git", "-c", "core.hooksPath=/dev/null",
             "-C", str(checkout), "worktree", "add", "--detach",
             str(work), base])
        if code == 0:
            mode = "worktree"
    if mode == "copy":
        shutil.copytree(checkout, work, ignore=shutil.ignore_patterns(".git"))
    session["workdir_mode"] = mode
    save(session)
    return work


def prompt_for(profile: dict, session: dict, message: str) -> str:
    checks = ", ".join(c.get("name", "?") for c in profile.get("checks", [])) or "none"
    knowledge = ", ".join(profile.get("knowledge", [])) or "none"
    issue = session.get("issue") or "none linked"
    history = tail_text(session["id"]) or "(first turn)"
    return (
        f"You are the subscribed Muse maintainer for {profile.get('github')} "
        f"(base branch {profile.get('baseBranch', 'main')}).\n"
        f"Linked issue: {issue}. Checks: {checks}. Knowledge: {knowledge}.\n"
        "House rules: answer from the checkout at your workspace; never post, "
        "merge, deploy, or send channel messages; propose drafts, not sends.\n"
        f"Conversation so far:\n{history}\n\nUser: {message}"
    )


def plan_prompt_for(profile: dict, diagnosis: dict) -> str:
    """Autonomous-maintenance plan prompt from a quota-free diagnosis."""
    drift = diagnosis.get("drift") or []
    issues = diagnosis.get("issues") or []
    decision_ko = {"start": "새로 시작", "resume": "이어하기",
                   "skip-waiting": "대기 중", "skip-active": "진행 중",
                   "skip-done": "완료됨", "skip-draft": "초안 대기"}
    if drift:
        drift_txt = "\n".join(
            f"- {r.get('name')}: {r.get('pinned')} → {r.get('latest')}" for r in drift)
    else:
        drift_txt = "(없음)"
    if issues:
        issue_txt = "\n".join(
            f"- #{i.get('number')} {i.get('title')} "
            f"({decision_ko.get(i.get('decision'), i.get('decision'))})" for i in issues)
    else:
        issue_txt = "(열린 이슈 없음)"
    return (
        f"You are the subscribed Muse maintainer for {profile.get('github')} "
        f"(base branch {profile.get('baseBranch', 'main')}).\n"
        "House rules: answer from the checkout at your workspace; never post, "
        "merge, deploy, or send channel messages; propose drafts, not sends.\n"
        "Write the autonomous maintenance plan in Korean from this diagnosis "
        "(nothing is applied yet):\n"
        f"Dependency drift:\n{drift_txt}\nOpen issues:\n{issue_txt}\n"
        "Format: numbered items in execution order, each with what and why in "
        "one or two lines. If there is genuinely nothing to do, say so in one "
        "line. End with exactly one line: 승인하면 자동으로 실행합니다."
    )


def _text_via_export(muse_session_id: str, dest_dir: Path):
    """Local `muse export` read (no quota). Returns text or None."""
    if not muse_session_id:
        return None
    dest = dest_dir / "export.json"
    code, _, _ = run_cmd(["muse", "export", "--session", muse_session_id,
                          "--out", str(dest)])
    if code != 0:
        return None
    try:
        doc = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    texts = []
    for msg in doc.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() not in ("assistant", "model"):
            continue
        val = msg.get("text", msg.get("content", msg.get("message")))
        if isinstance(val, str) and val.strip():
            texts.append(val.strip())
        elif isinstance(val, list):
            parts = [p.get("text", "") for p in val
                     if isinstance(p, dict) and p.get("text")]
            if parts:
                texts.append("\n".join(parts).strip())
    return texts[-1] if texts else None


def _text_via_stream(raw: str):
    """Collect text-ish fields from exec --json events; plain text passes through."""
    collected = []
    parsed_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except ValueError:
            continue
        parsed_any = True
        stack = [evt]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                kind = str(node.get("kind", "")) + str(node.get("type", ""))
                if any(k in kind for k in ("output", "delta", "result", "message")):
                    for key in ("text", "delta", "content"):
                        val = node.get(key)
                        if isinstance(val, str) and val:
                            collected.append(val)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    if collected:
        return "".join(collected).strip()
    return None if parsed_any else (raw.strip() or None)


def assistant_text(raw: str, usage: dict, dest_dir: Path):
    """Return (text, source). The raw log is always kept by the caller."""
    if (usage or {}).get("session_id"):
        text = _text_via_export(usage["session_id"], dest_dir)
        if text:
            return text, "export"
    text = _text_via_stream(raw)
    if text:
        return text, "passthrough" if text == raw.strip() else "stream"
    tail = raw.strip()[-2000:]
    return tail or "(empty worker output)", "raw"


def send(session: dict, profile: dict, message: str, executor, workdir: Path) -> dict:
    """One turn: record the user message, run the worker, record the reply."""
    session["waitReason"] = None
    session.pop("ended_at", None)
    append_turn(session, "user", message)
    prompt = prompt_for(profile, session, message)
    try:
        code, out, usage = executor.muse_step("chat", prompt, workdir)
    except runner.QuotaHit as exc:
        session["waitReason"] = "subscription-quota in chat"
        session.setdefault("notes", []).append(
            {"at": utcnow(), "kind": "quota-wait", "detail": str(exc)[-300:]})
        save(session)
        raise
    log = session_dir(session["id"]) / f"chat.{session.get('turns', 0)}.log.txt"
    log.write_text(out[-20000:], encoding="utf-8")
    text, source = assistant_text(out, usage or {}, session_dir(session["id"]))
    usage = dict(usage or {})
    usage["text_source"] = source
    usage["exit_code"] = code
    if code != 0:
        text = f"(worker exit {code})\n{text}"
    return append_turn(session, "assistant", text, usage=usage)


def send_stream(session: dict, profile: dict, message: str, executor, workdir: Path,
                on_delta, prompt_override=None) -> dict:
    """Streaming turn: forward worker deltas, record the same transcript.

    prompt_override replaces the user turn (plan turns): no user message is
    recorded, only the assistant reply.
    """
    session["waitReason"] = None
    session.pop("ended_at", None)
    if prompt_override is None:
        append_turn(session, "user", message)
        prompt = prompt_for(profile, session, message)
    else:
        prompt = prompt_override
    buf = {"text": ""}

    def _guarded(chunk):
        if not chunk or chunk == buf["text"]:
            return
        if chunk.startswith(buf["text"]) and len(chunk) > len(buf["text"]):
            buf["text"] = chunk  # cumulative-style delta: replace
        else:
            buf["text"] += chunk
        on_delta(chunk)

    try:
        code, out, usage = executor.muse_step_stream("chat", prompt, workdir, _guarded)
    except runner.QuotaHit as exc:
        session["waitReason"] = "subscription-quota in chat"
        session.setdefault("notes", []).append(
            {"at": utcnow(), "kind": "quota-wait", "detail": str(exc)[-300:]})
        save(session)
        raise
    log = session_dir(session["id"]) / f"chat.{session.get('turns', 0)}.log.txt"
    log.write_text(out[-20000:], encoding="utf-8")
    final = ((usage or {}).get("terminal_text") or "").strip() or buf["text"].strip()
    if final:
        text, source = final, "stream-live"
    else:
        text, source = assistant_text(out, usage or {}, session_dir(session["id"]))
    usage = dict(usage or {})
    usage["text_source"] = source
    usage["exit_code"] = code
    if code != 0:
        text = f"(worker exit {code})\n{text}"
    return append_turn(session, "assistant", text, usage=usage)


def export_markdown(session: dict) -> str:
    head = (f"# Chat {session['id']} — {session.get('repo', '')}"
            f"#{session.get('issue', '')} {session.get('title', '')}").rstrip()
    lines = [head, f"pc={session.get('pc')} created={session.get('created_at')}", ""]
    for turn in read_transcript(session["id"]):
        lines.append(f"## {turn.get('role')} ({turn.get('at')})")
        lines.append(turn.get("text") or "")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def list_sessions(limit: int = 20) -> list:
    d = paths.sessions_dir()
    if not d.is_dir():
        return []
    out = []
    for found in d.glob("*/session.json"):
        try:
            out.append(json.loads(found.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return out[:limit]
