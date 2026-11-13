# Integrated reliability verification

This is a new verification of the integrated engine and ingestion after the
historical [F5 snapshot](F5.md). The frozen runtime is `f8370bb` on
`fix/engine-reliability`. It does not replace or reinterpret the F5 JSON reports,
paired experiment or historical result counts.

The independent reproduction bundle lists 17 root-cause groups against runtime
`6f8cbc0` (validator also reviewed at `f40a15f`), plus a separately reported CLI
publication defect. These witnesses used controlled inputs; they do not establish
how often a defect occurred in Quickstart, Dev or a real building. Some original
scripts assert the buggy behavior, so their successful exit is not an acceptance
test. The repaired behavior is checked by the regression tests linked below.

## Verification state

The final test log reports **1,026 passed in 75.04 s** (`0:01:15`). All tests
in this run are committed, including the follow-up controls committed in
`0e9ad82`; the runtime code is frozen at `f8370bb`. Both full profile runs
completed independently, with no model calls:

| Profile | Input events | Decisions | Safety rejections | Independent output validation |
|---|---:|---:|---:|---|
| Quickstart | 3,939 | 342 | 0 | [342 audit joins, 0 errors](reliability_quickstart_validation.json) |
| Dev | 65,434 | 7,201 | 0 | [7,201 audit joins, 0 errors](reliability_dev_validation.json) |

The version 1.1 [verification manifest](reliability_verification.json) records
**25/25 passing checks**, the test-log hash, configurations, validator joins and
exact artifact identities. All 35 tracked runtime source hashes match the
before/after snapshot; all 17 input files per profile are hashed. The root
`decisions.jsonl` and `decisions_audit.jsonl` have been promoted from the validated
Dev generation and match its hashes.

Two independent Dev processes (`PYTHONHASHSEED=71` and `11`) produced byte-identical
decisions and audit. Configurations and diagnostics also match; output paths and
elapsed runtime are excluded from metadata equality. See the
[reproducibility report](reliability_reproducibility.json). The profile output
bytes also match the preceding `06524ac` generation after the A–E fixes.
Recorded elapsed times are 6.077 s for Quickstart and 134.385/134.920 s for the two
Dev processes. Counts and timings do not measure detector accuracy or provide
signed build attestation.

Independent external reviewers reported all original 17 groups and A–E green
across 102 bounded checks, with a separate G6 review covering all groups. These
are reported external review results; the local committed suite and hashed
replay/validator artifacts provide the directly reproducible evidence here.

## The 17 reported groups

Every row below describes a repaired behavior with a regression control. A test
checks the specified boundary; it is not a proof that every related input or
failure mode has been exhausted.

