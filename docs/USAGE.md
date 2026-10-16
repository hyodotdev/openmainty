# USAGE — worked examples

## 1. Register the OpenIAP repo (portable profile + PC-local checkout)

```sh
openmainty repo add --profile examples/repos/openiap.json
openmainty repo link --name openiap --checkout ~/Github/hyodotdev/openiap
openmainty repo show --name openiap --export   # copy this JSON to another PC
```

Profile = GitHub id, base branch, knowledge pointers, check/E2E commands,
release/announce procedure. Checkout paths and capabilities stay in
`pc.json` on each PC — never in the profile.

## 2. Analyze a real issue in an isolated worktree (no checkout writes)

```sh
gh issue view 487 --json body --jq .body > /tmp/issue-487.md
openmainty run --repo openiap --issue 487 --title "..." \
  --body-file /tmp/issue-487.md --only triage,analyze
openmainty status --run <run-id>
```

Observed: run `run-39b70a92` on OpenIAP `a3ad196b` — triage + analyze done
via two subscription worker sessions; checkout `git status` clean; work in
`<home>/runs/<id>/work` (detached worktree).

## 3. Full local flow on a scratch bug (subscription, end to end)

```sh
openmainty run --repo calcbug --issue 1 --title "add() subtracts" \
  --body "add(2, 3) returns -1, expected 5."
```

Observed: run `run-135e1caa` — all 8 steps done; `calc.py` fixed
(`-` → `+`); reviewer found VALID findings and they were applied;
`python3 test_calc.py` passed; PR/release/Medium/Discord/Slack drafts
written as files; nothing posted, no PR created.

## 4. Interruption, resume, duplicates

- Quota hit → step stays pending, `waitReason=subscription-quota` saved;
  `resume --run` continues without repeating completed steps.
- Second `run` for the same repo+issue while one is active is refused
  (local lock; `--allow-duplicate` overrides).
- Code changed after a verification → that verification is invalidated
  and re-runs on resume (never reuses stale results).

## 5. External actions

```sh
openmainty external --run <run-id> --dry-run
```

Always preview first. v1 never posts/merges/deploys/sends: even an approved
call only prepares payloads. Sending channel messages or deploys requires
a future explicit policy + implementation (see COORDINATOR_NEXT.md).

## 6. Continuous care (drift + issues, daily)

```sh
openmainty watch --repo locanara --start-run   # drift runs + new/resumed issue runs
```

Per-issue rule: no local run → start; failed/blocked run → resume it;
active, quota-waiting, or completed run → skip (never duplicate).
Daily cron (installed 2026-10-16, 09:00, all managed repos):

```sh
for R in locanara nitro-sound calendar; do openmainty watch --repo $R --start-run; done
```

Progress lands in `~/.openmainty/watch.log`; the dashboard shows it live.
Quota hits pause a run (`waitReason`) for a human `resume`; sleep suspends
the schedule (cron cannot wake a sleeping Mac).
