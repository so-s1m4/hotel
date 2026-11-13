"""Stage a complete output generation, then publish with best-effort rollback.

The filesystem has no multi-file atomic replace. A failed replacement restores
prior files where possible; failed restoration is explicitly reported as partial
publication. Handled Python interrupts also trigger rollback; this is not machine power-loss
atomicity.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable, Mapping


class OutputPublicationError(OSError):
    def __init__(self, cause, *, state, changed=(), rollback_errors=()):
        self.publication_state = state
        self.changed_paths = [str(path) for path in changed]
        self.rollback_errors = list(rollback_errors)
        detail = f"Output publication {state}: {cause}"
        if self.changed_paths:
            detail += "; possibly changed files: " + ", ".join(self.changed_paths)
        if self.rollback_errors:
            detail += "; rollback errors: " + "; ".join(self.rollback_errors)
        super().__init__(detail)


def preflight_outputs(destinations, data_dir, *, protected_inputs=()):
    """Reject invalid/aliased destinations before replay, without replacing files."""
    paths = [Path(path) for path in destinations]
    try:
        resolved = [path.resolve() for path in paths]
        source = Path(data_dir).resolve()
        protected = [Path(path).resolve() for path in protected_inputs]
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot resolve output paths: {exc}") from exc
    if len(set(resolved)) != len(paths):
        raise ValueError("Output paths must be distinct (including aliases)")
    for i, path in enumerate(resolved):
        if path.is_relative_to(source) or path in protected:
            raise ValueError(f"Write outputs outside source inputs: {paths[i]}")
        for other in resolved[i+1:]:
            if path in other.parents or other in path.parents:
                raise ValueError("An output file cannot be another output's parent directory")
    # Existing hard links can alias despite different resolved path strings.
    existing = [path for path in paths if path.exists()]
    for i,path in enumerate(existing):
        if not path.is_file():
            raise ValueError(f"Output destination is not a regular file: {path}")
        if any(path.samefile(other) for other in existing[i+1:]):
            raise ValueError("Output files must not alias the same existing file")
    source_files = [p for p in source.rglob('*') if p.is_file()] if existing else []
    source_files += [p for p in protected if p.is_file()]
    for path in existing:
        if any(path.samefile(input_path) for input_path in source_files):
            raise ValueError(f"Output aliases a source input: {path}")
    for path in paths:
        for parent in path.parents:
            if parent.exists() and not parent.is_dir():
                raise ValueError(f"Output parent is not a directory: {parent}")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Opening a sibling proves the actual filesystem is writable. No
        # destination is opened/truncated during preflight.
        descriptor, name = tempfile.mkstemp(prefix='.hotel-preflight-',dir=path.parent)
        os.close(descriptor)
        Path(name).unlink()


def _temporary(destination, phase):
    descriptor, name = tempfile.mkstemp(prefix=f'.hotel-{phase}-{destination.name}-',dir=destination.parent)
    os.close(descriptor)
    return Path(name)


def publish_artifacts(writers: Mapping[Path, Callable[[Path], object]]):
    """Prepare every artifact and prior-file backup before replacing any output.

    Writers receive temporary sibling paths and must finish serialization there.
    Call preflight_outputs before replay; this function also rejects destinations
    that became directories before publication.
    """
    staged = {}
    backups = {}
    attempted = []
    temporary = []
    recovery_backups = set()
    publication_succeeded = False
    failure = None
    try:
        for raw, writer in writers.items():
            destination = Path(raw)
            if destination.exists() and not destination.is_file():
                raise ValueError(f"Output destination became non-file: {destination}")
            stage = _temporary(destination,'stage')
            temporary.append(stage)
            writer(stage)
            with stage.open('rb') as stream:
                os.fsync(stream.fileno())
            staged[destination] = stage
        for destination in staged:
            if destination.is_symlink():
                backup = _temporary(destination,'backup')
                temporary.append(backup)
                backup.unlink()
                backup.symlink_to(os.readlink(destination))
            elif destination.exists():
                backup = _temporary(destination,'backup')
                temporary.append(backup)
                shutil.copy2(destination,backup)
            else:
                backup = None
            backups[destination] = backup
            if backup is not None:
                recovery_backups.add(backup)
        for destination, stage in staged.items():
            # Register before the syscall: an interrupt can arrive after the OS
            # replaced the file but before Python records its successful return.
            attempted.append(destination)
            os.replace(stage,destination)
        publication_succeeded = True
    except BaseException as exc:
        publication_succeeded = False
        failure = exc
        rollback_errors = []
        changed = []
        rollback_interrupt = None
        restoration_attempted = False
        for destination in reversed(attempted):
            backup = backups[destination]
            try:
                # Atomic replacement consumes its source. A surviving stage
                # proves this attempt did not replace the destination, so do
                # not rewrite a still-original file just because it was tried.
                if staged[destination].exists():
                    recovery_backups.discard(backup)
                    continue
                restoration_attempted = True
                if backup is None:
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(backup,destination)
            except BaseException as rollback_error:
                changed.append(destination)
                rollback_errors.append(f'{destination}: {type(rollback_error).__name__}: {rollback_error}; recovery backup: {backup}')
                if not isinstance(rollback_error, Exception) and rollback_interrupt is None:
                    rollback_interrupt = rollback_error
            else:
                recovery_backups.discard(backup)
        # Backups for destinations never attempted are unnecessary. Failed
        # restores stay protected, even if restoration itself was interrupted.
        for destination, backup in backups.items():
            if destination not in changed:
                recovery_backups.discard(backup)
        state = 'partial_publication' if changed else 'rolled_back' if restoration_attempted else 'not_published'
        metadata = {
            'publication_state': state,
            'changed_paths': [str(path) for path in changed],
            'rollback_errors': rollback_errors,
            'recovery_backups': [str(path) for path in sorted(recovery_backups)],
        }
        # A cancellation raised during rollback also remains a cancellation.
        # If publication was already interrupted, retain that original object.
        propagate = exc if not isinstance(exc, Exception) or rollback_interrupt is None else rollback_interrupt
        for key, value in metadata.items():
            setattr(propagate, key, value)
        propagate.add_note(f'Output publication {state}; possibly changed files: {metadata["changed_paths"]}; '
                           f'recovery backups: {metadata["recovery_backups"]}; rollback errors: {rollback_errors}')
        if propagate is not exc:
            failure = propagate
            raise propagate from exc
        if isinstance(exc, (OSError, ValueError, TypeError)):
            wrapped = OutputPublicationError(exc,state=state,changed=changed,rollback_errors=rollback_errors)
            wrapped.recovery_backups = metadata['recovery_backups']
            failure = wrapped
            raise wrapped from exc
        raise
    finally:
        for path in temporary:
            if not publication_succeeded and path in recovery_backups:
                continue
            try:
                path.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                if failure is not None:
                    failure.add_note(f'Temporary cleanup did not finish for {path}: {type(cleanup_error).__name__}')
                elif not isinstance(cleanup_error, OSError):
                    # Every output has been published at this point. Preserve
                    # an interrupt during cleanup rather than report success.
                    cleanup_error.add_note('Output generation was fully published before temporary cleanup was interrupted.')
                    raise