| Group | Reported failure | Current behavior and regression evidence |
|---|---|---|
| 01 | Approval or a cached result could revive a cancelled command. | Withdrawal is terminal for the old decision ID, including a replaced/omitted calendar and previously completed proposals. A new ID can reuse released capacity after fresh planning. [Terminal cancellation tests](../../tests/engine/test_terminal_cancellation.py), [calendar identity](../../tests/engine/test_cancelled_calendar_identity.py). |
| 02 | ONCALL housekeeping was accepted as a robot or technician. | Capability checks reject technical/robot work regardless of approval or catalog relabelling, while legitimate on-call housekeeping retains approval and room holds. [Capability boundaries](../../tests/engine/test_oncall_capability_boundaries.py). |
| 03 | A clean room retained an obsolete future ONCALL hold. | Fresh clean evidence can withdraw unnecessary cleaning; stale samples and clean samples preceding a newer checkout cannot cancel the required job. [Delayed-state controls](../../tests/engine/test_planner_delayed_state.py), [turnaround boundaries](../../tests/engine/test_turnaround_boundary_regressions.py). |
| 04 | A delayed start resurrected work already known completed. | Matching known completion suppresses an older late start; an unrelated completion cannot release other work or cancel a newer plan. [Delayed-state controls](../../tests/engine/test_planner_delayed_state.py). |
| 05 | Valid fractional-second timestamps crashed slot planning. | UTC formatting preserves subsecond instants; whole-second formatting remains unchanged. A `.500` request no longer produces a slot starting before the request. [Turnaround boundaries](../../tests/engine/test_turnaround_boundary_regressions.py). |
| 06 | A cancelled bin transfer stayed open and prevented retry. | Cancellation reconciles the transfer marker and capacity commitment; restored capacity permits a newly planned transfer. [Logistics lifecycle and capacity](../../tests/engine/test_logistics_capacity_boundaries.py). |
| 07 | A future bin transfer survived fresh destination-overflow evidence. | New observed destination capacity is checked against all existing commitments and can withdraw future transport. Elapsed simulated work and unrelated/too-early pickups do not invent freed volume. [Logistics lifecycle and capacity](../../tests/engine/test_logistics_capacity_boundaries.py). |
| 08 | Schema-accepted payload values crashed guest parsing inside the detector. | Non-guest payloads are not interpreted as guest intents; `request_type=[]` and `original_text=null` do not crash or authorize entry. Schema-invalid guest intent envelopes remain subject to contract validation. [Turnaround boundaries](../../tests/engine/test_turnaround_boundary_regressions.py). |
| 09 | A gateway exception consumed dedup identity or left partial audit/reservations without returned decisions. | Escaping event failures roll back owned state, including nested state and retained calendar identities. Identical retry matches a healthy run. A rejected candidate is separately tested to preserve other accepted work. [Event transactions](../../tests/engine/test_planner_transactions.py), [executor atomic boundaries](../../tests/engine/test_executor_atomic_boundaries.py). |
| 10 | Two waiting rooms received the same ONCALL slot despite available later capacity. | A copied planning calendar holds each proposal locally; the next room uses the next feasible interval. Actual approval/consent holds remain executor-owned. [Turnaround boundaries](../../tests/engine/test_turnaround_boundary_regressions.py). |
| 11 | A pending cancellation concealed overlapping reservations in the independent validator. | Only an effective, valid cancellation changes capacity; a cancellation merely awaiting approval cannot hide a live overlap or backdate a later effective withdrawal. [Validation approval boundaries](../../tests/engine/test_validation_approval_boundaries.py). |
| 12 | Incoming reassignment failed to withdraw room-entry work in the newly occupied room. | An observed occupied destination cancels both pending and simulated room-entry plans. A planned reassignment into a still-vacant room alone does not assert occupancy. [Delayed-state controls](../../tests/engine/test_planner_delayed_state.py). |
| 13 | A technician proposal's own pending hold triggered redundant external service. | A live hold belonging to that AHU incident suppresses the duplicate contractor proposal; unrelated or expired holds do not suppress a legitimate service review. [Maintenance pending lifecycle](../../tests/engine/test_maintenance_pending_lifecycle.py). |
| 14 | An older delayed completion/checkout replaced newer lifecycle memory. | Latest observed occurrence is monotonic; late old evidence does not clear newer busy work or invalidate cleaning after a newer checkout. [Delayed-state controls](../../tests/engine/test_planner_delayed_state.py). |
| 15 | Missing room identity skipped booking lookup and request expiry. | Missing/general identity can resolve from a booking known as of the request; genuinely unknown requests receive traceable clarification and retire at 48 hours. An explicit undeclared room is rejected without silently substituting a known booking's room. [Turnaround boundaries](../../tests/engine/test_turnaround_boundary_regressions.py), [unknown-room planner controls](../../tests/engine/test_planner_unknown_room.py). |
| 16 | Verified maintenance recovery left a future pending technician hold active. | Sustained observed recovery resolves the incident and withdraws its unexecuted proposal, releasing the hold with a structured cancellation. Mixed or insufficient recovery cannot do so. [Maintenance lifecycle](../../tests/engine/test_maintenance_pending_lifecycle.py), [delayed-state controls](../../tests/engine/test_planner_delayed_state.py). |
| 17 | The validator matched a roomless completion to unrelated unidentified cleaning. | Matching respects worker, room and role. A known worker's roomless completion clears its own work, but cannot clear an unidentified worker in another room. [Validation approval boundaries](../../tests/engine/test_validation_approval_boundaries.py). |

## CLI output publication

The separately reported CLI defect is covered by
[output preflight and publication tests](../../tests/engine/test_cli_output_preflight.py).
Invalid destinations fail before replay: directories, non-directory parents,
colliding/aliased outputs and paths aliasing source data or the configuration.
None of these checks truncates an existing output file.

All decisions, audit, rejections and requested insight/annual reports are
serialized to temporary siblings before publishing any file. Existing outputs
are backed up first. A failed replacement attempts to restore the preceding
generation; failed restoration reports `partial_publication`, lists possibly
changed paths and retains available recovery backups. `replay_complete` is
printed only after every artifact has been published successfully.

This is best-effort recovery from serialization/write/replace failures and
handled Python interrupts. It preserves cancellation control flow rather than
converting Ctrl-C into an ordinary exit or a successful publication.
The operating system does not provide atomic multi-file replacement; this is not
a power-loss transaction or a guarantee against every external filesystem race.

## Post-gate fixes A–E

