# PLATO-40 — Independent Dev engine review

Prepared 2026-11-13 by ChatGPT / GPT-6 Astra Pro with three parallel reviewers.

**Status update:** The work-order summary leak described below was corrected in db9abf1bbd5d69d7991f9a44e62cd8b0d7bc1c66. Independent verification found identical WO00035 creation events under alternate future outcomes; all 76 Dev work orders hide summaries at creation; five focused regression tests pass. The coordinator reports 337 tests passing and byte-identical complete offline decisions before and after the fix (Shared Chat message 82). Our focused verification did not repeat those full-suite/full-replay checks. Engine findings below remain open.

> **Recovered report; no new rerun.** This Markdown is a formatting-only recovery of the complete original `REVIEW.txt` from `plato40_engine_review.zip`, with short qualitative rubric annotations. The original review examined `feature/ingestion` at `9c386c58f2fb82904663e01f589fa1b708421afd`; the work-order summary correction was subsequently verified at `db9abf1bbd5d69d7991f9a44e62cd8b0d7bc1c66`. Statements about runs and verification below describe that recovered work, not fresh execution during this conversion. No score, precision, or recall has been measured or inferred.

**Rubric reference for the added annotations:** repository `docs/source/CHALLENGE.md`, §9 “Evaluation — 100 points” and its fairness paragraph, read from the recovered checkout at `db9abf1bbd5d69d7991f9a44e62cd8b0d7bc1c66`. Category associations are qualitative; they are not measured points or a forecast of the jury’s grade.

## Scope and provenance

This is a read-only review of feature/ingestion at commit 9c386c58f2fb82904663e01f589fa1b708421afd in github.com/so-s1m4/hotel. It covers causal work-order context, resource feasibility, repeated proposals, release/approval boundaries, and selected water-detection coverage limits. No engine, ingestion, frozen common contract, or dataset files were changed. No holdout was accessed and no model calls were made by this review.

The coordinator supplied engine-outputs-dev-qs.tgz in Shared Chat message 76: SHA256 e4feb23ea08aad65919b6677db23238a4d2c82c56e74b943a470b528f22675d0. The live Dev export has 2,041 decisions across 1,208 episodes. Its SHA256 is e50e0dbf3503dc396e8ec3084384ee7b78336b955c11a9ea0fa55a50b58df818. Our forced-offline Dev replay produced 1,656 decisions from 65,434 events, with zero safety-gate rejections. Its decisions and executor audit are byte-for-byte identical to the coordinator's offline exports. Offline decision SHA256: a4a8b9c37e98d01401bdea543c63d4f496957a011e4f3b5e4d0d96dae6907af3.

The coordinator subsequently supplied engine-live-audit.tgz (Shared Chat messages 85–86). Its 2,041 unique Dev audit IDs match the 2,041 live decisions exactly, including action and timestamp, with no missing, extra, or duplicate entries. Statuses are 1,024 simulated, 862 simulated_message_proposed, and 155 pending_human_approval. The offline audit was never attached to live outputs.

## F1. Corrected priority 1 — Future work-order outcomes changed historical inputs

ingestion/replay.py:357-373 masks final status and completion time, but reveals preventive/metering summaries at creation only for eventually completed orders. That condition makes the historical payload depend on a future outcome.

Source witness: Dev WO00035, WM_R105. Creation is 2026-07-01T21:00:00Z; scheduled work is July 4 at 21:00Z; completion is 21:15Z. At creation, the eventual-completed case exposes the meter-exchange summary. Changing only final status/completed_utc to a postponed outcome yields summary="". Both public views still say status=open and completed_utc=null. The summary is the only differing creation-event payload field.

engine/planner.py:49-52 caches creation summaries, and engine/detectors/water.py:126-135 uses room/asset matching and maintenance keywords within two hours of a scheduled slot to suppress room-water candidates. A controlled same-observation fixture through the real WaterDetector yields zero inspection candidates for eventual completion and one for eventual postponement, even while both outcomes are in the future. This establishes a possible causal suppression mechanism. It is not a measured missed real Dev incident: the actual R105 source window does not qualify because of its readings and occupancy.

### Suggested fix

Compatible correction: summary="" for every order until an observed completion, independent of final status and order type. Afterwards expose the source summary or "". Retain structured creation-time asset/type/schedule fields. Do not simply show every preventive summary: postponed narratives contain future failure reasons. No source field establishes an immutable creation-time room list.

The coordinator's specific BUILDING flushing concern does not prevent this correction on the supplied runs. Quickstart's one and all 11 Dev BUILDING flushing slots are at 10:00 Vienna. Their 08:00–12:00 suppression windows never overlap the detector's 00:00–06:00 night gate. This observation says nothing about holdout schedules. Observed work_order_completed events can update the engine's context by work_order_id at their availability time; the pinned planner ignores them.

**Evidence:** summary/review.txt, result.json, repro_summary_causality.py, planned_scope_note.txt, planned_scope_findings.json, and fix_verification/.

**Scoring relevance (qualitative):** Future-dependent historical inputs affect evidence-based detection, robustness to look-alikes, and the credibility of the decision trail. The recovered verification documents this correction; it does not measure any point gain.

