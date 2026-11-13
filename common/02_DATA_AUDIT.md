# PLATO-40 v2 | Verified data audit

This report is based on inspection of the actual Quickstart and Dev archives, files and headers, not an assumed schema. Counts below are **row counts of parsed input files**, not validated incidents.

| CSV/JSONL | Quickstart | Dev | Primary fields / purpose |
|---|---:|---:|---|
| `bookings.csv` | 281 | 4,088 | booking_id, room_id, created_utc, arrival_utc, departure_utc, guest_count, channel, status |
| `access_log.csv` | 3,787 | 67,557 | timestamp_utc, door_id, credential, result |
| `room_telemetry.csv` | 80,642 | 1,401,640 | timestamp_utc, room_id, temperature_c, setpoint_c, humidity_pct, co2_ppm, presence, window_open, housekeeping_state |
| `room_water.csv` | 80,640 | 1,401,600 | timestamp_utc, room_id, meter_total_l (cumulative) |
| `water_main.csv` | 2,016 | 35,040 | timestamp_utc, meter_total_l (cumulative) |
| `energy.csv` | 2,016 | 35,040 | timestamp_utc, building_load_kw, hvac_electric_kw, pv_kw, battery_discharge_kw_signed, battery_soc_kwh, grid_import_kw, grid_export_kw |
| `equipment.csv` | 16,128 | 280,320 | timestamp_utc, asset_id, metric, value; HP1, AHU1 |
| `logistics.csv` | 3,528 | 61,320 | timestamp_utc, asset_id, metric, value |
| `weather.csv` | 2,016 | 35,040 | timestamp_utc, outdoor_c, cloud_cover_pct |
| `weather_forecast.csv` | 972 | 17,484 | issued_utc, target_utc, outdoor_c_forecast |
| `tariff.csv` | 504 | 8,760 | timestamp_utc, price_eur_kwh, published_utc |
| `staff_roster.csv` | 102 | 1,760 | shift_id, resource_id, role, start_utc, end_utc, status, status_known_utc |
| `work_orders.csv` | 9 | 76 | work_order_id, created_utc, asset_id, type, status, scheduled_utc, completed_utc, assigned_to, summary |
| `guest_requests.csv` | 112 | 1,612 | request_id, timestamp_utc, room_id, booking_id, channel, language, request_type, text, priority, response_due_utc, requires_guest_consent |
| `events.jsonl` | 1,406 | 22,744 | event_id, timestamp_utc, type and event-specific fields |

Other supplied files: `building.json` (40 rooms, 12 assets, staff/robots, sensors, constraints), `manifest.json`, `README.md`, `CHALLENGE.md`, `DECISION_CONTRACT.md`, `decisions_sample.jsonl`, `insights_sample.jsonl`.

## Real guest request labels

Quickstart: `early_checkin` 27, `temperature_cold` 23, `late_checkout` 11, `noise_complaint` 9, `extra_towels` 7, `wifi_help` 6, `waiting_for_room` 6, `arrival_delay` 4, `luggage_storage` 4, `cleaning_request` 4, `parking` 3, `maintenance_issue` 3, `invoice` 2, `air_quality` 2, `water_issue` 1.

Dev: `early_checkin` 349, `late_checkout` 271, `temperature_hot` 201, `noise_complaint` 173, `wifi_help` 140, `extra_towels` 112, `arrival_delay` 87, `parking` 57, `maintenance_issue` 50, `cleaning_request` 44, `luggage_storage` 44, `temperature_cold` 29, `invoice` 19, `access_issue` 17, `waiting_for_room` 14, `air_quality` 4, `water_issue` 1.

Languages: de/en/it/fr/es/nl. No message is answered/delivered in data. `requires_guest_consent` contains **strings** `required` or `not_required`, not booleans; consent is not automatically granted.

## Event kinds observed

`stay_in_progress`, `checkin`, `checkin_early`, `checkin_after_wait`, `arrival_waiting_room`, `room_reassigned` (Dev only), `checkout`, `checkout_early`, `no_show`, `cleaning_started`, `cleaning_completed`, `robot_floor_cleaning_started`, `robot_floor_cleaning_completed`, `oncall_housekeeping_called` (Quickstart), `floor_bin_emptied`, `waste_pickup`, `waste_bin_full` (Dev), `vending_restocked`, `vending_out_of_stock` (Dev), `bms_alarm`.

BMS codes seen: `SENSOR_COMM_LOSS`, `WATER_NIGHT_FLOW`, `ROOM_TEMP_LOW`, `ROOM_TEMP_HIGH` (Dev), `FILTER_DP_HIGH`, `AIRFLOW_LOW`. They can be delayed/false. Never use them alone as the label for a detected incident.

## Real data quality observations

- Quickstart has **2** identical duplicate `(timestamp_utc, room_id)` rows in `room_telemetry.csv`; Dev has **40** identical duplicates. Keep one and audit the deduplication.
- Dev room telemetry has **7,240 missing `temperature_c`**, **6,810 missing `co2_ppm`**, **2,470 missing `housekeeping_state`**; other columns have blanks too. `room_water.csv` has 5,267 missing `meter_total_l` in Dev. `equipment.csv` has 1,169 blank `value` fields in Dev.
- `status_known_utc` is present for staff and may be earlier or later than a shift's effective start; apply the latest known status only when historically available.
- `bookings.status` and `work_orders.status` are **not timestamped statuses**; treating them as if known at creation time can leak future information.
- Quickstart = 2026-01-05 00:00Z to Jan 26 00:00Z. Dev = Jan 1 2026 to Jan 1 2027. They are **independent**, NOT training/dev concatenation.

## Assets / operations verified

- 40 rooms (R101–R410), 4 floors, 2 guests max per room.
- Humans: CLEANER_1, CLEANER_2, CLEANER_3, TECH_1. Robots: ROBOT_1, ROBOT_2. On-call: ONCALL_1 (not on roster).
- Check-in 15:00 local, checkout 11:00 local, cleaners usually 07:00–15:30 local, technician usually 08:00–16:30 Mon–Fri; actual capacity must be checked from `staff_roster.csv`.
- Occupied comfort 20–23 °C, default 21 °C, vacant setback 18 °C. Heating-only HP1; no cooling.
- Robots only floor-clean and carry bins, no bathroom cleaning, guest entry or release certification. ONCALL_1 15:30–22:00 local, ~45-min travel, one room at a time, costly, intended for waiting guests only.
- A room can be released only if vacant, clean, not blocked, and inspection passed if required.

## Files not readable from this set

`PLATO40_v2_holdout_encrypted.7z` exists, but is encrypted. It cannot be used for evaluation until organizers provide a password/release; it was NOT read or analyzed here. The prompts do not use its contents. The v2 brief says release ~11:00 Nov 14.

## Deadline discrepancy

The older case book says **15:00**, but the updated `PLATO40_v2_CHALLENGE.pdf` and included `CHALLENGE.md` both say **14:00 sharp on Nov 14**. Confirm via event Discord; plan to be ready at 14:00.
