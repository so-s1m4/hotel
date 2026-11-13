"""Stage a complete output generation, then publish with best-effort rollback.

The filesystem has no multi-file atomic replace. A failed replacement restores
prior files where possible; failed restoration is explicitly reported as partial
publication. This protects ordinary write failures, not machine power loss.
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
    published = []
    temporary = []
    recovery_backups = set()
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
        for destination, stage in staged.items():
            os.replace(stage,destination)
            published.append(destination)
    except (OSError, ValueError, TypeError) as exc:
        rollback_errors = []
        changed = []
        for destination in reversed(published):
            try:
                backup = backups[destination]
                if backup is None:
                    destination.unlink()
                else:
                    os.replace(backup,destination)
            except OSError as rollback_error:
                changed.append(destination)
                backup = backups[destination]
                if backup is not None:
                    recovery_backups.add(backup)
                rollback_errors.append(f'{destination}: {rollback_error}; recovery backup: {backup}')
        state = 'partial_publication' if changed else 'rolled_back' if published else 'not_published'
        raise OutputPublicationError(exc,state=state,changed=changed,rollback_errors=rollback_errors) from exc
    finally:
        for path in temporary:
            if path in recovery_backups:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Cleanup cannot turn a completed generation into a failed run.
                # Publication/rollback failures themselves are reported above.
                pass
