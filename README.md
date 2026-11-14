# Hotel Operations Prototype

## Cerebras demo site

An interactive 3D hotel: all 40 rooms on one overview level, a reception desk,
housekeeping room, guest messages, and a manager's phone screen. The layout is
illustrative; rooms and their states remain linked to the source dataset.

```bash
npm --prefix hotel_web ci
npm --prefix hotel_web run build
.venv/bin/python -m hotel_bot
# http://127.0.0.1:8765
```

On Mac, run `Start Hotel.command`. The Cerebras key is stored outside the project
at `~/.config/hotel/cerebras.key` or supplied through `CEREBRAS_API_KEY`.
The site passes extracted GPT-OSS-120B intents to the real `DecisionEngine`.
The engine queues routine tasks autonomously; the manager's phone shows only
decisions that need approval. Entry to an occupied room requires separate guest
consent. Early check-in follows the existing housekeeping planning with resource
checks. All actions run through `MockExecutor`, with no physical effects and no
fabricated message delivery.

The current 3D layout was restored from the reviewed
`hotel-current-3d-review.zip` snapshot (SHA-256
`43b737cf1937f652835862e09b044d4317296d3c6a09e7c6527eda22c54f1965`), using
source snapshot `f88f3fb` plus local fixes. The browser preview at
`http://127.0.0.1:8765` was visually checked with its hotel view, sidebar,
manager phone, and theme. The isolated frontend passed all 68 JavaScript tests,
and the frontend build succeeded. These UI and English-string updates were made
after the frozen `bff983d` Holdout runtime and are not part of that run.

In interactive mode, rooms use demonstration data. For the original Quickstart
and Dev datasets, the site opens saved full runs with **Play / Pause**, a moving
clock, speed control, seeking, and Restart. Events play from the first to the last
record in the normalized stream; counters and the journal are limited to the
selected point in time. **3-minute tour** shows the whole period in three minutes
without calling the model again. Preparing a new run is a separate **Prepare
dataset** button. People in the 3D view show confirmed occupancy; their activities
and positions are illustrative. **Upload ZIP** imports a new PLATO40 dataset from
nested folders. After one **Prepare dataset** operation, the saved history can be
replayed without the model.

The full journal includes action reasons, source evidence, and simulation results.
**Action log → Download log** exports records up to the selected point; at the end,
it exports the full journal. Results are stored in `outputs/bot-session/` and
`outputs/bot-replays/`.
[Demo setup and walkthrough](docs/DEMO_SITE.md).

The action vocabulary extension for `enable_guest_services=True` adds
`create_service_task`, meaning **queue an administrative task or item handoff**,
such as Wi-Fi, towels, or an access check. Its audit phase is `task_queued`; this
does not authorize entry, unlocking a door, billing, dispatching a staff member,
or changing equipment. `engine.safety` checks allowed tasks and services; the
action does not reserve a staff member. The flag is enabled in the site and its
dataset replay, but disabled by default in `DecisionEngine`, so the original CLI
below keeps its previous configuration.

An offline operations prototype for the fictional PLATO-40: causal replay → risk
→ planning → safety/approval → mock audit → observed verification → annual findings.

`ingestion/` and `engine/` are integrated: S01–S05, battery/PV, preheating, robots,
waste, and vending. Real Quickstart and Dev datasets run without an API key.
Historical observations are not changed, messages are not treated as delivered,
and actions do not control a real building. **Historical status:** at checkpoint
`bff983d`, the organizer Holdout had not been opened. The authorized primary
Cerebras Holdout run has now completed on that unchanged snapshot: **65,691
events, 1,534 guest messages, and 8,986 decisions**. Both the built-in and
independent frozen validator report zero errors. The earlier offline diagnostic
is preserved separately; it is not the primary API result.
[Run provenance and hashes](docs/reviews/holdout_run.json),
[validation](docs/reviews/holdout_validation.json),
[submission decisions](decisions_holdout.jsonl).

[Independent review and remaining limitations](docs/reviews/RESOLUTION.md).
[Algorithms and assumptions](docs/ENGINE.md). [Measured annual analysis](insights.md).

## Installation and data

