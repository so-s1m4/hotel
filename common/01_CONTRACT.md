# PLATO-40 v2 | Integration contract v1.0 (FROZEN)

## 0. The key separation

**Not the same document or same JSON format:**

1. `NormalizedEventV1`: **internal team contract** for data/event extraction. Лёша emits this, Максим consumes it. Defined in `common/contracts/schemas.py`, JSON Schema in `common/contracts/normalized_event_early_checkin.schema.json`.
2. `ReplayDataGateway`: **internal team Python API** for querying telemetry and known facts at a historical instant. Defined in `common/contracts/api.py`; Лёша implements, Максим calls.
3. `DecisionRecordV2`: **official organizer contract** for decisions, produced **only by Максим's engine**, one line per decision in `decisions.jsonl` or `decisions_holdout.jsonl`. Defined in supplied `DECISION_CONTRACT.md` and mirrored in `common/contracts/schemas.py`.

No LLM output can directly trigger `release_room`, dispatch technicians, water shutoff or guest reassignment. The decision engine alone determines actions and approvals based on observed evidence and constraints.

## 1. Fixed file tree and public integration points

```text
project/
  common/
    contracts/schemas.py             # frozen Pydantic models
    contracts/api.py                 # frozen ReplayDataGateway Protocol
    examples/*.json                  # traceable reference examples
    requirements-contract.txt
  ingestion/                         # Лёша owns
    __init__.py
    loader.py                        # physical CSV/JSON readers + indexes
    replay.py                        # ReplayDataGateway implementation
    guest_parser.py                  # LLM extraction, fallback and cache
    events.py                        # NormalizedEventV1 emitters
    cli.py                           # normalized-events export
  engine/                            # Максим owns
    __init__.py
    detectors/turnaround.py
    detectors/water.py
    detectors/energy.py
    planner.py
    safety.py
    executor.py
    decisions.py
    insights.py
    cli.py
  tests/                              # each owner adds own tests
  run.py                              # Максим owns final one-command pipeline
  README.md, LICENSE, .env.example
  decisions.jsonl, decisions_holdout.jsonl, insights.md
```

**Only mandatory cross-team imports:**

```python
from ingestion.replay import ReplayData
from ingestion.events import iter_normalized_events
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2

repo = ReplayData(data_dir="PLATO40_v2_quickstart")
for event in repo.iter_events():
    now = event.available_at_utc  # NEVER use a future known timestamp
    current_room = (repo.get_room_state(event.entities.room_id, now)
                    if event.entities.room_id not in (None, "BUILDING") else None)
    # Engine's detectors / planner create zero or more DecisionRecordV2 records.
```

**Agreed constructors and signatures (implement exactly):**

```python
class ReplayData:
    def __init__(self, data_dir: str | Path): ...
    def iter_events(self) -> Iterator[NormalizedEventV1]: ...
    def get_room_state(self, room_id: str, as_of_utc: str) -> dict | None: ...
    def get_booking(self, booking_id: str, as_of_utc: str) -> dict | None: ...
    def get_bookings_for_room(self, room_id: str, start_utc: str, end_utc: str,
                              as_of_utc: str) -> list[dict]: ...
    def get_staff_slots(self, start_utc: str, end_utc: str, as_of_utc: str,
                        role: str | None = None) -> list[dict]: ...
    def get_measurements(self, filename: str, start_utc: str, end_utc: str,
                         as_of_utc: str, entity_id: str | None = None,
                         metric: str | None = None) -> list[dict]: ...
    def get_weather_forecast(self, as_of_utc: str, until_utc: str) -> list[dict]: ...
    def get_tariffs(self, as_of_utc: str, until_utc: str) -> list[dict]: ...
    def get_events(self, start_utc: str, end_utc: str, as_of_utc: str,
                   room_id: str | None = None,
                   event_type: str | None = None) -> list[dict]: ...
```

`get_measurements`: accepted `filename` must be one of the supplied CSV time-series (e.g. `room_telemetry.csv`, `room_water.csv`, `water_main.csv`, `energy.csv`, `equipment.csv`, `logistics.csv`, `weather.csv`). Return source column names unchanged and include `timestamp_utc`; use `entity_id` to filter `room_id` or `asset_id` according to the file; `metric` filters long-format equipment/logistics only. Range is **[start_utc, min(end_utc, as_of_utc)]**. Nulls remain `None`, never 0. The query must not load entire Dev into memory per call.

