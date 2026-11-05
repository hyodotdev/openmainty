# OpenMainty — the maintainer that keeps your repos healthy

One-liner: OpenMainty watches your open-source repos for dependency
drift and open issues, verifies each update in a sandbox on your
own Mac, and prepares reviewable batches and PR drafts. You merge;
it never pushes.

The problem: small open-source repos rot between releases. A
dependency update sits unreviewed, a lockfile drifts from the
manifest, a native lockfile pins last month's pod — and checking
each bump means a full install, test and build cycle nobody has
time for. OpenMainty does that cycle itself, records the evidence,
and stops honestly wherever a human is needed.

## The pipeline

Watch → review → prepare → fastcheck → batch → hold/attest →
rollup PR draft.

- **Watch** scans registries for drift and queues candidates.
- **Review** records an automated verdict per candidate.
- **Prepare** replays the candidate onto the current base batch in
  an isolated worktree. A lockfile-only conflict is re-resolved
  with the repo's own package manager and recorded as a derived
  source, never as the original commit. A pure version bump —
  one dep, or a group that must move together — whose cherry-pick
  conflicts only around its own range lines is re-applied on the
  current base and re-resolved the same way, recorded as a
  derived source ("re-derived from \<sha\> on \<base\>"); lines
  already in base are skipped, never re-committed. A blocked
  prepare stays terminal until a human presses "다시 시도": one
  explicit retry (owner/root with confirm), never a loop, never
  from the driver. Some upgrades only ship together — a library
  major that pins a framework minor, or the reverse. OpenMainty
  notices the pair (two candidates whose locks mix the same two
  RN minors in opposite directions). The status page lists
  "<dep> <target>과 함께 올려야 함" under 사람이 할 일, and
  the run's card shows the 함께 다시 시도 button. A human
  presses one button ("함께 다시 시도",
  owner/root with confirm); OpenMainty re-applies both
  candidates' range edits onto the recorded lane base
  (re-verified fresh and clean when pressed; drift refuses) and
  records one derived source for both runs, never either
  original commit. If the tree still mixes RN minors, nothing
  is committed and the block says so.
- **Fastcheck** runs the repo's configured fastcheck steps in
  a sandbox (file writes denied except the worktree, temp HOME,
  caches and /dev; lifecycle scripts off at install, network
  denied afterwards; a Flutter SDK toolchain also allows its
  own bin/cache stamps) and records a census per bump: executed
  directly, loaded by a tool (convention guess, said as one),
  not run, or unknown. For react-native-nitro-sound: install,
  prepare/build, typecheck, lint, unit. For the Flutter repo:
  pub get, analyze, test.
- **Batch** registers verified work with worker-only gates; root
  gates stay pending.
- **Hold / attest** is human-only: the owner or root holds a
  batch with a reason, and approves each gate after reviewing
  the receipt. A worker can never attest or hold.
- **Rollup** merges a lane's ready leaves into one branch and
  writes stacked PR drafts as copy-only text. The owner runs
  `git push` and `gh pr create` by hand.
- **Consumer gate** (M2.15): when a bump touches only a
  workspace manifest (e.g. `@babel/runtime` ^7 → ^8 in
  `example/package.json`), root-level steps resolve a
  different dependency tree, so the census stays "not run"
  and the workspace's own one-shot build (`example`'s
  `build:web`) runs instead in a scratch clone. A human
  still reads the census line before trusting a green
  check.
- **Re-verify** (M2.16): a finished check runs again on
  demand ("검증 다시 실행", owner/root with confirm). The
  old receipt is renamed to
  `*.fastcheck.prev-<UTC>.json` (plus its step logs) —
  archived, never deleted — then one fresh run under the
  worker lease. A human presses the button; the driver
  never calls it.
- **Compiler/runtime mismatch** (M2.16): a MAJOR bump of a
  runtime-helpers package (`@babel/runtime`, `@swc/helpers`,
  `tslib`) while its compiler in the same manifest stays on
  the old major never auto-batches — the batch stage refuses
  with "컴파일러와 메이저 불일치". Existing batches carry
  the same finding as a warning only, and rollup redrafts
  list it under 확인 필요. A human decides whether the pair
  really must move together.