The original 17-group review was followed by five additional boundary findings.
They are tracked separately rather than silently included in the earlier result.

| Gate | Additional failure boundary | Implemented control and regression evidence |
|---|---|---|
| A | A pending approval could reuse an old command timestamp after its original calendar had advanced, including by supplying an old explicit execution time or a replacement calendar. | The original authoritative clock requires fresh replanning. An already completed receipt remains an idempotent receipt, not new execution. [Executor clock boundaries](../../tests/engine/test_executor_clock_boundaries.py). |
| B | A non-string operational `type` could crash lifecycle interpretation or poison retries. | Such a value remains unknown and cannot create observed work or a dirty trigger. Ingestion quarantines malformed type/identity values under `source_fields`, preserving nested provenance and independent copies; identical engine retries remain idempotent. [Operational type boundary](../../tests/engine/test_operational_type_boundary.py), [ingestion sanitising/provenance controls](../../tests/ingestion/test_replay.py). |
| C | The validator's report output could hard-link to an input and overwrite source evidence. | Report publication rejects input aliases and preserves every source byte; a separate existing report remains refreshable. [Validator output aliases](../../tests/engine/test_validator_output_aliases.py). |
| D | A future whole-bin transport retained the old amount after fresh source growth, understating destination commitments. | Fresh growth withdraws the immutable old command. A new ID must reserve the updated amount; insufficient headroom blocks it, while stale/invalid/future readings cannot invent a smaller commitment. [Logistics capacity boundaries](../../tests/engine/test_logistics_capacity_boundaries.py). |
| E | A handled Python interrupt between file replacements bypassed rollback and could discard recovery backups. | `KeyboardInterrupt` and `SystemExit` trigger rollback and retain their original control flow. Interrupted/failed restoration preserves recovery backups and attaches partial-publication details. Before-first-replace interruption preserves old file identity; after-syscall interruption restores the preceding generation. [CLI interruption controls](../../tests/engine/test_cli_output_preflight.py). |

A related clock control checks a successfully rejected unknown-room input: it
still advances the planner's known time, so it cannot leave an older pending
approval executable against a stale clock. The rejection creates no room-entry
workflow. [Rejected-input clock control](../../tests/engine/test_planner_unknown_room.py).

## Additional integrated controls

The same integration includes input robustness checks from the ingestion merge,
[comfort notification scopes](../../tests/engine/test_comfort_notification_scope.py),
[preheat reassignment](../../tests/engine/test_preheat_reassignment.py) and
[sustained surge recovery](../../tests/engine/test_surge_recovery.py). These are
separate from the 17 root-cause groups and are not counted as additional field
incidents. Executor reservation/volume writes also have
[atomic failure controls](../../tests/engine/test_executor_atomic_reservations.py).

## Interpreting the output changes

The [read-only notice-scope check](reliability_notice_scope.json) classifies all
1,890 Dev heating-comfort notification proposals: 1,032 first notices in physical
episodes, 611 first notices to a newly observed guest in an existing episode and
247 upward-severity notices to the same guest. It finds zero same-guest,
non-escalating repeats in that selection. All selected notices have a causally
known occupied booking and telemetry no older than 30 minutes. The physical
episode IDs come from the engine; the classification does not independently
establish that a notice was needed or delivered. Its comparison baseline is
`213dfe4`, explicitly identified in the report.

Quickstart changed from the historical F5 count of 335 to 342: ten additional
heating-comfort notices, two fewer `schedule_cleaning` records and one fewer
`defer_action` record. This is not a maintenance-count increase. These count
changes alone do not measure an improvement in operational outcomes.

The reproducibility report separately verifies that the four Dev water records
are byte-identical to its pinned baseline export and that water diagnostic
counters match the historical F5 run. This preserves that dataset output; it
does not rerun or supersede the historical F5 paired experiment or establish
water-detection accuracy.

## Scope and limits

Quickstart and Dev remain separate synthetic datasets. The frozen public
contracts remain unchanged. The default CLI is offline, with no real actuation;
proposed messages are not delivered messages, pending approval is not approval,
and simulated work does not replace observed history. Unknown performers and
unverified room readiness remain conservative capacity/entry constraints.

The independent validator checks its implemented schema, audit joins, approvals,
cancellations, resource/room conflicts, known observed work and exact duplicate
notifications. Zero reported errors applies only to those checks on the hashed
files; it does not certify all engine behavior. The controlled regressions do
not measure defect prevalence, confirmed leak truth, precision/recall, operational
savings or real-world safety. Holdout has not been used, and the final recorded
demonstration remains separate work.
