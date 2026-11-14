"""Launcher consistency and no-execution boundaries, with inert local fixtures."""
from __future__ import annotations

import builtins
from contextlib import nullcontext
import hashlib
import importlib
import json
import math
import struct
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_data(root: Path, *, suffix: str = "") -> Path:
    prepare = importlib.import_module("scripts.prepare_guest_sft").prepare_dataset
    root.mkdir()
    rows = []
    for number in range(80):
        text = f"Please bring towels for synthetic offline scenario {suffix}{number}."
        rows.append({
            "id": f"row-{number}", "group_id": f"group-{number}", "source_dataset": "synthetic",
            "record": {"request_id": f"request-{number}", "timestamp_utc": "2026-01-06T09:00:00Z",
                       "language": "en", "request_type": "extra_towels", "text": text},
            "context": {"as_of_utc": "2026-01-06T09:00:00Z"},
            "target": {"intents": [{"intent": "extra_towels", "requested_time_utc": None,
                                    "requested_time_text": None, "evidence_quote": text,
                                    "details": {}, "needs_review": False}]},
            "annotation": {"reviewed": True, "reviewer": "offline-test-fixture",
                           "reviewer_kind": "assistant", "method": "manual"},
        })
    source = root / "source.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    prepared = root / "prepared"
    prepare(source, prepared, valid_fraction=0.2, test_fraction=0.2)
    return prepared


@pytest.fixture
def launcher():
    return importlib.import_module("scripts.train_guest_lora")


@pytest.fixture
def local_inputs(tmp_path):
    data = make_data(tmp_path / "dataset")
    model = tmp_path / "base-model"
    model.mkdir()
    (model / "config.json").write_text(json.dumps({"model_type": "ministral3", "num_hidden_layers": 16}))
    (model / "tokenizer.json").write_text(json.dumps({"test_fixture_only": True}))
    (model / "tokenizer_config.json").write_text("{}")
    # Deliberately not real weights. Planning must hash rather than load them.
    (model / "model.safetensors").write_bytes(b"inert-test-data-not-model-weights")
    return data, model


def cli_args(local_inputs, out: Path, *extras: str) -> list[str]:
    data, model = local_inputs
    return ["--data-dir", str(data), "--model-dir", str(model), "--out-dir", str(out), *extras]


def args(launcher, local_inputs, out: Path, *extras: str):
    return launcher.build_parser().parse_args(cli_args(local_inputs, out, *extras))


def adapter_bytes(training, hidden_layers=16, *, dtype="F32", value=0.25):
    """Tiny real-format LoRA matrices; no native runtime or model is loaded."""
    rank = training["lora_parameters"]["rank"]
    header, payload = {"__metadata__": None}, bytearray()
    for layer in range(hidden_layers - training["num_layers"], hidden_layers):
        for key in training["lora_parameters"]["keys"]:
            for factor, shape in (("lora_a", [2, rank]), ("lora_b", [rank, 2])):
                fmt = {"F16": "e", "BF16": "H", "F32": "f", "F64": "d"}[dtype]
                scalar = struct.pack("<" + fmt, value)
                data = scalar * math.prod(shape)
                header[f"language_model.model.layers.{layer}.{key}.{factor}"] = {
                    "dtype": dtype, "shape": shape, "data_offsets": [len(payload), len(payload) + len(data)]}
                payload.extend(data)
    encoded = json.dumps(header).encode()
    return len(encoded).to_bytes(8, 'little') + encoded + payload


