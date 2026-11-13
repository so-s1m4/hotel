# PLATO × CLARA Future — The Autonomous Building Challenge

**One Building. One Year. Thousands of Decisions.**  
**Proximata Hackathon ’26 · Systems for Built Space · 13–14 November 2026 · Home of Innovation, Vienna**  
**Track partner:** Rhomberg · **Teams:** 2–4 people · **Track prize:** €1,500

> **Challenge:** Build a working prototype of the autonomous building of the future: AI agents, robots and automations that run a PLATO self-check-in hotel through a full year — detecting problems, deciding and acting safely, and feeding what they learn back into the next PLATO building.

## About Rhomberg

Rhomberg is a leading Austrian construction company. With **PLATO**, Rhomberg builds elevated modular timber buildings above existing parking spaces — new space without losing parking capacity — for employee and student housing, self-check-in hotels and serviced apartments. **CLARA** is the intelligence behind the building: sensors and operating data reveal inefficiencies, detect faults early and enable smarter building operations. *PLATO creates the space. CLARA makes it smarter.* Rhomberg brings this case to explore how both could grow into a learning, increasingly autonomous system — from operating a building to building the next one better.

## 1. The vision

Imagine a modular **PLATO** building operating as a self-check-in hotel, with **CLARA** as its intelligent building operating system. In the future vision **CLARA Future**, the building largely runs itself: bookings, check-in, access and guest communication are automated; heating, ventilation and energy are controlled on demand; robots clean and handle logistics; faults are detected early and service is organised automatically. Humans stay in the loop where safety, privacy or cost require it.

And the loop does not end in operations. Every year of operation teaches CLARA how to run the building better — and teaches PLATO how to **build** the next one better.

```
   PLATO (build)  ──►  CLARA (operate)  ──►  agents & robots (act)
        ▲                                            │
        └──────────  learn & feed back  ◄────────────┘
```

**Reality boundary:** **PLATO-40** is a fictional reference property with 40 rooms on four floors. It is **not** a real PLATO site, not an export from CLARA and not a claim that CLARA currently operates autonomous agents or robots. All data, including guest messages, is synthetic. **Every building property, weakness or finding you discover concerns the fictional PLATO-40 only — it is not a statement about real PLATO buildings.** Please say "PLATO-40" in your pitch when you present findings.

## 2. The problem

Building operations span reservations, access, room readiness, heating, ventilation, energy, water, waste, supplies, maintenance, staffing and guest communication. Signals sit in different systems, arrive late, drop out or are wrong. One change creates issues elsewhere: an early departure and an early arrival collide with a short-staffed cleaning shift; a running toilet hides in normal night-time usage; a degraded heat pump meets a cold snap during high-price hours; a busy weekend overflows the waste container. Today, people stitch this together by hand — and the lessons rarely reach the people who design the next building.

**Why it matters.** Rooms that are not ready on time, unnoticed leaks, cold rooms, overflowing bins and avoidable call-outs cost money, energy and guest trust — and every lesson that is not fed back gets built again. (No figures are claimed here: the dataset is synthetic.)

**Who is affected.** Guests; operations and front-of-house teams; cleaners; maintenance technicians and service contractors; facility and energy managers; building owners and investors; and the planners who design the next PLATO building.

## 3. Your mission — 24 hours

Use the **same provided datasets** as all other teams. Build a system that:

1. **Observes** — loads and correlates building, booking, access, telemetry, weather, tariff, equipment, logistics, staffing, maintenance and guest data.
2. **Understands** — detects operational risks and explains the evidence, including uncertainty, bad data and benign look-alikes.
3. **Decides & acts** — chooses feasible, prioritised actions across domains and **executes them through agents, robots or automations** within the autonomy rules (§7). Because the data is a replay, execution goes to a **mock executor or your own simulation**; label simulated effects as simulated.
4. **Coordinates** — respects staff and robot availability, room readiness, comfort, deadlines, guest consent and approvals.
5. **Verifies** — states which signals would confirm success or require escalation.
6. **Learns & feeds back** — derives evidence-based recommendations from the year: for **CLARA** (better operating rules) and for **PLATO** (better design of the next building).