## F2. Priority 1 — Observed busy cleaners are offered immediate other-room jobs

In live Dev, 64 of 122 autonomous cleaning proposals start immediately while the same cleaner has a strictly earlier observed cleaning_started event in another room and no completion known at decision time. Another 28 have an equal-time source start; those are reported separately because exact stream delivery order cannot be reconstructed from the decision export alone. They are distinct decision IDs, not multiple witnesses counted for one proposal.

Clear live witness: decision:4f2ffb9bd2f3dff0cbe8 at 2026-01-02T10:39:45Z proposes CLEANER_1 in R202 until 11:24:45Z. E0000099 already started CLEANER_1 in R201 at 10:15Z. The corresponding completion appears at 10:45Z, which is future information at the decision time and cannot justify the immediate start.

The offline replay contains four strictly-prior conflicts and three same-time conflicts among nine simulated cleaning reservations. Its executor audit confirms the status "simulated". Non-tie offline witness: decision:70aa79875f5da855cc38 at July 20 09:00Z reserves CLEANER_2 in R310 for 45 minutes, although E0012080 already shows CLEANER_2 cleaning R410 since 08:45Z without a known completion.

### Suggested fix

ResourceCalendar tracks proposed reservations but does not import observed source work. A scheduled shift establishes eligibility, not current freedom. Ingest causal cleaning-start/completion observations into busy-resource state; prevent immediate conflicting reservations; release observed busy state only from a known completion or explicitly modeled human resolution. An assumed planning duration is not evidence that observed cleaning ended. Preserve the separation between observed work and simulated proposals.

The matching live audit now confirms "simulated" status for all 92 conflicts, with unchanged witness IDs, source IDs, and intervals. These are infeasible simulated dispatches, not physical hotel actions. Only already available work is counted; later unannounced historical jobs are not used to accuse an earlier planner of a conflict.

**Evidence:** safety/SAFETY_REVIEW.md, output_review_offline.json, output_review_live_decisions.json, capacity_conflicts_live.csv, review_output.py. The authoritative matching live audit verification and completed checks are in live_audit/verification_note.txt, verified_live_execution_review.json, id_join_validation.json, and capacity_comparison.json. The earlier decisions-only report is retained as a clearly separate stage of the review.

**Scoring relevance (qualitative):** The observed conflicts directly concern prioritisation and constraints, safe and proportionate autonomy, and cross-domain staff coordination. The rubric awards no points for unsafe actions; no numerical deduction is established here.

## F3. Priority 2 — One waiting stay can receive identical simultaneous updates

The live output contains ten groups sharing the same booking, room, timestamp, action, and exact reason across separate episodes. They contain 23 notify_guest proposals, or 13 beyond one per group. These are proposals, not delivery logs.

At 2026-05-12T05:00:00Z, BK001393 / R409 receives the identical cleaning-in-progress proposal through decisions aa8255f72d789cc9a16c, db641d93214a3025120e, and df05ea1f6a894b145c6e (all IDs have the "decision:" prefix). They originate from GR000465, GR000466, and arrival-waiting event E0007587. One later cleaning start, E0007596, triggers the same update in all three source-keyed episodes.

The two original guest requests have different times, channels, and questions, so their initial acknowledgements can be legitimate. The identical later state update is the clearer redundancy. Sixteen stays split across source episodes; that broader count alone is not a count of spam incidents.

### Suggested fix

Correlate a continuing turnaround by booking/stay and room, retain contributing source IDs/channels, and coalesce identical state updates. Preserve meaningful variants, new explicit queries, changed estimates, staff cancellation, room reassignment, and observed recovery. Grouping solely by room or suppressing all repeated actions would hide legitimate changes and independent stays.

**Evidence:** episodes/review.txt, simultaneous_live_duplicate_groups.json, live_dev_episode_metrics.json, offline_dev_episode_metrics.json, analyze_episodes.py. Relevant source: engine/detectors/turnaround.py:25-33, engine/decisions.py:23-26, engine/planner.py:66.

**Scoring relevance (qualitative):** Identical repeated updates concern guest communication and deadlines, and the rubric explicitly counts alert spam negatively while counting repeated alerts for one incident as one detection. No numerical penalty or delivery impact is established here.

## F4. Heating — Recovery margin needs evaluation

All 1,455 heating-comfort decisions match exactly between live and offline: 869 setpoint proposals, 431 guest notices, 155 inspections, across 1,032 comfort episodes. No heating episode repeats the same action. The observed repetition comes from closing and reopening episodes after threshold crossings.

R106 on October 5 illustrates the behavior. A proposal at 17:45Z follows 19.92/19.91/19.99 C readings. A brief recovery to 20.00–20.09 C closes the episode; readings of 19.91/19.96/19.96 C trigger another proposal at 20:15Z. The room setpoint is 20.5 C and window_open=0 throughout. The code uses the same comfort boundary for recovery and retriggering, without a separate margin.

### Suggested evaluation

Consider a documented sensor/recovery margin and sustained recovery criterion, with severity-aware evaluation. Neither a 19.9 C reading nor a summer date by itself proves a false alarm. No incident ground-truth labels are supplied, so this review does not report precision or a false-positive rate. Details and exact decision IDs are in episodes/review.txt.

