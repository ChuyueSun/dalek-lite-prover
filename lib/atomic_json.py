"""Concurrency-safe JSON read-modify-write.

Phase 0 of the parallel-orchestration plan (docs/parallel_orchestration_design.md):
the shared aggregate files — failure_memory.json, proven_registry.json,
catalog_cache.json — were each mutated with a plain load -> append -> write_text.
Two processes interleaving that pattern silently lose one update, which is the
one true correctness bug under any concurrency (including the `xargs -P` fan-out
CLAUDE.md already suggests).

Two primitives, stdlib-only, POSIX (`fcntl.flock` — the repo runs on darwin/linux):

- `locked_update(path, default)` — hold an exclusive lock, hand the caller the
  parsed JSON (or `default` if absent/corrupt), then atomically write back. The
  read happens *inside* the lock, so the modify is serialized — no lost updates.
- `atomic_write_json(path, obj)` — write via temp file + `os.replace` (atomic on
  POSIX). For pure writes with no read-modify-write (e.g. the catalog cache),
  this just removes the torn-write window.

Contract for `locked_update`: mutate the yielded object **in place**
(`data["x"].append(...)`). Rebinding the name (`data = {...}`) is not written back.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically: temp file in the same dir, then
    `os.replace` (atomic rename on the same filesystem). A unique temp name
    keeps the no-lock `atomic_write_json` path safe under concurrency too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: Path, obj: Any, **json_kwargs: Any) -> None:
    """Serialize `obj` and write it to `path` atomically. `json_kwargs` are
    passed through to `json.dumps` (defaults to `indent=2`)."""
    json_kwargs.setdefault("indent", 2)
    _atomic_write_text(Path(path), json.dumps(obj, **json_kwargs))


@contextmanager
def locked_update(path: Path, default: Any, **json_kwargs: Any) -> Iterator[Any]:
    """Exclusive read-modify-write on a JSON file.

    Holds an `flock` on `<path>.lock` for the whole critical section, reads the
    current contents (a deep copy of `default` if missing or corrupt — matching
    the tolerant behaviour the old `load()` helpers had), yields the parsed
    object for in-place mutation, then atomically writes it back.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except json.JSONDecodeError:
                    data = copy.deepcopy(default)
            else:
                data = copy.deepcopy(default)
            yield data
            atomic_write_json(path, data, **json_kwargs)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
