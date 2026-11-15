"""Atomic, durable state persistence with a previous-version backup."""
import json
import os
import tempfile
from pathlib import Path

from .model import State, StateError


def save(state: State, path: str) -> None:
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix=target.name + '.', delete=False) as stream:
            temporary = stream.name
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state.to_dict(), stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(target, str(target) + '.bak')
        except FileNotFoundError:
            pass
        os.replace(temporary, target)
        temporary = None
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def load(path: str) -> State | None:
    for candidate in (path, path + '.bak'):
        try:
            with open(candidate, encoding='utf-8') as stream:
                return State.from_dict(json.load(stream))
        except (OSError, ValueError, StateError):
            continue
    return None