Any architecture, language, rule engine, optimiser, ML model, LLM or agent framework is welcome. Multi-agent systems, robot task planning and visualisations are encouraged — but we reward grounded, working results, not buzzwords.

## 4. Levels and scenarios

### Level 1 — Operate & detect (mandatory)

- **S01 The Perfect Checkout.** An early departure, an early check-in request and a tight cleaning shift create a turnaround conflict. Never release an uncleaned room; prioritise cleaning; give the guest a realistic time.
- **S02 The Hidden Water Leak.** Unusual consumption — sometimes loud, sometimes barely above normal, sometimes only in the gap between the main meter and the room meters. Separate it from guests, cleaning and meter artefacts; inspect and escalate safely.
- **S03 Energy vs Comfort.** Cold weather, dynamic prices and reduced heating performance meet occupied rooms. Protect occupants, use the forecast, organise service — don't blindly minimise cost.

### Level 2 — Act autonomously (advanced)

- **S04 The Maintenance Cascade.** Filter degradation, missing sensor readings and an unavailable technician. Tell a data gap from real plant degradation and organise a realistic service.
- **S05 The Unexpected Weekend.** An event weekend fills the house while a cleaner calls in sick. Plan cleaning, robots and on-call staff; inform guests; never invent capacity.
- **Building & energy optimisation.** Use day-ahead prices, PV, battery and weather forecasts to shift load without leaving the comfort band.
- **Cleaning & logistics.** Robots, waste bins and the vending machine: plan robot tasks, order extra waste pickups or restocks before things run out.

### Level 3 — Learn & feed back (the loop)

- What should **CLARA** do differently next year (thresholds, cleaning strategy, pre-heating, maintenance intervals, alarm tuning)?
- What should **PLATO** build differently next time? Some properties of the building only show up over a year of operation — find them, prove them with data, and turn them into design recommendations.
- Sketch the **target system**: which data, agents, robots, technologies and interfaces would the real autonomous building need?

Each scenario class occurs **several times per year** with varying severity. The data also contains **benign look-alikes** and long stretches of normal operation. Scenarios describe problem classes, not timestamps or prescribed algorithms.

## 5. Data

Two participant datasets with identical structure, **independent synthetic runs** (do not concatenate):

- **Quickstart (21 days, January):** every scenario class occurs at least once — develop here.
- **Dev (365 days, 2026):** seasonality, multiple instances, look-alikes, a full year to learn from.

| File | Content |
|---|---|
| `building.json` | Rooms, assets, sensors, gateways, staff and robot capabilities, operating rules |
| `manifest.json` | Profile, time range, units, known limitations |
| `bookings.csv` | Reservations incl. creation time, channel, cancellations |
| `access_log.csv` | Door access events (main entrance, room doors): credential and result |
| `room_telemetry.csv` | 15-min temperature, setpoint, humidity, CO₂, presence, window contact, housekeeping state |
| `room_water.csv`, `water_main.csv` | **Cumulative** water meter readings in litres |
| `energy.csv` | Building load, HVAC electric, PV, battery (positive = discharge), grid import/export |
| `weather.csv`, `weather_forecast.csv` | Measured outdoor conditions; daily 48 h forecasts |
| `tariff.csv` | Hourly day-ahead electricity price with publication time |
| `equipment.csv` | Long format (`timestamp_utc, asset_id, metric, value`) for heat pump `HP1` and air handling unit `AHU1` |
| `logistics.csv` | Hourly fill levels of waste bins and the central container, vending machine stock |
| `staff_roster.csv` | Shifts of cleaners, technician and robots incl. absences and when they became known |
| `work_orders.csv` | Maintenance log: preventive, corrective and postponed work |
| `guest_requests.csv` | Synthetic multilingual free-text requests with deadlines and consent requirements |
| `events.jsonl` | Operational events (stays, cleaning, logistics) and building-management (BMS) alarms |