- **Root QA attest**: each of the 10 root gates (install,
  prepare, typecheck, lint, unit, loaded-config,
  peer-explain, diffcheck, source-review, root-qa) is
  attested by a human (owner/root) after reviewing the
  receipt; a worker can never attest, and holds work the
  same human-only way.
- **Independent review verdict** (M2.16c): an auto-batch
  becomes READY only after a human records 승인, 메모와 함께
  승인, or 수정 요청 on its primary — the step that made
  the first auto-batch READY. Workers and the driver can
  never set a verdict; the previous verdict is kept, never
  dropped.
- **Newer-leaf hints** (M2.16d): a rollup card names a newer
  READY leaf on the same repo, lane and dep with the same
  inherited base ("새 버전 준비됨 … — 다시 합치기 가능").
  Display only — nothing re-rolls by itself; a human or
  Root triggers the new rollup.
- **PR awareness** (M3.5): the dashboard shows open PRs
  read-only. It never comments, merges, or pushes.
- **Test-isolation guard** (M2.15b): the suite refuses to
  run against the real home — a test that would touch
  `~/.openmainty` fails instead of writing there. It
  protects the owner's data from the developers' own
  tests; humans do nothing.
- **Joint refresh** (M2.17 series): a doomed singleton (an
  expo major on an RN-old base, a runtime/compiler mismatch)
  is skipped pre-prepare with a recorded reason instead of
  costing a full prepare + fastcheck; when a newer member
  of a done joint appears, the driver derives one refreshed
  joint instead of a standalone, and the batch stage exempts
  exactly the superseded old joint. Four gaps were found by
  watching real ticks and closed the same way: joint glob
  scoping, phase-1 exemption + claim order, the import-gate
  exemption with its held precondition, collision-safe batch
  ids. A human still records the verdict and attests; none
  of this batches by itself.
- **QA evidence pack** (M2.18): one button builds
  `qa/<batch>/pack-<UTC>.json` — changed files plus lock
  entries flagged inside/outside the bumped tree, the
  base-vs-head peer-explain diff, release notes with
  breaking/removal stems flagged, the consumer receipt,
  and one ≤200-char suggested note per root gate. Each
  gate still needs the human click plus a confirm dialog;
  the pack only pre-fills. The driver never builds or
  attests.
- **Native lock label** (M2.5c/M2.16e/M2.22): a tracked
  `Podfile.lock` pinning another version names the owner
  command — `cd example/ios && pod update … --no-repo-update`
  when prebuilt pods are pinned (`pod install` fails on
  those bytes), plain `pod install` otherwise. Checked for
  every joint/family member plus the tree's own RN version
  at receipt time, with a live multi-primary fallback for
  older receipts. Display only — OpenMainty never runs pod.
- **Index-safe probes** (M2.21/M2.21b/M2.22b): every
  read-only git probe runs with `GIT_OPTIONAL_LOCKS=0`
  against a per-call private copy of the index that keeps
  the index mtime — linked worktrees never see their
  (owner-`.git`-resident) index refreshed, and a
  same-second edit still reads dirty. A human does nothing;
  the full-tick proof is below.
- **gc stale_tmp**: leftover `summary/*.tmp` files older
  than an hour whose process is gone are listed by
  `gc --dry-run` and removed only by an explicit owner
  `gc --apply`; the daemon never applies. A human reads the
  byte count, then runs the apply.

## Safety model

- Never pushes, merges, releases or comments. Push/PR commands
  exist only as draft text; there is no push code path.
