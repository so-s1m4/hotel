# PLATO-40 v2 — Dataset reference

**Synthetic offline replay of a fictional 40-room self-check-in hotel.** Not a real PLATO building, not a CLARA export. Proposed actions do not change later observations. See `CHALLENGE.md` for the task and `DECISION_CONTRACT.md` for the required output.

## Conventions

- Timestamps: **UTC**, ISO 8601 (`2026-01-01T00:15:00Z`). Operations run on local time **Europe/Vienna** (check-in from 15:00, check-out until 11:00, housekeeping 07:00–15:30 local) — mind daylight saving time.
- Telemetry interval: **15 minutes**; a row stamped `t` describes the interval/state at `t`.
- **Empty field = missing value.** Never treat it as zero.
- Units: °C, %, ppm, kW, kWh, litres, EUR/kWh.
- IDs are consistent across all files and defined in `building.json` (rooms `R101`–`R410`, assets `HP1`, `AHU1`, `PV1`, `BAT1`, `WM_MAIN`, `ACS1`, waste bins `WASTE_F1`–`WASTE_F4`, container `WASTE_MAIN`, vending machine `VEND1`, room water meters `WM_Rxxx`, gateways `GW_F1`–`GW_F4`, staff/robot resources).
- **Time of availability:** some records state when information became known (`created_utc`, `published_utc`, `issued_utc`, `status_known_utc`). A realistic decision at time *t* only uses information known at *t*.

## Files

### `building.json`
Rooms (floor, area, orientation, bathtub), assets with nominal ratings, sensors and their gateway, staff and robot resources with capability notes, operating rules (`operations`) and constraints (`constraints`, e.g. comfort band 20–23 °C for occupied rooms, release rules, consent rule).

### `bookings.csv`
`booking_id, room_id, created_utc, arrival_utc, departure_utc, guest_count, channel, status`
- `arrival_utc`/`departure_utc` are the **booked** dates at standard times (15:00 / 11:00 local), not the actual check-in/out moments — those are in `events.jsonl`.
- `status`: `confirmed` or `cancelled`. Confirmed bookings can still be no-shows, leave early or get a different room.

### `access_log.csv`
`timestamp_utc, door_id, credential, result`
Door events from the access-control system `ACS1`: `door_id` is `MAIN_ENTRANCE` or a room ID; `credential` is a `booking_id` (guest), a staff/robot `resource_id`, or `UNKNOWN_CARD`; `result` is `granted` or `denied`. Exits are not logged.

### `room_telemetry.csv`
`timestamp_utc, room_id, temperature_c, setpoint_c, humidity_pct, co2_ppm, presence, window_open, housekeeping_state`
- `setpoint_c`: active setpoint (18 °C setback when unoccupied; guests may change it).
- `presence`: motion sensor within the interval (0/1) — guests can be in the room and asleep without motion; cleaners trigger it too.
- `window_open`: window contact (0/1).
- `housekeeping_state`: `clean`, `dirty`, `in_progress`, `inspection_required`.

### `room_water.csv`, `water_main.csv`
`timestamp_utc, room_id, meter_total_l` / `timestamp_utc, meter_total_l`
**Cumulative meter readings** in litres — derive consumption from differences. Meters can be exchanged (counter restarts; see `work_orders.csv`). The main meter also covers common areas (lobby, staff rooms, technical room, roof-garden irrigation) and small losses.

### `energy.csv`
`timestamp_utc, building_load_kw, hvac_electric_kw, pv_kw, battery_discharge_kw_signed, battery_soc_kwh, grid_import_kw, grid_export_kw`
`building_load_kw` includes `hvac_electric_kw` (heat pump) and ventilation fans. Battery: **positive = discharging**, negative = charging.
Balance: `grid_import + pv + battery_discharge ≈ building_load + battery_charge + grid_export`.

### `weather.csv`, `weather_forecast.csv`
Measured outdoor temperature and cloud cover per 15 min. Forecasts: issued daily (`issued_utc`, 06:00 local) for the next 48 h, hourly `target_utc`; forecast error grows with lead time.

