# Local guest-parser evidence review

Current feature source is `62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799`, which adds
raw-event publication propagation to the completed `14d1eae` hydration and
correction-identity repair. Parser `1.2.2` and the latest fresh-model response
measurements remain pinned to `efc5b45224020190abb7ecdb6f3c1fac26d75355`.
The completed pre-repair pipeline, its multi-intent failure and avoidable churn
are retained below. The latest publication-repair full suite, separate offline
replays and cached 112-request replay have completed verification; no new model inference is
being claimed. Main, `common/` and preholdout inputs remain unchanged.

## Scope and reproducibility

Work is isolated to `feat/local-parser-structured`; main, the frozen `common/`
contract and preholdout inputs are unchanged. The feature left `engine/` unchanged
through `efc5b45`; `046f250` then modified `engine/planner.py`, and current
`14d1eae` additionally repairs that planner and the optional availability index
in `ingestion/replay.py`. Current `62e51d1` also updates raw publication handling
in `ingestion/loader.py`, `ingestion/events.py` and `ingestion/replay.py`, preserving
the `14d1eae` planner and parser bytes. `NotificationLedger` is unchanged; merged correction
identity is handled at the planner call site. Source checkpoints:

| Checkpoint | Meaning | Guest-parser SHA-256 |
|---|---|---|
| `1647ebbfadeaa85f71acc0c0bf85fe5c90047423` | Contract-profile baseline | `62ffdce378e36d16d5739e14e620aafcca32854e07394f9d9f768c8a12f55f98` |
| `97ca2f09302ec7bd9ff30c6a1f6d476dac36e524` | Extraction profile | `1b45e03580fbe9230ea970624e5670d7f950317301c28f59319c81bb375b7b7c` |
| `3c3ef902a00d2a205ba2afb6d90f0647f5f01d55` | Separate acceptance cohort and 8B runs; same parser bytes as `97ca2f0` | `1b45e03580fbe9230ea970624e5670d7f950317301c28f59319c81bb375b7b7c` |
| `efc5b45224020190abb7ecdb6f3c1fac26d75355` | Final parser `1.2.2`, scoped explicit-refusal guard | `46fb1bc09514ab4b790e5e1a7a3ca92314c1e2153961b1ce537b77f189cb4ff9` |
| `046f2506621ce21771c1dc08bca52ce5535d5fa8` | Repaired activity hydration in planner; same parser bytes as `efc5b45` | `46fb1bc09514ab4b790e5e1a7a3ca92314c1e2153961b1ce537b77f189cb4ff9` |
| `14d1eaeebf3b1555d556e386f51c6dca245585e5` | Late-known activity index/fallback, completed-history prepass and correction identities; parser unchanged | `46fb1bc09514ab4b790e5e1a7a3ca92314c1e2153961b1ce537b77f189cb4ff9` |
| `62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799` | Raw publication time propagated through normalizer and causal views; parser/planner unchanged | `46fb1bc09514ab4b790e5e1a7a3ca92314c1e2153961b1ce537b77f189cb4ff9` |

The 15-case synthetic development cohort is unchanged since benchmark checkpoint
`2961124`; file SHA-256:
`02625cecfcd3dd90b7151b407d43d55c80c2520dcb24fb3670d7b6c044786cde`.
The independently authored 24-case directed acceptance cohort has SHA-256:
`a6187e07386f38a93ecc838a809326882f4889ca5961acf4ba0fd939e6249078`.
It is a separate check, not representative guest traffic or a statistical
held-out accuracy estimate. The three-case connection pilots are a subset of
15 development cases; never add them to the denominator as independent cases.

Each report retains its input/selected-cohort, parser, prompt, generation schema,
response-format and declared-runtime hashes, plus explicit profile, output cap,
timeout and worker count. Both response-format arms within a profile share a
prompt; changing profile changes the task and prompt. The fresh-model experiment
tables used fresh per-run application caches and have zero cache hits and no
source-label-only results. The separate final cached replay deliberately has
112 cache hits and zero transport attempts; it is not fresh inference. Transport counts are client `_call_llm` invocations,
not independently measured server receipts.

