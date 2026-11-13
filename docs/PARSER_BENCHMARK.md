# Bounded guest-parser comparison

`scripts/benchmark_guest_parser.py` compares an explicitly selected cohort through
the existing `parse_request_full` interface. It preserves the production parser,
prompt, context reduction and host validation. Planning is the default; only
`--execute` permits configured transport calls.

The included `tests/fixtures/parser_benchmark_cases.jsonl` contains 15 synthetic,
manually adjudicated cases across German, English, Italian, French, Spanish and
Dutch. They are directed acceptance checks, not a representative quality sample.
The negation and instruction-only cases explicitly use a conservative `unknown`
plus review policy because the frozen schema has no no-request label. Dataset
source labels, teacher outputs and model responses are not gold labels.

## Prepare a candidate

Use the project's Python environment. The operator must configure and verify the
model server separately; this script does not install runtimes, download models,
train models or start services. Record the full weights digest or immutable model
commit, runtime/version, quantization, context and output limits, server parallel
slots, maximum loaded models, machine/RAM and the repository commit alongside the
reports. A revision argument is a declared pin, not an independent verification
of the server's loaded weights.

Use one standalone process per run. Instrumentation and environment changes are
scoped and restored, but they are process-global; unrelated parser work must not
share that process. Candidate and run IDs must be safe single path components.
All JSONL lines are validated before any parser call. `--limit` selects the first
N validated cases before execution. Duplicate source records are rejected.

The input shape is one object per line with `case_id`, `record`, `context` and
`gold`. Gold uses one intent label in `intents`, a UTC `requested_time_utc` or null,
and boolean `needs_review`; optional rationale, evidence notes and required
uncertainty fields explain the adjudication. Gold is never sent to the model.
Only historically known booking/context facts belong in a case.

The following Bash example uses a preconfigured local endpoint. Replace the
model and digest placeholders first. For a server without authentication, use an
explicit nonsecret `LLM_API_KEY`; an authenticated endpoint needs its actual key
in the environment. Do not put credentials into the JSONL or command arguments.

```bash
candidate_model='REPLACE_WITH_INSTALLED_MODEL_NAME'
candidate_revision='sha256:REPLACE_WITH_THE_FULL_64_HEX_DIGEST'
export LLM_API_KEY='local-nonsecret'

benchmark_args=(
  --input tests/fixtures/parser_benchmark_cases.jsonl
  --limit 15
  --candidate local-candidate-q4
  --revision "$candidate_revision"
  --cache-root outputs/parser-benchmark/cache
  --base-url http://127.0.0.1:11434/v1
  --model "$candidate_model"
  --timeout-seconds 15
  --response-format-mode json_object
  --max-tokens 384
)

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --workers 1 --run-id workers1-01 \
  --out outputs/parser-benchmark/plan-workers1.json
```

Plan mode validates input and configuration and writes a `status="planned"`
report with zero completed cases and zero calls. Those zeros describe an actual
zero-call plan. It does not create the managed cache. Inspect the
selected case IDs, count, endpoint and pinned hashes before execution. Report
paths must be new; existing files, including symlink/hardlink aliases, are rejected
before calls. Reports cannot be placed inside the managed candidate/run cache.

## Structured generation and declared runtime settings

`--response-format-mode json_object` is the default and keeps the JSON-object
transport contract. `--response-format-mode json_schema` opts into the complete
strict generation schema. The host still validates and grounds every reply;
constrained generation is not evidence that the model understood a request.
A server may reject an unsupported strict schema; inspect transport/fallback
paths instead of counting that as a successful structured prediction.

`--max-tokens 384` requests an explicit output cap (integer 1..16384). Omitting
this option means no output cap is requested by this harness; a server may apply
its own limit. The benchmark explicitly sets `LLM_RESPONSE_FORMAT` and overrides
inherited `LLM_MAX_TOKENS`, restoring the caller's environment afterward. The
report records `max_tokens=null` when no cap was requested. These flags describe
request settings, not measurements of actual output tokens.