Timestamps are **UTC** (ISO 8601); telemetry steps are **15 minutes**; local operations follow Europe/Vienna. Full column reference in the dataset `README.md`. Data access: Proximata GitHub repository and download link, both shared on Discord.

**What makes it realistic — and hard:**

- **Data-quality issues occur throughout the year.** Blank means missing; never treat missing as zero. Expect implausible values.
- **BMS alarms are delayed, incomplete and sometimes false.** A clue, not ground truth.
- **Information has a time of availability** (forecasts, tariffs, roster changes). Decide only on what was known at that moment.
- A booking does not guarantee a clean, released room. **Two robots** clean floors and can carry waste bins; they **cannot certify or release a room**. One on-call housekeeper is available in the evening at limited capacity.
- The participant archives contain **no ground truth**.

## 6. What to build and show

**Minimum viable demo:** a reproducible pipeline plus an interface showing evidence, risk, decision, autonomy level / approval and verification for **S01–S03**, and at least one evidence-based feedback recommendation. A dashboard alone is not enough; a chat agent without grounded decisions is not enough either.

**Strong additions:** Level 2 scenarios, agents and robots executing tasks in a mock executor or your own simulation, energy optimisation, logistics planning, multilingual deadline-aware guest messaging, robust handling of bad data, a transparent audit trail, a convincing Level 3 feedback report and target architecture.

### What great looks like

Examples, not the only valid directions:

- **A building copilot with real autonomy:** a team of agents (operations, energy, housekeeping, maintenance, guest service) that detect, decide and act within the autonomy rules — with a transparent action trail and clean escalation to humans.
- **A turnaround and logistics orchestrator:** cleaners, robots, on-call staff, waste and supplies planned together against arrivals, early check-ins and event weekends, with realistic guest messages in the guest's language.
- **A learning loop for PLATO:** an analysis that turns a year of operation into evidence-based design recommendations for the next PLATO building and better operating rules for CLARA — plus a credible target architecture.

### Deliverables

- **`decisions.jsonl`** — one JSON object per decision, following `DECISION_CONTRACT.md` (sample included). Required: `event_id`, `timestamp_utc`, `affected_assets`, `issue_type`, `action`, `priority` (`critical|high|medium|low`), `reason`, `requires_human_approval`. Recommended: `autonomy`, `episode_id`, `evidence`, `expected_outcome`, `verification_plan`, `dependencies`, `guest_request_ids`, `requires_guest_consent`, `confidence`.
- **`insights.md`** — the Level 3 feedback: recommendations for CLARA and for PLATO, each with evidence from the data and its uncertainty, plus your target architecture (data, agents, robots, interfaces). Optionally also as `insights.jsonl` (see contract).

Action vocabulary: `schedule_cleaning`, `dispatch_robot`, `adjust_room_setpoint`, `optimize_energy`, `block_room`, `release_room`, `reassign_room`, `request_inspection`, `dispatch_technician`, `order_service`, `notify_guest`, `flag_anomaly`, `defer_action`, `request_human_approval`. Others are allowed if documented.

## 7. Autonomy rules — safe by design

The building may act on its own — within clear limits:

| Level | Examples | Rule |
|---|---|---|
| **Autonomous** | Schedule cleaning, dispatch robots, notify guests, adjust vacant-room setpoints or occupied rooms within the comfort band, battery/load shifting, order waste pickup or restock, flag anomalies | May be executed directly (in your mock executor); must be logged and explainable |
| **Human approval** | Dispatch technician or contractor, block a room, move a booked guest, anything with notable cost | `requires_human_approval = true` |
| **Approval + guest consent** | Entering an occupied room, water shutoff, safety-relevant interventions | `requires_human_approval = true` and `requires_guest_consent = true`; consent is **needed**, not granted |

- A proposed guest message is not proof of delivery; a simulated effect is not a measurement.
- Never schedule staff or robots that are absent; never release an uncleaned room.
- Never silently trade safety or comfort for an energy metric.

## 8. Holdout evaluation

To check that systems detect problems rather than memorising the dev data, a **third, unseen dataset** (same structure, different run) is released shortly before code freeze.

