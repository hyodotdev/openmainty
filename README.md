# OpenMainty — Your AI maintainer for open source.

OpenMainty is a local maintainer tool: it watches dependency drift and
open issues/PRs/Discussions, verifies updates in a sandbox on your Mac,
and prepares reviewable batches and PR drafts (triage → reproduce →
analyze → fix → review → verify). You review and merge; it never posts,
merges, deploys, or sends channel messages by itself.

Verified state (2026-11-05): promoted app `75ab15721af543f8` after 32
independently gated promotions; 127 test modules / 3,672 tests pass on
Python 3.14 and 3.9 (one sandbox-fenced module excluded by design); the
web dashboard was checked in a real browser at 1280px and 390px, light
and dark. Not yet shown: one real maintenance case run end to end, and
proof of the subscription worker binding. See
[docs/SUBMISSION.md](docs/SUBMISSION.md) for the evidence.

Primary worker: your Muse Code subscription login via `muse exec`
(`--provider meta --model muse-spark-1.3 --contributor-mode on`).
No direct model APIs, no pay-per-use keys.
Subscriber-only: `chat` and worker runs require `muse login`;
API-key billing (`META_API_KEY`) is refused.

## Hackathon

OpenMainty is entered in the Meta Global AI Developer Hackathon
(Oct 27 – Nov 5 KST). Start here: [docs/HACKATHON.md](docs/HACKATHON.md) —
what it does, the safety model, honest limits, and how to regenerate
every number. Daily operation stays in
[docs/OWNER_RUNBOOK.md](docs/OWNER_RUNBOOK.md); the 3-command
quickstart:

```sh
openmainty metrics --lang en          # regenerate every number (read-only)
./openmainty release                   # full tests → promote → restart → verify
~/.openmainty/app/current/openmainty gc --dry-run   # disk report, deletes nothing
```

## Install (reproducible, stdlib only, Python ≥ 3.9)

```sh
git clone git@github.com:hyodotdev/openmainty.git && cd openmainty
ln -s "$PWD/openmainty" ~/.local/bin/openmainty   # or copy to any PATH dir
muse login                  # this PC's subscription login
openmainty doctor           # quick, cost-free
openmainty doctor --live    # one tiny subscription run, first time only
```

No dependencies, no build step, no server, no CI service required.

## Add your repo and let it maintain itself

```sh
openmainty repo add --name mylib --github myorg/mylib \
  --base-branch main --checkout ~/code/mylib \
  --check 'unit=pytest tests' --check 'types=mypy src'
openmainty repo show --name mylib --export > mylib.json   # portable profile
```

The profile (GitHub id, base branch, knowledge pointers, check/E2E
commands, dependency pins, suggested labels) is portable. Checkout paths
and device capabilities stay in `pc.json` on each PC — never in the
profile, never committed to the target repo.

Then one command runs the whole loop:

```sh
openmainty watch --repo mylib --start-run
```

Per-issue rule: no local run → start; failed run → resume it; active,
quota-waiting, or completed run → skip (never duplicate). Drifted
dependencies are batched into a single run per day, not one run each.

## Daily commands

```sh
openmainty doctor [--live]
openmainty plan --repo mylib --issue 12
openmainty run --repo mylib --issue 12 --title "..." --body-file issue.md \
  [--executor muse|echo|fake] [--only triage,analyze]
openmainty status --run <run-id> [--json]
openmainty resume --run <run-id>
openmainty external --run <run-id> --dry-run     # previews only; never sends
openmainty deps --repo mylib [--check]           # registry drift
openmainty chat start --repo mylib [--issue 12] [--message "..."]  # REPL when --message absent
openmainty chat send --session <id> --message "..."               # continue a saved session
openmainty chat list | show --session <id> | export --session <id> [--out f.md]
openmainty chat delete --session <id>                               # remove a saved session
openmainty serve --port 8901                     # read-only local dashboard
openmainty app install                           # login-persistent dashboard (macOS)
```

Open `http://127.0.0.1:8901/` in a browser: the dashboard is a
maintenance console — continue chat sessions, check drift/issues, and
start maintain/resume jobs. It binds loopback only; quota-spending
actions enforce the subscriber gate. Jobs still produce drafts only:
nothing posts, merges, deploys, or sends by itself.

## Run it on another computer

```sh
git clone git@github.com:hyodotdev/openmainty.git && cd openmainty
ln -s $PWD/openmainty ~/.local/bin/openmainty
muse login && openmainty doctor --live
gh repo clone <your targets somewhere on this PC>
openmainty repo add --profile mylib.json          # the portable file
openmainty repo link --name mylib --checkout <this-PC-path>
```

Each PC keeps its own state (`~/.openmainty`), login, and capabilities.
Run history does not sync: finish in-flight runs on the old PC, and never
open the same issue on two PCs at once (the duplicate lock is local).

## House rules (defaults, not suggestions)

OpenMainty follows the target repo's own `AGENTS.md` and workflows, and
these defaults on top:

- Label everything it files: profiles may declare
  `"labels": {"pr": [...]}` and drafts carry them as `Suggested labels`.
- Never spray issues or PRs: related drift is one batched run per day;
  one issue gets one run; failures resume instead of duplicating.
- Group context before acting: triage and analyze share one evidence
  trail with fix and review, so one change produces one coherent PR draft.
- No silent sends, no auto-merge, no deploys — drafts stop at review.

## Layout

- `src/openmainty/` — core: worker (`muse.py`), ordering/state (`runner.py`,
  `store.py`), conversations (`chat.py`), portable profiles (`repos.py`),
  PC config (`pcconfig.py`), drift (`deps.py`), dashboard (`serve.py`,
  `dashboard.html`), `doctor.py`, `planner.py`, `artifacts.py`, `external.py`,
  `cli.py`
- `examples/repos/` — portable profile examples
- `tests/` — state/resume/dedup, cost-policy, drift, model-select, watch
- `docs/` — `USAGE.md`, `MULTI_PC.md`, `COORDINATOR_NEXT.md`, `HACKATHON.md`

State lives in `$OPENMAINTY_HOME` (default `~/.openmainty`), never in the
target checkout. `OPENMAINTY_HOME=/tmp/x` fully isolates an installation.