def completed_parent(launcher, local_inputs, path: Path) -> dict:
    """Simulate wrapper metadata only; no training is performed or claimed."""
    plan = launcher.prepare_plan(args(launcher, local_inputs, path))
    adapters = path / "adapters"
    adapters.mkdir(parents=True)
    (adapters / "adapters.safetensors").write_bytes(adapter_bytes(plan["training"]))
    (adapters / "adapter_config.json").write_text(json.dumps(plan["training"]), encoding="utf-8")
    artifact_names = ["adapters/adapters.safetensors", "adapters/adapter_config.json"]
    record = {
        "schema_version": "guest-lora-run-v1", "status": "completed", "plan": plan,
        "runtime": {"test_fixture_only": True}, "returncode": 0,
        "artifacts": {name: digest(path / name) for name in artifact_names},
    }
    (path / "run.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def test_default_plan_never_loads_model_starts_process_or_writes_output(
        launcher, local_inputs, tmp_path, monkeypatch, capsys):
    out = tmp_path / "planned-run"
    execute = Mock(side_effect=AssertionError("Default planning must not execute"))
    monkeypatch.setattr(launcher, "execute_plan", execute)
    process = Mock(side_effect=AssertionError("Default planning must not start subprocesses"))
    monkeypatch.setattr("subprocess.run", process)
    monkeypatch.setattr("subprocess.Popen", process)
    original_import = builtins.__import__

    def reject_model_runtime(name, *positional, **keywords):
        if name == "mlx" or name.startswith(("mlx.", "mlx_lm")):
            raise AssertionError("Default planning must not import a model runtime")
        return original_import(name, *positional, **keywords)

    monkeypatch.setattr(builtins, "__import__", reject_model_runtime)
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}

    assert launcher.main(cli_args(local_inputs, out)) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "planned"
    assert report["resume"] is None
    execute.assert_not_called()
    process.assert_not_called()
    assert not out.exists()
    after = {str(path.relative_to(tmp_path)): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("split", ["train", "valid", "test"])
def test_tampered_split_cannot_be_planned(launcher, local_inputs, tmp_path, split):
    data, _ = local_inputs
    with (data / f"{split}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{}\n')
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "run"))
    assert not (tmp_path / "run").exists()


def test_wrong_manifest_schema_cannot_be_planned(launcher, local_inputs, tmp_path):
    data, _ = local_inputs
    path = data / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["schema_version"] = "different-unverified-exporter"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "run"))


def test_manifest_cannot_omit_parser_source_identity(launcher, local_inputs, tmp_path):
    data, _ = local_inputs
    path = data / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["code"]["files"].pop("ingestion/guest_parser.py")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "run"))


def test_unavailable_remote_model_id_is_not_treated_as_a_download_request(launcher, local_inputs, tmp_path):
    selected = args(launcher, local_inputs, tmp_path / "run")
    selected.model_dir = Path("unavailable-provider/model-not-installed")
    with pytest.raises((ValueError, FileNotFoundError)):
        launcher.prepare_plan(selected)


def test_remote_code_configuration_is_rejected(launcher, local_inputs, tmp_path):
    _, model = local_inputs
    path = model / "config.json"
    config = json.loads(path.read_text())
    config["auto_map"] = {"AutoModelForCausalLM": "untrusted_remote.CustomModel"}
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "run"))


def test_existing_run_directory_is_never_overwritten(launcher, local_inputs, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "checkpoint.txt").write_bytes(b"keep-existing-run")
    with pytest.raises((ValueError, FileExistsError)):
        launcher.prepare_plan(args(launcher, local_inputs, out))
    assert (out / "checkpoint.txt").read_bytes() == b"keep-existing-run"
    assert {path.name for path in out.iterdir()} == {"checkpoint.txt"}


def test_complete_matching_resume_is_planned_in_a_new_directory(launcher, local_inputs, tmp_path):
    prior = tmp_path / "prior-run"
    completed_parent(launcher, local_inputs, prior)
    before = {str(path.relative_to(prior)): path.read_bytes()
              for path in prior.rglob("*") if path.is_file()}
    out = tmp_path / "continued-run"

    plan = launcher.prepare_plan(args(launcher, local_inputs, out, "--resume-run", str(prior)))

    assert plan["status"] == "planned"
    assert plan["resume"] is not None
    assert not out.exists()
    assert before == {str(path.relative_to(prior)): path.read_bytes()
                      for path in prior.rglob("*") if path.is_file()}