- Checks run dependency code, so every step runs under
  sandbox-exec with file writes denied by default: ONLY the op
  worktree, its own git admin dir, the per-op temp HOME, the app
  caches and /dev may be written (recorded per step as
  `write_allow`). Install keeps network for the registry with
  lifecycle scripts force-disabled; later steps run
  network-denied (loopback-only for `flutter test`). The
  environment is scrubbed (no tokens, no secrets). No
  integration git operation (prepare, re-resolve, family,
  followup, rollup) runs owner hooks (`core.hooksPath=/dev/null`),
  and neither do the scheduler's own worker-code commits or any
  app-created worktree (`worktree add` in the runner and in
  chat). The commit message stays commitlint-shaped by
  construction, and the sandboxed fastcheck re-runs
  typecheck/lint/unit before anything is batched. The owner's working tree and current branch
  are never modified;
  OpenMainty only adds its own worktrees (registered under
  .git/worktrees) and local codex/* branches, and never pushes
  them.
- Owner/root identity is asserted by the local caller for attest
  and hold (TTY confirm at the CLI, explicit attestor at the
  API); "worker" is refused everywhere a human must decide. There
  is no per-install human token yet, so on a shared machine any
  local process could claim owner — treat attestor names as
  attribution, and keep the dashboard on loopback.
- Every code change ships as reviewed, tested and promoted: an
  independent FULL + Py3.9 suite, byte-exact promotion, and a
  one-command rollback.
- Blocked work stops with the reason on screen. No retry storms,
  no silent re-runs.
- The joint retry is human-only (owner/root with confirm, under
  the worker lease; the M2 driver never calls it). It uses the
  same hook-free git, scrubbed env and scripts-off lock regen as
  re-derive; a still-mixed tree is an honest block with nothing
  committed.

## Live evidence

Numbers are never pasted here — they regenerate from any home.
For the independently verified state (promotions, suites, browser
checks with record names), see docs/SUBMISSION.md: verification
records are fixed facts, while metrics regenerate:

```sh
openmainty metrics --lang en          # plain English report
openmainty metrics --json             # raw numbers
openmainty metrics --days 7           # last-7-days flow counts
```

It reports, per registered repo: autonomy start, prepares
done/blocked, fastcheck receipts by census status plus
green/red and archived re-runs, auto batches (total, held with
reasons, waiting on human review), lockstep and native-lock
holds, duplicate skips — and pushes, always 0.

2026-10-23 09:46Z: one human joint retry (react-native 0.88.0 +
expo 58.0.17, lane next) derived a done record on its base; the
tree resolves RN to 0.88 only. 결과는 다음 틱에서 확인.

### 2026-10-23: webpack 5.109.2 → 5.112.2, end to end

Batch `auto-prep-run-25dd835d-nitro-next-eslint-pair-20261019`
(react-native-nitro-sound, lane next) — the first auto-batch to
go scan → prepare → fastcheck (+ consumer build) → batch →
Root QA → verdict → READY. Times are the event log's, in UTC:

- 12:47:19Z the prepare op is created; 12:47:20Z "자율 준비
  완료 webpack 5.109.2 -> 5.112.2" and the check starts.
- 12:48:24Z the check passes with "이 패키지를 직접 실행한
  단계 없음" — webpack is used only by the example web
  scripts (web, build:web), which no fastcheck step runs.
  That honest "not run" is what the M2.15 consumer gate and
  the M2.16 re-verify were built for.
- 12:48:51Z the auto-batch is registered (verdict pending);
  12:49:07Z Root holds it with a reason.
- 19:35:52Z the re-verify receipt: consumer `example:
  build:web` passes, rc 0 in about 7 seconds
  ("소비 스크립트 build:web 통과 (7초)"); the old receipt
  is archived as
  `prep-run-25dd835d-….fastcheck.prev-20261023T193552Z.json`,
  never deleted. 19:36:46Z "검증 다시 실행 (by root)";
  19:37:15Z Root releases the hold (reason: the verified
  19:35Z rerun).
- 19:41:04Z–19:44:34Z Root attests all 10 gates through the
  API (install, prepare, typecheck, lint, unit by receipt;
  loaded-config, peer-explain, diffcheck, source-review,
  root-qa direct), including a browser smoke test of the
  built web demo.
- 22:46:51Z Root records accept-with-notes through
  `/api/batch/review` (the pending verdict is kept under
  "previous"); evaluate flips to READY. The recorded
  findings say the diff is limited to `example/package.json`
  plus webpack's own lock tree, the demo renders home and
  the useSound screen with 0 console errors — and that the
  committed `example/dist` still holds the older bundle
  (the owner's choice, not part of the bump), and that
  mic/record and remote playback were not exercised in the
  smoke test.

### 2026-10-24: RN 0.88.0 + expo 58.0.19 — derived, checked, registered, waiting on the owner

Batch `auto-prep-run-79bbc781-nitro-next-eslint-pair-2026101-21baa386a5`.
Every time is from the event log (`events.jsonl`) or Root's QA
record (`claude66/root66-rn088-joint-qa-20261024.json`), UTC:

- 06:19:19Z the daemon derives the refreshed joint by itself
  ("자율 함께 올림 완료 expo 58.0.19 + react-native 0.88.0",
  actor m2). No human step.
- 06:50:56Z its fastcheck passes 6/6 in 46 s plus consumer
  `build:web` in 6 s. It still could not batch: the old
  58.0.17 joint held the keys. Four gaps were found by
  watching real ticks and fixed the same way — joint glob
  scoping (M2.17c), phase-1 exemption + claim order (M2.17d;
  Root's 09:27Z tick record), the import-gate exemption with
  its held precondition (M2.17f; review 12:27Z),
  collision-safe batch ids (M2.17g).
- 13:35:22Z the first tick after the last fix registers the
  batch by itself ("자율 묶음 등록 …", 검사 46초 ·
  가져오기 38초).
- 13:38:28Z Root QA on a `git archive` probe of head
  `b2a052b3`: install, check-nitro-versions, bob build and
  tsc all rc 0; peers identical at base and head (13
  incorrectly-met both, one dev-tooling count change); the
  expo delta is patch-only; the QA pack built in 11 s.
  Verdict: NOT attested — `Podfile.lock` still pins 0.86.2,
  and attesting before the owner's pod update commit would
  stale on the new head.
- 13:38:10Z Root holds the batch carrying the exact owner
  command (the new card showed `pod_drift = null` — the
  receipt had checked only the record dep expo, and the
  fallback covered single-primary batches only → M2.22).
  After the M2.22 promote the card shows the label by
  itself; 17:46:33Z Root releases the hold.
- Owner safety that day: read-only probes ran a full live
  tick with all 113 owner index files unchanged (16:09:40Z
  tick record). And honestly: the private-index copy
  briefly shipped a bug — a fresh-timestamp copy broke
  git's racy rule, so `status` missed same-second
  same-size edits and two suites "flaked". Root diagnosed
  it from the flakes and it was fixed forward the same day
  with a mtime-preserving copy (reviews ACCEPT).

### 2026-10-24: babel 8 — BLOCKED, with sources

Question: can the `@babel/runtime` 8 line move as one
family (core + runtime + preset-env + whatever else must
move), or does the RN toolchain pin babel 7? A read-only
analysis (`claude66/muse66-m220a-babel8-note.md`, review
`root66-m220a-review.json`) says BLOCKED:
`@react-native/babel-preset` 0.88.0,
`metro-babel-transformer` 0.87.1 and
`react-native-builder-bob` 0.44.1 all need `@babel/core`
^7, while latest core is 8.0.9. The follow-up product rule
(M2.20b) will mark `@babel/*` 8 candidates "생태계 대기"
until the blocker releases, instead of re-proposing them
forever. The M2.16 compiler/runtime refusal stands
regardless.

### The honest counter-example: the RN 0.88 joint

The next day, 2026-10-24, shows where the pipeline stops.
A 00:10:03Z `m2-skip` refuses the standalone expo 57.0.11 →
58.0.19 auto-prepare (run-23fdef89 — its fastcheck passed
5/5 at 2026-10-23T23:39:18Z with "no step directly ran this
package") for "RN 그룹 혼재" (`@react-native/codegen`
0.86.2 + 0.88.0 side by side).
And the 23:07:46Z Root record for the RN 0.88 + expo 58
joint (`auto-prep-run-79bbc781-…-joint`) reads: JS-green
(install, check-nitro-versions, bob build all rc 0), but
`pod install` fails because `Podfile.lock` still pins
0.86.2. After `pod update ReactNativeDependencies
React-Core-prebuilt hermes-engine`, the iOS simulator
Release build succeeds and the app launches — verdict:
NOT attested, because the integrated bytes need a native
follow-up commit (`example/ios/Podfile.lock`) first, and
that commit is the owner's decision. OpenMainty never
pushes.

## Honest limits

- The RN 0.88 joint above is NOT READY and no verdict is
  recorded: the owner still has to run the pod update,
  commit `example/ios/Podfile.lock`, and then Root re-runs
  QA (JS probe + iOS build + sim launch) and attests the 10
  gates plus the verdict. By design, not a missing feature.
- OpenMainty never pushes. PR drafts are owner actions
  (`git push`, `gh pr create` by hand); there is no push
  code path.
- Gate 1 is open: the real-app end-to-end case (case856) has not
  run through; its fix/build/review steps wait behind gate 2
  preflight.
- Gate 2 is open: the subscription profile bind on the product
  path is shown (no model turn yet); usage and contributor binding
  to the same alive worker are not yet observed, and the worker
  model id (D1) and the settings write-lock permission (D2) are
  owner decisions still pending.
- macOS only; yarn berry, npm and Flutter pub. No CocoaPods
  (`pod install` is labelled, never run) and no device testing.
- Re-derive covers range edits in the JSON manifests
  (`package.json`, `example/package.json`); `pubspec.yaml`
  (Flutter) is refused with the existing honest block, because
  every manifest goes through a JSON parse. The lock is
  regenerated with yarn berry only
  (`yarn install --mode=update-lockfile`) and anything else is
  refused before anything is committed. A group line the base
  moved to a third value is refresh/human work, never re-derived.
- Pair detection only covers the RN lockstep family; other
  co-dependent bumps still strand with their own honest block.
- A native (CocoaPods) lock drift stays an owner step (`pod
  install` is labelled, never run).
- Joint needs both candidates to be pure range bumps
  (`_rederive_eligible` must pass for each), and refuses when
  the lane base moved since the pair was found.
- Rollup autonomy stays off: rollups run from the UI and drafts
  stay text until the owner pushes.
- Fastcheck says what it actually executed; a green check that
  never ran the bumped package reads "no directly-executed
  step", not "verified".
- An auto-batch becomes READY only after a human (owner or
  root) records a review verdict — by design, not a missing
  feature. No verdict, no READY, no matter how green the
  gates are.
- Native follow-ups stay owner steps: a `Podfile.lock`
  refresh, simulator or device runs beyond what the owner
  chooses to do by hand, and any store submission never run
  inside OpenMainty.

## Self-run

Daily use, updates, rollback and disk care are in
[docs/OWNER_RUNBOOK.md](OWNER_RUNBOOK.md): one glance page,
human todos, the autonomy switches, `openmainty release`
(`./openmainty release --rollback --yes` rolls back), and
`openmainty gc`.

## Basis vs new work (contest split)

- Pre-existing before the contest period: the `muse` CLI
  subscription; the OpenMainty v1 repo, started 2026-10-16
  (git log shows the first commit `0a632c2` on that date);
  the owner's open-source repos `react-native-nitro-sound`
  and `flutter_calendar_carousel` as work material. OpenIAP
  was the first integration target on 2026-10-16 but is no
  longer registered, by owner decision (the registered set is
  exactly the two repos above).
- Built during nightly maintenance since 2026-10-18: the
  autonomy pipeline above (prepare, sandboxed fastcheck with
  census, batch, hold/attest, rollup drafts), the glance
  dashboard, metrics, and the runbook.
- The contest-period rules are not yet verified — re-check
  after the official rules are published (on/after
  2026-10-26). No endorsement by Meta is claimed.
- Work after the first commit is not yet committed to git; the
  dated evidence is in the OpenMainty event log and the
  supervision records.
