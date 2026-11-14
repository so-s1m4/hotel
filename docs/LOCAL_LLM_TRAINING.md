# Local guest-parser weight training

`scripts/prepare_guest_sft.py` prepares reviewed examples;
`scripts/train_guest_lora.py` plans or explicitly launches a bounded run using an
already installed MLX-LM trainer. Neither utility installs software, downloads a
model, starts an inference server, or provisions cloud machines.

## Current state and scope

The preparation and bounded-launcher utilities are integrated in this checkout,
with offline fixture tests for export boundaries, checkpoint corruption and
completion/resume handling. This does not certify a trained candidate. A native
run launched from a separate frozen source snapshot keeps that snapshot's
provenance; later launcher improvements do not retroactively validate its output.
Check such an adapter separately with the hardened validator after its training
process has exited, without changing its original artifacts.

The 14 November 2026, 00:09 Europe/Vienna, shared-chat host check (#517) found Ollama Ministral 3B/8B on the
owner's Mac, but no MLX trainer, training process, adapter, training checkpoint,
or Hugging Face cache. The completed parser pilots changed extraction prompts,
schemas and host validation; they did not train model weights. A later operator
report confirmed a native-Mac one-step smoke run with MLX-LM 0.32.2 and MLX
0.32.4. That establishes a small training execution, not improved model quality
or a previous checkpoint lineage for this wrapper. The released trainer's
padding-loss issue described below must be resolved before this launcher allows
a run. Host execution remains coordinated with the Mac's operator.

Keep the existing parser and downstream checks unchanged. A successful training
process does not establish improved guest-intent quality, correct time reasoning,
or permission for hotel actions. Evaluate a candidate through the existing
[parser benchmark](PARSER_BENCHMARK.md) before any separate deployment decision.

## Prepare one reviewed dataset

Supply JSONL annotations using the exporter schema in
`scripts/prepare_guest_sft.py`: `id`, `group_id`, `source_dataset`, `record`,
`context`, `target`, and `annotation`. `annotation` records `reviewed: true`,
`reviewer`, `reviewer_kind` (`human`, `assistant`, or `model`) and `method`
(`manual` or `teacher_reviewed`). These declarations are provenance assertions;
model-generated or assistant-reviewed labels are not independently verified gold.

Use one source per export (`dev`, `quickstart`, or `synthetic`). Never combine
the independent Quickstart and Dev simulations, or use organizer Holdout.
Teacher outputs require review; do not promote source labels or cached model
responses to evidence-grounded extraction targets automatically.

```bash
python -m scripts.prepare_guest_sft \
  --input /absolute/path/reviewed-dev.jsonl \
  --out outputs/sft-dev-v1
```

The exporter retains the production extraction prompt and causal context filter,
requires an extraction target with null UTC fields and empty details, and records
source/annotation/prompt/code hashes. Connected request, booking and declared
group identities stay together across deterministic train/valid/test splits.
Normalized exact benchmark messages and their connected groups are excluded.
Exact declared evaluation request and booking IDs are also exclusion seeds,
even when their message text has changed; their connected training groups are
excluded as well. This cannot detect undeclared paraphrases or falsely declared
source identities.
Inspect split counts, languages, target quality and warnings: small connected
datasets can have empty splits. The launcher requires nonempty train and valid
before execution. An empty test split is an evaluation limitation.

## Use an explicit compatible local base

`--model-dir` must point to an existing Ministral 3 MLX directory with local
`config.json`, `model*.safetensors`, `tokenizer.json` and `tokenizer_config.json`.
The launcher fingerprints weights, configuration and tokenizer/template files.
It rejects a Hub identifier, an Ollama model name, GGUF-only files, or custom
model-code configuration. No substitute base is selected.

An Ollama Q4_K_M serving artifact is not a training checkpoint. The published
MLX 4-bit conversion uses a different quantization representation. Any initial
conversion/download must be a separately coordinated preparation step with
explicit model lineage; this wrapper does not perform it. [1][2]

MLX-LM added `ministral3`/`mistral3` architecture support in September 2026, so the
older claim that the model always requires MLX-VLM is no longer sufficient.
Execution checks the installed MLX-LM APIs, loads the exact local model lazily,
checks its adapter targets and tokenizer, and records package versions and
trainer source hashes. It does not claim compatibility based on a guessed
minimum package version. An unsupported installation fails before training. [3]

Before loading any model weights, execute mode calls the installed trainer's
`default_loss` on tiny synthetic padded, unpadded and ragged arrays with uniform
stub logits. No gradients or optimizer updates are computed. The check requires
the correct number of retained targets after shifting, including prompt masking
and excluding padding. MLX-LM 0.32.2's inclusive upper mask boundary counts an
extra padding target for padded rows and fails this check. Use a reviewed
dependency build with corrected loss masking and rerun the preflight; the
launcher does not modify dependencies or supply a replacement trainer. [8][9]

## Inspect a plan, then explicitly execute

Use the Python interpreter of the separately prepared MLX training environment.
The repository's ordinary dependencies are unchanged. Plan mode requires no MLX
installation and performs no model load or training subprocess:

```bash
python -m scripts.train_guest_lora \
  --data-dir outputs/sft-dev-v1 \
  --model-dir /absolute/path/to/existing-ministral-mlx \
  --out-dir outputs/lora-dev-001 \
  > outputs/lora-dev-001-plan.json
```

The plan is printed to stdout. `--out-dir` must not exist; planning does not
create it. Data-file hashes/counts, prompt identity and exporter/parser code
hashes must match. When source changes, re-export and review the new identity.
Hashes check consistency, not annotation truth or independent authenticity.

On the owner's Apple Silicon Mac, after its operator has coordinated exclusive
accelerator use, the same command with `--execute` runs the recorded trainer:

```bash
python -m scripts.train_guest_lora \
  --data-dir outputs/sft-dev-v1 \
  --model-dir /absolute/path/to/existing-ministral-mlx \
  --out-dir outputs/lora-dev-001 \
  --execute
```

Execution requires macOS/Apple Silicon and available MLX Metal. A nonblocking
host lock prevents two copies of this wrapper from running together. A process
check rejects recognized active trainers and model runners; an empty Ollama
daemon is permitted. This check is not a universal GPU scheduler: agree with
the other operator before launching, and do not start another model job during
the run. The wrapper never terminates other processes.

The environment forces Hugging Face/Transformers/Datasets offline modes and
disables remote model code. Before gradient updates, the installed trainer's
`ChatDataset.process` and the actual model tokenizer format every train/valid
example, including system prompt, template and target. Overlength examples,
empty completions and inconsistent prompt masks fail instead of truncating a
target. After validation, train/valid are copied into the fresh run directory
and rehashed. The test split is recorded in the source manifest but never
passed to the training CLI. [4][5]
Keep the model and tokenizer files immutable throughout execution: the final
preflight rehash detects preceding changes but does not lock external writers
while the child trainer reloads its base.

## Bounded settings and measurements

Defaults are **100 microsteps**, microbatch **1**, last **8** layers, LoRA rank
**8**, `q_proj`/`v_proj`, scale **20**, dropout **0**, learning rate **1e-5**,
maximum formatted length **2048**, and completion-only loss. These are a modest
initial configuration, not a measured optimum or a guarantee of fitting 16 GB.
The wrapper launches `python -m mlx_lm lora --config config.json`; JSON is used
as a YAML-compatible configuration file. [4][6]

- `--iters`: 1–1000; this is a microstep count, not necessarily optimizer updates.
- `--grad-accumulation-steps`: 1–32; `iters` must be divisible by it, so the
  selected trainer cannot leave a final partial accumulation unapplied.
- `--max-seq-length`: 128–8192; full token validation still applies.
- `--grad-checkpoint`: trade extra recomputation for reduced activation memory.
- `--num-layers`, `--rank`, `--learning-rate`: explicit bounded experiments;
  changing adapter structure is not a continuation of the original adapter.
- `--steps-per-eval`: validation cadence, 1 through `iters`; default remains
  `min(50, iters)`. Checkpoints still save every `min(50, iters)` updates.
- `--val-batches`: maximum validation batches per pass, 1–10000; default 25.
  With batch size one, set this to at least the VALID row count to cover the
  complete split in each pass. Keeping all files does not itself mean a capped
  validation pass reads every row.

For a preselected final 200-step candidate with 48 VALID rows,
`--steps-per-eval 200 --val-batches 48` makes two complete validation passes
in the inspected MLX-LM 0.32.2 loop, rather than five capped passes. Validation
runs before that iteration's update: the log labelled step 200 evaluates the
weights after 199 updates, not the saved final 200-update checkpoint. Retain
this distinction; neither this loss nor a faster cadence certifies quality.

Compare sustained tokens/sec, peak memory, validation loss and task-specific
held-out results. Keep inference servers and competing training work off the
accelerator during the measurement. Gradient checkpointing and smaller batches
are memory controls, not automatic speed improvements. Do not infer training
memory from the Ollama download size. [4]

The run directory retains `config.json`, `source_manifest.json`, `run.json`,
`training.log`, data snapshots and adapter checkpoints. `run.json` records
identities, command, versions, loss-mask and token-length checks, exit status and hashes of
produced checkpoints. Failed trainer runs retain their files. A zero exit code
and an existing adapter filename are insufficient for completion. The launcher
first checks the safetensors header, payload offsets, nonempty floating LoRA
factors, configured layers/rank, and finite values without optional dependencies.
It then uses native MLX decoding and finite checks and lazily loads the adapter
against the exact local base, checking the complete parameter key/shape set.
These native checks occur only after the trainer has exited, under the same
exclusive-run lock. Completion records `adapter_validation`; a failed check
records a failed run and preserves the checkpoint and log. Model-quality
evaluation and serving/export remain separate operations.

## Continue a verified completed adapter run

This small wrapper supports `--resume-run /path/to/completed-run` only for its
own completed, recorded runs. It checks the exact base/tokenizer fingerprint,
dataset/prompt identity, adapter structure/config and checkpoint hashes, then
copies the final adapter into a new output directory. Original outputs remain
intact. Missing metadata, an unfinished parent, changed data/base, or arbitrary
external adapters are rejected; provenance is not invented.
Even matching hashes do not admit zero-byte, malformed, truncated, incomplete
or non-finite factors. Planning validates their bytes without importing MLX;
execute preflight also performs native adapter compatibility checks before
launching the continuation trainer. A successful structural check alone is
reported with `native_load_verified=false`, not as proof of model usability.

Continuation restores **adapter weights only**. MLX-LM constructs a fresh
optimizer; this wrapper does not restore the old optimizer, scheduler progress,
RNG or data position. Its fixed seed starts a new sampling stream and can repeat
examples. State this as another training stage, not exact mid-run recovery. [6]

## Google server

Use the existing `ccc-setup/ai/vm.sh` helper on the owner's Mac; its local path
is recorded in the shared-chat setup handoff (#70).
Its last reported `status` check in shared-chat #517 was still billing-blocked.
Recheck live availability through that authorized helper before assigning work.
This package does not call `up`, create a paid VM, alter billing, or assume access.

If the existing CPU VM is available, useful independent tasks are annotation
validation/deduplication, split and exclusion checks, tokenization preparation,
scoring saved generations, and checkpoint/hash verification. The documented
`n2-standard-32` configuration has 32 vCPUs and 128 GB RAM; that is not evidence
of a GPU or a faster heterogeneous training setup. [7] Keep gradient training
on the verified local accelerator until measurements justify a different plan.
Copy results off any automatically expiring VM before its lifetime ends.

## Primary references

1. [Ollama model import](https://docs.ollama.com/import).
2. [MLX Ministral 3B conversion config](https://huggingface.co/mlx-community/Ministral-3-3B-Instruct-2512-4bit/blob/main/config.json).
3. [MLX-LM Ministral3 support, merged PR #860](https://github.com/ml-explore/mlx-lm/pull/860).
4. [MLX-LM LoRA guide: masking, memory and accumulation](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/LORA.md).
5. [Installed trainer formatting contract](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/tuner/datasets.py).
6. [MLX-LM CLI, adapter loading and new optimizer](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/lora.py).
7. [Google N2 machine specifications](https://docs.cloud.google.com/compute/docs/general-purpose-machines#n2_machine_types).
8. [MLX-LM 0.32.2 release artifacts](https://pypi.org/project/mlx-lm/0.32.2/).
9. [MLX-LM trainer loss and batch padding](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/tuner/trainer.py).

Upstream main branches can change. The execute preflight records the installed
sources; these references do not replace verification on the actual Mac.

## Explicit split-preserving Dev/Synthetic composition

`scripts.compose_guest_sft` creates a new `guest-sft-mixed-v1` export labelled
`mixed_dev_synthetic`. It accepts exactly one explicitly hash-pinned approved
Dev export and one Synthetic export. It copies their TRAIN and VALID chat rows
without changing targets or original split/line/group identities; it does not
resplit them through the single-source exporter. The new TEST file is empty.
Parent TEST contents are never opened; only identifiers and hashes already in
the pinned parent manifest are used for exclusions. This cannot establish
cross-source semantic or paraphrase disjointness of unseen test messages.

Required arguments are `--dev-export`, `--synthetic-export`, their respective
`--dev-manifest-sha256` / `--synthetic-manifest-sha256`, and a fresh `--out`.
Optional `--augmentation-chat` and `--augmentation-provenance` must be supplied
together. The latter uses `guest-sft-train-fragments-v1`: a pinned chat-file hash,
TRAIN-only row reviews and exact pointers to approved TRAIN parent lines, input
and target hashes. All linked parent groups remain connected even if an operator
assigns a new fragment group name. Reviews are operator assertions, not automatic
proof of semantic correctness or that every new word derives from a source.
Independent data review must precede training.

`--augmentation-weight` defaults to 1 and permits integer 1..4. Repeated fragments
are explicitly recorded training exposures, not independent examples. The manifest
separates parent rows, unique canonical production inputs, fragment counts and
exposures. VALID remains the original approved VALID rows only; it may contain
no multi-intent cases. No validation or evaluation message is derived from TRAIN
fragments, and Dev31 remains separate and untouched.

Creation and launcher validation both reconstruct the complete output from parent
and fragment provenance. Parent/code snapshots are recorded honestly; old parent
code hashes are not relabelled as current code. Current system/schema must match
those parents. The legacy launcher checks are unchanged for single-source exports;
only the explicit reconstructable mixed schema enters the new path. It rejects
changed targets, conflicting canonical inputs, unreviewed fragments, unknown or
non-TRAIN parent pointers, cross-split group/ID/text matches, and mandatory
normalized evaluation-text/identity matches. Additional evaluator sources can be
passed through repeated `--exclude-evaluation`; their byte hashes are pinned.
These exact-match guards do not certify absence of undeclared paraphrases.

The normal training launcher still plans by default and trains only with
`--execute`. Existing exact native tokenizer/prompt-mask checks refuse target
truncation; choose a sufficient maximum sequence length after that preflight.
A composition plan neither starts a model nor establishes quality. Fix the intended
final training step before comparing candidates; validation loss is not permission
to select checkpoints using diagnostic or held-out targets.
