"""Plan a bounded local Ministral LoRA run; train only with explicit --execute.

No installer, downloader, inference server, or substitute training implementation.
See docs/LOCAL_LLM_TRAINING.md for provenance and continuation limitations.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RUN_SCHEMA = "guest-lora-run-v1"
SPLITS = ("train", "valid", "test")
STRUCTURAL = ("fine_tune_type", "num_layers", "lora_parameters", "mask_prompt")
EXPORTER_SOURCES = {"scripts/prepare_guest_sft.py", "ingestion/guest_parser.py",
                    "ingestion/guest_generation.py", "ingestion/request_polarity.py",
                    "ingestion/llm_transport.py", "common/contracts/schemas.py"}
OFFLINE = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
           "HF_HUB_DISABLE_TELEMETRY": "1", "WANDB_DISABLED": "true"}


def _hash(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_ctime_ns):
        raise ValueError(f"File changed while hashing: {path.name}")
    return digest.hexdigest()


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs,
                       parse_constant=_invalid_number)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError("Non-finite JSON number")


def validate_adapter_file(path: Path, training: dict, hidden_layers: int) -> dict:
    """Bounded, dependency-free safetensors/finite checks, not a native model load.

    This launcher trains only dense floating q/v LoRA factors. Safetensors uses
    an eight-byte little-endian JSON-header length followed by indexed raw bytes.
    A nonempty filename/hash alone cannot establish a checkpoint's validity.
    """
    formats = {"F16": (2, "e"), "BF16": (2, "H"), "F32": (4, "f"), "F64": (8, "d")}
    before = path.stat()
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError("Adapter has no safetensors header")
        header_size = int.from_bytes(prefix, "little")
        if not 2 <= header_size <= min(16_000_000, before.st_size - 8):
            raise ValueError("Invalid adapter header size")
        header = json.loads(stream.read(header_size), object_pairs_hook=_unique_pairs,
                            parse_constant=_invalid_number)
        if not isinstance(header, dict):
            raise ValueError("Adapter header must be an object")
        # MLX's native writer may emit __metadata__: null.
        metadata = header.pop("__metadata__", None)
        if metadata is not None and (not isinstance(metadata, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items())):
            raise ValueError("Invalid adapter metadata")
        if not header:
            raise ValueError("Adapter contains no tensors")
        selected = set(range(hidden_layers - training["num_layers"], hidden_layers))
        keys = set(training["lora_parameters"]["keys"])
        rank = training["lora_parameters"]["rank"]
        factors, intervals = {}, []
        for name, info in header.items():
            match = re.fullmatch(r"(?:[A-Za-z_][A-Za-z_0-9]*\.)*layers\.(\d+)\.(self_attn\.(?:q_proj|v_proj))\.(lora_[ab])", name)
            if not match or int(match[1]) not in selected or match[2] not in keys:
                raise ValueError("Adapter contains unexpected LoRA parameter names")
            if not isinstance(info, dict) or set(info) != {"dtype", "shape", "data_offsets"}:
                raise ValueError("Invalid adapter tensor descriptor")
            dtype, shape, offsets = info["dtype"], info["shape"], info["data_offsets"]
            if dtype not in formats or not isinstance(shape, list) or len(shape) != 2 or any(type(x) is not int or x <= 0 for x in shape):
                raise ValueError("Adapter factors must be nonempty floating matrices")
            if shape[1 if match[3] == "lora_a" else 0] != rank:
                raise ValueError("Adapter factor rank differs from training configuration")
            if not isinstance(offsets, list) or len(offsets) != 2 or any(type(x) is not int or x < 0 for x in offsets):
                raise ValueError("Invalid adapter offsets")
            width, fmt = formats[dtype]
            start, end = offsets
            if end - start != math.prod(shape) * width:
                raise ValueError("Adapter byte length does not match tensor shape")
            identity = (int(match[1]), match[2], match[3])
            if identity in factors:
                raise ValueError("Duplicate adapter factor identity")
            factors[identity] = name
            intervals.append((start, end, dtype, fmt))
        expected = {(layer, key, factor) for layer in selected for key in keys for factor in ("lora_a", "lora_b")}
        if set(factors) != expected:
            raise ValueError("Adapter is missing trained LoRA factors")
        cursor = 0
        for start, end, dtype, fmt in sorted(intervals):
            if start != cursor or end > before.st_size - 8 - header_size:
                raise ValueError("Adapter offsets overlap, have gaps or exceed its payload")
            stream.seek(8 + header_size + start)
            remaining = end - start
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    raise ValueError("Truncated adapter payload")
                for (value,) in struct.iter_unpack("<" + fmt, block):
                    finite = (value & 0x7F80) != 0x7F80 if dtype == "BF16" else math.isfinite(value)
                    if not finite:
                        raise ValueError("Adapter contains NaN or infinite weights")
                remaining -= len(block)
            cursor = end
        if cursor != before.st_size - 8 - header_size:
            raise ValueError("Adapter contains unindexed trailing payload")
    checkpoint_hash = _hash(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_ctime_ns):
        raise ValueError("Adapter changed during validation")
    return {"status": "passed", "method": "safetensors_header_offsets_shapes_finite_v1", "tensors": len(header),
            "sha256": checkpoint_hash, "native_load_verified": False}


def validate_adapter_runtime(plan: dict, directory: Path) -> dict:
    """After training, validate native decoding and exact-base adapter loading."""
    checkpoint = directory / "adapters.safetensors"
    checked = validate_adapter_file(checkpoint, plan["training"], plan["model"]["num_hidden_layers"])
    import gc
    import mlx.core as mx
    from mlx.utils import tree_flatten
    from mlx_lm import utils

    weights = mx.load(str(checkpoint))
    if not isinstance(weights, dict) or len(weights) != checked["tensors"]:
        raise ValueError("Native adapter load returned unexpected tensors")
    for value in weights.values():
        finite = mx.all(mx.isfinite(value))
        mx.eval(finite)
        if not bool(finite.item()):
            raise ValueError("Native adapter load contains non-finite weights")
    model = tokenizer = None
    try:
        model, tokenizer = utils.load(plan["model"]["path"], adapter_path=str(directory), lazy=True,
                                      trust_remote_code=False,
                                      tokenizer_config={"local_files_only": True, "trust_remote_code": False})
        params = {name: value for name, value in tree_flatten(model.parameters()) if name.endswith((".lora_a", ".lora_b"))}
        if set(params) != set(weights) or any(tuple(params[name].shape) != tuple(weights[name].shape) for name in weights):
            raise ValueError("Adapter keys/shapes do not match the exact base model")
        if _hash(checkpoint) != checked["sha256"]:
            raise ValueError("Adapter changed during native load")
    finally:
        del model, tokenizer, weights
        gc.collect()
        mx.clear_cache()
    return {**checked, "native_load_verified": True,
            "native_method": "mlx.load_finite_and_exact_base_adapter_key_shape_load", "quality_verified": False}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"messages"}:
            raise ValueError(f"Expected exported chat rows in {path.name}")
        messages = row["messages"]
        if (not isinstance(messages, list) or len(messages) != 3
                or [m.get("role") if isinstance(m, dict) else None for m in messages]
                != ["system", "user", "assistant"]
                or any(set(m) != {"role", "content"} or not isinstance(m["content"], str)
                       or not m["content"].strip() for m in messages)):
            raise ValueError(f"Expected system/user/assistant strings in {path.name}")
    return rows


def _data_identity(directory: Path) -> dict:
    manifest = _json(directory / "manifest.json")
    if (manifest.get("schema_version") != "guest-sft-v1"
            or manifest.get("generation_profile") != "extraction"
            or manifest.get("source_dataset") not in {"quickstart", "dev", "synthetic"}):
        raise ValueError("Expected one guest-sft-v1 extraction export; Holdout is not accepted")
    outputs = manifest.get("outputs", {})
    if set(outputs) != {f"{split}.jsonl" for split in SPLITS}:
        raise ValueError("Manifest must identify train/valid/test files")
    system_hash = manifest.get("prompt", {}).get("system_sha256")
    if not _digest(system_hash) or not _digest(manifest.get("prompt", {}).get("generation_schema_sha256")):
        raise ValueError("Missing prompt/schema identity")
    for name, expected in outputs.items():
        path = directory / name
        if not isinstance(expected, dict) or not _digest(expected.get("sha256")) or _hash(path) != expected["sha256"]:
            raise ValueError(f"Dataset hash mismatch: {name}")
        rows = _rows(path)
        if type(expected.get("rows")) is not int or expected["rows"] != len(rows):
            raise ValueError(f"Dataset row count mismatch: {name}")
        if any(_sha(row["messages"][0]["content"]) != system_hash for row in rows):
            raise ValueError(f"System prompt mismatch: {name}")
    files = manifest.get("code", {}).get("files", {})
    if not isinstance(files, dict) or set(files) != EXPORTER_SOURCES:
        raise ValueError("Missing exporter/parser code identity")
    for name, expected in files.items():
        path = (ROOT / name).resolve()
        if not _inside(path, ROOT) or not _digest(expected) or _hash(path) != expected:
            raise ValueError(f"Exporter/parser code hash mismatch: {name}")
    return {"path": str(directory), "manifest_sha256": _hash(directory / "manifest.json"), "manifest": manifest}


def _model_identity(directory: Path) -> dict:
    if not directory.is_dir():
        raise ValueError("--model-dir must be an existing local MLX model directory, not a Hub ID or Ollama name")
    config = _json(directory / "config.json")
    text = config.get("text_config", config)
    if config.get("model_type") not in {"ministral3", "mistral3"} or text.get("model_type") != "ministral3":
        raise ValueError("This wrapper supports only an explicit compatible Ministral 3 MLX base")
    if config.get("auto_map") or config.get("model_file"):
        raise ValueError("Custom model code is not supported")
    required = ("config.json", "tokenizer.json", "tokenizer_config.json")
    if any(not (directory / name).is_file() for name in required):
        raise ValueError("Incomplete local model/tokenizer; no files will be downloaded")
    if not list(directory.glob("model*.safetensors")):
        raise ValueError("Local model*.safetensors required; Ollama GGUF is not a training checkpoint")
    files = {p.name: _hash(p) for p in sorted(directory.iterdir()) if p.is_file()
             and (p.suffix in {".json", ".safetensors", ".jinja", ".tiktoken", ".model"}
                  or p.name.startswith("tokenizer.model"))}
    if any(p.stat().st_size == 0 for p in directory.glob("model*.safetensors")):
        raise ValueError("Empty model weights")
    return {"path": str(directory), "files": files, "fingerprint": _sha(_canonical(files)),
            "model_type": config["model_type"], "num_hidden_layers": text.get("num_hidden_layers")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("data-dir", "model-dir", "out-dir"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--iters", type=int, default=100)
    parser.add_argument("--max-seq-length", type=int, default=2048)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    parser.add_argument("--num-layers", type=int, default=8)
    parser.add_argument("--rank", type=int, default=8)
    parser.add_argument("--grad-accumulation-steps", type=int, default=1)
    parser.add_argument("--grad-checkpoint", action="store_true")
    parser.add_argument("--resume-run", type=Path, help="Completed run from this wrapper; adapter-only continuation")
    parser.add_argument("--execute", action="store_true")
    return parser


def prepare_plan(args: argparse.Namespace) -> dict:
    if args.out_dir.expanduser().is_symlink():
        raise ValueError("--out-dir must be fresh, not a symbolic link")
    data, model, output = (getattr(args, key).expanduser().resolve() for key in ("data_dir", "model_dir", "out_dir"))
    if output.exists() or output.is_symlink():
        raise ValueError("--out-dir must be fresh; existing outputs are never overwritten")
    if _inside(output, data) or _inside(output, model):
        raise ValueError("Output must be outside the source dataset and model directories")
    if (not 1 <= args.iters <= 1000 or not 1 <= args.grad_accumulation_steps <= 32
            or args.iters % args.grad_accumulation_steps or not 128 <= args.max_seq_length <= 8192
            or not 1 <= args.rank <= 64 or not 1 <= args.num_layers <= 64
            or not math.isfinite(args.learning_rate) or not 0 < args.learning_rate <= 1e-3):
        raise ValueError("Invalid bounded training settings; iters must be divisible by gradient accumulation")
    data_info, model_info = _data_identity(data), _model_identity(model)
    if type(model_info["num_hidden_layers"]) is not int or args.num_layers > model_info["num_hidden_layers"]:
        raise ValueError("Requested layers exceed the model's declared layers")
    config = {"model": str(model), "train": True, "data": str(output / "data"),
              "adapter_path": str(output / "adapters"), "fine_tune_type": "lora", "batch_size": 1,
              "iters": args.iters, "max_seq_length": args.max_seq_length, "learning_rate": args.learning_rate,
              "num_layers": args.num_layers, "grad_accumulation_steps": args.grad_accumulation_steps,
              "grad_checkpoint": args.grad_checkpoint, "mask_prompt": True, "seed": 17,
              "steps_per_report": 10, "steps_per_eval": min(50, args.iters), "val_batches": 25,
              "save_every": min(50, args.iters), "test": False, "trust_remote_code": False,
              "report_to": None, "optimizer": "adam", "lr_schedule": None,
              "lora_parameters": {"keys": ["self_attn.q_proj", "self_attn.v_proj"],
                                  "rank": args.rank, "scale": 20.0, "dropout": 0.0}}
    resume = None
    if args.resume_run:
        previous = args.resume_run.expanduser().resolve()
        run = _json(previous / "run.json")
        if run.get("schema_version") != RUN_SCHEMA or run.get("status") != "completed" or run.get("returncode") != 0:
            raise ValueError("Resume requires a completed wrapper run with verified provenance")
        old = run.get("plan", {})
        if old.get("model", {}).get("fingerprint") != model_info["fingerprint"]:
            raise ValueError("Resume base model/tokenizer fingerprint mismatch")
        if old.get("data", {}).get("manifest_sha256") != data_info["manifest_sha256"]:
            raise ValueError("Resume dataset/prompt identity mismatch")
        adapter_config = _json(previous / "adapters/adapter_config.json")
        for key in STRUCTURAL:
            if config[key] != old.get("training", {}).get(key) or config[key] != adapter_config.get(key):
                raise ValueError(f"Resume adapter configuration mismatch: {key}")
        if adapter_config.get("model") != old.get("training", {}).get("model"):
            raise ValueError("Resume adapter base path provenance mismatch")
        for name in ("adapters/adapters.safetensors", "adapters/adapter_config.json"):
            expected = run.get("artifacts", {}).get(name)
            if not _digest(expected) or _hash(previous / name) != expected:
                raise ValueError(f"Resume checkpoint hash mismatch: {name}")
        checked = validate_adapter_file(previous / "adapters/adapters.safetensors", config, model_info["num_hidden_layers"])
        if checked["sha256"] != run["artifacts"]["adapters/adapters.safetensors"]:
            raise ValueError("Resume checkpoint changed during validation")
        if _inside(output, previous):
            raise ValueError("Continuation output must be outside the previous run")
        resume = {"run": str(previous), "run_sha256": _hash(previous / "run.json"),
                  "checkpoint_sha256": run["artifacts"]["adapters/adapters.safetensors"],
                  "semantics": "adapter weights only; new optimizer and RNG/sampling stream, not exact state resume"}
        config["resume_adapter_file"] = str(output / "resume/adapters.safetensors")
    return {"schema_version": RUN_SCHEMA, "status": "planned", "data": data_info, "model": model_info,
            "out_dir": str(output), "training": config, "resume": resume,
            "command": [sys.executable, "-m", "mlx_lm", "lora", "--config", str(output / "config.json")],
            "offline_environment": OFFLINE, "launcher_sha256": _hash(Path(__file__)),
            "limits": ["Plan does not load a model or prove training feasibility/quality.",
                       "Execution requires local Apple Silicon, compatible installed MLX APIs and complete token checks.",
                       "Process checks detect recognized jobs; coordinate exclusive accelerator use with its operator.",
                       "No optimizer/RNG state restoration, hosted calls, downloads, installs or cloud provisioning."]}


def _check_model_processes() -> None:
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, check=True)
    processes = [line.strip().split(None, 2) for line in result.stdout.splitlines() if line.strip()]
    parents = {int(p[0]): int(p[1]) for p in processes if len(p) == 3}
    ignored, pid = set(), os.getpid()
    while pid and pid not in ignored:
        ignored.add(pid)
        pid = parents.get(pid, 0)
    pattern = re.compile(r"\b(?:mlx_lm(?:\.|\s+)lora|mlx_vlm(?:\.|\s+)(?:train|lora)|"
                         r"train_guest_lora\.py|scripts\.train_guest_lora|ollama\s+(?:runner|run)|"
                         r"ollama_llama_server|llama[-_](?:server|cli)|torchrun|accelerate\s+launch|"
                         r"(?:train|train_fast|trainer)\.py|vllm)\b", re.I)
    active = [p[0] for p in processes if len(p) == 3 and int(p[0]) not in ignored and pattern.search(p[2])]
    if active:
        raise ValueError("Recognized model/training jobs are active (PIDs " + ", ".join(active) + "); coordinate before running")


@contextmanager
def _exclusive_run():
    import fcntl
    path = Path(tempfile.gettempdir()) / f"hotel-guest-lora-{os.getuid()}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as exc:
        raise ValueError("Another guest LoRA wrapper owns this host's training lock") from exc
    finally:
        os.close(descriptor)


def validate_token_lengths(tokenizer, dataset_class, rows: list[dict], maximum: int) -> dict:
    """Use the installed trainer's formatter; no approximate character limits."""
    dataset = dataset_class(rows, tokenizer, mask_prompt=True)
    lengths, targets = [], []
    for index, row in enumerate(rows, 1):
        tokens, offset = dataset.process(row)
        if len(tokens) > maximum:
            raise ValueError(f"Formatted row {index} has {len(tokens)} tokens > {maximum}; refusing target truncation")
        if not 0 < offset < len(tokens) - 1:
            raise ValueError(f"Formatted row {index} has no usable masked completion")
        prefix = tokenizer.apply_chat_template(row["messages"][:-1], tools=None,
                                               add_generation_prompt=True, return_dict=False)
        if list(tokens[:offset]) != list(prefix):
            raise ValueError(f"Formatted row {index} prompt mask is not a token prefix")
        lengths.append(len(tokens))
        targets.append(len(tokens) - offset)
    return {"rows": len(rows), "max_tokens": max(lengths, default=0),
            "min_target_tokens": min(targets, default=0), "max_target_tokens": max(targets, default=0)}


