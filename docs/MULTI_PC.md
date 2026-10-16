# MULTI_PC — independent installs (v1 scope)

Each PC installs OpenMainty separately and works alone:

- PC A manages OpenIAP; PC B manages another repo. Each uses its own Muse
  login, checkout paths, tools, and devices.
- Share setup by copying the portable repo profile
  (`repo show --name X --export > X.json`, carry the file, `repo add
  --profile X.json`), then `repo link` to that PC's own checkout path.
- Run history and caches live under that PC's `$OPENMAINTY_HOME`
  (`~/.openmainty`); target checkouts are never polluted.
- Credentials stay on the PC: profiles and run files hold no secrets
  (presence of `META_API_KEY` is reported by name only, never read).
- Device-gated work is a PC capability: `pc.json` flags like `ios_device`
  gate profile steps that `requires:` them; unmet → step BLOCKED, not faked.

Verified in this session (macOS arm64, two separated homes `/tmp/om-a`,
`/tmp/om-b` on one machine):

- independent `repo list`, independent runs, no shared state;
- duplicate-run lock works within one home.

NOT claimed: two physical PCs (no second machine available — the two-home
test is local separation, not two-PC verification); Windows/Linux installs
(support matrix below); any automatic handoff or sync between PCs.

| Area | macOS | Linux | Windows |
|---|---|---|---|
| Muse subscription worker (`muse exec`) | verified | expected, unverified | expected, unverified |
| Install / CLI / mock tests | verified | expected, unverified | expected, unverified |
| Project builds / device E2E | per-PC capability | per-PC capability | per-PC capability |