@pytest.mark.parametrize("corruption", ["empty", "garbage", "empty-tensors", "truncated", "nan", "infinity", "missing-factor", "bad-offset", "wrong-rank"])
def test_self_consistent_invalid_checkpoint_hash_is_not_resume_evidence(launcher, local_inputs, tmp_path, corruption):
    prior = tmp_path / "prior"
    record = completed_parent(launcher, local_inputs, prior)
    path = prior / "adapters/adapters.safetensors"
    training = record["plan"]["training"]
    content = adapter_bytes(training)
    if corruption == "empty": content = b""
    elif corruption == "garbage": content = b"not weights"
    elif corruption == "truncated": content = content[:-1]
    elif corruption in {"nan", "infinity"}:
        content = adapter_bytes(training, value=float('nan') if corruption == "nan" else float('inf'))
    else:
        size = int.from_bytes(content[:8], 'little')
        header = json.loads(content[8:8+size]); payload = content[8+size:]
        tensor = next(key for key in header if key != '__metadata__')
        if corruption == "empty-tensors": header = {}; payload = b""
        elif corruption == "missing-factor": del header[tensor]
        elif corruption == "bad-offset": header[tensor]['data_offsets'][0] = 1
        elif corruption == "wrong-rank": header[tensor]['shape'][1] += 1
        encoded = json.dumps(header).encode(); content = len(encoded).to_bytes(8, 'little') + encoded + payload
    path.write_bytes(content)
    record['artifacts']['adapters/adapters.safetensors'] = digest(path)
    (prior / 'run.json').write_text(json.dumps(record))
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / 'next', '--resume-run', str(prior)))
    assert not (tmp_path / 'next').exists()


@pytest.mark.parametrize('dtype,value', [('F32', .25), ('F64', .25), ('F16', .25), ('BF16', 0x3F80)])
def test_dependency_free_check_accepts_finite_native_mlx_style_factors(launcher, tmp_path, dtype, value):
    training = {'num_layers': 2, 'lora_parameters': {'rank': 2, 'keys': ['self_attn.q_proj', 'self_attn.v_proj']}}
    path = tmp_path / 'adapter.safetensors'
    path.write_bytes(adapter_bytes(training, 4, dtype=dtype, value=value))
    report = launcher.validate_adapter_file(path, training, 4)
    assert report['tensors'] == 8 and report['native_load_verified'] is False
    assert report['sha256'] == digest(path)


def test_dependency_free_check_rejects_bfloat_nan(launcher, tmp_path):
    training = {'num_layers': 1, 'lora_parameters': {'rank': 2, 'keys': ['self_attn.q_proj', 'self_attn.v_proj']}}
    path = tmp_path / 'adapter.safetensors'
    path.write_bytes(adapter_bytes(training, 4, dtype='BF16', value=0x7FC0))
    with pytest.raises(ValueError, match='NaN or infinite'):
        launcher.validate_adapter_file(path, training, 4)