def validate_loss_mask(mx, trainer) -> dict:
    """Probe the installed loss with tiny uniform logits, without weights or gradients."""
    cases = (
        ("masked_padded", [[1, 2, 3, 4, 5, 0, 0, 0]], [[3, 5]], 2),
        ("unmasked_padded", [[1, 2, 3, 4, 5, 0, 0, 0]], [[0, 5]], 4),
        ("unpadded", [[1, 2, 3, 4, 5, 6, 7, 8]], [[3, 8]], 5),
        ("ragged", [[1, 2, 3, 4, 5, 0, 0, 0], [1, 2, 3, 4, 5, 6, 7, 0]],
         [[3, 5], [2, 7]], 7),
    )
    correction = ("Use a reviewed MLX-LM build whose shifted-target loss excludes padding, "
                  "then rerun this preflight. The launcher does not patch dependencies; "
                  "no model weights were loaded or training started.")

    def uniform_logits(inputs):
        return mx.zeros((*inputs.shape, 16), dtype=mx.float32)

    results = {}
    for name, batch, lengths, expected in cases:
        try:
            loss, count = trainer.default_loss(
                uniform_logits, mx.array(batch, dtype=mx.int32), mx.array(lengths, dtype=mx.int32))
            mx.eval(loss, count)
            observed, loss_value = float(count.item()), float(loss.item())
        except Exception as exc:
            raise ValueError(f"Installed MLX-LM loss-mask check failed for {name}. {correction}") from exc
        if observed != expected or not math.isfinite(loss_value):
            raise ValueError(f"Installed MLX-LM loss-mask check failed for {name}: "
                             f"expected {expected} supervised tokens, got {observed}; "
                             f"finite loss={math.isfinite(loss_value)}. {correction}")
        results[name] = {"expected_tokens": expected, "observed_tokens": int(observed)}
    return {"status": "passed", "cases": results, "gradient_updates": 0,
            "method": "installed default_loss with uniform stub logits; no model weights"}