The pre-planner-repair `efc5b45` deterministic gate is **1396 passed in 59.70 s**, recorded in
`pytest-efc5b45.log`. The parser hash above was calculated from the committed Git
blob. Independent external review gates reported by the coordinator are green:
Astra4 checked six-language positive/negative controls and eight service-scope
cases; Astra8 checked both original witnesses in both profiles, 20 safety cases
and eight fallback cases. These are bounded regressions, not model-quality
measurements or complete semantic proof.

The historical `pytest-final.log` result, **1259 passed in 59.88 s**, belongs to
`1647ebb`; the earlier `pytest-full.log` records 1194 tests. Neither old log is
the current parser's test gate.

Final actual 3B Quickstart ingestion plus the 15- and 24-case model runs are
complete in local `final-efc5b45-run2/`; final figures are separated below. An
earlier `final-efc5b45/` attempt was interrupted by an instrumentation stdout-
collector conflict. Its `pipeline-summary.json` records that driver issue, not an
application regression. It supplies no completed cohort counts and is excluded
from comparisons; the corrected run used a fresh cache. This interruption is
separate from the historical interrupted 8B strict arm.

Authenticated HTTP is routed through the private, non-redirecting urllib opener;
provider aliases may send keys only to the same scheme/hostname/effective-port
origin. Userinfo URLs, malformed endpoints and query/fragment endpoint forms
(including bare `?` or `#`) are rejected by parser configuration. Local Ollama
uses explicit loopback HTTP; HTTPS backends retain default certificate
verification. No insecure TLS bypass or global opener is installed. The TLS
portability change is test-only: it accepts urllib's lazily initialized default
HTTPS context or an eager secure context (`CERT_REQUIRED`, hostname checking).
It does not change runtime TLS code, the CA store or certificate verification.

## Recorded runtime

Operator-declared settings were Ollama **0.34.1**, Q4_K_M weights, Apple M4
(CPU10/GPU8), 16 GiB RAM, context 4096, one parallel server slot and at most one
loaded model. The reports mark these declarations `operator_supplied_not_measured`.
Retained `/api/ps` and tag snapshots independently record served digest/context
at the sampled times; a CLI revision argument alone is not server attestation.

| Installed model tag | Reported parameter size | Full served SHA-256 digest |
|---|---|---|
| `ministral-3:3b` | 3.8B | `f04aa1c738f64e13c625b82ae92504fc0260fa6723b509ed1ece0fa188179b1d` |
| `ministral-3:8b` | 8.9B | `1922accd5827ebe6829e536369195db25eaf664528dc66206d646ea3bb386b71` |

Runs used one client worker, a 15-second timeout and 512 output tokens. The
initial cold schema canary is separate: 60-second diagnostic timeout, 64-token
cap, 4.253 s elapsed. Its successful constrained reply demonstrates that canary
schema support, not correct understanding of hotel requests. An explicit native
empty-prompt keepalive warmup (0.016 s) preceded the baseline/profile pairs.
Acceptance warmup records show 0.001 s for already loaded 3B and 4.884 s for
loading 8B after 3B; these times are retained separately, not hidden in a claimed
cold-run comparison. The measured arms below are server-warm/application-cache-
fresh runs, not cold model-loading measurements.

One-second `/api/ps` sampling records server-reported model allocations. It does
**not** measure process RSS, total machine peak RAM, or isolated inference memory.
Client elapsed time includes parsing, transport and server queue/generation;
it is not isolated inference latency. The 8B interrupted arm has unknown total
cost/counts even if memory sampling observed activity.

## Verified final AFTER gate (`62e51d1`)

Current source: `62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799`. The actual-pipeline
regression exposed a separate gap: an event occurring at 08:30 but explicitly
published at 09:00 could reach normalized ingestion and causal views at 08:30,
even though the optional availability query already withheld it correctly.