def test_zero_exit_empty_adapter_is_failed_and_preserved(launcher, local_inputs, tmp_path, monkeypatch):
    selected = args(launcher, local_inputs, tmp_path / 'training', '--execute')
    plan = launcher.prepare_plan(selected)
    monkeypatch.setattr(launcher.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(launcher.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(launcher, '_exclusive_run', lambda: nullcontext())
    monkeypatch.setattr(launcher, '_check_model_processes', lambda: None)
    monkeypatch.setattr(launcher, '_runtime_preflight', lambda plan: {'test_fixture_only': True})
    def finished_trainer(*_args, **kwargs):
        adapter = Path(plan['out_dir']) / 'adapters'; adapter.mkdir()
        (adapter / 'adapters.safetensors').write_bytes(b'')
        (adapter / 'adapter_config.json').write_text(json.dumps(plan['training']))
        kwargs['stdout'].write('simulated trainer exit 0, no valid checkpoint\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(launcher.subprocess, 'run', finished_trainer)
    with pytest.raises(ValueError, match='safetensors header'):
        launcher.execute_plan(selected, plan)
    record = json.loads((Path(plan['out_dir']) / 'run.json').read_text())
    assert record['status'] == 'failed' and record['returncode'] == 0
    assert record['error_type'] == 'ValueError'
    assert 'adapters/adapters.safetensors' in record['artifacts']
    assert (Path(plan['out_dir']) / 'adapters/adapters.safetensors').read_bytes() == b''


@pytest.mark.parametrize('native_failure', [False, True])
def test_completion_requires_native_check_after_trainer_exits(launcher, local_inputs, tmp_path, monkeypatch, native_failure):
    selected = args(launcher, local_inputs, tmp_path / 'training', '--execute')
    plan = launcher.prepare_plan(selected)
    monkeypatch.setattr(launcher.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(launcher.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(launcher, '_exclusive_run', lambda: nullcontext())
    monkeypatch.setattr(launcher, '_check_model_processes', lambda: None)
    monkeypatch.setattr(launcher, '_runtime_preflight', lambda plan: {'test_fixture_only': True})
    timeline = []
    def finished_trainer(*_args, **_kwargs):
        adapter = Path(plan['out_dir']) / 'adapters'; adapter.mkdir()
        (adapter / 'adapters.safetensors').write_bytes(adapter_bytes(plan['training']))
        (adapter / 'adapter_config.json').write_text(json.dumps(plan['training']))
        timeline.append('trainer_exited')
        return SimpleNamespace(returncode=0)
    def native_check(actual_plan, directory):
        assert timeline == ['trainer_exited']
        timeline.append('native_validation')
        if native_failure: raise ValueError('injected native loading failure')
        return {'status': 'passed', 'native_load_verified': True, 'quality_verified': False}
    monkeypatch.setattr(launcher.subprocess, 'run', finished_trainer)
    monkeypatch.setattr(launcher, 'validate_adapter_runtime', native_check)
    if native_failure:
        with pytest.raises(ValueError, match='native loading failure'): launcher.execute_plan(selected, plan)
    else:
        launcher.execute_plan(selected, plan)
    record = json.loads((Path(plan['out_dir']) / 'run.json').read_text())
    assert timeline == ['trainer_exited', 'native_validation']
    assert record['status'] == ('failed' if native_failure else 'completed')
    if not native_failure: assert record['adapter_validation']['native_load_verified'] is True


@pytest.mark.parametrize('native_error', [None, 'bad-shape', 'non-finite'])
def test_native_completion_loads_exact_base_and_checks_all_factor_shapes(launcher, tmp_path, monkeypatch, native_error):
    training = {'num_layers': 1, 'lora_parameters': {'rank': 2, 'keys': ['self_attn.q_proj', 'self_attn.v_proj']}}
    path = tmp_path / 'adapters.safetensors'; content = adapter_bytes(training, 2); path.write_bytes(content)
    size = int.from_bytes(content[:8], 'little'); header = json.loads(content[8:8+size])
    weights = {k: SimpleNamespace(shape=tuple(v['shape']), finite=native_error != 'non-finite')
               for k, v in header.items() if k != '__metadata__'}
    params = dict(weights)
    if native_error == 'bad-shape':
        first = next(iter(params)); params[first] = SimpleNamespace(shape=(99, 99))
    modules = {name: ModuleType(name) for name in ('mlx', 'mlx.core', 'mlx.utils', 'mlx_lm', 'mlx_lm.utils')}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
        if '.' in name:
            parent, attribute = name.rsplit('.', 1); setattr(modules[parent], attribute, module)
    mx = modules['mlx.core']
    mx.load = lambda p: weights
    mx.isfinite = lambda value: value.finite
    mx.all = lambda value: SimpleNamespace(item=lambda: value)
    mx.eval = lambda value: None
    mx.clear_cache = lambda: None
    modules['mlx.utils'].tree_flatten = lambda mapping: list(mapping.items())
    load_calls = []
    def native_load(model, **kwargs):
        load_calls.append((model, kwargs))
        return SimpleNamespace(parameters=lambda: params), object()
    modules['mlx_lm.utils'].load = native_load
    plan = {'training': training, 'model': {'num_hidden_layers': 2, 'path': '/fixture/exact-base'}}
    if native_error:
        with pytest.raises(ValueError): launcher.validate_adapter_runtime(plan, tmp_path)
    else:
        checked = launcher.validate_adapter_runtime(plan, tmp_path)
        assert checked['native_load_verified'] is True and checked['quality_verified'] is False
    if native_error != 'non-finite':
        assert load_calls[0][0] == '/fixture/exact-base'
        assert load_calls[0][1] == {'adapter_path': str(tmp_path), 'lazy': True,
            'trust_remote_code': False, 'tokenizer_config': {'local_files_only': True, 'trust_remote_code': False}}


def test_resume_rejects_base_weights_changed_at_the_same_path(launcher, local_inputs, tmp_path):
    prior = tmp_path / "prior-run"
    completed_parent(launcher, local_inputs, prior)
    _, model = local_inputs
    (model / "model.safetensors").write_bytes(b"different-inert-base-model-weights")
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "next", "--resume-run", str(prior)))


def test_resume_rejects_different_reviewed_data(launcher, local_inputs, tmp_path):
    prior = tmp_path / "prior-run"
    completed_parent(launcher, local_inputs, prior)
    new_data = make_data(tmp_path / "different-dataset", suffix="changed-")
    _, model = local_inputs
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, (new_data, model), tmp_path / "next", "--resume-run", str(prior)))


def test_resume_rejects_incompatible_lora_rank(launcher, local_inputs, tmp_path):
    prior = tmp_path / "prior-run"
    completed_parent(launcher, local_inputs, prior)
    with pytest.raises(ValueError):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "next",
                                   "--resume-run", str(prior), "--rank", "4"))