**Stable `get_room_state()` return object shape:**

```json
{
  "room_id": "R308", "as_of_utc": "2026-01-05T22:37:53Z",
  "last_observed_utc": "2026-01-05T22:30:00Z",
  "temperature_c": null, "setpoint_c": null, "humidity_pct": null,
  "co2_ppm": null, "presence": null, "window_open": null,
  "housekeeping_state": null, "occupancy": "unknown",
  "current_booking_id": null, "evidence": []
}
```

The numbers/nulls above are a **shape template, not a factual reading**. `occupancy` is one of `occupied`, `vacant`, `unknown`, inferred from observed `checkin/stay_in_progress/checkout`, room changes and their timing. Never infer vacancy solely from `presence=0`. Do not introduce a `released=true` field unless the required release checks were actually observed. `evidence` is a list of human-readable references like `room_telemetry.csv:R308:2026-01-05T22:30:00Z`.

**Stable `get_staff_slots()` row shape:** preserve original `shift_id, resource_id, role, start_utc, end_utc, status, status_known_utc`, plus `effective_status_asof` (`scheduled|sick|vacation|out_of_service|unknown`). If status change has not become known, mark the status as planned/uncertain rather than pretending sick absence is already known. Derive `ONCALL_1` separately from building.json rules and explicitly mark simulated availability assumptions.

`get_bookings_for_room`: match booking `room_id` with planned arrival/departure window and only entries `created_utc <= as_of_utc`; beware `status=cancelled` has **no cancellation timestamp**, so it must not be treated as known historical status at the request time. The operator can mark status unresolved instead of claiming the room is free.

`get_staff_slots`: return overlapping shifts, enforce `status_known_utc` before applying sick/vacation/out_of_service statuses. A person may have a shift scheduled on paper but not actually be available. `ONCALL_1` is not in roster; its capabilities and time windows come from `building.json`.

`get_weather_forecast`: future target times are allowed **only** when forecast `issued_utc <= as_of_utc`. For each target select latest issuance then known. `get_tariffs`: include only rates with `published_utc <= as_of_utc`; future hours are allowed if already published. Any measured weather, HVAC, meter, access or guest event at a future time is NOT allowed.

`get_events`: only events with `timestamp_utc <= as_of_utc`; `get_room_state`: most recent telemetry `<= as_of_utc` plus guest occupancy derived from historical guest events, not mere motion. If no reliable data, return `None` for unknown quantities rather than assuming zero or empty.

**Clock ticks:** `iter_events()` must ALSO yield one `event_type="clock_tick"` per 15-minute interval from `manifest.json` (Dev: 35,040 ticks), otherwise water/HVAC incidents with no discrete event will never be detected. Tick envelope source may be `{file:"__simulation_clock__",record_id:"tick:<UTC timestamp>"}` and must be labelled generated, not original evidence. Sort guest events, raw operational events and ticks together by available timestamp. Only telemetry is queried through `ReplayDataGateway`, not emitted as 1.4M LLM events.

**Convenience:** all times are UTC strings with trailing `Z`. Local hotel hours use `zoneinfo.ZoneInfo("Europe/Vienna")`, including DST. `intents` are normalized via LLM only in guest requests. All other streams must be processed deterministically; avoid 1.4 million individual LLM calls.

## 2. `NormalizedEventV1` envelope

Model fields (exact names):

| Field | Type | Purpose |
|---|---|---|
| `schema_version` | exactly `"1.0"` | shared version |
| `event_id` | non-empty string | stable input event ID; e.g. `guest:GR000005`, `raw:E0001234` |
| `timestamp_utc` | UTC ISO string `...Z` | when trigger occurred |
| `available_at_utc` | UTC ISO string `...Z` | when agent could actually know it; event processing must sort by this |
| `event_type` | enum | `guest_request`, `operational_event`, `bms_alarm`, `booking_created`, `roster_update`, `work_order_created`, `clock_tick`, `unknown` |
| `source` | object | `{file, record_id, timestamp_utc?}` from raw data, never invented by LLM |
| `entities` | object | `{room_id, booking_id, guest_request_id, asset_id, resource_id}`; each nullable |
| `priority_hint` | enum/null | raw guest `priority`; NOT the engine's final priority |
| `payload` | JSON object | copied raw details plus parsed `intents`; preserve `request_type` |
| `extraction` | object/null | method, model name, review status, uncertain fields |