Python 3.11+. Run these commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
# Put the organizer ZIP in the repository root or use --archive-dir /path/to/downloads
python scripts/prepare_data.py
python run.py --data-dir data/PLATO40_v2_quickstart --check
python -m pytest -q
```

Reproducible dependencies with uv: `uv sync --frozen --extra dev`.
Archives and extracted datasets are not stored in Git; [how to connect them](data/README.md).
Quickstart (21 days) and Dev (365 days) are independent synthetic simulations.

## Running and independent validation

```bash
python run.py --data-dir data/PLATO40_v2_dev \
  --out decisions.jsonl --audit decisions_audit.jsonl

python -m engine.validation --data-dir data/PLATO40_v2_dev \
  --decisions decisions.jsonl --audit decisions_audit.jsonl \
  --out outputs/dev_validation.json
```

The repository root contains historical results from an actual Dev replay; the validation
JSON contains hashes for the exact decisions, audit, and inputs that were
checked. Validation checks ID/action/time joins, approvals, structured
cancellations, resource/room overlaps, already-known occupancy, and exact
duplicate notifications. It is not a certificate that all errors are absent.

[Historical Dev validation for runtime `f8370bb`](docs/reviews/reliability_dev_validation.json),
[historical Quickstart validation](docs/reviews/reliability_quickstart_validation.json),
[historical summary of 25 checks and hashes](docs/reviews/reliability_verification.json).

Runtime `f8370bb` produced 342 Quickstart decisions and 7,201 Dev decisions with
matching audit rows, no validator errors, and no safety rejections. The verified
Dev generation is the one published at the repository root. All 1,026 tests are
recorded in the repository and passed in 75.04 seconds; the runtime code was
`f8370bb`. Two independent Dev processes with `PYTHONHASHSEED=71` and `11`
produced byte-identical decisions and audit:
[reproducibility](docs/reviews/reliability_reproducibility.json).
[Fixes for 17 defect groups, A–E, CLI, and validation boundaries](docs/reviews/HARDENING.md).

The historical F5 snapshot is saved separately: runtime `6f8cbc0`, 699 tests,
335 Quickstart decisions, and 6,343 Dev decisions. Its JSON reports and paired
experiment have not been replaced with newer results: [F5 report](docs/reviews/F5.md).

The CLI disables the model by default. `--llm` explicitly enables the configured
parser; if the model is absent or fails, the source label is retained as an
unverified fallback. That fallback does not receive priority for immediate
cleaning; this differs from an explicitly selected offline mode. Settings are in
`.env.example`; do not commit secrets. `--config PATH.json` overrides validated
`EngineConfig` thresholds. `--insights PATH.md` writes a decision summary;
`--annual-insights PATH.md` writes a measured retrospective report after replay,
plus a neighboring JSON file. All outputs must be outside the source data directory.

Local model: [run Ollama and the extraction profile](docs/LOCAL_LLM.md).
Trained candidates: [measured results and limitations](docs/MODEL_CARD.md).
The model extracts intents and verbatim phrases; code validates time and
constraints. [3B/8B comparison, control sets, and validation boundaries](docs/reviews/LOCAL_PARSER.md).

## Coverage

| Area | Implemented |
|---|---|
| S01 turnaround | Known occupancy and readiness, actual shifts, conditional estimates, cancellations/rescheduling, late on-call approval |
| S02 water | Continuous deltas, resets/gaps, vacancy/clean/night, strict main-meter residual, separate cautious low-rate review and exception diagnostics |
| S03 comfort | Sustained cold/overheating, stable recovery with margin, HP1 running-only COP, available forecasts/tariffs |
| S04 maintenance | AHU filter/airflow versus data gaps, known work orders, technician shifts, and external-service approval |
| S05 occupancy | Concurrent known scheduled workload, actual occupancy, staff absence, shared resources, allowed robot floor tasks |
| Energy | Bounded battery charge/discharge using PV/published prices, SOC/efficiency/power, vacant preheat with a known arrival and fresh conditions |
| Logistics | Bin capacity with scheduled transfers, actual robot slots, shortage forecasts, regular visits, and extra-order lead time |
| Learning loop | Dev measurements, CLARA/PLATO recommendations, uncertainty, and target architecture |

Thresholds/durations are documented prototype assumptions; the battery uses a
one-step heuristic, not global optimization. The conservative model blocks
unknown/observed occupancy until confirmed. Missing meter values must not be
interpolated, and a main-meter residual must not be presented as a confirmed
leak. F5 now has two cautious paths: ≥3h with an independent quiet baseline and
≥4h of distributed signed flow without assuming an elevated baseline is normal.
Full paired runs produced two additional uncertain review flags in Dev (R403/R208)
and none in Quickstart; these are not confirmed leaks or measured recall.
[F5 conditions and artifacts](docs/reviews/F5.md).

## Annual analysis, separate from decisions

```bash
python -m engine.annual --data-dir data/PLATO40_v2_dev \
  --out insights.md --json docs/analysis_dev.json