@pytest.mark.parametrize("corruption", ["unfinished", "missing-adapter", "tampered-adapter"])
def test_resume_requires_completed_and_intact_parent_artifacts(launcher, local_inputs, tmp_path, corruption):
    prior = tmp_path / "prior-run"
    record = completed_parent(launcher, local_inputs, prior)
    if corruption == "unfinished":
        record["status"] = "running"
        (prior / "run.json").write_text(json.dumps(record), encoding="utf-8")
    elif corruption == "missing-adapter":
        (prior / "adapters/adapters.safetensors").unlink()
    else:
        (prior / "adapters/adapters.safetensors").write_bytes(b"modified-after-completion")
    with pytest.raises((ValueError, FileNotFoundError)):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / "next", "--resume-run", str(prior)))


def test_model_changed_during_preflight_cannot_reach_trainer(launcher, local_inputs, tmp_path, monkeypatch):
    selected = args(launcher, local_inputs, tmp_path / "run", "--execute")
    plan = launcher.prepare_plan(selected)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(launcher, "_exclusive_run", lambda: nullcontext())
    monkeypatch.setattr(launcher, "_check_model_processes", lambda: None)

    def preflight_that_observes_an_external_replacement(_plan):
        (local_inputs[1] / "model.safetensors").write_bytes(b"base-changed-during-preflight")
        return {"test_fixture_only": True}

    monkeypatch.setattr(launcher, "_runtime_preflight", preflight_that_observes_an_external_replacement)
    trainer_calls = []

    def forbid_trainer(*_args, **_kwargs):
        # Never retain or print the child's environment, even on a regression.
        trainer_calls.append(True)
        raise ValueError("Trainer was reached despite changed inputs")

    monkeypatch.setattr("subprocess.run", forbid_trainer)
    with pytest.raises(ValueError):
        launcher.execute_plan(selected, plan)
    assert not trainer_calls, "Changed inputs reached the trainer"
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("tokens,offset,prefix,maximum", [
    ([1, 2, 3, 4], 2, [1, 2], 3),  # Full target would be truncated.
    ([1, 2, 3, 4], 0, [], 10),  # No independently masked prompt.
    ([1, 2, 3, 4], 3, [1, 2, 3], 10),  # No usable completion beyond terminal token.
    ([1, 2, 3, 4], 2, [1, 9], 10),  # Trainer's mask does not match its prompt prefix.
])
def test_token_preflight_rejects_truncation_and_invalid_completion_masks(
        launcher, tokens, offset, prefix, maximum):
    class Tokenizer:
        def apply_chat_template(self, *_args, **_kwargs):
            return prefix

    class Dataset:
        def __init__(self, *_args, **_kwargs):
            pass

        def process(self, _row):
            return tokens, offset

    with pytest.raises(ValueError):
        launcher.validate_token_lengths(Tokenizer(), Dataset, [{"messages": [{}, {}, {}]}], maximum)


def test_token_preflight_accepts_an_intact_masked_completion(launcher):
    class Tokenizer:
        def apply_chat_template(self, *_args, **_kwargs):
            return [1, 2]

    class Dataset:
        def __init__(self, *_args, **_kwargs):
            pass

        def process(self, _row):
            return [1, 2, 3, 4], 2

    report = launcher.validate_token_lengths(Tokenizer(), Dataset, [{"messages": [{}, {}, {}]}], 4)
    assert report["rows"] == 1
    assert report["max_tokens"] == 4
    assert report["min_target_tokens"] == 2


class _Scalar:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class _Array:
    def __init__(self, values=None, *, shape=None):
        self.values = values
        self.shape = tuple(shape) if shape is not None else (len(values), len(values[0]))


class _NumericBackend:
    """Only the shape/scalar operations needed by the no-weights probe."""

    float32 = "float32"
    int32 = "int32"

    @staticmethod
    def array(values, dtype=None):
        return _Array(values)

    @staticmethod
    def zeros(shape, dtype=None):
        return _Array(shape=shape)

    @staticmethod
    def eval(*values):
        pass


