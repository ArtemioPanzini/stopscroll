# Schema 1 payload contract for controller, provider and CLI

Wire payloads used by the Android controller, shell-only provider and desktop CLI.
Policy schema, canonical bytes and time semantics are defined in the
[Android design](../docs/android-vpn-design.md).

## Envelope

Request fields: `schema: 1`, canonical lowercase UUID `request_id`, `op`, `args`;
mutations additionally require nonnegative integer `expected_revision`.
Exactly five operations; their argument fields are listed in the
[USB protocol and CLI section](../docs/android-vpn-design.md#6-usb-протокол-и-cli).

Reply fields: `schema: 1`, echoed `request_id`, boolean `ok`, nonnegative integer
`revision`, `applied_revision` (nonnegative integer or null), and exactly one of
`data` (object for success) or `error` (object for failure).

Error is exactly `{code: string, message: string}`. Code is an uppercase ASCII
identifier, 1–64 characters; the design's error-code list is illustrative, not
closed. A failure may additionally have `saved: true` only with `APPLY_FAILED`.
Do not emit `saved: false` or put it on success. Strings must contain valid Unicode
scalar values. A read RPC succeeding does not assert healthy filtering.

## Operation data

`policy.get`: `{policy: POLICY, policy_hash: SHA256}`.

`policy.replace`: `{changed: boolean, policy_hash: SHA256}`. Identical policy is
a no-op except independent due time transitions; it does not close a session or
reset spend. Preview and confirmation belong to the desktop CLI.

`session.open`: `{site: ID, granted_sec: integer, deadline_elapsed_ms: integer,
remaining_sec: integer}`. The granted duration is the actual reservation, not the
unclamped requested duration. Never return a successful grant before durable
reservation and current native application acknowledgement.

`session.close`: `{site: ID, closed: boolean, remaining_sec: integer}`. `closed`
is false when there was no active session to close. Replaying an existing request
ID returns its recorded result; it does not close a later session or refund twice.

`status` minimum data fields:

```json
{
  "policy_hash": "64 lowercase hex digits",
  "timezone": "Europe/Belgrade",
  "day_id": "2026-11-18",
  "reset_hour": 4,
  "default_session_min": 15,
  "clock": {"wall_ms": 0, "elapsed_ms": 0, "boot_count": 0},
  "protection": {
    "state": "active",
    "tunnel": true,
    "always_on": true,
    "lockdown": true,
    "ignoring_battery_optimizations": true,
    "preferred_state": "ready",
    "reason": null
  },
  "sites": {
    "video": {
      "mode": "limited",
      "budget_min": 45,
      "remaining_sec": 2580,
      "reserved_sec": 480,
      "session": {"start_elapsed_ms": 0, "deadline_elapsed_ms": 600000}
    }
  }
}
```

The example is a shape illustration, not a valid sampled clock/budget fixture.
The real snapshot uses one ClockSample. `remaining_sec` includes `reserved_sec`
still available in the current session; unreserved time is their difference.
Blocked groups have zero budget/remaining/reserved and null session. A closed
limited group has null session. Domain configuration is read through policy.get,
not duplicated into status. No request history, visited URLs or flow counters in
the production status payload.

Protection state is one of `active`, `degraded`, `stopped`, `unprotected`.
Preferred state is `pending`, `installing`, `ready` or `failed`. `reason` is null
or a human-readable string. Active requires current service/adapter receipt and
applied revision matching saved revision, verified Preferred, Always-on, lockdown
and the required own-package battery exemption. A missing journal on an initialized
installation is STORAGE_ERROR, not a fresh empty policy. Do not reset corrupt
storage through a normal replace operation. If no trustworthy state exists,
return STORAGE_ERROR; revision 0 is only an unavailable-state envelope placeholder
and never authorizes initialization/overwriting a damaged journal.

Neutral additional status diagnostics may include actual elapsed deadline/cutoff
timestamps. Data consumers validate the fields they use and may ignore additional
status diagnostics; policy and envelope fields stay strict. Self-reported status
does not replace separate-UID network acceptance or installed APK signature checks.

## Persistence and replay

Use the single journal for policy/budget and last-64 committed mutation outcomes.
Define pending intent/outcome handling explicitly so a crash between reservation,
native application and reply recording cannot reserve twice or invent applied
success. Persist an acknowledged successful outcome before returning success.
Never rerun an operation merely because a reply was lost. Storage/apply failures
must leave admission closed, with honest saved/applied distinctions; failed I/O
cannot be treated as an acknowledged durable result. Test crash/failure seams at
these boundaries and document any outcome that remains indeterminate.

The controller is the only durable writer. Deadline admission revocation and the
captured adapter's sweep must not wait on blocking controller diagnostics or
initialization. Keep the short admission claim independent of the serial durable
transaction. Physical acceptance must test the completed APK from a separate
application UID, including live traffic, sleep and reboot. Host tests and self-reported status do
not establish those results. USB commands are manual; there is no connection
watcher or background synchronization.