```

[Metrics and SHA-256](docs/analysis_dev.json). Retrospective data are never fed
back into historical decisions. Import cost is observed cost, not savings;
capped full-bin samples are not overflow volume. No precision/recall, measured
improvement, or real-building claims are made.

## Structure and collaboration

```text
common/             frozen schemas and gateway protocol
ingestion/          Lyosha: loading, indexes, causal replay, guest parser
engine/             Maksym: S01–S05, resources, safety, mock executor, validation, annual
tests/              contracts, scenarios, counterfactuals, independent audit regressions
docs/source/         original organizer instructions
docs/reviews/       original independent review, resolution, verified artifacts
docs/tasks/          preserved original assignments A/B
data/               local datasets; excluded from Git
outputs/             intermediate results; excluded from Git
run.py               one-command replay, audit, optional analysis
```

[Contract](common/01_CONTRACT.md), [ingestion](ingestion/README.md),
[integration order](docs/DEVELOPMENT.md). Do not change the shared contract
independently. Concurrent Git operations require separate checkouts/worktrees;
do not switch branches in the shared working directory. The original source
dataset folders are preserved locally.

## Changes since the original `pre-holdout`

### Checkpoints and tag move

The original annotated `pre-holdout` tag was created **November 13, 2026,
22:48:46 CET (Europe/Vienna)**: tag object
`9a2c3288c47e5f5a826f86f59e0c3b6d4131b067`, pointing to commit
`4987c7fb4e05b94e89ef6cfa894e0588b895774d`. Its original object and timestamp
are preserved unchanged at
[`pre-holdout-original-2026-11-13`](https://github.com/so-s1m4/hotel/tree/pre-holdout-original-2026-11-13).

The current `pre-holdout` tag now points to reviewed commit
`bff983d229b77f94b9819ddc8d6c9a2c26b8768e`. Its new annotated tag object is
`58bc29e82fb9b086f3d7e5d36b81ceb2df83ddc1`, created **November 14, 2026,
12:23:31 CET (Europe/Vienna)**. The original annotated tag object and its
original target remain preserved at `pre-holdout-original-2026-11-13` above.
This retarget happened after the Holdout release was announced; it is not the
original pre-release checkpoint and its date is not backdated. It does not prove
that all of its code existed before 11:00. Use the preserved original tag for
before/after comparisons; commit history was not rewritten.

The latest published version of our line before **November 14, 11:00 CET** was
[`866a448`](https://github.com/so-s1m4/hotel/commit/866a448455e5b6dce811302dc2901bb4c48f0136),
at 09:24:44. Changes after 11:00 were not limited to documentation: input
decoders, ingestion behavior, housekeeping matching/availability, and annual
analysis were changed. All of these changes are listed below; we do not claim
that "the core did not change after 11:00."

### Data used for the updates

**Historical team statement at the `bff983d` checkpoint:** the organizer Holdout
had not been decrypted, unpacked, or run. Its examples, labels, and results had
not been used for training, rule/threshold/prompt tuning, or model selection.
`decisions_holdout.jsonl` had not yet been created, and no Holdout result was
claimed at that checkpoint. The first use was an offline diagnostic at
11:32:28 UTC on November 14. The primary Cerebras run then completed from
11:46:27 to 12:04:14 UTC using the same frozen code. Neither run was used
for training, model selection, prompt/threshold tuning, or engine/parser changes.
Their modes and artifacts are recorded separately in
[the run receipt](docs/reviews/holdout_run.json).

The work relied on the participant-accessible, **separate Quickstart and Dev**
datasets, synthetic regression fixtures, and independently repeatable source
review. The specific later fixes addressed the format, causality, and isolation
problems described below, together with synthetic controls—not errors from the
Holdout.

Do not confuse this statement with a claim that "the model was never trained on
Dev": the local Dev100 candidate was trained on a designated Dev TRAIN split;
Synthetic100 was trained on a separate synthetic TRAIN split. Composition
tools preserve TRAIN/VALID/TEST boundaries. Sources, checkpoint hashes, and
limitations are recorded in the [MODEL_CARD](docs/MODEL_CARD.md) and
[training documentation](docs/LOCAL_LLM_TRAINING.md). The names Dev31, parser
acceptance, and diagnostic sets do not refer to the organizer Holdout. Some
diagnostics informed host/parser fixes and are not represented as an untouched
final test. No training or tuning on the organizer Holdout was performed before
the first authorized run began.

### What changed and why

| Area | Reason and scope | Evidence to check |
|---|---|---|
| Parser, time, and refusals | Do not invent dates/UTC times, turn a refusal into a request, or lose an adjacent independent request; send keys only to an allowed origin. | Synthetic time/polarity/origin tests; [parser review](docs/reviews/LOCAL_PARSER.md), [benchmark](docs/PARSER_BENCHMARK.md). |
| Engine causality | Account for already-known events at the same time, late publication, and correction identity; do not read facts that are not yet known. | `test_same_time_activity_hydration`, `test_activity_hydration_adversarial`, `test_raw_publication_*`, `test_available_event_window`. |
| Local training and evaluation | Check masking/length/compatibility, preserve split boundaries, and separate raw model output from host validation. | `tests/training/`, [training documentation](docs/LOCAL_LLM_TRAINING.md), [model card](docs/MODEL_CARD.md). This is not evidence of 100% accuracy. |
| Service, Cerebras, and demo | Pass evidence to the real DecisionEngine; routine queue-only tasks are autonomous, while actual exceptions require approval/consent. Preserve the full journal, clock/playback, and new ZIP import. | `tests/bot/`, `test_guest_services`, JS playback/import tests; [demo scope](docs/DEMO_SITE.md). The site enables `enable_guest_services=True`; the original CLI keeps the default `False`. |
| BOM: validator / CLI / ZIP | Accept an allowed BOM only at the start of a file; preflight and later reading must agree; do not publish a dataset with unusable JSONL. | `test_validation_bom_inputs`, `test_cli_bom_metadata`, `test_dataset_jsonl_bom`; generated output remains strict UTF-8. |
| Snapshots / roster | A returned mutable event must not change the cache or the next request; a scheduled shift without a publication timestamp is not confirmed available capacity. | `test_snapshot_and_roster_bounds`; known cutoffs and the documented absence-overwrite policy are preserved. |
| On-call housekeeping | General human cleaning and the on-call subtype must match correctly; an unknown performer holds the relevant capacity and cancels incompatible future proposals. | `test_oncall_observation_roles`; strict known-ID, room/time, and robot controls remain. Capability classes and approvals are not removed. |
| Annual analysis | Equivalent UTC timestamps must sort and join consistently; BOMs must not change raw hashes; missing coverage must not be reported as a measured zero or proven effect. | `test_annual_timestamp_instants`, `test_annual_bom_inputs`, `test_annual_report_coverage`. Analysis is retrospective and is not fed into historical decisions. |
| Restored 3D interface | Restore the reviewed hotel/sidebar/manager-phone visual direction and English interface strings after the Holdout runtime freeze; presentation changes do not enter the frozen run. | Reviewed archive `hotel-current-3d-review.zip` (SHA-256 `43b737cf1937f652835862e09b044d4317296d3c6a09e7c6527eda22c54f1965`), source snapshot `f88f3fb` plus local fixes; isolated 68-test frontend suite, successful build, and browser preview at `:8765`. |

### Full commit ledger relative to the original snapshot

Times below are **committer times in Europe/Vienna (CET)**, not the tag creation
date. The first three feature commits predate the original tag, but were not in
its code; they entered the combined history in the `af73158` merge at 23:17.
The range therefore lists every commit missing from the original snapshot, not
only commits with a later calendar date. For transferred A5 patches, the table
shows our integration commit IDs; the original author/source histories remain
in Git.

| Commit | CET | Change / rationale |
|---|---|---|
| [`c58dd45`](https://github.com/so-s1m4/hotel/commit/c58dd45e97eb208b9b0039f82444aabf59bc7802) | Nov 13 22:20:19 | Dutch relative dates and retry of unusable parser cache entries. |
| [`82be82e`](https://github.com/so-s1m4/hotel/commit/82be82e92e53288f7d26dc3983ef5ff587ea7919) | Nov 13 22:21:11 | Bounded benchmark with separate raw-response and host-validation checks. |
| [`2961124`](https://github.com/so-s1m4/hotel/commit/2961124f39b307ccf5b119866116b640d008a294) | Nov 13 22:36:19 | Incomplete measurements marked unknown instead of successful zeroes. |
| [`af73158`](https://github.com/so-s1m4/hotel/commit/af731582f607152485c80b6ff8d603e5483cb23d) | Nov 13 23:17:40 | Merged main into the feature branch; preserved both lines of history. |
| [`fb7c818`](https://github.com/so-s1m4/hotel/commit/fb7c818b3866226f24a7d8af63d959e37813d32b) | Nov 13 23:20:46 | Opt-in constrained parsing and reproducible local profiles. |
| [`50f367b`](https://github.com/so-s1m4/hotel/commit/50f367b0cbe2b9f30747de04972c989e389160e2) | Nov 13 23:21:05 | Keep alternative dates ambiguous instead of inventing a choice. |
| [`054245c`](https://github.com/so-s1m4/hotel/commit/054245cabbe0852c5bcb32b3db651f9c86bbf492) | Nov 13 23:25:42 | Preserve clock ambiguity and test local parser boundaries. |
| [`1647ebb`](https://github.com/so-s1m4/hotel/commit/1647ebbfadeaa85f71acc0c0bf85fe5c90047423) | Nov 13 23:29:06 | Reject partial clock tokens; restrict credential forwarding to the permitted origin. |
| [`97ca2f0`](https://github.com/so-s1m4/hotel/commit/97ca2f09302ec7bd9ff30c6a1f6d476dac36e524) | Nov 13 23:35:20 | Extraction-only profile; UTC derivation remains in validated host code. |
| [`3c3ef90`](https://github.com/so-s1m4/hotel/commit/3c3ef902a00d2a205ba2afb6d90f0647f5f01d55) | Nov 13 23:38:08 | Sealed an independent multilingual parser acceptance set. |
| [`8b79013`](https://github.com/so-s1m4/hotel/commit/8b7901314190dd79106e4eff188910b4183cda0f) | Nov 13 23:53:17 | Explicit guest service refusals require review before resource planning. |
| [`efc5b45`](https://github.com/so-s1m4/hotel/commit/efc5b45224020190abb7ecdb6f3c1fac26d75355) | Nov 13 23:56:59 | Check direct and compound refusals with scope-aware negation. |
| [`046f250`](https://github.com/so-s1m4/hotel/commit/046f2506621ce21771c1dc08bca52ce5535d5fa8) | Nov 14 00:22:28 | Hydrate known operational facts before planning at the same timestamp. |
| [`14d1eae`](https://github.com/so-s1m4/hotel/commit/14d1eaeebf3b1555d556e386f51c6dca245585e5) | Nov 14 00:37:48 | Handle late-published events and preserve merged correction identities. |
| [`62e51d1`](https://github.com/so-s1m4/hotel/commit/62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799) | Nov 14 00:57:44 | Preserve source publication time through the normalizer and causal gateway. |
| [`b6977e8`](https://github.com/so-s1m4/hotel/commit/b6977e8b02041d95c1b98fea295320619f5f4f7e) | Nov 14 01:11:59 | Document measured local runs and integration-evidence limits. |
| [`e8ecab1`](https://github.com/so-s1m4/hotel/commit/e8ecab1d5e4789b3f1f3945655bc5a8d6dc30759) | Nov 14 01:54:24 | Prepare/verify local training, handle time, and add release verification. |
| [`83210ef`](https://github.com/so-s1m4/hotel/commit/83210ef92fafcc1c420d4a4fdd343f89f1e3dc41) | Nov 14 02:11:26 | Validate review-flag consistency in multi-intent scoring. |
| [`8fadd82`](https://github.com/so-s1m4/hotel/commit/8fadd82d7f9e1db44e05ef6a296fb73e5c842c0b) | Nov 14 02:29:07 | Ground guest-wait evidence in source text and parse source time safely. |
| [`d658c10`](https://github.com/so-s1m4/hotel/commit/d658c10d3ce5a36e67df1065fa5a818190b94f32) | Nov 14 02:30:39 | Fix refusal polarity; preserve independent requests when another service is refused. |
| [`de7f25f`](https://github.com/so-s1m4/hotel/commit/de7f25f98c870a9f9567d9fff7342d1d18a08c58) | Nov 14 02:34:45 | Compose examples only from reviewed TRAIN; do not move VALID/TEST into training. |
| [`ad2fb5d`](https://github.com/so-s1m4/hotel/commit/ad2fb5d593db39a62eb4c2665a200d1718067071) | Nov 14 02:44:47 | Keep parser failures under review and close source-clock edge cases. |
| [`73cbc7c`](https://github.com/so-s1m4/hotel/commit/73cbc7ca49a9c3acba779198c1dd7a7136fd098d) | Nov 14 02:53:36 | Reject Unicode timezone offsets without losing source qualifiers. |
| [`866a448`](https://github.com/so-s1m4/hotel/commit/866a448455e5b6dce811302dc2901bb4c48f0136) | Nov 14 09:24:44 | Cerebras service using the real DecisionEngine, autonomous queue-only tasks, journal, site, playback, and ZIP import. |
| [`2bea5d7`](https://github.com/so-s1m4/hotel/commit/2bea5d78566fc65dc48c40980a541eb4ad423cb3) | Nov 14 11:23:34 | Validator accepts one file-leading BOM in inputs; generated JSONL remains strict. |
| [`42854a0`](https://github.com/so-s1m4/hotel/commit/42854a0a55df62e3ed3e6847dae117aeab431fad) | Nov 14 11:35:17 | CLI reads BOM-prefixed `building.json` consistently with input preflight. |
| [`bb41edc`](https://github.com/so-s1m4/hotel/commit/bb41edc1859d605a0fac853b3307f6bdfe28176a) | Nov 14 11:38:48 | ZIP admission applies file-level BOM handling and rejects later/repeated BOMs. |
| [`256d5cb`](https://github.com/so-s1m4/hotel/commit/256d5cb6e158d5f1ca7004b06006b5924383ea0a) | Nov 14 11:45:26 | Isolate event snapshots; shifts without recorded publication do not add available capacity. |
| [`9d62448`](https://github.com/so-s1m4/hotel/commit/9d62448c2f51315f44e89604ffd48a97ecf83d6d) | Nov 14 11:49:03 | Match human/on-call observations, retain unknown capacity, and cancel incompatible proposals while preserving ID/room/time constraints. |
| [`7360c7c`](https://github.com/so-s1m4/hotel/commit/7360c7ce7d9b8b8f16a9f4e714ab252824824a67) | Nov 14 12:15:44 | Compare UTC instants in annual analysis instead of timestamp spellings. |
| [`8ebd2aa`](https://github.com/so-s1m4/hotel/commit/8ebd2aa6c09bc327b3554c2ffa3f3211176fe36a) | Nov 14 12:15:44 | Accept file-leading BOMs in annual JSON/JSONL/CSV sources without changing bytes. |
| [`95145e7`](https://github.com/so-s1m4/hotel/commit/95145e7b2cc2fa82fe6691fab22d36157e3d35a2) | Nov 14 12:15:44 | Distinguish missing coverage from measured zero; qualify partial and signed values in the report. |

Inspect the range without accessing the Holdout:

```bash
git log --reverse --format='%H %cI %s' pre-holdout-original-2026-11-13..main
git diff pre-holdout-original-2026-11-13 main -- engine ingestion run.py hotel_bot hotel_web
```

### Results and limits of the evidence

[Public summary of hashes and runs](docs/reviews/post_tag_verification.json)
contains only code/input/output identities, commands, exit codes, and aggregates;
it contains no keys, raw guest records, or model weights.

At exact commit `866a448`: **2,362 Python tests**, **60 JS tests**; at exact
commit `9d62448`: **2,407 Python tests**, **60 JS tests**. The offline CLI (without
`--llm`) processes Quickstart: **3,939 events / 342 decisions**, and Dev:
**65,434 / 7,173**. At both snapshots the validator reports zero errors,
resource/room overlaps, and exact duplicate notifications. Between those two
snapshots, decisions and audit for each dataset are byte-identical; all 17 input
files for each dataset retain their hashes. This does not prove that all bugs are
absent: synthetic edge cases were fixed even when they did not occur in these
particular inputs.

The final runtime snapshot `95145e7b2cc2fa82fe6691fab22d36157e3d35a2` (source
tree `c2c43183b7753e070bbd02c1220d8cfbb7f14e0f`) passed **2,438 Python tests**
and **60 JS tests**. All 178 checked tracked code-file hashes remained
unchanged. The full annual analysis of the original Dev dataset completed
successfully: all 17 input hashes were unchanged, and the JSON was semantically
equal to the previously published `docs/analysis_dev.json`. The new timestamp,
BOM, and coverage fixes are not presented as measured improvements on this
particular Dev dataset.

This README and the metadata-only receipt are added in a separate documentation
commit on top of the tested code; its SHA is visible in Git and is not included
in its own contents. This does not substitute for a repeated Holdout run.

Site/Cerebras archives and local model experiments have separate source/input/
profile identities. Offline CLI results are not presented as fresh Cerebras
calls or results from new adapters. Replaying previously extracted information
or cached data and using Play are not new inference; fresh network call counts
cannot be inferred from the number of processed messages.

Commits, synthetic tests, and input hashes make the stated fixes and results
reproducible. Git and hashes alone are not independent proof that no external
access to data occurred. A validator pass means the checked format, consistency,
and constraints passed; incident precision, recall, confirmed savings, and jury
evaluation do not follow from it. Neither `pre-holdout` nor this section
replaces the actual unchanged Holdout run and its artifacts.

## Submission and licenses

The unchanged primary Cerebras run on the separate Holdout is complete.
`decisions_holdout.jsonl` and `decisions_holdout_audit.jsonl` each contain
8,986 records; the rejection file is empty. All 1,534 guest messages have
`method=llm`, with zero fallback/source-label extractions or parser/source
errors. A separate Python audit hook observed 1,534 official-endpoint HTTP
attempts; built-in LLM counters alone remain cache-inclusive. There are
234 extraction review flags, 8,583 autonomous decisions, and 403 emitted
pending-approval audits. These are processing counts, not accuracy scores
or a final active-pending total.

The independent validator found zero resource/room overlaps and exact duplicate
notifications. All 250 frozen tracked files, 17 original inputs, 17 imported
copies, and both archives retained their recorded hashes. The journal terminal
marker binds the completed summary. [Full provenance](docs/reviews/holdout_run.json)
and [validation](docs/reviews/holdout_validation.json) bind the submitted files.
The earlier offline diagnostic is retained separately. The original pre-release
tag is preserved, and the actual move of the current `pre-holdout` tag and all
differences are disclosed above; no tag was moved after Holdout processing began.

The completed run can be played locally in the restored English UI at
[Open saved Holdout](http://127.0.0.1:8765/?replay=replay-3205dc5524b1474b9c54386d97afa4f5).
Play/Pause were checked with moving time, occupancy and engine decisions.
Playing saved results does not repeat model inference. The frontend restoration
and English-copy commit `c23d3a0` is separate from the frozen runtime.

A real three-minute video and final demonstration are also still required for
the submission; the [recording scenario](docs/DEMO.md) is not a recorded video.

Code is MIT. Data is CC BY 4.0: “PLATO-40 synthetic dataset, Proximata Hackathon
'26 – Rhomberg track”. PLATO/CLARA are Rhomberg product names; PLATO-40 is
fictional.