The manifest includes the exact `response_format` object plus its canonical
SHA-256, the full generation-schema SHA-256 and the requested token cap. Even
JSON-object mode pins the generation schema for identity. These are separate
from the normalized public `GuestIntentV1` schema hash. Changing either mode,
schema, cap or declared runtime settings invalidates a warm run identity. Use a
new run ID for an intentional comparison; never relabel one mode's cached output
as a result from another mode.

Optionally pass `--runtime-metadata path/to/runtime.json`. This is an input JSON
object; the report retains its validated values under
`manifest.runtime_metadata`, explicitly marked
`provenance="operator_supplied_not_measured"`, and hashes those values into the
run identity. Example:

```json
{
  "served_model_digest": "sha256:REPLACE_WITH_FULL_64_HEX_DIGEST",
  "runtime": "operator-declared runtime name",
  "runtime_version": "operator-declared version",
  "quantization": "operator-declared quantization",
  "context_tokens": 4096,
  "num_parallel": 1,
  "max_loaded_models": 1,
  "gpu_layers": 0,
  "load_state": "operator-declared server warm state",
  "memory_sampling": "not sampled"
}
```

Replace the digest placeholder; the CLI requires the full 64 hex digits, with
optional `sha256:` prefix. Allowed additional fields are `machine` (nonempty
string), `total_ram_gib` (finite positive number), and `server_slots` (positive
integer alias for `num_parallel`; declarations must agree if both are present).
Context, parallel and loaded-model counts must be positive integers; GPU layers
may be zero. No field is inferred or required: absent declarations remain absent.
Unknown fields are rejected to keep credentials and arbitrary configuration out
of reports. Do not put secrets into any allowed string field.

The runner does not query server settings or verify its served digest. A declared
weights digest, slots, context, loaded-model limit, load state or sampling method
must never be presented as measured server attestation. The `revision` argument
and `served_model_digest` are distinct operator declarations; keep their meaning
explicit when a repository revision differs from a weights digest. Actual memory,
load and token measurements need separately retained operator evidence.

## Cold, cache-warm and queued-client runs

Keep the same server configuration throughout a paired cold/cache-warm run.
Start with one client worker. A fresh run requires an absent or empty cache
directory for that candidate/run ID.

`--execute` permits transport but does not clear inherited parser overrides.
For an intended live-model comparison, clear `GUEST_PARSER_OFFLINE` first; a case
with `context.use_llm=false` also remains offline. The included 15-case fixture
does not set that override. After execution, check the report status,
`manifest.transport_kind`, transport/raw-prediction counts and individual paths.
A complete execution containing only `source_label` paths and zero transport
calls is an offline result, not a live-model measurement.

```bash
unset GUEST_PARSER_OFFLINE

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --workers 1 --run-id workers1-01 --execute \
  --out outputs/parser-benchmark/cold-workers1.json

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --workers 1 --run-id workers1-01 --execute --warm \
  --out outputs/parser-benchmark/cache-warm-workers1.json

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --workers 6 --run-id workers6-01 --execute \
  --out outputs/parser-benchmark/cold-workers6.json

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --workers 6 --run-id workers6-01 --execute --warm \
  --out outputs/parser-benchmark/cache-warm-workers6.json
```

Warm mode requires matching candidate/run identity, revision, inputs, worker
count, endpoint/model, parser/prompt/schema hashes, timeout, response-format mode,
full generation-schema and response-format hashes, output token cap and declared
runtime metadata hash. Transport or
validation fallbacks are not cached. Valid normalized extractions are cached even
when a gold check fails, so a warm run can retry uncached cases but does not rerun
every gold-check failure.
Use a different run ID for a fresh worker-count comparison. Six client workers
are not a claim of six parallel server inference slots. Record the independently
configured server concurrency and measure its memory use separately.

