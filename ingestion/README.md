# ingestion/ — data loading, time-causal replay, normalized events (module A)

Implements the frozen team contract in `common/` (`ReplayDataGateway`,
`NormalizedEventV1`). Python 3.11+, stdlib + pydantic only. No API key or
network is needed; with a key, guest messages are additionally parsed by an LLM.

## Run

```bash
python -m pip install -e '.[dev]'
python -m pytest -q common/tests tests/ingestion
python -m ingestion.cli --data-dir data/PLATO40_v2_quickstart --out outputs/events_quickstart.jsonl --no-llm
python -m ingestion.cli --data-dir data/PLATO40_v2_dev --out outputs/events_dev.jsonl --no-llm
```

`--llm` forces model extraction, `--no-llm` (alias `--dry-run`) forbids it, default
follows the environment (`.env.example`). `--no-ticks`, `--limit N`, `-v` are optional.
The CLI prints event counts and the de-duplication audit to stderr.

```python
from ingestion.replay import ReplayData
repo = ReplayData("data/PLATO40_v2_quickstart")          # one directory, never merged
for event in repo.iter_events():                     # sorted by (available_at_utc, event_id)
    now = event.available_at_utc
    room = (repo.get_room_state(event.entities.room_id, now)
            if event.entities.room_id not in (None, "BUILDING") else None)
```

## Event stream

| event_type | event_id | available_at_utc | source |
|---|---|---|---|
| `guest_request` | `guest:<request_id>` | request `timestamp_utc` | guest_requests.csv |
| `operational_event` / `bms_alarm` | `raw:<event_id>` | event `timestamp_utc` | events.jsonl |
| `booking_created` | `booking:<booking_id>` | `created_utc` | bookings.csv |
| `roster_update` | `roster:<shift_id>:<status>` | reconstructed plan publication; actual `status_known_utc` for observed changes | staff_roster.csv |
| `work_order_created` | `wo:<id>` | `created_utc` | work_orders.csv |
| `operational_event` (payload.type `work_order_completed`) | `wo:<id>:resolved` | `completed_utc` only (postponed orders have no status timestamp and stay `open`) | work_orders.csv |
| `clock_tick` | `tick:<UTC ts>` | every `interval_seconds` in the manifest range | generated (`__simulation_clock__`) |

Quickstart: 3,939 events. Dev: 65,434 events (35,040 ticks). The offline Dev export takes
a few seconds (machine-specific); with a warm LLM cache it is the same.

## Causality rules that are enforced

- Every getter clamps to `as_of_utc`; measurements use `[start, min(end, as_of)]`.
- **Bookings:** visible from `created_utc`. The final `status` column has no timestamp, so
  it is always returned as `"unresolved"`. What was actually observed is in
  `observed_state` (`not_arrived|waiting_for_room|checked_in|checked_out|no_show`),
  `observed_room_id`, `observed_checkin_utc`, `observed_checkout_utc`, `evidence`.
- **Roster:** a shift is visible from a reconstructed publication time 14 days before start.
  This is a planning assumption supported by every supplied scheduled row, not an
  observed publication timestamp for absence rows; it is applied uniformly to all shifts. Sick/vacation/out_of_service is applied only from
  `status_known_utc`; before that the row looks exactly like a normal planned shift.
  `effective_status_asof` is added. `ONCALL_1` slots are derived from `building.json`
  (15:30–22:00 local, DST-aware) and marked `simulated_assumption: true`.
- **Work orders:** at creation `status="open"`, `completed_utc=None`. `summary` is always a
  string: corrective summaries describe the diagnosis/outcome and are `""` until
  `completed_utc`; preventive/metering summaries describe planned scope (which taps are
  flushed, which meter is exchanged) and are visible from creation. Postponed orders have
  no timestamp for the status change, so they stay `open` with `""`. The full row appears
  with the `wo:<id>:resolved` event. `repo.get_work_orders(as_of_utc, asset_id=None)` is a
  convenience getter outside the frozen Protocol.
- **Tariffs / forecasts:** only rows with `published_utc` / `issued_utc` <= as_of, from the
  hour containing `as_of` up to `until`; per forecast target the latest known issue.
  Price history: `get_measurements("tariff.csv", ...)`.
- **Room state:** latest telemetry row <= as_of, blanks stay `None` (no forward fill).
  `occupancy` comes only from lifecycle events (`stay_in_progress`, `checkin*`,
  `checkout*`); `presence` is never used. A room with no observed lifecycle event yet is
  `unknown` (absence of events does not certify vacancy); in-house room moves are followed. There is no `released` field.
- Blank CSV cell = `None`. Duplicate `(timestamp, key)` rows are dropped (first wins) and
  counted in `repo.audit` (Quickstart 2, Dev 40 telemetry duplicates).
- `get_measurements` files: room_telemetry, room_water, water_main, energy, weather,
  equipment, logistics, access_log, tariff. Unknown file -> `ValueError`; unknown
  entity -> `[]`; unknown room in `get_room_state` -> `None`.

## Guest messages

`payload` keeps the dataset metadata (`request_type`, `language`, `channel`,
`response_due_utc`, `requires_guest_consent` as the original string, `source_priority`)
separate from `payload.intents` (extraction). Without a model, or on any model error,
the intent is the dataset label with `needs_review=true`, no time is invented, and
`extraction.method` is `source_label`/`fallback`. If the model's intents do not contain
the dataset label, both are kept and `extraction.needs_review=true`.