### `tariff.csv`
`timestamp_utc, price_eur_kwh, published_utc` — hourly day-ahead price incl. grid fees, published 13:00 local on the previous day.

### `equipment.csv`
Long format `timestamp_utc, asset_id, metric, value`.
- `HP1` (air-source heat pump, space heating + domestic hot water): `heat_output_kw`, `electric_kw`, `supply_temp_c`, `status` (`running`, `standby`, `defrost`, other codes possible).
- `AHU1` (air handling unit with heat recovery): `airflow_m3h`, `filter_dp_pa` (filter differential pressure), `fan_kw`, `supply_air_c`.
No fault labels and no direct capacity measurement are provided.

### `logistics.csv`
Hourly long format `timestamp_utc, asset_id, metric, value`:
- `WASTE_F1`–`WASTE_F4` (`fill_pct`): floor waste bins, emptied by housekeeping in the afternoon when needed; robots *could* carry them (see `building.json`).
- `WASTE_MAIN` (`fill_pct`): central container, regular municipal pickup Tue + Fri 07:00 local; extra pickups can be ordered (~24 h lead time).
- `VEND1` (`stock_drinks`, `stock_snacks`): lobby vending machine stock in units; regular restock Mon + Thu 10:00 local; extra restock can be ordered (~24 h lead time).
Values are capped at 100 % — a full bin may be overflowing.

### `staff_roster.csv`
`shift_id, resource_id, role, start_utc, end_utc, status, status_known_utc`
Roles `cleaner`, `technician`, `robot`. `status`: `scheduled`, `sick`, `vacation`, `out_of_service`. A `sick` shift means the person does not work it; `status_known_utc` says when operations learned about it. The on-call housekeeper (`ONCALL_1`) has no roster rows; see `building.json`.

### `work_orders.csv`
`work_order_id, created_utc, asset_id, type, status, scheduled_utc, completed_utc, assigned_to, summary`
Preventive, corrective, postponed and metering work by the in-house technician or external contractors. Records are written by people — they can be late or terse.

### `guest_requests.csv`
`request_id, timestamp_utc, room_id, booking_id, channel, language, request_type, text, priority, response_due_utc, requires_guest_consent`
Synthetic multilingual free text (de, en, it, fr, es, nl). `room_id=BUILDING` is building-wide. `requires_guest_consent=required` means entering the occupied room needs consent — **it is not consent granted**. No request is ever answered in the data.

### `events.jsonl`
One JSON object per line with `event_id`, `timestamp_utc`, `type` and type-specific fields.
- Guest lifecycle: `checkin`, `checkin_early`, `checkin_after_wait`, `arrival_waiting_room`, `room_reassigned`, `checkout`, `checkout_early`, `no_show`, `stay_in_progress` (stay began before the data window).
- Housekeeping: `cleaning_started`, `cleaning_completed` (with `resource_id`), `robot_floor_cleaning_started/_completed`, `oncall_housekeeping_called`.
- Logistics: `floor_bin_emptied`, `waste_pickup`, `waste_bin_full`, `vending_restocked`, `vending_out_of_stock`.
- `bms_alarm` with `alarm_code`, `state` (`raised`/`cleared`), `asset_id`, `value`: rule-based building-management alarms. **They are delayed, incomplete and sometimes false** — a clue, not ground truth.

### `manifest.json`
Profile, time range, units, conventions and known limitations.

## Licence

Synthetic data, provided under **CC BY 4.0** (attribution: "PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track") PLATO and CLARA are names of Rhomberg products; PLATO-40 is fictional and all its properties are invented.

## Known limitations

Simplified, uncalibrated models (thermal zones, heat pump, ventilation, water use, waste and vending); synthetic weather and prices; one in-house technician; guest behaviour and messages are invented. Data-quality problems (gaps, outages, frozen or implausible values, duplicates, meter exchanges) are deliberately included. Benchmark results on this data are not real-world performance.