def _runtime_preflight(plan: dict) -> dict:
    import gc
    import mlx.core as mx
    import mlx_lm.lora as lora
    import mlx_lm.tuner.datasets as datasets
    import mlx_lm.tuner.trainer as trainer
    import mlx_lm.utils as utils

    if not mx.metal.is_available() or mx.default_device() != mx.gpu:
        raise ValueError("MLX Metal accelerator is unavailable")
    required = {"mask_prompt", "grad_accumulation_steps", "lora_parameters", "resume_adapter_file"}
    if (not required <= set(lora.CONFIG_DEFAULTS) or not hasattr(datasets.ChatDataset, "process")
            or not {"lazy", "trust_remote_code", "tokenizer_config"} <= set(inspect.signature(utils.load).parameters)):
        raise ValueError("Installed MLX-LM APIs do not match this reviewed launcher; no training started")
    versions = {name: importlib.metadata.version(name) for name in ("mlx", "mlx-lm", "transformers")}
    loss_mask = validate_loss_mask(mx, trainer)
    model, tokenizer = utils.load(plan["model"]["path"], lazy=True, trust_remote_code=False,
                                  tokenizer_config={"local_files_only": True, "trust_remote_code": False})
    if len(model.layers) < plan["training"]["num_layers"] or not tokenizer.chat_template:
        raise ValueError("Model layers or explicit chat template are unavailable")
    for layer in model.layers[-plan["training"]["num_layers"]:]:
        if not all(hasattr(layer.self_attn, key) for key in ("q_proj", "v_proj")):
            raise ValueError("Installed model does not expose the selected adapter targets")
    lengths = {}
    for split in ("train", "valid"):
        rows = _rows(Path(plan["data"]["path"]) / f"{split}.jsonl")
        if not rows:
            raise ValueError(f"Execution requires a nonempty {split} split")
        lengths[split] = validate_token_lengths(tokenizer, datasets.ChatDataset, rows, plan["training"]["max_seq_length"])
    del model, tokenizer
    gc.collect()
    mx.clear_cache()
    resume_check = None
    if plan.get("resume"):
        resume_check = validate_adapter_runtime(plan, Path(plan["resume"]["run"]) / "adapters")
    return {"packages": versions, "python": platform.python_version(), "system": platform.platform(),
            "module_sha256": {module.__name__: _hash(Path(module.__file__))
                              for module in (lora, datasets, trainer, utils)}, "token_lengths": lengths,
            "loss_mask_check": loss_mask,
            "resume_adapter_validation": resume_check,
            "model_loaded_lazily": True, "gradient_updates_in_preflight": 0}


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def execute_plan(args: argparse.Namespace, plan: dict) -> dict:
    if not args.execute:
        raise ValueError("Execution requires --execute")
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        raise ValueError("Execution requires the operator's Apple Silicon Mac; this host is unsupported")
    with _exclusive_run():
        _check_model_processes()
        if prepare_plan(args) != plan:
            raise ValueError("Inputs changed after planning")
        original_environment = {key: os.environ.get(key) for key in OFFLINE}
        os.environ.update(OFFLINE)
        try:
            runtime = _runtime_preflight(plan)
            _check_model_processes()
            if prepare_plan(args) != plan:
                raise ValueError("Inputs changed during preflight; no training started")
            output = Path(plan["out_dir"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.mkdir()
            (output / "data").mkdir()
            # Test split is hashed in the export identity but never supplied to the trainer.
            for name in ("train.jsonl", "valid.jsonl"):
                shutil.copyfile(Path(plan["data"]["path"]) / name, output / "data" / name)
                if _hash(output / "data" / name) != plan["data"]["manifest"]["outputs"][name]["sha256"]:
                    raise ValueError("Source data changed during snapshot")
            if plan["resume"]:
                (output / "resume").mkdir()
                shutil.copyfile(Path(plan["resume"]["run"]) / "adapters/adapters.safetensors", output / "resume/adapters.safetensors")
                if _hash(output / "resume/adapters.safetensors") != plan["resume"]["checkpoint_sha256"]:
                    raise ValueError("Checkpoint changed during snapshot")
            _write(output / "config.json", plan["training"])
            _write(output / "source_manifest.json", plan["data"]["manifest"])
            result = {"schema_version": RUN_SCHEMA, "status": "running", "plan": plan, "runtime": runtime,
                      "started_utc": datetime.now(timezone.utc).isoformat(), "artifacts": {}}
            _write(output / "run.json", result)
            try:
                with (output / "training.log").open("x", encoding="utf-8") as log:
                    process = subprocess.run(plan["command"], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT)
                result["returncode"] = process.returncode
                if process.returncode:
                    raise ValueError(f"MLX-LM exited with status {process.returncode}; checkpoint files and training.log are preserved")
                saved = _json(output / "adapters/adapter_config.json")
                if any(saved.get(key) != plan["training"][key] for key in STRUCTURAL):
                    raise ValueError("Trainer saved unexpected adapter configuration")
                if saved.get("model") != plan["training"]["model"]:
                    raise ValueError("Trainer saved unexpected adapter base provenance")
                result["adapter_validation"] = validate_adapter_runtime(plan, output / "adapters")
                result["status"] = "completed"
            except BaseException as exc:
                result["status"] = "failed"
                result["error_type"] = type(exc).__name__
                raise
            finally:
                result["finished_utc"] = datetime.now(timezone.utc).isoformat()
                result["artifacts"] = {str(path.relative_to(output)): _hash(path)
                                       for path in sorted((output / "adapters").glob("*")) if path.is_file()}
                _write(output / "run.json", result)
            return result
        finally:
            for key, value in original_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = prepare_plan(args)
        result = execute_plan(args, plan) if args.execute else plan
    except (ValueError, OSError, ImportError, KeyError, TypeError) as exc:
        print(f"Training not completed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