The loader now retains canonical trusted availability separately from occurrence.
The normalizer uses that availability in `NormalizedEvent.available_at_utc` while
preserving the original precise occurrence in the event/source timestamps.
`ReplayData` applies the same publication boundary to both event-query APIs,
room occupancy and booking lifecycle views, keeping occurrence indexes/order.
Absent publication retains conservative occurrence-grid behavior; malformed or
unknown explicit publication fails closed with a diagnostic. Private metadata is
not exposed as source fields. Precise fractional timestamps remain occurrence
facts rather than being silently replaced by publication time.

Sixteen raw-availability regressions plus one actual tiny CSV→ingestion→engine
integration regression were added; the focused gate reported **62 passed**.
The distributed Quickstart/Dev source audit reported no explicit raw publication
fields, so this concrete API/pipeline defect is not being presented as an observed
failure on those supplied datasets. Parser `1.2.2` and the `14d1eae` planner are
byte-unchanged; no new model inference is claimed.

Committed source SHA-256:

| Component | SHA-256 |
|---|---|
| `ingestion/loader.py` | `fbe860ff1f27e8d63f2b277972e8960bd9f72ca92a578a3d09727d4e8157b6be` |
| `ingestion/events.py` | `c08a3b332b800e6179cdcc338d71725df0647347b0f0e4b6f5fb711afb394c81` |
| `ingestion/replay.py` | `60d58dc00d9084821c5d06e2d9c869acb29474408695f67a1e5f2a1874a388d7` |

Final verification on this source completed **1449 tests in 60.61 s**. The
verification report records unchanged hashes for 134 source files and 17 required
input files per dataset, with no runtime worktree differences. Offline outputs
are byte-identical to the `14d1eae` intermediate outputs; the final source has its
own reports and is not inferred to pass from earlier artifacts.

| Final-source replay | Normalized events | Decisions | Runtime safety rejects | File-validator errors / room overlaps / resource overlaps / exact duplicate notices |
|---|---:|---:|---:|---|
| Offline Quickstart | 3939 | 342 | 0 | 0 / 0 / 0 / 0 |
| Offline Dev | 65434 | 7173 | 0 | 0 / 0 / 0 / 0 |
| Quickstart, validated cached LLM extractions | 3939 | 352 | 0 | 0 / 0 / 0 / 0 |

The final cached replay reports **112 cache hits, zero transport attempts**, no
same-time cleaning proposal/cancellation pairs and 4.620 s client replay time.
Its file validator is valid. This repairs the before/after scheduling churn
without making a new inference-quality or isolated latency claim. The full-suite
and offline timing evidence was collected while cached verification ran
concurrently, so those wall times are not an isolated performance comparison.
Full Dev with live LLM extraction was not run.

The final report `revalidation-payloads.json` retains all 112 post-revalidation
intent lists and extraction metadata, alongside their stored validated-cache
counterparts. All 112 intent lists and all 112 metadata objects are exactly equal
to those stored entries: a **current validated-cache fixed point**. This is not
proof that current outputs equal the historical pre-repair raw model responses.
The 112 Quickstart raw response strings were never retained, and earlier cache
entries are host-normalized artifacts, not raw-model evidence. Keep that historical
BEFORE-cache scope limitation when interpreting the pipeline comparison.

One optional traceability limitation remains: three causal identifiers appear in
reason text rather than dedicated structured provenance references. They remain
human-readable, but structured-only provenance consumers cannot rely on those
references without parsing prose. This is a bounded audit-format limitation, not
an execution defect or evidence of new work; no extra code change is claimed.

Final local artifacts are under
`planner-fix/62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799/` (suite/offline gates) and
`planner-fix/62e51d1-cached-llm/` (cached replay). Verification SHA-256:
`1d47ce845fa36392e18c1c4c025814ef43a9221512b8c39038536ee28fb5f304`.
These are reproducibility/consistency checks, not signed execution attestation,
semantic precision/recall or real-world safety certification.

## Completed intermediate AFTER gate (`14d1eae`)

Intermediate source: `14d1eaeebf3b1555d556e386f51c6dca245585e5` (parent `046f250`).
Committed planner SHA-256:
`5d70b94da3e7c563396a536040e8131745dc2dd54f8522b625e2c67241644e5b`;
committed replay SHA-256:
`ac54327f43fdf930da6093c9adba48ace3962096bf5c998ebe84a402ddbdc245`.
The parser entry point remains byte-identical to `efc5b45`, version `1.2.2`.

