"""Synthetic offline demo-home seeder (no network, no model, no git).

`seed(home)` creates a fully synthetic OpenMainty home under an empty
directory: two fake repo profiles, one run blocked on an operator
question (whose analysis text carries a GFM pipe table, a fenced code
block containing a table, and workfile: links), one finished run with a
stored patch plus a parsed diffstat (historical availability, 1 file),
one run whose steps ran but whose live work folder is gone with no
stored patch (unavailable — never planned/no-changes), one run with
some steps done and the rest pending but nothing running (the honest
stopped state, "멈춤 — 진행 중인 작업 없음"), and a second repo with
no runs at all (the empty-state screens). Repo names use only synthetic
labels; no real checkout paths, no `~/.openmainty` reads, no
subprocess calls.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = "demo-sample-repo"
EMPTY_REPO = "demo-empty-repo"
GITHUB = "example-org/sample-repo"
EMPTY_GITHUB = "example-org/empty-repo"

WAITING_RUN_ID = "run-demowait01"
DONE_RUN_ID = "run-demodone01"
MISSING_RUN_ID = "run-demomiss01"
STOPPED_RUN_ID = "run-demostop01"

ANALYSIS_MD = """## 데모 분석 메모 (합성 데이터, 모델 미사용)

| 모듈 | 상태 | 파일 |
| --- | --- | --- |
| demo_button.ts | 확인 필요 | [demo_button.ts](workfile:demo_button.ts) |
| demo_header.ts | 양호 | [demo_header.ts](workfile:demo_header.ts) |

아래 울타리 안의 표는 코드로 유지됩니다:

```
| a | b |
| --- | --- |
| 1 | 2 |
```

차단 질문: 데모 위젯의 빈 화면 문구를 확정해 주세요.
"""

FIX_DIFF = """diff --git a/demo_button.ts b/demo_button.ts
--- a/demo_button.ts
+++ b/demo_button.ts
@@ -1,2 +1,3 @@
 export const label = "시작";
+export const hint = "데모 합성 변경";
"""

FIX_SUMMARY = "FILES CHANGED: demo_button.ts (합성 기록, 과거 패치)"

# The stored evidence the product actually parses: `_parse_diffstat_text`
# reads "<path> | <n> <+/->" lines and a "<n> file(s) changed" total.
# A unified diff alone (`fix.diff.txt`) parses to zero files, so the
# count lives here — the terminal text above is the model claim, this
# file is the stored evidence, and both must agree.
FIX_DIFFSTAT = """demo_button.ts | 1 +
1 file changed, 1 insertion(+)
"""


def _fixed_run(repo: str, issue: str, title: str, run_id: str) -> dict:
    """Create a run with a deterministic id (reproducible docs/links)."""
    from . import store

    state = store.create(repo, issue, title, "demobase0", {})
    src = store.run_dir(state["id"])
    dst = store.run_dir(run_id)
    if dst.exists():
        raise SystemExit(f"error: demo run id collision: {run_id}")
    os.rename(src, dst)
    state["id"] = run_id
    store.save(state)
    return state


def seed(home) -> dict:
    """Seed an empty directory as a synthetic demo home. Read-only safe.

    Refuses when the directory exists and is non-empty; otherwise writes
    ONLY under it (all product paths resolve through OPENMAINTY_HOME,
    which is pointed at the target for the duration of the call and
    restored afterwards). No network, no model CLI, no git, no reads of
    the real home. Returns the created repo names and run ids.
    """
    from . import repos, store

    root = Path(home)
    if root.exists() and any(root.iterdir()):
        raise SystemExit(
            f"error: refusing to seed non-empty directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("OPENMAINTY_HOME")
    os.environ["OPENMAINTY_HOME"] = str(root)
    try:
        for name, slug in ((REPO, GITHUB), (EMPTY_REPO, EMPTY_GITHUB)):
            repos.save({"name": name, "github": slug,
                        "baseBranch": "main", "knowledge": [],
                        "checks": [], "e2e": []})
        waiting = _fixed_run(REPO, "7", "데모: 빈 화면 문구 확정",
                             WAITING_RUN_ID)
        waiting["waitReason"] = ("question: 데모 위젯의 빈 화면 문구를 "
                                 "확정해 주세요.")
        waiting["question_step"] = "analyze"
        waiting["steps"]["triage"]["status"] = "done"
        waiting["steps"]["triage"]["exit_code"] = 0
        waiting["steps"]["analyze"]["status"] = "blocked"
        waiting["steps"]["analyze"]["exit_code"] = 1
        waiting["steps"]["analyze"]["note"] = "waiting on operator"
        waiting["steps"]["analyze"]["usage"] = {"terminal_text": ANALYSIS_MD}
        store.save(waiting)
        # Real finished-run shape: `_run_finished` (and the "완료" label)
        # require every step done. The runner only writes done/failed/
        # blocked/pending/running — never "error".
        done = _fixed_run(REPO, "3", "데모: 버튼 힌트 추가", DONE_RUN_ID)
        for step in done["steps"]:
            done["steps"][step]["status"] = "done"
            done["steps"][step]["exit_code"] = 0
        done["steps"]["fix"]["usage"] = {"terminal_text": FIX_SUMMARY}
        store.save(done)
        (store.run_dir(DONE_RUN_ID) / "fix.diff.txt").write_text(
            FIX_DIFF, encoding="utf-8")
        (store.run_dir(DONE_RUN_ID) / "fix.diffstat.txt").write_text(
            FIX_DIFFSTAT, encoding="utf-8")
        missing = _fixed_run(REPO, "9", "데모: 사라진 작업 폴더",
                             MISSING_RUN_ID)
        # Steps ran, but the live work folder is gone and no patch was
        # ever stored: `_run_never_started` is False and
        # `_has_stored_artifacts` is False, so the product must report
        # this run unavailable instead of planned/no-changes.
        missing["steps"]["triage"]["status"] = "done"
        missing["steps"]["triage"]["exit_code"] = 0
        # The runner never writes "error": TERMINAL is
        # {done, failed, blocked}. A ran-then-lost step is failed.
        missing["steps"]["analyze"]["status"] = "failed"
        missing["steps"]["analyze"]["exit_code"] = 1
        missing["steps"]["analyze"]["note"] = (
            "합성: 작업 폴더가 사라져 소스를 읽을 수 없음")
        work = store.run_dir(MISSING_RUN_ID) / "work"
        if work.exists():
            # Fail closed: the seeder never creates work/, so an existing
            # one means unexpected state — refuse instead of deleting it.
            raise SystemExit(
                f"refusing to seed over existing demo work dir: {work}")
        store.save(missing)
        # F15: the stopped state needs no failure and no live job — some
        # steps done, the rest pending, nothing running.
        stopped = _fixed_run(REPO, "5", "데모: 멈춘 실행", STOPPED_RUN_ID)
        stopped["steps"]["triage"]["status"] = "done"
        stopped["steps"]["triage"]["exit_code"] = 0
        store.save(stopped)
        return {"home": str(root), "repos": [REPO, EMPTY_REPO],
                "runs": {"waiting": WAITING_RUN_ID, "done": DONE_RUN_ID,
                         "missing": MISSING_RUN_ID,
                         "stopped": STOPPED_RUN_ID}}
    finally:
        if previous is None:
            os.environ.pop("OPENMAINTY_HOME", None)
        else:
            os.environ["OPENMAINTY_HOME"] = previous
