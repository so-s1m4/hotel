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
)

python -m scripts.benchmark_guest_parser "${benchmark_args[@]}" \
  --workers 1 --run-id workers1-01 \
  --out outputs/parser-benchmark/plan-workers1.json
```

Plan mode validates input and configuration and writes a report with zero
completed cases and zero calls. It does not create the managed cache. Inspect the
selected case IDs, count, endpoint and pinned hashes before execution. Report
paths must be new; existing files, including symlink/hardlink aliases, are rejected
before calls. Reports cannot be placed inside the managed candidate/run cache.

## Cold, cache-warm and queued-client runs

Keep the same server configuration throughout a paired cold/cache-warm run.
Start with one client worker. A fresh run requires an absent or empty cache
directory for that candidate/run ID.

```bash
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
count, endpoint/model, parser/prompt/schema hashes and timeout. Transport or
validation fallbacks are not cached. Valid normalized extractions are cached even
when a gold check fails, so a warm run can retry uncached cases but does not rerun
every gold-check failure.
Use a different run ID for a fresh worker-count comparison. Six client workers
are not a claim of six parallel server inference slots. Record the independently
configured server concurrency and measure its memory use separately.

Parser `1.0.4` deliberately changes cache keys from `1.0.3`; old teacher caches
will not supply warm hits. Do not use a full replay or slice its event iterator
to bound model work: event construction is eager. The benchmark's input selection
occurs before parsing. The production public batch helper is also sequential,
so this harness creates its own explicit worker pool.

## Read the results

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
