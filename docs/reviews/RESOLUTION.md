# Independent review resolution

The supplied [independent review](plato40_engine_review.md) examines older source/output
identities. Its findings are evidence to reproduce, not current-state certification.
This change preserves the frozen `common/` API and does not access holdout data.

| Finding | Resolution | Evidence and remaining boundary |
|---|---|---|
| F1 future work-order narratives | Merged ingestion fix `db9abf1` | Historical creation envelopes are identical under alternative future status/summary outcomes; `test_external_review.py`. Observed completion updates engine context by work_order_id. |
| F2 observed busy cleaners | Fixed causal observed-work calendar | Starts remain busy until matching known completions; unknown performers create capacity uncertainty. New actual work cancels incompatible plans, logged as structured cancellations plus official dependencies. Pending approvals have separate capacity holds. |
| F3 simultaneous identical stay updates | Booking-scoped outbox and semantic deduplication | Multiple contributing source IDs are retained. Explicit new questions, accepted changed estimates and corrections survive. Same-event withdrawal/correction proposals coalesce. Independent output validator checks exact duplicate groups. |
| F4 recovery boundary | Added sustained recovery margin | Entry threshold remains the building's comfort boundary. Recovery needs consecutive observations inside the band by 0.25 °C (configurable), rather than barely touching 20 °C. This is a prototype hysteresis policy, not a measured false-positive improvement. |
| F5a low-rate coverage | Extended cautiously; not fully resolved | Separate 3 h low-rate review requires a quiet independent earlier-night baseline and emits only `flag_anomaly`. R208 still does not qualify where that independent baseline is unavailable/not quiet. No claimed recall increase or retrospective label fitting. |
| F5b missing main-room balance | Explicit availability diagnostics | Incomplete room sums are never zero-filled/interpolated. Per-room invalidity and main-window/baseline exclusions are exported. Unobservable residual remains unobservable; additional branch meters are a design recommendation. |

## Further defects found and corrected during integration

- Missing actor/role assignment and multi-room use of one room snapshot at the safety gate.
- Consent bypass via `entry_required=False`; unassigned technician proposals cannot execute after a grant.
- Executable parameters changing between proposal and approval under one decision ID.
- Silent cancellation of a future reservation leaving the old job apparently active in output.
- S05/robot plans surviving a new known absence; dependent jobs now retire together.
- Stale old-room requests after observed check-in/reassignment; completed cleaning supersedes older dirty telemetry.
- Two pending ONCALL offers overlapping; separate tentative holds now consume planning capacity without executing.
- Conflicting event IDs, caller mutation of retained event/decision objects, and same-time episode reopening reusing IDs.
- Stale valid weather hidden by a newer missing observation, and old forecast/tariff values resurrected after a bad update.
- Water recovery streaks surviving missing observations, future scheduled flushing explaining present flow, and missed room moves.
- Historical full/empty logistics readings reused after actual pickup/restock; wrong-container pickups releasing capacity.
- S05 considering only new arrivals, robot-only room jobs masquerading as human cleaning, and overdue service plans duplicated.

Each area has isolated negative/positive regression tests in `tests/engine/test_*_audit.py`.
The independent file-level validator lives in `engine/validation.py` and has separate
regression tests. It rejects older known-bad ONCALL output as well as detecting
busy-source conflicts and duplicate notices; it is not a success-only checker.

## Verification scope

Run the tests, then run the same offline pipeline separately for Quickstart and Dev.
Validate each decisions/audit pair, including interval truncation at structured
cancellation times. Repeat in a new Python process and compare decisions and audit
bytes. The validation reports identify exact input/output SHA-256 hashes.

A clean schema, audit join and scheduling check do not prove all decisions are
optimal, all incidents are detected, or all unknown future data is safe. No incident
labels, precision, recall, real savings or jury score are claimed. See the validation
reports and run manifests for the tested artifact identities and diagnostics.

## Verified artifact snapshot

- Full local suite: 538 passed (`python -m pytest -q`).
- Quickstart: 3,939 events, 335 decisions and 335 matching audit rows.
- Dev: 65,434 events, 6,341 decisions and 6,341 matching audit rows.
- Both default offline runs: zero gate rejections; independent validator reports
  zero live resource/room overlaps, known-busy conflicts and exact duplicate notices.
- Explicit pre-start cancellations: 4 Quickstart and 14 Dev. Pending capacity is
  audited separately from execution, with no effects applied.
- Two full Dev processes with Python hash seeds 11 and 71 produced byte-identical
  decisions and audit. A real cross-process evidence-order difference was found
  during verification and fixed by canonical serialization before this final check.
- Warm tariff/forecast/entity lookup indexes are tested against reference scans.
  Microbenchmark gains are not presented as end-to-end acceleration; measured full
  run timings and diagnostics are in the verification manifest.

Artifact identities: [Dev](dev_validation.json), [Quickstart](quickstart_validation.json),
[run/source hash manifest](verification.json). These counts describe this supplied
synthetic run and the validator's scope, not incident detection accuracy.