The planner hydrates queryable operational facts available at T before planning
at T. `ReplayData` now offers an optional availability-indexed event query for
incremental cursor windows. Generic gateways that expose only occurrence-window
`get_events` use a full as-of history fallback: an occurrence before the cursor
may first become known now. No new method is required by the frozen gateway
contract. The generic fallback trades scan cost for correctness; indexed replay
queries avoid that scan. Future occurrence/availability is still excluded.

A filtered completion/checkout prepass prevents newly disclosed, already-finished
historical work or stays from transiently cancelling a valid future job before
the matching end fact is processed. Equal-time occupancy facts remain
conservative; unknown future ends cannot suppress a known start. Normal fact
processing still applies observed end effects once. Transactions restore query
cursor, fact IDs, completion watermarks, cancellations and capacity on failure.

Merged cancellation corrections now retain causal withdrawal IDs in their reason
and dependencies, and incorporate them into decision identity. Distinct withdrawn
plans remain distinguishable even with the same replacement estimate; identical
retries remain deduplicated. This is a planner fix, not a change to
`NotificationLedger` or a relaxation of its rate limits.

This intermediate source completed **1432 tests in 62.55 s**. Offline Quickstart
emitted **342** decisions, Dev **7173**, and the cached 112-request Quickstart
**352**; all had zero runtime safety rejections and valid file-level reports with
zero errors, room/resource overlaps or exact duplicate notices. The cached run
had 112 cache hits, zero transport attempts, no same-time cleaning pairs and
5.315 s total client replay time. Its 87 live/two pending intervals are capacity
checks, not a quality score. Original reports remain under
`planner-fix/14d1eaeebf3b1555d556e386f51c6dca245585e5/` and
`planner-fix/14d1eae-cached-llm/`; they are intermediate, not the latest `62e51d1`
gate. No new inference or model-quality measurement follows from cached extracts.

### Intermediate source (`046f250`), retained separately

The earlier repair used an inclusive cursor and lifecycle deduplication but
needed the further late-known/completed-history fixes above. Its planner SHA-256
was `9815febf62ecf65230f7906982a3df4ef028a8c1c4cc5de8f551c6f0cf9d0f19`.
Ten new regressions and 679 focused engine tests preceded its full gate:
**1406 passed in 61.69 s**. Offline Quickstart emitted **342** decisions and
Dev **7173**, both with zero runtime safety rejections. Its cached 112-request
Quickstart emitted **352**, with 112 cache hits, zero transport attempts and no
same-time cleaning proposal/cancellation pairs. Those artifacts remain under
`planner-fix/046f2506621ce21771c1dc08bca52ce5535d5fa8/` and
`planner-fix/046f250-cached-llm/` and are not rewritten or relabelled as `14d1eae`.
They are intermediate evidence, not the current AFTER gate.

## Completed pre-planner-repair pipeline (`efc5b45`)

Final measured settings were 3B, `extraction`, `json_schema`, one worker,
15-second timeout and 512 output tokens, with the served digest/runtime above.
All model calls used fresh application caches. No new 8B or full-Dev LLM run is
claimed for this final source.

| Workload | Source cases/events | Live model / calls | Fallback/errors | Host acceptance | Client total, s |
|---|---:|---:|---:|---:|---:|
| 15 development cases | 15 | 15 / 15 | 0 / 0 | 12/15 | 60.742 |
| 24 acceptance cases | 24 | 24 / 24 | 0 / 0 | 20/24 | 90.632 |
| Quickstart full ingestion/replay | 112 requests; 3939 normalized events | 112 / 112 | 0 / 0 | Not independently adjudicated | 345.5175 |

The two directed cohorts total 39 live predictions with zero cache hits or
fallbacks; they are not one representative accuracy sample. Quickstart extraction
marked 19 requests for review. Its model output included the source label on
110/112 requests: this is label agreement, **not accuracy**, because source labels
are not independently adjudicated truth.