`guest_request` payload:

```json
{
  "request_type": "early_checkin",
  "original_text": "Wir kommen schon gegen 13:00 an, wäre ein früherer Check-in möglich?",
  "language": "de",
  "response_due_utc": "2026-01-06T01:37:53Z",
  "requires_guest_consent": "not_required",
  "intents": [{
    "intent": "early_checkin",
    "requested_time_utc": "2026-01-06T12:00:00Z",
    "requested_time_text": "gegen 13:00",
    "evidence_quote": "Wir kommen schon gegen 13:00 an",
    "details": {"approximate_time": true},
    "needs_review": false
  }]
}
```

This example is based on Quickstart's actual `GR000005` and `BK000173` booking. Date = booking arrival **6 Jan 2026 local**, requested 13:00 Europe/Vienna = **12:00Z**. `evidence_quote` means extract text, not LLM-generated reasoning. If text says "tomorrow" interpret relative to message date in **Vienna**; if no unambiguous date, use booking arrival only where justified, else null and needs_review. Do not convert "after midnight" to a fabricated exact time.

Other event classes (`checkin`, `checkout_early`, `cleaning_completed`, `bms_alarm`, etc.) are normalized deterministically from `events.jsonl`; preserve raw type and details in `payload`. Source `event_id` remains in `source.record_id`, while wrapper ID gets `raw:<source_event_id>`.

A guest message may contain **multiple intents** in `payload.intents`. Keep the dataset's explicit `request_type` **separate** from LLM interpretation; if they conflict, set `extraction.needs_review=true` rather than silently replacing dataset data. If there is no model or it fails, use `request_type` alone, `method="source_label"` or `"fallback"`, set time unknown/null and `needs_review` if needed; still generate a valid event.

## 3. Official `DecisionRecordV2` output

Required in `decisions.jsonl` (one JSON object **per decision**, UTC, stable order):

| Required | Exact type | Rule |
|---|---|---|
| `event_id` | string | UNIQUE DECISION ID, not input raw ID; multiple actions may share an episode |
| `timestamp_utc` | UTC string | decision time, cannot exceed available evidence time |
| `affected_assets` | nonempty list[str] | genuine IDs from `building.json` (`R101`..`R410`, `HP1`, `WM_MAIN`, `BUILDING`, etc.) |
| `issue_type` | string | classifier e.g. `room_turnaround`, `water_leak`, `heating_comfort` |
| `action` | string | official action vocabulary preferred |
| `priority` | `critical|high|medium|low` | based on severity and deadlines |
| `reason` | string | concise evidence-grounded explanation |
| `requires_human_approval` | bool | MUST correctly reflect action safety requirements |

Official optional: `autonomy`, `episode_id`, `evidence` (**array of strings**, not structured objects), `expected_outcome`, `verification_plan`, `dependencies`, `guest_request_ids`, `requires_guest_consent`, `resource_id`, `confidence`. Source `DECISION_CONTRACT.md` in dataset is authoritative. `common/contracts/schemas.py::DecisionRecordV2` validates these plus internal safety consistency.

Permitted action vocab: `schedule_cleaning`, `dispatch_robot`, `adjust_room_setpoint`, `optimize_energy`, `block_room`, `release_room`, `reassign_room`, `request_inspection`, `dispatch_technician`, `order_service`, `notify_guest`, `flag_anomaly`, `defer_action`, `request_human_approval` (extra ones only if documented in final README).

One incident `episode_id` can produce several `event_id` decisions (schedule cleaning + notify guest), but do not reissue identical alerts every 15 minutes. A repeated alarm during one open episode counts as **one** detection; spam is penalized. `dependencies` contains prior decision IDs or explicit prerequisite IDs. If an action requires human approval, log the **proposed** action with `requires_human_approval=true`, then a separate executor records `pending_approval`; never execute it automatically.

## 4. Time and causality rules (mandatory)