Parser `1.1.0` changes the generation prompt and cache identity from `1.0.4`; old
parser or teacher caches will not supply warm hits. Both response-format arms use
the same full envelope prompt; the transport constraint is their explicit difference. Do not use a full replay or slice its event iterator
to bound model work: event construction is eager. The benchmark's input selection
occurs before parsing. The production public batch helper is also sequential,
so this harness creates its own explicit worker pool.

## Read the results

Report schema `1.1` has a top-level `status`. For numerical execution measurements,
require both `mode="execute"` and `status="complete"`; `mode` alone only records
the requested operation. Completion does not imply that every case passed or that
any fresh model reply was received.

| Status | Interpretation |
|---|---|
| `planned` | Input/configuration plan only. Numeric execution counters and elapsed time are legitimately zero; gold pass totals remain null. |
| `running` | Execution has not produced a complete report. Measured summary fields are null; work may already have occurred. |
| `complete` | The complete selected cohort was retained and summarized. Inspect its transport, cache, fallback and error paths before drawing conclusions. |
| `interrupted` | Execution stopped by interruption. Measured summary fields are null and `error_type` records only the exception class. |
| `failed` | Execution or reporting failed. Measured summary fields are null and `error_type` records only the exception class. |

For `running`, `interrupted` and `failed`, `summary.selected_count` remains known,
but completed/call/cache/path counts, elapsed time, prediction counts and gold
pass totals are all null. Treat their execution cost as unknown, never as zero.
An empty `results` list means no complete cohort was retained, not that no calls
were attempted. The initial execute report already uses these unknown values.

Writing an interrupted/failed report is best effort. The original exception is
re-raised; reporting failure must not replace it. An abrupt process termination
or failed report write may leave a `running` or unreadable artifact. That is not
a completed measurement. This harness does not retain partial-case accounting or
resume an interrupted report. A later invocation, including one that explicitly
reuses matching cache entries with `--warm`, is a separate measurement and cannot
reconstruct the missing counts.

Each case records the original raw-response SHA-256, retained raw reply, separate
host-normalized intents/extraction metadata, raw and host checks, execution path,
transport/cache counters, error classes and elapsed times. Redaction flags state
when credentials or URL details changed retained raw/normalized text. The hash
refers to the original raw reply before redaction. Exception messages, headers and
configuration secrets are not included in reports.

`transport_calls` counts invocations of the original `_call_llm` seam. It is not a
server-confirmed request or token count. Preserve `manifest.transport_kind` when
sharing results: injected tests are distinct from configured-parser runs.
`extraction.method="llm"` can also describe cached output, so use the explicit
`live_llm`, `cache_llm`, `source_label`, `fallback` and error counts.

Raw checks assess schema, intended label/time, exact evidence/time quotations and
allowed grounded detail values. Raw model review flags are not compared with
host review gold: the host can add review for source-label disagreement or safety
normalization. Host checks additionally compare review and required uncertainty.
A wrong non-null timestamp fails even if marked for review. Cache hits have no
new raw prediction; source-label/fallback output does not become model-quality
credit. Use `raw_predictions_count` as the stated denominator for aggregate raw
and corresponding host pass counts; these remain null when no new raw reply was
observed. Inspect individual failures and conservative abstentions separately.

Per-case client timings are available for total parsing, transport and cache
lookup. Report sample counts and calculate p50/p95 separately for successful
transport, failed transport and cache paths. Total transport duration includes
client/network/server queue time; it is not isolated inference time. Measure cold
model loading, server-warm inference, peak server RAM and tokens using the operator's
runtime measurements. Application-cache warmth and server/model warmth are
different conditions. Keep the 15-second operational budget separate from any
explicitly labelled diagnostic run with a longer timeout (maximum 60 seconds).

No live-model quality, latency, memory or fine-tuning conclusion follows from the
injected test suite. Use independently adjudicated held-out messages and per-language
results for a later model decision; keep this small directed corpus separate.