The Quickstart engine emitted 390 decisions with zero runtime safety rejections.
Its independent file validator is valid with zero errors, resource/room overlaps
and exact duplicate notifications. It joins 390 decisions to 390 audit records,
retaining 87 live intervals and two pending intervals. All eight input hashes in
that validator report were independently re-read and matched. Decision SHA-256:
`4b3be348d7530abfab298613379524f6d5f77bf69f01c2013cf3ab5509c54524`;
audit SHA-256:
`8a0789fb0e3503392a729a34cfbb256456a6b5e75f7076a230276eb4e4204f0e`.

**Operational limitation:** the earlier offline Quickstart baseline had 342
decisions. The 390-decision LLM result includes eleven additional same-timestamp
proposal/cancellation pairs and 27 additional turnaround notices; final live
intervals (87) and pending intervals (2) are unchanged. Extra logged decisions
are not evidence of better quality. The same-time hydration defect was identified
in `046f250` and extended in `14d1eae`; the completed final AFTER verification is
reported above. Zero file-level overlaps or exact duplicates does not certify useful
notice volume, minimal replanning or general policy correctness. Earlier offline
Dev/main results remain unchanged; full Dev with this LLM profile was not run.

A separate two-case multi-intent smoke check used two model calls. French arrival
plus luggage storage passed all intent, time, source-quotation and review checks.
Dutch check-in plus checkout produced the right intent set, but the checkout
quotation was not an exact source substring; its time/evidence were cleared and
review required. Strict semantic acceptance is therefore **1/2**, not 2/2. This
failure is retained in `multi-smoke.json` and is not hidden by the successful
intent-set extraction or by the host's safe abstention.

## Historical measurements and interrupted arm

Host pass requires all existing normalized cohort checks, including intended
label/time, quotation grounding, review policy and required uncertainty. It is
a directed-case acceptance count, not universal accuracy. Live/fallback counts
are case paths; a fallback may still follow a transport attempt or raw reply.
All complete rows have one attempted transport call per selected case.

| Cohort | Model | Profile | Format | Live | Fallback | Host pass / selected | Client total, s | Status |
|---|---|---|---|---:|---:|---:|---:|---|
| 15 development | 3B | contract | `json_object` | 14 | 1 | 8/15 | 42.458 | complete |
| 15 development | 3B | contract | `json_schema` | 15 | 0 | 5/15 | 53.065 | complete |
| 15 development | 3B | extraction | `json_object` | 15 | 0 | 13/15 | 34.608 | complete |
| 15 development | 3B | extraction | `json_schema` | 15 | 0 | 12/15 | 41.099 | complete |
| 15 development | 8B | extraction | `json_object` | 14 | 1 | 6/15 | 113.812 | complete |
| 15 development | 8B | extraction | `json_schema` | 15 | 0 | 5/15 | 109.611 | complete |
| 24 acceptance | 3B | extraction | `json_object` | 24 | 0 | 20/24 | 56.462 | complete |
| 24 acceptance | 3B | extraction | `json_schema` | 24 | 0 | 20/24 | 68.507 | complete |
| 24 acceptance | 8B | extraction | `json_object` | 24 | 0 | 12/24 | 191.824 | complete |
| 24 acceptance | 8B | extraction | `json_schema` | unknown | unknown | unknown | unknown | interrupted |

For the interrupted 8B strict 24-case arm, the retained report explicitly sets
completed/live/call/cache/fallback/pass/time measurements to null. Its empty
results do not mean zero work, and no partial pass rate is reported.

Overlapping three-case pilot totals are recorded separately:

| Model / checkpoint | Profile | Object host pass; seconds | Schema host pass; seconds |
|---|---|---|---|
| 3B / `1647ebb` | contract | 3/3; 11.618 | 1/3; 10.312 |
| 3B / `97ca2f0` | extraction | 3/3; 7.814 | 3/3; 7.203 |
| 8B / `3c3ef90` | extraction | 2/3; 26.076 | 2/3; 20.484 |