- Actual dataset Quickstart is **5–26 Jan 2026**, Dev is **1 Jan 2026–1 Jan 2027**, independent runs. No merge of independent runs, no hardcoded dates/IDs for detection.
- `bookings.csv`: `created_utc` says when plan becomes known; `arrival_utc` and `departure_utc` are **scheduled** times, not actual guest movements. `events.jsonl` supplies actual movements.
- `guest_requests.csv`: `timestamp_utc` = request known; `request_type` already labelled; `priority` a hint; `requires_guest_consent=required` is a requirement, NEVER consent already obtained. No request is answered in the dataset.
- `room_telemetry.csv`: observations at 15-min intervals; `presence` is motion, not proof of guest occupancy; housekeeping state `clean` != room cleared if occupant, block, or inspection missing.
- `staff_roster.csv`: `status_known_utc` controls knowledge of sickness/absence; do not silently use future shift statuses.
- `work_orders.csv`: `created_utc` provides known order; `completed_utc` is future outcome until it occurs; final `status` is NOT known in the past; meter exchanges cause counter resets.
- `room_water.csv`/`water_main.csv`: `meter_total_l` cumulative, calculate differences; negative delta can indicate meter exchange, not negative usage. Main meter covers common areas and losses.
- `tariff.csv`: future prices known only after `published_utc` (typically prior day 13:00 Vienna). `weather_forecast.csv`: future forecast target permitted after `issued_utc` (06:00 Vienna); no future actual weather.
- `events.jsonl` BMS alarms delayed/incomplete/false, not ground truth. No unseen labels; do not optimize for specific Dev event IDs.
- Empty CSV cell means missing, never zero. Data contains duplicate `room_telemetry` rows (Quickstart 2, Dev 40 identical timestamp-room duplicates). Deduplicate and record it.

## 5. Safety levels (checked by engine, not by LLM)

| Level | Typical actions | Requirement |
|---|---|---|
| Autonomous | `schedule_cleaning` if staff actually available, `dispatch_robot` safely, `notify_guest` as simulated attempt, setpoint within occupied comfort 20–23 °C, safe load shifts, flag anomaly | allowed in mock executor with audit |
| Human approval | dispatch technician/contractor, block room, move an already booked guest, anything with notable expense | `requires_human_approval=true`, executor stays pending |
| Human approval **and guest consent** | enter occupied room, water shutoff, safety-critical intervention | both approval and consent flags `true`, consent not presumed |

Robots only clean floors/carry waste; they cannot certify or release rooms. `ONCALL_1`: 15:30–22:00 local, ~45 min lead, one room at a time, high cost and for waiting guests only. Occupied comfort band from `building.json` = 20–23 °C; no mechanical cooling. **Never release an uncleaned room.** Proposed guest messages ≠ delivered messages; simulated effects ≠ measurements.

## 6. Exact integration acceptance test

1. With **Quickstart** directory, Лёша's `ReplayData.iter_events()` produces `NormalizedEventV1` objects in order by `(available_at_utc, event_id)`; a real `GR000005` carries request_type, source reference and extracted/unknown requested time. All source IDs and timestamps are traceable.
2. The mocked LLM network failure doesn't stop replay; it returns source-only event with `needs_review` and no hallucinated time.
3. `ReplayData.get_room_state('R308', '2026-01-05T22:37:53Z')` only uses observations up to that time; no future checkout/cleaning data.
4. Person B's CLI runs with `--data-dir PLATO40_v2_quickstart --out decisions.jsonl`, returns valid **official** decisions, detects S01–S03 from data and avoids unsafe room release.
5. The exact same CLI with `PLATO40_v2_dev` runs without hardcoding incident dates, without loading every LLM call for telemetry, and writes one decision per JSON line. No repeated alert spam per episode.
6. `python -m pytest` passes contract tests, at least one test per scenario, tests for approval/consent, no lookahead, no false-leak from meter reset, absent staff, duplicate telemetry.
7. Final documented **one command** accepts a data directory and writes `decisions.jsonl`. Run unchanged on decrypted holdout when released, commit as `decisions_holdout.jsonl` after `git tag pre-holdout`.

## 7. Changes and ownership

- **Never let LLM invent** IDs, readings, dates, authorizations, decisions, actual delivery, booking statuses, safety conditions.
- Source references belong to ingestion, priorities and action approvals to engine.
- Until both modules are ready, the engine can use `common/examples/normalized_event_early_checkin.json` and a deterministic fake `ReplayData` for unit tests.
- If LLM source intent disagrees with raw request_type, keep both and review. `confidence` output is a self-assessment, not calibrated; omit it or keep `null` rather than claiming statistical certainty.
- Only complete **actual observed year-long analysis** justifies `insights.md`. A synthetic example in the organizer archive is not evidence.