- Your pipeline must run as **one documented command that takes the data directory as an argument** and writes `decisions.jsonl`.
- **Before** the holdout release, tag your code **`pre-holdout`**.
- Holdout release: **Saturday, 14 November, ~11:00** on Discord. Run your pipeline **unchanged** and commit the output as **`decisions_holdout.jsonl`** before code freeze.
- Changes after `pre-holdout` must be justified in the README; the jury compares both states.
- An automated check (detection per incident, false alarms, alert spam, safety violations) gives the jury **input, not the final grade**.

## 9. Evaluation — 100 points

| Category | Points | What matters |
|---|---:|---|
| Technical implementation | **35** | Reproducible single-command pipeline incl. holdout run (10); evidence-based detection on dev **and** holdout (10); prioritisation and constraints (8); robustness to bad data and look-alikes (7) |
| Autonomous operation & impact | **30** | Safe, proportionate autonomy (10); cross-domain coordination of staff, robots, HVAC and logistics (8); guest communication and deadlines (6); energy/comfort/efficiency trade-offs (6) |
| Learning loop & vision | **15** | Evidence-based feedback to CLARA and PLATO (10); target architecture: data, agents, robots, interfaces (5) |
| Demo & presentation | **20** | Scenario demo and visualisation (8); decision and action trail (7); limitations and verification (5) |
| **Total** | **100** | |

**Fairness:** No points for fabricated execution, unsafe actions or savings presented as real. Simulated estimates are welcome if labelled as such. Repeated alerts for one incident count as one detection; alert spam counts negatively. False alarms are judged across normal operation. Ties go to the safer, more reproducible and better-evidenced solution.

## 10. Submission

One entry per team by code freeze on **Saturday, 14 November, 14:00 sharp**. Track judging runs 15:00–16:00 as a **live demo** of your solution (as described in §6); the track winner then pitches in the final round. Submit a **public MIT-licensed GitHub repository** in the Proximata organization (path assigned by the organizer), a **3-minute end-to-end demo video** and the written submission (title, one-line pitch, team, problem, solution, stack, links). Optional: live demo, PDF pitch deck.

Repository root: `LICENSE` (MIT), a runnable `README` (setup, run command, environment, architecture, assumptions), `.env.example` without secrets, `decisions.jsonl`, `decisions_holdout.jsonl`, `insights.md` and the `pre-holdout` tag. **Never commit credentials or API keys.**

## 11. Scope

**In scope:** autonomous-operation prototypes, AI agents and multi-agent systems, robot task planning, mock executors and your own simulations or digital-twin views, energy optimisation, logistics, guest communication workflows, auditability, feedback to building design.
**Out of scope:** integration with real PLATO/CLARA systems, physical robot hardware, real guest data, a full booking backend, real safety-critical control, claims of measured savings.

## 12. Mentors, jury and contact

Mentors and jury: **[TBC – announced at kick-off]**. Questions and data access: Proximata Discord; general contact **contact@proximata.io**.

## 13. Names, data licence and intellectual property

- **PLATO and CLARA** are names of Rhomberg products. Use them only to refer to this challenge context; do not use Rhomberg logos and do not name your own product or company after them.
- **Dataset licence:** the PLATO-40 datasets (quickstart, dev, holdout) are synthetic and provided under **CC BY 4.0** (attribution: "PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track"). You may include them or extracts in your public repository; prefer loading them via script.
- **Your work:** per the Proximata General Rules, code is published under the MIT licence, you keep the copyright, and the track partner Rhomberg receives a perpetual, worldwide, royalty-free, **non-exclusive** licence to use, modify, distribute and commercialise your work product; you keep the right to develop and commercialise it yourself. The original rules govern.
- Make sure third-party components you use are compatible with publishing under MIT, and never include confidential or personal data.

## 14. Technical honesty

PLATO-40 data is engineered for a 24-hour hackathon. Building, HVAC, water and logistics models are simplified and **not calibrated against a real property**. No action changes the replayed history. PLATO and CLARA are the inspiration; **PLATO-40 and CLARA Future are challenge concepts**, not product capability claims.
