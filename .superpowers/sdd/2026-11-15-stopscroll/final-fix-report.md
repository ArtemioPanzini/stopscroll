# Final review recovery fixes — Important 1–3

Scope: authorized recovery fix wave on main, starting from `9668e79`. No installed code, live state, services, DNS, nft rules, or owner acceptance sessions were changed. All system commands below were exercised through FakeRunner/temp directories. Important 4 accounting and doctor/remove_exception remain unchanged pending the controller's owner decision.

## Changes

- Every fresh System instance establishes applied dnsmasq/resolved configuration by scheduling a one-time DNS restart, resolver restart and cache flush. Failed effects remain pending in that instance; another fresh instance repairs the file-written/service-not-reloaded crash window. Subsequent no-change reconciliation stays quiet.
- Daemon close events journal real close jobs in the sole state.json, keyed by IPv4/IPv6 source set. Each captured address snapshot and deadline is saved before nft enforcement. One nft transaction adds timed quarantine elements and deletes only captured elements still present in the source. Failure logs a message, preserves source atomically, and leaves the job for minute reconciliation and restart recovery. Newly arrived exception addresses outside the snapshot are neither deleted nor quarantined; a closed site without a real close job causes no quarantine work.
- Successful jobs are removed and persisted. Replay uses the original deadline, so a recovered snapshot does not repeatedly extend successful quarantine. After an expired deadline, retained captured source elements are evidence of unapplied work and receive a fresh interval; absent captured source elements retire the stale job. A fresh actual close replaces an older snapshot so newly closed addresses are captured.
- The new optional v1 quarantine field is defaulted only when absent. All original required fields remain strict; existing installed policy, usage, sessions and queue are preserved. Metadata is typed and address/family/name validation rejects malformed journal data.
- Persistence returns success/failure and tracks dirty state until a successful save. Minute retries run with an empty change queue. Failed command persistence returns ok=False with an explicit Russian error while in-memory tightening still takes effect. Startup event handling now performs one reconciliation rather than the former duplicate.

## Verification commands and results

1. Initial focused RED: `python3 -m unittest tests.test_system tests.test_daemon -q` reproduced missing recovery APIs and missing restart. The first budget test initially used an invalid change kind; corrected it to the actual `set_budget` interface before accepting the save regression.
2. Confirmed RED against pristine baseline modules copied with `git show 9668e79:stopscroll/{daemon,system,model}.py` into a temporary package, without changing checkout modules: ran the three focused restart/quarantine/durability tests with temporary PYTHONPATH. Result: 3 tests, 2 failures (lost restart and falsely successful persistence response), 1 missing-new-API error, exit 1.
3. Focused GREEN: `python3 -m unittest tests.test_system tests.test_daemon tests.test_model tests.test_store -q` — 49 tests passed before the final fresh-close snapshot test was added. Expected simulated disk-full and nft errors were logged.
4. Full GREEN: `python3 -m unittest discover -s tests -v > /tmp/stopscroll-final-fix-tests.log 2>&1` — 128 tests passed, 0.376 seconds. Includes restart recovery, fixed deadline replay, failure past deadline, exception preservation/no flush, snapshot-before-enforcement durability, dirty save retry with empty queue, failed close enforcement, startup single reconciliation, metadata roundtrip, and legacy model/store preservation.
5. `git diff --check` — exit 0, no whitespace errors.

## Limits and remaining decisions

- A process can crash after a successful atomic nft transaction but before saving job acknowledgement. Source evidence plus the fixed deadline handles normal replay. If the exact same captured IP is later repopulated by an exception and recovery happens only after the original deadline, it is indistinguishable from unapplied source work without an additional kernel marker/transaction journal. This bounded ambiguity can cause another quarantine interval for that address. The controller accepted retaining the compact design and documenting this limit.
- If storage remains unavailable, the daemon explicitly reports non-durability and continues enforcing its in-memory policy/retrying. A crash before any successful save cannot make unsaved policy durable; no extra state file is introduced.
- The retained standalone System.quarantine(site) compatibility entry point remains best effort. Durable retries are provided by the daemon's explicit state-backed close journal.
- Owner decisions for expiry accounting, dynamic doctor checks and remove_exception CLI remain outside this commit; live installation and timed/browser acceptance belong to the controller.

## Follow-up: bound quarantine replay after wall-clock rollback

Re-review identified that the stored wall deadline could emit a timeout longer than the intended 120 seconds after a backward clock adjustment. Added `test_quarantine_replay_after_wall_clock_rollback_is_bounded`: a snapshot captured at 1000 with deadline 1120 and replayed at -2600 previously emitted 3720000ms. The focused test failed RED with that exact script.

The emitted duration now clamps to at most QUARANTINE_SEC (120000ms); the persisted deadline remains unchanged, preserving ordinary fixed-deadline replay behavior. No live runtime changes were made, and owner accounting/minor decisions remain untouched.

Verification:
- `python3 -m unittest tests.test_system.SystemTests.test_quarantine_replay_after_wall_clock_rollback_is_bounded -q` before fix: 1 test, 1 failure (RED).
- `python3 -m unittest tests.test_system tests.test_daemon tests.test_model tests.test_store -q`: 51 tests passed (GREEN).
- `python3 -m unittest discover -s tests -q`: 129 tests passed, 0.748 seconds.
- `git diff --check`: clean.
