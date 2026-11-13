# ЗАДАНИЕ B: МАКСИМ — Detection, Decision Engine, Safety, Mock Executor и сдача

**Скопируй ВЕСЬ этот файл в Codex / Claude Code вместе с папкой `common/` и двумя исходными архивами `PLATO40_v2_quickstart.zip`, `PLATO40_v2_dev.zip`, а также `PLATO40_v2_CHALLENGE.pdf`.**

Ты senior Python/optimization engineer. Не отвечай планом; нужно ПИСАТЬ РАБОЧИЙ КОД и запустить тесты. Это Rhomberg PLATO-40 v2 24h hackathon (реально синтетический offline replay, 40 комнат, 4 этажа). Напарник Лёша независимо делает **ingestion/ + LLM Guest Message Parser**, предоставляет `ReplayData` и `NormalizedEventV1`; ты делаешь **decision engine**, принимающий решения по всем данным, а не только по гостевым сообщениям. Твоя зона: `engine/`, `run.py`, `decisions.jsonl`, `insights.md`, итоговая интеграция, финальный CLI. Обновлённый brief говорит **14.11 14:00**, старый Case Book 15:00, готовься к 14:00. Holdout появляется около 11:00.

## ПЕРЕД КОДОМ

1. Распакуй Quickstart и Dev отдельно. Прочитай `common/00_READ_ME_FIRST.md`, `common/01_CONTRACT.md`, `common/02_DATA_AUDIT.md`, `common/contracts/schemas.py`, `common/contracts/api.py`, `common/examples/*`, реальные `README.md`, `CHALLENGE.md`, `DECISION_CONTRACT.md` и `building.json` внутри архива. **Заморозить общий контракт**: публичные схемы не переименовывать.
2. Поскольку ingestion ещё может быть не готов, создай тестовую заглушку с теми же методами `ReplayDataGateway` и считай `NormalizedEventV1` из `common/examples/`. Мок нужен только для модульных тестов, НЕ для отправки жюри.
3. Архитектура должна сохранять **причинность**: на шаге `now` нельзя читать будущие фактические датчики, событие checkout, окончательную отмену брони, будущую болезнь уборщика или ещё не опубликованный прогноз/тариф. `available_at_utc` — источник времени выполнения. Переход Quickstart→Dev — только заменой `--data-dir`, наборы НЕ склеивать.
4. Главное для оценки: рабочий one-command pipeline, evidence, мало false positives/alert spam, корректные согласования и результат на незнакомом holdout. Три сценария S01–S03 ОБЯЗАТЕЛЬНЫ, ещё одна рекомендация в `insights.md` ОБЯЗАТЕЛЬНА.

## ЧТО РЕАЛИЗОВАТЬ

```
engine/
  __init__.py
  detectors/
    __init__.py
    turnaround.py    # S01 early checkout/checkin and cleaning shift conflict
    water.py         # S02 leak, room meters, main-room residual, artifacts
    energy.py        # S03 occupied temperature, weather, prices, HP1 degradation
  planner.py         # choose feasible, prioritized actions and resource allocation
  safety.py          # hard invariant validation before any executor
  executor.py        # mock-only; no real actions; approvals remain pending
  decisions.py       # serialize official DecisionRecordV2 JSONL, dedup incidents
  insights.py        # evidence-based CLARA/PLATO annual report
  cli.py             # commands for test/scenario runs
run.py               # python run.py --data-dir PATH --out decisions.jsonl
```

Input: `ingestion.replay.ReplayData(data_dir)` with full `common/contracts/api.py` methods, `repo.iter_events()` emits `NormalizedEventV1`, including `clock_tick` every 15 min, guest requests + operational events. Output: **official `DecisionRecordV2`** from `common/contracts/schemas.py`, not `NormalizedEventV1`. Official fields: `event_id` UNIQUE DECISION ID (do not reuse raw event ID), `timestamp_utc`, nonempty `affected_assets` from building.json, `issue_type`, `action`, `priority`, `reason`, `requires_human_approval`; optional `autonomy`, `episode_id`, `evidence` array of STRINGS, `expected_outcome`, `verification_plan`, `dependencies`, `guest_request_ids`, `requires_guest_consent`, `resource_id`, `confidence`. Follow exact official `DECISION_CONTRACT.md` and `decisions_sample.jsonl`; organizer sample illustrative, not source evidence. One JSON per line, sorted by `(timestamp_utc,event_id)`, and no repeated spam.

## S01 — Perfect Checkout / Turnaround

Take already-known `guest_request` (request_type often `early_checkin` + optional LLM-extracted time), bookings created at that time, `checkout_early`, housekeeping status and observed cleaning actions, roster and deadline. A request alone is not an anomaly. Detect risk when desired arrival clashes with old guest departure, dirty/in_progress/inspection-required room, crew capacity or known absence. Design a realistic job scheduler: cleaners only during actual eligible shifts and not double-booked; on-call ONCALL_1 15:30–22:00 Vienna (45m lead, one room simultaneously, costly, urgent waiting guests only). Robots only floor-clean and carry bins and NEVER certify room release. If capacity exists: `schedule_cleaning` with available `resource_id`, expected time and verification; can `notify_guest` about realistic estimated readiness with proposed/simulated message state. If not: `notify_guest` of realistic time, `defer_action` or `request_human_approval` if room reassignment/cost needed. Never `release_room` without proof vacant+clean+no block+passed inspection. `clean` by itself does NOT mean vacant/releasable. No invented staffing capacity or successful actions.

## S02 — Hidden Water Leak

