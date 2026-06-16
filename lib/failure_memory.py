"""Persistent per-function failure memory.

Keyed by `(module, function)`. Each entry records what the agent tried
in prior runs so the next attempt can see "here's what failed before —
don't repeat."

Kept deliberately shallow: one JSON file. If you need richer query /
slicing, upgrade to SQLite as an extension.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from lib import atomic_json


@dataclass
class FailureRecord:
    timestamp: str                  # ISO-8601
    run_id: str
    module: str                     # e.g. `curve25519_dalek::field::field_specs`
    function: str                   # target fn name
    rounds_used: int                # how many rounds before giving up
    final_error: str                # truncated Verus stderr (first ~2000 chars)
    tried_strategies: list[str]     # free-form notes the agent wrote into result.json
    end_reason: str                 # LIMIT | ERROR | STATEMENT_CHANGED | COMPLETE
    # --- Feature 1: near-miss memory. Defaults keep old records loadable. ---
    failed_decls: list[str] = field(default_factory=list)  # decls verus rejected last
    near_miss: str = ""             # the agent's final proof source for those decls


def path_for(results_root: Path) -> Path:
    return results_root / "failure_memory.json"


def load(results_root: Path) -> list[FailureRecord]:
    p = path_for(results_root)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    return [FailureRecord(**r) for r in raw.get("records", [])]


def record(
    results_root: Path,
    run_id: str,
    module: str,
    function: str,
    rounds_used: int,
    final_error: str,
    tried_strategies: Optional[list[str]] = None,
    end_reason: str = "LIMIT",
    failed_decls: Optional[list[str]] = None,
    near_miss: str = "",
) -> None:
    """Append one failure record. Truncate the error blob to keep the file small.

    The load -> append -> write happens under an exclusive lock (the read is
    inside the lock) so concurrent failures don't clobber each other's records.
    """
    rec = FailureRecord(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        run_id=run_id,
        module=module,
        function=function,
        rounds_used=rounds_used,
        final_error=final_error[:2000],
        tried_strategies=tried_strategies or [],
        end_reason=end_reason,
        failed_decls=failed_decls or [],
        near_miss=near_miss[:4000],
    )
    with atomic_json.locked_update(
        path_for(results_root), {"records": []}, ensure_ascii=False,
    ) as data:
        data.setdefault("records", []).append(asdict(rec))


def query(results_root: Path, module: str, function: str) -> list[FailureRecord]:
    """Return prior failures for (module, function), most recent first."""
    records = load(results_root)
    matches = [r for r in records if r.module == module and r.function == function]
    matches.sort(key=lambda r: r.timestamp, reverse=True)
    return matches


def as_prompt_block(records: list[FailureRecord], max_entries: int = 3) -> str:
    """Render the most recent failures as a markdown block the prompt can include.

    The caller's template owns the `## Prior failed attempts` heading; this
    function returns only the body (per-entry subsections).
    """
    if not records:
        return ""
    lines: list[str] = []
    for r in records[:max_entries]:
        lines.append(f"### Attempt on {r.timestamp} (run {r.run_id}, {r.rounds_used} rounds)")
        lines.append(f"- **End reason**: {r.end_reason}")
        if r.tried_strategies:
            lines.append(f"- **Tried strategies**: {'; '.join(r.tried_strategies)}")
        if r.failed_decls:
            lines.append(f"- **Verus rejected these declaration(s)**: "
                         f"{', '.join(r.failed_decls[:8])}")
        lines.append(f"- **Final Verus error (truncated)**:")
        lines.append("```")
        lines.append(r.final_error[:800])
        lines.append("```")
        if r.near_miss.strip():
            lines.append("- **Your previous proof attempt for the rejected "
                         "declaration(s)** (this is the exact code that did NOT "
                         "verify — improve it, do not rewrite from scratch):")
            lines.append("```rust")
            lines.append(r.near_miss.strip())
            lines.append("```")
        lines.append("")
    lines.append("Do not repeat the strategies above. Start from the near-miss "
                 "code shown and fix the specific obligation Verus rejected. If "
                 "the prior error shows a missing lemma / symbol, search the "
                 "catalog before assuming it doesn't exist.")
    blob = "\n".join(r.final_error for r in records[:max_entries]).lower()
    if "rlimit" in blob or "assert_nonlinear_by" in blob or "timed out" in blob:
        lines.append(
            "\n**Hint (rlimit or whole-file timeout on nonlinear arith):** "
            "this codebase's canonical proofs delegate nonlinear work to "
            "pre-proven helpers, not `broadcast use` or `nonlinear_arith` "
            "blocks. Run `search_module crate::lemmas::common_lemmas::mul_lemmas`; "
            "`lemma_mul_distributive_{3..8}_terms` and `lemma_mul_is_distributive_add` "
            "(the workhorse) are the key entries. Avoid over-calling "
            "`lemma_mul_is_commutative` — Z3 derives that from distributivity, "
            "and redundant calls bloat the proof toward the 300s wall-clock cap."
        )
    return "\n".join(lines)


# ---------------- CLI ----------------

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="Inspect persistent failure memory")
    ap.add_argument("results_root")
    ap.add_argument("--module", help="filter by module")
    ap.add_argument("--function", help="filter by function")
    ap.add_argument("--as-prompt", action="store_true",
                    help="Render as markdown block")
    args = ap.parse_args()

    root = Path(args.results_root)
    records = load(root)
    if args.module:
        records = [r for r in records if r.module == args.module]
    if args.function:
        records = [r for r in records if r.function == args.function]

    if args.as_prompt:
        print(as_prompt_block(records))
    else:
        for r in records:
            print(f"{r.timestamp} {r.module}::{r.function} — {r.end_reason} "
                  f"({r.rounds_used} rounds)")


if __name__ == "__main__":
    _main()