**Scoring relevance (qualitative):** Recovery behaviour concerns robustness to look-alikes, energy/comfort trade-offs, and the rubric’s treatment of alert spam and false alarms across normal operation. These observations identify an evaluation need; they do not establish a false-alarm count or score change.

## F5. Water — Two verified coverage limits, without a recall estimate

The output has two water-leak inspections, R201 on March 19 at 23:45Z and R401 on August 16 at 22:45Z. They precede later corresponding hose-repair order creation by 20 h 30 min and 20 h respectively (WO00016 and WO00044). Dev's two WATER_NIGHT_FLOW source rows are one WM_MAIN raised/cleared pair (E0004268 and E0004269), not two independently verified incidents. The R201 inspection occurs two hours before the alarm is raised; this does not prove the alarm's cause.

### F5a. Low-rate vacant-room coverage boundary

R208 supplies a clear low-rate coverage boundary. At November 16 23:45Z (November 17 00:45 Vienna), four consecutive observed deltas are exactly 1.1 L per 15 minutes. All five room states are vacant and clean, supported by checkout E0019855; there is no recent cleaning, work-order, or lifecycle suppression. The production detector emits no candidate solely because its minimum is 3 L per interval. All 24 inspected night windows fail that minimum. WO00068's later running-toilet repair narrative selects the retrospective window; it was not used as a fact available to the earlier decision.

### F5b. Main-balance availability limit

The main-balance path has a separate availability limit. On July 18 local night, all 24 main-meter windows are valid, but R105 fails continuity at every tick; R402 also fails at the last two. Thus no window has all 40 room meters valid, and no residual calculation or baseline update occurs. A concrete R105 counter decrease is 1689.8 to 1687.7 L on July 17, 21:15 to 21:30Z. This completeness gate blocks assessment before any threshold or historical-baseline comparison. The later DHW-pipe repair order WO00040 is retrospective context; this review does not assert that an observable complete residual would exceed the threshold.

### Suggested evaluation and safeguards

Record explicit exclusion/availability reasons. Evaluate a longer-duration, lower-rate review path against historical baselines and independent negatives. Keep invalid meters unknown; do not silently interpolate or count missing rooms as zero. An independent main-meter-only anomaly signal would require explicit uncertainty about common-area use. Do not lower global thresholds solely to fit these examples. R403 before WO00031 is also below the rate threshold but occupied, so it lies outside this vacant-room detector's scope in two ways.

Five later water-related corrective work orders were inspected, but they are neither exhaustive incident labels nor independent ground truth. No 2/5 recall, false-positive rate, or causal impact estimate is justified by this comparison. Evidence: water/review.txt, result.json, and bounded_water_review.py.

### Maintenance cross-check

The separate maintenance check found no documented exchange/flushing overlap for either alert. R201's four positive deltas total 102.3 L; R401's total 78.5 L. Both have five consecutive valid readings, observed vacancy/cleanliness, and no cleaning within the preceding two hours. Their nearest applicable known flushes are over ten days earlier. Across eleven flushing and two metering orders, no alert overlaps the inspected maintenance windows. Actual R105 and R405 exchange windows contain large negative counter resets and are rejected by meter_deltas. The daytime flushing slots are also outside the night gate, so zero overlaps cannot be attributed solely to maintenance suppression. These checks rule out the documented confounders for these two alerts; they do not establish true-leak labels or a false-positive rate. Evidence: maintenance/MAINTENANCE_REVIEW.md, maintenance_review.json, and review_maintenance.py.

**Scoring relevance (qualitative):** The verified coverage boundaries concern evidence-based detection, robustness to bad data, and evidence-based feedback to CLARA and PLATO. Their scoring effect depends on independent incident labels and evaluation; no recall, precision, or point change is measured.

## Boundaries that held in the reviewed outputs

Neither output contains release_room. The pinned engine intentionally does not generate that action, so this is absence of unsafe releases in these outputs, not exercised proof that every possible release path is safe.

The checker found no room-precondition, approval/consent-flag, simulation-label, or shift-eligibility violation across 1,035 offline and 1,148 live room checks. The resource conflicts above are a separate identified defect. In both matching audits, all 155 approval-and-consent-required inspections remain pending_human_approval. The audits do not record real actuation or changes to observed history. Zero other checker violations does not erase the demonstrated resource-feasibility defect or prove safety of unexercised paths.

## Reproduction and limits

Each lane supplies a read-only Python script and machine-readable results. Provide the pinned checkout and independent Dev dataset paths when running the scripts. Data and the repository are intentionally not duplicated in this package. Root performed one full forced-offline replay; lane analysis reused its outputs and the coordinator's live export without extra model/engine runs.

The frozen contract, source schema/docs, causal gateway, and exact source-event IDs are the basis of findings. Source narratives and later repair records can guide retrospective investigation but cannot become historical engine inputs. Zero safety-gate rejections do not establish sound planning or alert quality. Recommendations here were not implemented by this read-only review. A later commit or rerun must be evaluated under its own source/output identity.