The smaller model's extraction profile performed better on these retained
cohorts than the measured larger model; strict formatting did not establish
better semantic results. These were bounded single runs with differing prompts
between profiles, no repeated-trial uncertainty estimate and no production
traffic evaluation. They support further testing, not an automatic model choice
or a guarantee of safe autonomous decisions.

## Interpretation and remaining limits

The extraction profile requires generated UTC to be null and `details` to be
`{}`. It keeps intent plus verbatim evidence/time text; the unchanged host derives
justified UTC and applies source-date, alternative-clock and DST checks. Original
source text and evidence quotations remain available. Structured assistance details
are deliberately unavailable in this profile; callers must not invent those
fields or treat empty details as proof that no assistance was requested.

Historical raw scoring is unchanged. On 15 cases, raw aggregate passes were
5/15 and 1/15 for 3B contract object/schema; 7/15 and 7/15 for 3B extraction;
6/14 raw predictions and 5/15 for 8B extraction. The object 8B fallback yielded
no retained raw prediction, whereas the contract 3B object fallback did retain
one. A required-null model UTC intentionally fails raw UTC-vs-gold matching on
exact-date extraction cases even when host derivation passes. Compare raw intent
and quotation checks separately; use normalized host checks for end-to-end
acceptance. `raw.schema_valid` validates the frozen contract with defaults, not
all required generation fields or semantic correctness.

The pre-guard experiments exposed explicit refusal being interpreted as a
positive service request. Final parser `1.2.2` adds a conservative six-language
scoped declined-service guard. Its verified directed cases preserve uncertainty
and block autonomous scheduling for a refused service while affirmative requests,
other independent service scopes and problem descriptions such as “Wi-Fi does
not work” remain usable. The bounded pre-planner-repair pipeline completed with
the failures and churn above; final repaired-engine verification is recorded
separately. Those regressions do not prove complete negation, sarcasm,
cancellation or arbitrary multilingual intent understanding.
Source-label fallback, approvals, consent and observed hotel state remain separate
boundaries; a parser output is not authorization or a real action.

## Evidence inventory

The JSON reports, runtime samples, caches and model weights are local ignored
artifacts; this Git report does **not** publish them. Share the evidence bundles
separately and verify their SHA-256. Paths below are filenames relative to local
`outputs/local-parser/`, not public download links.

| Available local archive | SHA-256 |
|---|---|
| `hotel_local_parser_baseline_1647ebb.zip` | `99f39c586e8c13a289d2045182932c44455964bd36d0902973e61b312806db49` |
| `hotel_parser_extraction_3b_97ca.zip` | `8afacff386bb674322cb937102b24b625e4082db8c8649e843414b5b227d9f1a` |
| `hotel_local_parser_efc5b45_evidence.zip` — BEFORE planner repair; uploaded separately | `028595f48149fd204fec33a4a85b46502690da8a93cd95204955bba4059a7998` |
| `hotel_local_parser_after_14d1eae.zip` — INTERMEDIATE AFTER; uploaded separately | `d36e7363d326ced0b526ef14898799b0cf8978f4b0d4b643f6609b253b3132b0` |
| `hotel_local_parser_final_62e51d1.zip` — FINAL AFTER evidence archive | `b6e9e077f93417c0c0659847fee6b1814db2f1db881722da4ece8ca4aa097acc` |

The uploaded `efc5b45` BEFORE archive contains 34 evidence files and 113 source
files. It preserves the 390-decision result and does not contain the later
`046f250`/`14d1eae` planner repairs or their AFTER results. The uploaded
`14d1eae` intermediate AFTER archive contains 33 evidence files, 112 validated
cache entries and 121 source files, for evaluation only. It does not contain the
later `62e51d1` publication repair. The final `62e51d1` AFTER archive contains
34 evidence files, 112 validated-cache entries and 123 source files, for evaluation
only. Its SHA-256 is recorded above; neither an intermediate nor a BEFORE bundle
substitutes for this final evidence. The archive is shared separately from Git;
recording its filename/hash here does not make an ignored local path a public URL.
The 24-case cohort lives in `tests/fixtures/parser_local_acceptance.jsonl`;
the 15-case cohort in `tests/fixtures/parser_benchmark_cases.jsonl`. Harness and
configuration instructions are in [PARSER_BENCHMARK.md](../PARSER_BENCHMARK.md)
and [LOCAL_LLM.md](../LOCAL_LLM.md).

