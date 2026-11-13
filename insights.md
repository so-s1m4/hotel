# PLATO-40: evidence-based operating and design review

Dataset: **dev**, 365 days, `2026-01-01T00:00:00Z..2027-01-01T00:00:00Z`.
Retrospective analysis of the fictional synthetic property. This is not a claim about real PLATO buildings or current CLARA capability.
Data credit: PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track (CC BY 4.0).

## Measured findings

- Comfort: 837,046 valid temperature samples with event-derived occupied state; 63,266 below the comfort band (7.56%), 209,068 above. Each sample represents a 15-minute observation; these counts are not independent guests or labelled incidents.
- Data quality: 7,240 missing temperature samples; 40 duplicate timestamp/room readings skipped; 0 out-of-order readings excluded. Unknown occupancy is retained, never inferred from motion.
- Water: 22,969 intervals have valid matched readings from the main meter and every room. Median main-minus-room residual is 3.60 L/15 min. Common areas, irrigation and losses are included; this is **not** measured leakage.
- Water quality: 8103 negative room-meter deltas (possible resets or artefacts; not confirmed exchanges) and 5,267 missing/invalid room-meter samples; negative deltas and gaps are excluded from consumption.
- Energy: observed building load 171,394.6 kWh, HVAC 58,159.9 kWh, PV 55,269.1 kWh. Observed import cost 27,639.32 EUR across 35,040 price-covered samples; export revenue is not assumed.
- Actual checkout→observed cleaning-completed median: 75.0 minutes over 3,714 paired events. This is not a room-release or service-level guarantee.

### Logistics and staffing observations

- `VEND1:stock_drinks:recorded_empty_samples`: 350 recorded samples
- `WASTE_MAIN:recorded_full_samples`: 72 recorded samples
- `WASTE_MAIN:recorded_ge80_samples`: 176 recorded samples
- `cleaner:sick:shifts`: 24
- `robot:out_of_service:shifts`: 13
- `technician:sick:shifts`: 3
- `technician:vacation:shifts`: 17

## CLARA: operating recommendations

1. Prioritise sustained discomfort in observed occupied rooms, using the 63,266 cold samples as a review pool, not ground truth. Separate open windows, defrost and sensor gaps before dispatch. Validate thresholds on held-out data and report false positives; never lower occupied comfort to exploit a cheap/expensive tariff.
2. Retain availability timestamps, verify staff absence before reallocating jobs, and measure checkout→human-cleaning latency against the observed 75.0 minute median. Add an explicit inspection/release event rather than treating clean telemetry as authorisation.
3. Trigger lead-time-aware waste/restock forecasts, reusing scheduled visits when they precede predicted shortage. Verify actual collection/delivery instead of treating an order or simulated robot task as a physical state change.

## PLATO: future-building design recommendations

1. Instrument common-area water branches and meter-replacement events. The 22,969 matched main/room intervals show measurable unallocated consumption, but the provided sensors cannot attribute it to irrigation, common use or leakage. Additional submeters address this demonstrated attribution gap; no quantified leak saving is claimed.
2. Provide explicit room inspection/release signals and resilient room/gateway telemetry. The 7,240 missing temperature records and separate housekeeping/occupancy semantics demonstrate observability gaps; test store-and-forward and outage reporting before deciding on a hardware retrofit.
3. Investigate thermal variation by orientation with matched area, floor, window and occupancy controls before changing envelope/HVAC design. The exposure table below is descriptive and does not establish a design defect.

| Orientation | Occupied samples | Below-band samples | Below-band rate |
|---|---:|---:|---:|
| N | 420,148 | 34,990 | 8.33% |
| S | 416,898 | 28,276 | 6.78% |

## Evidence and reproducibility

Source windows are the dataset coverage above. Metrics are in the companion analysis JSON, including per-room exposure and SHA-256 hashes. References:

- `room_telemetry.csv` + `events.jsonl`: temperature/occupancy counts, orientation exposure, observed cleaning pairs.
- `room_water.csv` + `water_main.csv`: contiguous positive deltas and strictly matched residual intervals.
- `energy.csv` + `tariff.csv`: observed energy and import-only expense, not counterfactual savings.
- `logistics.csv` + `staff_roster.csv`: recorded capacity/stock states and final retrospective shift counts.

Reproduce with `python -m engine.annual --data-dir PATH --out insights.md --json analysis.json`.

## Target architecture

Time-stamped source adapters and durable event store → causal indexed gateway → guest/housekeeping, water, maintenance, energy and logistics agents → shared resource/capacity planner → central safety gate → authenticated human approval and guest consent → robot/BMS/vendor adapters (mock-only here) → observed verification → annual operating/design feedback. In a real system, commands need acknowledgement, durable idempotency, consent scope/expiry and conflict recovery.

## Limits and uncertainty

No participant ground truth exists. Findings describe synthetic observations, not real-world savings, true fault counts or causal design effects. Missing intervals are not imputed as zero. Occupancy before the first lifecycle event stays unknown. Room reassignment changes occupancy only for a booking already observed in that source room; a booking reassignment alone does not prove entry. Source ordering and 15-minute semantics are assumptions; inconsistent telemetry is excluded and counted. Water flushing/cleaning may explain vacant-night use. Full-bin readings are capped and do not quantify overflow. Annual analysis may use the whole historical dataset only after replay; it must never feed future knowledge back into decisions.
