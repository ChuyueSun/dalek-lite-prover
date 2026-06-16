# Dalek-Lite-MVP — Spec

## Context

A slim Verus proof-synthesis agent for the dalek-lite codebase. Small enough to read in one sitting, debug by stepping through one loop, extend by dropping a new skill file in.

Informed by 20+ experiments from a prior baseline and a reference agent architecture, but aggressively narrowed: one level of cascade, one persistent session, one coordinator (no subagents), one proof attempt at a time.

## Scope — what's in the MVP

| Area | Shape |
|---|---|
| **Loop** | `claude -p` session, max N rounds. After each round: run Verus, detect `END_REASON`, continue or stop. |
| **Search** (pain-point #1: the LLM didn't have the theorems) | 4 search skills sharing one canonical catalog — semantic, module, macro (incl. name-prefix validation), proven |
| **Verification** | `verus_check` — module-scoped `cargo verus ... --verify-module`, JSON output |
| **Integrity gate** | `spec_check` — before/after signature diff; fails the round if any `fn` signature / `requires` / `ensures` / `decreases` drifted |
| **Failure memory** | Per-function persistent JSON (`results/failure_memory.json`). Injected into the prompt on retry. |
| **Result layout** | Numina-style: `results/<run_id>/<target_id>/{result.json, round_N.json, claude_raw/round_N.jsonl, cli.log}` |
| **Prompt** | One file (`prompt.md`). Lists forbidden constructs (`external_body`, spec weakening, bare `admit()` outside L3). Sets `END_REASON:COMPLETE\|LIMIT`. |

## Scope — what's deferred

See `extension_spec.md`. Five features, each documented with the pain it addresses, the trigger that would justify building it, a design sketch, and integration points:

1. Multi-level cascade (L1 widen + L3 human admit)
2. Subagents / autoproof coordinator mode
3. CHECKLIST.md multi-module campaign state
4. Informal-prover skill (Gemini-backed NL proof refinement)
5. Full spec integrity tracker (auto-restore on drift)

## The loop

```
run.py target.rs [--rounds 5] [--run-id foo]
  ├─ snapshot signatures (spec_check --snapshot)
  ├─ round 1:
  │    ├─ claude -p --verbose --output-format stream-json prompt.md
  │    │    ├─ Claude edits target.rs
  │    │    └─ Claude calls skills/{verus_check, search_*} as needed
  │    ├─ save claude_raw/round_1.jsonl
  │    ├─ spec_check --verify  → gate: did agent weaken specs?
  │    ├─ verus_check target.rs → final verification
  │    └─ record round_1.json
  ├─ round 2: claude -c "continue" (reusing session state)
  │    ...
  └─ on END_REASON:COMPLETE or N rounds: write result.json, update failure_memory.json
```

## File inventory

```
dalek-lite-mvp/
├── run.py                    # ~250 LOC — the driver
├── prompt.md                 # the task prompt
├── README.md                 # how to use
├── docs/
│   ├── mvp_spec.md           # this file
│   └── extension_spec.md     # the 5 deferred features
├── lib/
│   ├── catalog.py            # ~200 LOC — canonical catalog builder
│   ├── failure_memory.py     # ~60 LOC — per-function persistent record
│   └── results.py            # ~80 LOC — result-dir helpers
└── skills/
    ├── verus_check.py        # ~100 LOC — cargo verus wrapper → JSON
    ├── spec_check.py         # ~120 LOC — snapshot / verify signature integrity
    ├── search_semantic.py    # ~60 LOC — keyword/substring over catalog
    ├── search_module.py      # ~60 LOC — pull all sigs from one module
    ├── search_macro.py       # ~90 LOC — static expansion of lemma_*! macros (incl. --name-prefix symbol check)
    └── search_proven.py      # ~40 LOC — ProvenRegistry reader
```

Target total: ~1100 LOC of Python across 10 code files. Small enough to fit in a single PR review.

## Success gate

Running the MVP against a simple dalek-lite module (e.g., one of the Layer 0 modules) should:
1. Drive `verus` to success in 1-3 rounds for easy targets (≤20 LOC proof bodies)
2. Refuse to commit on any spec drift (spec_check exit ≠ 0 → round fails)
3. Leave a readable, round-by-round trace in `results/<run_id>/`
4. Populate `failure_memory.json` with one entry per failed attempt, keyed by `(module, function)`

No claim about end-to-end benchmark performance yet — that comes after the MVP is stable enough to run the reference benchmark against.

## Non-goals (explicit)

- **No batch runner.** One target per invocation. Script over it with bash if you want a campaign.
- **No autosearch / multi-agent mode.** Single Claude session. Subagent spawning via Task tool is available to the model but not orchestrated by the harness.
- **No cascade levels.** If round N fails, round N+1 gets the error feedback + failure memory and tries again. No widening, no decomposition, no escalation.
- **No web UI / dashboard.** Results are JSON files — `jq` and `less` are the UI.
- **No cross-module campaign state.** Each run is independent; the only persistence is `failure_memory.json`.

## Why this shape

- **One file per skill** → easy to extend. Adding a 6th search skill is a new 60-LOC file, not a schema negotiation.
- **Skills are plain CLIs** → debuggable standalone. You can `python skills/search_semantic.py "lemma_pow2"` at the shell and see what the LLM will see.
- **State on disk, not in Python objects** → inspectable mid-run. `cat results/<run_id>/failure_memory.json` tells you everything the memory subsystem has.
- **`claude -p` over SDK** → no Anthropic/OpenAI dep in `pyproject.toml`. Claude Code handles caching, tool use, session continuation.
- **No custom orchestration classes** → `VerusAgent`, `RepairLoop`, `RecoveryCascade` are all collapsed into one 250-LOC `run.py`. If a round-handling question arises, there's exactly one place to look.