Use **cumulative** `room_water.csv` and `water_main.csv` (15-min): consumption = positive deltas per meter, with resets/meter exchange from `work_orders.csv`, missing values and artefacts handled. Detect sustained unexplained night flow in vacant rooms; also compare main delta vs sum room deltas, but main includes common areas and legitimate losses. Control for occupancy, housekeeper cleaning, day/night, weather/irrigation if supported, scheduled operations and meter quality. BMS alarm only evidence hint, NEVER ground truth. For uncertain differences use `flag_anomaly` or `request_inspection`; `dispatch_technician` needs approval; entering occupied room or shutting off water additionally needs BOTH human approval+guest consent. No automatic room entry/water shutoff.

## S03 — Energy vs Comfort

Use `room_telemetry`, `energy`, `equipment` HP1/AHU1, `weather`, `weather_forecast` (published `issued_utc`), `tariff` (published `published_utc`), room occupancy events/booking. Comfort band occupied **20–23 °C**; vacant setback ~18. Detect persistently cold occupied rooms, cold-snap forecasts and degraded heat pump (e.g., heat_output_kw/electric_kw ratio conditional on defrost/status, not naive one sample). Choosing energy saving is never permitted to compromise occupant comfort. Allowed: adjust setpoint within bounds, shift permitted battery/load, flag anomaly, propose service with human approval when required. No mechanical cooling; don't claim to lower hot room temperature by changing heating alone. For every decision include why, expected outcome (SIMULATED), concrete verification, uncertainty.

## SAFETY, COORDINATION, OUTPUT

- Central `safety.py` must enforce every action irrespective of detector/LLM. Autonomous actions: schedule available cleaning, notify guest (simulated attempt), robot jobs inside capability, setpoints within occupied comfort band, safe demand shifting, flag anomaly. Human approval required: technicians, external contractors, room block, move booked guest, noticeable expense. Plus **guest consent** for occupied room entry/water shutoff/safety interventions; consent requirement != consent obtained.
- `MockExecutor` only simulates; if approval not granted, status `pending_human_approval` and NO execution. Track resource reservations to prevent double scheduling; planned outputs cannot overwrite historical observed telemetry. Treat `notify_guest` as proposed/simulated, not delivered.
- Episode tracking: deterministic `episode_id` per same asset, issue class and continuous incident. Rate-limit new alarms; one case may emit *different* decisions (e.g. cleaning + guest notification), but no duplicate 15-minute alerts. Cancel/suppress when conditions resolved or evidence insufficient. `confidence` omit or `null` unless estimated and validated; no fake 0.99.
- `evidence` string refs like `room_water.csv:R109:2026-04-09T02:00:00Z..06:00:00Z`, `guest_requests.csv:GR000005`, measured values written in reason with units. No raw source invented. Source IDs matter; `affected_assets` must be actual building IDs or `BUILDING`.
- Implement valid chronological decisions with `issue_type=room_turnaround`, `water_leak`, `heating_comfort` or other documented classes. Real action vocabulary: `schedule_cleaning`, `dispatch_robot`, `adjust_room_setpoint`, `optimize_energy`, `block_room`, `release_room`, `reassign_room`, `request_inspection`, `dispatch_technician`, `order_service`, `notify_guest`, `flag_anomaly`, `defer_action`, `request_human_approval`.

## LEVEL 3 И СДАЧА

Build `insights.md` from **actual Dev year** using measured counts/aggregates (not illustrative organizer sample). At least ONE evidence-based CLARA operating recommendation AND ONE PLATO future-design recommendation, with source/time windows, uncertainty, and concrete target architecture: agents, replay/real data interfaces, robots, safety human-in-loop. No fabricated savings. If insufficient evidence, state limitations.

Build **exact one command** `python run.py --data-dir PLATO40_v2_dev --out decisions.jsonl` (may optionally accept `--insights insights.md`). Must work with Quickstart and Dev and later untouched Holdout, without GUI or API key, in reasonable time. This is the principal required deliverable. Write robust README, `.env.example`, LICENSE MIT, tests, three-minute demo script and example artifacts actually produced from real data. Do NOT create fake incidents just to fill scores.

Holdout: BEFORE release `git tag pre-holdout` and ideally push. On 14 Nov ~11:00 use released password/new data, run unchanged with `python run.py --data-dir PLATO40_v2_holdout --out decisions_holdout.jsonl`, commit before **14:00**. Any changes afterward disclose in README. Supplied encrypted `.7z` cannot be used without password; DO NOT try to brute force/guess. The release may be organizer-provided separately.

## TESTS REQUIRED

- contract tests `PYTHONPATH=. pytest -q common/tests tests/engine`; scenario test S01 explicit conflict and nonconflict, S02 real-vs-benign flow, S03 comfort vs expensive tariff and forecast, human approval+consent, occupied/dirty room not released, sick cleaner never scheduled, meter reset excluded, missing data != 0, no future source leakage, repeat suppression, deterministic same input same output, valid official JSONL.
- Quickstart and Dev pipelines run using the same code. Check event counts, decision counts, false alert/episode ratio, runtime, audit quality. Propose clean fallback when guest-parser has no LLM endpoint.
- `python -m pytest` passes offline. No fake success claims in README or 3-min video.

## INTEGRATION CONTRACT

Use imports:
```python
from ingestion.replay import ReplayData
from common.contracts.schemas import NormalizedEventV1, DecisionRecordV2
repo = ReplayData(data_dir)
for evt in repo.iter_events():
    now = evt.available_at_utc
    # your detectors invoke repo getters AS OF now
    # planner -> safety gate -> mock executor -> one or more DecisionRecordV2
```

While ingestion not ready create a substitute for testing only using public `ReplayDataGateway`. Keep your code independent of physical CSV field positions and hidden future values. **Do not change common API independently or implement `ingestion/` yourself**; tell your teammate if needed.

**Implement everything you reasonably can now, run real tests, and report changed files, reproducible command, actual results and blockers.**
