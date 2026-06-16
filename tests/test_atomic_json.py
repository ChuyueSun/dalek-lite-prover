"""Pin the Phase 0 concurrency invariant: locked_update must not lose updates
under real process-level contention, and atomic_write_json must never leave a
torn file behind.

Run: python3 -m unittest tests.test_atomic_json
"""
import json
import multiprocessing as mp
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import atomic_json  # noqa: E402


def _append_worker(path_str: str, value: int) -> None:
    """Module-level so it survives the spawn start method (macOS default)."""
    with atomic_json.locked_update(Path(path_str), {"items": []}) as data:
        # A tiny gap between read and write widens the race window an unlocked
        # implementation would lose to; the lock must serialize this.
        items = data.setdefault("items", [])
        snapshot = list(items)
        snapshot.append(value)
        data["items"] = snapshot


class TestAtomicWrite(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            atomic_json.atomic_write_json(p, {"a": 1, "b": [2, 3]})
            self.assertEqual(json.loads(p.read_text()), {"a": 1, "b": [2, 3]})

    def test_no_temp_files_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            atomic_json.atomic_write_json(p, {"a": 1})
            leftovers = [f.name for f in Path(d).iterdir() if ".tmp" in f.name]
            self.assertEqual(leftovers, [])


class TestLockedUpdate(unittest.TestCase):
    def test_missing_file_uses_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            with atomic_json.locked_update(p, {"items": []}) as data:
                data["items"].append(1)
            self.assertEqual(json.loads(p.read_text()), {"items": [1]})

    def test_corrupt_file_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            p.write_text("{ not json")
            with atomic_json.locked_update(p, {"items": []}) as data:
                data["items"].append(42)
            self.assertEqual(json.loads(p.read_text()), {"items": [42]})

    def test_default_not_mutated_across_calls(self):
        # deepcopy guard: a shared default dict must not accumulate state.
        with tempfile.TemporaryDirectory() as d:
            default = {"items": []}
            with atomic_json.locked_update(Path(d) / "a.json", default) as data:
                data["items"].append(1)
            self.assertEqual(default, {"items": []})

    def test_concurrent_appends_lose_nothing(self):
        n = 40
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shared.json"
            atomic_json.atomic_write_json(p, {"items": []})
            ctx = mp.get_context("spawn")
            procs = [ctx.Process(target=_append_worker, args=(str(p), i))
                     for i in range(n)]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=60)
            items = json.loads(p.read_text())["items"]
            # Every worker's value must be present exactly once. An unlocked
            # read-modify-write would drop some here.
            self.assertEqual(sorted(items), list(range(n)))


if __name__ == "__main__":
    unittest.main()
