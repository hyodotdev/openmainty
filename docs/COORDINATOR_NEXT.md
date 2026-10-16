# COORDINATOR_NEXT — cross-PC work (NOT in v1)

v1 status: **unimplemented**. Each PC's state is local; the local
duplicate lock is not global protection. Do not describe v1 as synced.

Seams kept small for later: `Runner` depends only on the `Executor`
interface (worker + shell) and the file-backed store helpers. A shared
coordinator replaces those two seams, not the step logic.

Minimum design for the next step:

1. Work lease — a coordinator issues a lease (run id + owner PC + expiry);
   only the lease holder executes; expiry + heartbeat survive disconnects.
2. Duplicate prevention — global claim table on repo+issue; local lock
   stays as a fast path, the claim table decides.
3. Device capability assignment — E2E steps route to a PC whose reported
   capabilities satisfy `requires:` (e.g. iOS device runs go to the Mac
   with the device).
4. Commit/artifact transfer — worktree commits and `runs/<id>/` evidence
   move PC-to-PC by content hash (base + work commit recorded already).
5. Reconnect recovery — `resume` replays from stored step state; completed
   steps are skipped, stale verifications re-run (already the local rule).