Exact report-byte hashes (including the interrupted artifact):

| Local report filename | SHA-256 |
|---|---|
| `fifteen-json_object-1647ebb.json` | `f9e4035ea83bb5c78539cce5b913a41969573f934abd014836e94efa6dc41255` |
| `fifteen-json_schema-1647ebb.json` | `6a04c6887c551215c7fa2133f8d3b02a6b0b02aa35a102e695864b1ab1e82e9c` |
| `extraction/fifteen-json_object-extract-97ca2f0.json` | `11ce4fa7ab8e7573d66728e7c709022d75ccfa986e24f2150dc039101197bff8` |
| `extraction/fifteen-json_schema-extract-97ca2f0.json` | `853fe5570d0ea2c25cc74ef1e26d0ed41612aa2825aba513b33472fb12dd83ab` |
| `acceptance/8b-fifteen-json_object-extract-3c3ef90.json` | `ddd5a65e37e67a9de6ea6dd7dcc2c1a3c73fd65cda4b7c6c8b6364be84b7f307` |
| `acceptance/8b-fifteen-json_schema-extract-3c3ef90.json` | `c548a8fd79c896df72dbd241dab79f755f209c8dbf7e984d91b184bde5c9bcf7` |
| `acceptance/3b-acceptance-json_object-extract-3c3ef90.json` | `85ac3bd906c01ba7d00a01da123565e797c9b492fab85fe4fda155c4f02ba4ed` |
| `acceptance/3b-acceptance-json_schema-extract-3c3ef90.json` | `e11e0f99f9e3f253e69982c6e0259c8e58c5b4d56885031727de373d59a86005` |
| `acceptance/8b-acceptance-json_object-extract-3c3ef90.json` | `1e1eb81e63cb683b876a63675e1357e511db5554c9c97732521a3365de3a2813` |
| `acceptance/8b-acceptance-json_schema-extract-3c3ef90.json` | `17d5652efb8c6c9f61d532d0a17f3cef67ffa5c35d2030713ec0c6f3d32407c6` |

Pre-planner-repair evidence, relative to `outputs/local-parser/final-efc5b45-run2/`:

| Local report filename | SHA-256 |
|---|---|
| `3b-fifteen-schema-extract-efc5b45.json` | `4119436074701dc0e7c4b5f9ef861d54c53418674a54f5614fc73688d44be13c` |
| `3b-acceptance-schema-extract-efc5b45.json` | `852986791176e1d82632836d1ae17f2c4f5f609ac46da2866a7c462ce0909bc3` |
| `quickstart-parser-counts.json` | `efda90a118638f1c16dd6bf3f21268f33707ed30469f04990f4e84796c07f685` |
| `quickstart-validation.json` | `314fab512684b76e133c524ac3b1b6b7121ed85cb6e80f925706f9793ae01a32` |
| `multi-smoke.json` | `7dacaa6b62bfad1559219468ddba234ee46bfed1876397b39fd0cf332ba0de01` |

Final AFTER gate artifacts, relative to `outputs/local-parser/planner-fix/`:

| Local report filename | SHA-256 |
|---|---|
| `62e51d1d62c17bd7d9243a5d34bd4c5a97f6e799/verification.json` | `1d47ce845fa36392e18c1c4c025814ef43a9221512b8c39038536ee28fb5f304` |
| `62e51d1-cached-llm/verification.json` | `7661b46eb69aadb39ecc8da834b23835f0172817819881116072d0274ac62712` |
| `62e51d1-cached-llm/validation.json` | `829b2690312a968568fc2ff5d9db1ca89345f61040b46620cfa3c5cbe3e4c7b2` |
| `62e51d1-cached-llm/revalidation-payloads.json` | `372cc012e482f51dec2e252864ba08f452ca9f61b982b6bc41e5f87483a05bbe` |
