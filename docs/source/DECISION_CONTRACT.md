# Decision output contract v2

One JSON object per line in `decisions.jsonl` (dev dataset) and `decisions_holdout.jsonl` (holdout dataset). UTF-8, one decision per line.

## Required fields

| Field | Type | Rule |
|---|---|---|
| `event_id` | string | Unique within the file |
| `timestamp_utc` | string | ISO 8601 UTC (`...Z`); the moment the decision is taken, using only information available at that time |
| `affected_assets` | array of strings | Non-empty; IDs from `building.json` (rooms `R101`–`R410`, `HP1`, `AHU1`, `BAT1`, `PV1`, `WM_MAIN`, `WASTE_*`, `VEND1`, `MAIN_ENTRANCE`, staff/robot IDs, `BUILDING`) |
| `issue_type` | string | Your classification, e.g. `room_turnaround`, `water_leak`, `heating_comfort`, `ventilation_maintenance`, `occupancy_surge`, `energy_optimization`, `logistics`, `data_quality` |
| `action` | string | Preferably from the vocabulary below |
| `priority` | string | One of `critical`, `high`, `medium`, `low` |
| `reason` | string | Short human-readable justification |
| `requires_human_approval` | boolean | `true` for anything in the approval levels of CHALLENGE §7 |

## Optional fields

| Field | Type | Meaning |
|---|---|---|
| `autonomy` | string | `autonomous`, `human_approval` or `approval_and_consent` (CHALLENGE §7) |
| `episode_id` | string | Groups repeated/follow-up decisions for the same incident (counts as one detection) |
| `evidence` | array of strings | References to observations, e.g. `room_water.csv:R109:2026-04-09T02:00Z..06:00Z` |
| `expected_outcome` | string | What should happen if the action works (simulated effects must be labelled as simulated) |
| `verification_plan` | string | Which signals confirm success or trigger escalation |
| `dependencies` | array of strings | Other `event_id`s or preconditions |
| `guest_request_ids` | array of strings | Related `request_id`s |
| `requires_guest_consent` | boolean | `true` if guest consent is needed before execution |
| `resource_id` | string | Staff member or robot assigned (must be available at that time) |
| `confidence` | number | 0.0–1.0 |

## Action vocabulary

`schedule_cleaning`, `dispatch_robot`, `adjust_room_setpoint`, `optimize_energy`, `block_room`, `release_room`, `reassign_room`, `request_inspection`, `dispatch_technician`, `order_service`, `notify_guest`, `flag_anomaly`, `defer_action`, `request_human_approval`. Other actions are allowed if documented in your README.

## Insights (Level 3)

`insights.md` is free-form. If you also provide `insights.jsonl`, use one object per recommendation:
`insight_id`, `target` (`PLATO` | `CLARA`), `topic`, `finding`, `evidence` (array), `recommendation`, `expected_benefit` (qualitative or labelled simulated estimate), `confidence`.

## Rules

- This is a **decision and action log**. Executions go to your mock executor or simulation — label simulated effects; never claim that the replayed telemetry changed.
- Do not invent measurements, guest names or personal data.
- A consent requirement is not consent granted; a proposed message is not a delivered message.

See `decisions_sample.jsonl` and `insights_sample.jsonl` — samples are **illustrative only** and do not refer to real observations in any dataset.
