"""Validation scheduling changes must not change the training/checkpoint budget."""
import pytest

from tests.training.test_train_guest_lora import launcher, local_inputs, args


def test_explicit_full_validation_preserves_update_and_checkpoint_counts(launcher, local_inputs, tmp_path):
    selected = args(launcher, local_inputs, tmp_path / 'run', '--iters', '200',
                    '--steps-per-eval', '200', '--val-batches', '48')
    config = launcher.prepare_plan(selected)['training']
    assert (config['iters'], config['steps_per_eval'], config['val_batches']) == (200, 200, 48)
    assert config['save_every'] == 50
    assert config['batch_size'] == config['grad_accumulation_steps'] == 1
    assert config['test'] is False
    assert not selected.out_dir.exists()


@pytest.mark.parametrize('steps', [1, 20, 100, 200])
def test_legacy_validation_defaults_are_unchanged(launcher, local_inputs, tmp_path, steps):
    config = launcher.prepare_plan(args(launcher, local_inputs, tmp_path / 'run', '--iters', str(steps)))['training']
    assert config['steps_per_eval'] == min(50, steps)
    assert config['val_batches'] == 25


@pytest.mark.parametrize('options', [
    ('--steps-per-eval', '0'), ('--steps-per-eval', '-1'), ('--steps-per-eval', '101'),
    ('--val-batches', '0'), ('--val-batches', '-1'), ('--val-batches', '10001'),
])
def test_invalid_validation_schedule_fails_before_model_identity(launcher, local_inputs, tmp_path, monkeypatch, options):
    def forbidden(*a, **k):
        raise AssertionError('Invalid scheduling must fail before reading model files')
    monkeypatch.setattr(launcher, '_model_identity', forbidden)
    with pytest.raises(ValueError, match='Validation cadence'):
        launcher.prepare_plan(args(launcher, local_inputs, tmp_path / 'run', *options))
