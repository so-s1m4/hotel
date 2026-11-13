# Three-minute evidence demo

1. 0:00–0:25: run `python run.py --data-dir data/PLATO40_v2_quickstart --out outputs/demo_decisions.jsonl`.
   Show the event/decision counts and the paired audit. Model calls are off unless `--llm` is explicit.
2. 0:25–0:55: show a real S01 decision, source booking/room/staff references, conditional readiness,
   and a structured cancellation. Explain observed work versus proposed work and approvals.
3. 0:55–1:25: show S02 meter deltas and its availability diagnostics. Missing values are unknown,
   not zero; a possible leak or low-rate review is not a confirmed fault or repair.
4. 1:25–1:50: show S03 cold/overheating and HP1 evidence. Comfort bounds override price savings;
   the heating-only plant is never presented as an air conditioner.
5. 1:50–2:20: show S04/S05 maintenance and busy-period coordination, including real shift
   eligibility, observed busy holds, robots' limited tasks and pending-cost approvals.
6. 2:20–2:40: show bounded battery power/SOC and logistics proposals, along with what would
   verify a real result. Simulated effects never replace the replay's observations.
7. 2:40–3:00: show `insights.md` measured year-long findings and the independent validation JSON.
   State the coverage/ground-truth limits and how the unchanged command accepts a new data directory.

Use actual generated outputs and inspected source rows. Never stage a fabricated
incident, claim that a proposed message was delivered, or label this script as a
recorded video. Holdout results are produced only after the official dataset release.