def _counting_trainer(*, inclusive=False, ragged_only=False, forced_loss=None):
    """Model the documented shifted-target positions, including the released bug."""
    calls = []

    def default_loss(uniform_logits, batch, lengths):
        calls.append(batch.shape)
        logits = uniform_logits(_Array([row[:-1] for row in batch.values]))
        use_inclusive = inclusive and (not ragged_only or len(batch.values) > 1)
        count = 0
        for row, (prompt, retained) in zip(batch.values, lengths.values):
            for position in range(1, len(row)):
                in_content = position <= retained if use_inclusive else position < retained
                count += position >= prompt and in_content
        loss = math.log(logits.shape[-1]) if forced_loss is None else forced_loss
        return _Scalar(loss), _Scalar(count)

    return SimpleNamespace(default_loss=default_loss, calls=calls)


def test_loss_mask_probe_accepts_real_targets_for_masked_unmasked_and_ragged_rows(launcher):
    trainer = _counting_trainer()
    report = launcher.validate_loss_mask(_NumericBackend, trainer)
    assert report["status"] == "passed" and report["gradient_updates"] == 0
    assert {name: case["observed_tokens"] for name, case in report["cases"].items()} == {
        "masked_padded": 2, "unmasked_padded": 4, "unpadded": 5, "ragged": 7,
    }
    assert len(trainer.calls) == 4


def test_loss_mask_probe_rejects_released_inclusive_upper_bound(launcher):
    trainer = _counting_trainer(inclusive=True)
    with pytest.raises(ValueError, match=r"loss-mask check failed.*expected 2 supervised tokens, got 3"):
        launcher.validate_loss_mask(_NumericBackend, trainer)


def test_loss_mask_probe_also_checks_ragged_batch_padding(launcher):
    trainer = _counting_trainer(inclusive=True, ragged_only=True)
    with pytest.raises(ValueError, match=r"loss-mask check failed.*ragged.*expected 7 supervised tokens, got 9"):
        launcher.validate_loss_mask(_NumericBackend, trainer)


@pytest.mark.parametrize("bad_loss", [float("nan"), float("inf")])
def test_correct_token_count_does_not_hide_a_nonfinite_loss(launcher, bad_loss):
    with pytest.raises(ValueError, match="loss-mask check failed"):
        launcher.validate_loss_mask(_NumericBackend, _counting_trainer(forced_loss=bad_loss))


def test_runtime_rejects_padding_loss_before_loading_model_weights(launcher, monkeypatch):
    """Use package stubs to verify ordering; no installed MLX is imported."""
    modules = {name: ModuleType(name) for name in (
        "mlx", "mlx.core", "mlx_lm", "mlx_lm.lora", "mlx_lm.tuner",
        "mlx_lm.tuner.datasets", "mlx_lm.tuner.trainer", "mlx_lm.utils",
    )}
    for name in ("mlx", "mlx_lm", "mlx_lm.tuner"):
        modules[name].__path__ = []
    for name, module in modules.items():
        if "." in name:
            parent, attribute = name.rsplit(".", 1)
            setattr(modules[parent], attribute, module)
        monkeypatch.setitem(sys.modules, name, module)
    mx = modules["mlx.core"]
    for name in ("float32", "int32", "array", "zeros", "eval"):
        setattr(mx, name, getattr(_NumericBackend, name))
    mx.gpu = "synthetic-device"
    mx.metal = SimpleNamespace(is_available=lambda: True)
    mx.default_device = lambda: mx.gpu
    modules["mlx_lm.lora"].CONFIG_DEFAULTS = {
        "mask_prompt": True, "grad_accumulation_steps": 1,
        "lora_parameters": {}, "resume_adapter_file": None,
    }
    modules["mlx_lm.tuner.datasets"].ChatDataset = type("ChatDataset", (), {"process": lambda *_args: None})
    modules["mlx_lm.tuner.trainer"].default_loss = _counting_trainer(inclusive=True).default_loss
    load_calls = []

    def load(path, *, lazy, trust_remote_code, tokenizer_config):
        load_calls.append(True)
        raise AssertionError("Model loading must follow a successful objective check")

    modules["mlx_lm.utils"].load = load
    monkeypatch.setattr(launcher.importlib.metadata, "version", lambda _name: "synthetic-test-version")

    with pytest.raises(ValueError, match="loss-mask check failed"):
        launcher._runtime_preflight({"model": {"path": "inert-model-must-not-be-loaded"}})
    assert not load_calls
