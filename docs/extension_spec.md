# Dalek-Lite — Extension Spec

Five features deliberately left out of the MVP, plus **E6** — a sixth pattern that was *not* left out but built on the `spec_gen` branch, recorded here for completeness with its own trigger. For each: **pain it addresses**, **trigger that would justify adding it**, **design sketch**, **integration points**, **rough LOC cost**. None of these should be added speculatively — each one adds surface area, and a prior baseline's history (a feature post-mortem found 15/19 features were dead-code / buggy / net-negative) shows what happens when features are built before they earn their place.

---

## E1. Multi-level cascade

### Pain it addresses

MVP has one level: edit-verify-retry with error feedback. When the LLM can't solve a function that way, the only options are "try again" (pointless if context didn't change) or "give up."

Inference-dalek's history shows two specific failure modes this doesn't cover:

- **Sibling errors** — fixing function `foo` introduces a Verus error in sibling `bar` in the same module. The LLM needs to see `bar` to understand; but if it edits `bar`, committed proofs get clobbered (fixed in commit `147a106`). MVP avoids by forbidding off-target edits in the prompt, but then some proofs are genuinely unfinishable at level 0.
- **Large bodies (>50 LOC)** — direct generation drops to ~50% success vs ~95% for ≤50 LOC (per pipeline_complete.md). Without decomposition, large modules are de-facto blocked.

### Trigger

Add when the MVP fails to make progress on ≥2 modules where post-mortem shows "the LLM couldn't see sibling context" or "the proof body exceeded ~80 LOC." Track via `results/<run_id>/<target>/result.json` `end_reason` and a tag `failure_mode`.

### Design sketch

Three levels, level stored in `round_N.json.level`. LLM sees current level in prompt.

```
L0 (MVP baseline):
  scope read = target fn only
  scope write = target fn only
  attempts = 3
  exit up on: verus ok
  exit across to L1 on: 3 attempts exhausted OR error line outside target fn

L1 (widen + surgical merge):
  scope read = full module
  scope write = target fn only (enforced by a new primitive: merge_target_only())
  attempts = 3, each using a different context ordering (sibling-first / obligation-first / strategy-first — from Exp 18a)
  exit up on: verus ok
  exit across to L2 on: 3 attempts, OR LLM reports "I need a sibling edit"

L2 (decompose OR informal — see E4):
  subagent-spawned (see E2)
  output = one or more sub-lemmas, each re-entering L0

L3 (human admit):
  only place admits are written
  writes <module>/DEFERRED.md with structured record
```

### Integration points

- New `scripts/merge_target_only.py` — takes LLM's full-module response and the original module, extracts only the target fn, merges. Uses `syn` or a simple brace-matcher.
- `run.py` gains a `--max-level` flag (default `0` = MVP). Each level is a separate prompt file (`prompt_l0.md` → `prompt_l1.md` → `prompt_l2.md`).
- `failure_memory.json` gains a `last_level_reached` per function — drives "don't re-attempt at L0 if L1 was needed last time."

### Cost

~400 LOC across merge primitive + 3 prompt files + level-transition logic in `run.py`. Roughly doubles the MVP size.

### Status note (`spec_gen`)

A *much lighter* response to this same "try again is pointless / give up is the only other option" pain is already built on the `spec_gen` branch: the **NEEDS_DECOMP escalation** (`END_REASON:NEEDS_DECOMP`). Instead of E1's L0→L3 level machine with a merge primitive and per-level prompt files, the agent emits a single escalation label when a proof is genuinely blocked on **missing infrastructure** (a helper lemma/chain that does not exist, or a sub-lemma split) and names what's missing. The loop breaks on it and records the label; a fresh `run_task` on the same target (e.g. a `run_layer` re-run) detects the prior NEEDS_DECOMP record and retries with +2 rounds, 1.5× wall-clock, and a "build the named infrastructure first" directive prepended to the failure-memory block. It does **not** widen edit scope, merge full-module responses, or spawn subagents — it just front-loads budget on the retry and tells the agent what to build. The final-state decision is the pure `run._final_end_reason` and the parse is `run.END_REASON_RE`, both pinned in `tests/test_admits.py`.

This is a partial answer to E1's `L2 (decompose)` transition only: it surfaces *that* decomposition is needed and names the gap, but the agent still does the decomposition itself in the retry session rather than via a coordinator. If NEEDS_DECOMP fires often *and* the budget-bumped retries still fail to build the named infrastructure unaided, that's the signal to build the real E1 cascade (level state, `merge_target_only`, per-level prompts).

---

## E2. Subagents / autoproof coordinator mode

### Pain it addresses

**Context explosion on long-horizon proofs.** A single Claude session accumulates context over many rounds; by round 10 on a complex module, the context is 100k+ tokens of accumulated trial-and-error. The model's instruction-following degrades (documented in the paper for Putnam A5 — "when the context becomes too long, the model's ability to follow instructions degrades significantly").

Numina's autosearch solves this by spawning subagents via Claude Code's Task tool: the coordinator has a short context (just CHECKLIST.md + subagent return messages); the proof work happens in throwaway subagent contexts that get discarded after they finish.

### Trigger

Add when (a) a target module's session grows past ~50k tokens before finishing, AND (b) post-mortem shows the model forgetting earlier instructions or repeating earlier mistakes. Track via `round_N.json.usage.input_tokens` — if it grows monotonically past 50k, this is the symptom.

### Design sketch

A new `prompts/autoproof/` directory modeled directly on Numina's `prompts/autosearch/`:

```
prompts/autoproof/
├── main_entry.md            # coordinator prompt — reads CHECKLIST, picks target, spawns subagent
└── subagent_prompts/
    ├── common.md            # forbidden: external_body, spec weakening, bare admit()
    ├── coordinator.md       # role spec for coordinator
    ├── proof_agent.md       # prove one function; forbid Task tool from re-spawning
    └── repair_agent.md      # interpret Verus error, propose fix
```

Coordinator runs as the outer Claude session (same `run.py`, different prompt). Each subagent is spawned via Claude Code's Task tool. Coordinator is **forbidden** from reading `.rs` files or calling Verus directly — all proof work delegated.

### Integration points

- `run.py --mode autoproof` switches prompt file from `prompt.md` to `prompts/autoproof/main_entry.md`.
- CHECKLIST.md (see E3) becomes the coordinator's working memory.
- Bounded parallelism: coordinator may spawn ≤2 subagents, must be on different files (prevents edit conflict — same rule as Numina).
- No new skills needed — subagents use the existing skill set.

### Cost

~6 markdown prompt files, ~0 Python. This is "free" once CHECKLIST.md and multi-target runs exist. Numina's autosearch prompts total ~2500 lines of markdown; we can start at ~800 lines because we have fewer roles.

### Critical caveat

From the paper's ablation (Table 2): subagent mode gave the final **12/12 vs 11/12** lift — one extra problem (A5). Most problems don't need it. Don't add it until you see the context-degradation symptom.

### Status note (`spec_gen`)

A *lighter* response to this same pain is already built on the `spec_gen` branch: `run.py --auto-reset` (on by default) starts a fresh `claude` session — dropping accumulated context — when the session's cache-creation tokens cross `--bloat-threshold-tokens`, or when two rounds stall with no admit progress. It does not spawn subagents; it just resets the one session, so it is a cheaper partial answer to E2's context-explosion symptom. If the bloat trigger fires often *and* resets don't recover progress, that's the signal to build real E2 subagents.

**Trigger fired — prompt-level delegation added (`spec_gen_subagent_context` branch).** Context degradation was observed as the dominant failure mode, so a *second* lighter-than-full-E2 lever was added on top of auto-reset, in two parts:

1. **Prompt-level subagent delegation** (`prompt.md`, section "Delegate hard sub-proofs to a subagent"). The single agent is told to stay a thin coordinator and push exploratory churn (multi-file lemma hunts, repeated `verus_check` cycles) into a Task subagent, which burns its *own* context and returns only the final working proof. The harness does **not** orchestrate this — `claude` already runs with `--permission-mode bypassPermissions` and no `allowedTools` restriction, so the Task tool is available; the prompt just encourages its use. This is strictly weaker than the full E2 coordinator design above (no `main_entry.md` / `subagent_prompts/`, no forbidding the coordinator from touching `.rs`, no CHECKLIST working memory) — it is a hint, not an enforced architecture.
2. **Earlier reset backstop.** `--bloat-threshold-tokens` default lowered 300000 → 200000 so the existing auto-reset sheds the session sooner; delegation keeps the parent context lean *between* resets.

This is deliberately the cheapest test of the E2 hypothesis: measure `round_N.json` `cache_creation_input_tokens` growth before/after to see whether delegation curbs the degradation. If it does not — i.e. the model ignores the delegation hint, or context still degrades despite it — *that* is the signal to build the full harness-orchestrated E2 coordinator mode (`--mode coordinator`, `build_coordinator_block`, `subagent_prompts/`) described in the design sketch above.

**Findings (2026-06-01) — corrects two assumptions above.** The prompt-level hint and a full coordinator prototype were tested on the `eval/spec-stripped-vartime` surface. Three results, the third of which reframes the whole feature:

1. **Prompt-only delegation never fired** — across 2 runs / 7 rounds with genuine context pressure, the agent spawned **zero** subagents. Optional encouragement isn't enough; the agent grinds inline.
2. **Tool name:** the subagent tool emits as `Agent` in the headless `claude -p` stream-json (the hint's "Task" wording is tolerated — the model maps it — but `Agent` is the literal name). It is available under `--permission-mode bypassPermissions`.
3. **Subagents do NOT pollute the parent context — so the premise was wrong.** Measured directly: a built-in `Agent` delegation returned **1721 bytes** (one `RESULT:` line) into the parent; the subagent's file reads / Verus output / Z3 dumps stayed in its own context. The bloat seen in a `--mode coordinator` prototype came from the *coordinator reading `.rs` files itself* (14–21 KB `tool_result` blobs), **not** from subagents. So "subagent context isolation" was never the missing piece — the built-in Agent tool already provides it.

Consequently the prototyped `--mode coordinator` and a `prove_obligation` skill (a separate-process prover) were **built then removed**: they duplicated isolation that already exists and did not fix the real problem (the agent doing heavy reads itself, which has no hard enforcement — `--disallowedTools` *propagates to subagents* and is `Bash cat`-bypassable). **Kept:** only the `--bloat-threshold-tokens` 300000→200000 change (validated). **The actual open lever** is behavioral: get the driver to delegate heavy exploration to the (already-isolating) built-in Agent tool instead of grinding inline — prompt-only does not induce this, and there is no harness mechanism to force it short of restructuring proving into separate `run.py`-level processes.

**Compliance rate.** Prompt-only delegation fired **0/7 rounds = 0.0%** (`subagent_check`: 0/2; `subagent_check2`: 0/5 — both standard mode). No published study reports a prompt-only delegation rate, so this is a (small-n) data point for that gap. For contrast, the now-reverted coordinator-mode prototype (`coord_check`) was the *only* configuration that delegated at all — **2 `Agent` spawns over 2 rounds** — confirming delegation needs explicit scaffolding, not a prompt hint.

**Standing plan (read-only offload + compaction):**
1. **Default single-threaded + compaction** for write-heavy proof work — the discovery-brief (cross-session distilled map) + `--auto-reset` (sheds bloated sessions) *are* the compaction layer. Per Cognition / MAST / Kim et al., this beats multi-agent decomposition for tightly-coupled coding.
2. **Subagents as read-only offloaders only** — delegate lemma-search / multi-file reads / Z3-dump digestion to the built-in `Agent` tool (which already isolates), and apply the returned summary yourself. Never delegate writing. (`prompt.md` recast to this framing is queued.)
3. **Do not rebuild the writing-coordinator.** It was redundant (subagents already isolate) and the real gap is behavioral — forcing delegation would need separate `run.py`-level prover processes, the only enforcement that survives `Bash cat` / tool-restriction propagation.

---

## E3. CHECKLIST.md multi-module campaign state

### Pain it addresses

Running multiple targets across a layer (or across all 10 layers) currently requires shell scripting around `run.py` and piecing together results from scattered JSON files. No single view of "what's proven, what's deferred, what's in-flight."

Numina's CHECKLIST.md solves this: one Markdown file per campaign, with structured entries per target (status, attempts, last error, informal-proof version, tmp file). Coordinator agent reads/writes it between rounds.

### Trigger

Add when you're running ≥5 targets in sequence and post-run analysis requires opening more than 3 result directories to understand state. Concretely: when you find yourself writing shell one-liners like `jq '.success' results/*/result.json | sort | uniq -c` regularly.

### Design sketch

```markdown
# Campaign: <run_id>
Generated: 2026-04-22 14:30:15

## Summary
- Total: 12
- Done: 7  In Progress: 2  Todo: 2  Blocked: 1

### [L0-001] field_specs.rs — field_add_spec
- Status: ✅ done
- Attempts: 1
- Verified: 2026-04-22 14:35
- Proof file: results/.../round_1.json

### [L0-002] field_specs.rs — field_mul_spec
- Status: 🔄 in_progress
- Attempts: 2
- Last error: "unresolved import vstd::math::pow"
- Failure memory: 2 entries (see failure_memory.json)

### [L0-003] primality_specs.rs — axiom_p_is_prime
- Status: ⏭️ skipped (axiom module)
...

### [L9-012] scalar_ops.rs — scalar_sub_borrow
- Status: ❌ blocked (exceeded 5 rounds)
- Next step: needs E4 informal-prover
```

### Integration points

- New `lib/checklist.py` — parse/write CHECKLIST.md (~150 LOC). Parser is strict markdown; writer uses a template.
- `run.py` gains `--campaign <path>`: reads CHECKLIST to pick next target, updates status after run.
- `scripts/checklist_stats.py` — summarize any CHECKLIST.md (replaces the shell one-liners).
- Only written by the harness (or, in autoproof mode, the coordinator). Never the LLM alone — prevents the class of bugs where self-reported success is fake.

### Cost

~250 LOC Python. Low friction once you want campaign-level tracking.

---

## E4. Informal-prover skill

### Pain it addresses

Some proofs fail not because the LLM lacks lemmas but because it **lacks a mathematical strategy**. E.g., a function whose correctness depends on a non-obvious invariant the LLM keeps failing to discover. No amount of syntactic search helps — you need a human-style proof outline first.

Numina's Informal Prover (`prompts/docs/prompts/informal_agent.md` in Numina) fills this: a Gemini/GPT loop that generates an NL proof, verifies it 3× against itself (score 0 / 0.5 / 1), refines on feedback, returns a verified NL outline. That outline then guides the Lean formal attempt.

Paper evidence (Table 2): adding the informal prover to Numina took Putnam 2025 from **4/12 to 11/12**. Single biggest ablation delta.

### Trigger

Add when you observe ≥3 modules where the LLM exhausts attempts at L0/L1 without Verus errors that suggest missing lemmas — the errors are "failed to verify" with unclear proof-state failures. Indicates strategy gap, not lookup gap.

### Design sketch

`skills/informal_prover.py` (Numina-faithful port):

```
Input: function signature + requires/ensures + optional Rust body
Process:
  for attempt in 1..K:
    gen_prompt = "Prove this carefully, atomic steps, no hand-waving ... {problem}"
    solution = call_gemini(gen_prompt) or call_gpt(gen_prompt)
    verify_prompt = "Score this solution 0/0.5/1 ... {problem} {solution}"
    verification = call_verifier(verify_prompt)  # run 3 times; accept only if all 3 say 1
    if score == 1: return solution
    refine_prompt = "Refine the solution based on feedback ... {problem} {solution} {verification}"
    solution = call_llm(refine_prompt)
  return best_solution_so_far
Output: markdown proof outline, saved to results/<run_id>/<target>/informal.md
```

### Integration points

- Call site is the **prompt** — when at L2 (or when MVP fails and you have E1 installed), the coordinator prompt invokes `informal_prover.py` to produce `informal.md`, then passes that path to the next L0 attempt.
- Gemini and/or GPT API key in env (`GEMINI_API_KEY`, `OPENAI_API_KEY`). Optional deps in `pyproject.toml`.
- 20-iteration cap (Numina's value); 3× verification check (Numina's value).
- Adds a real cost channel: each informal call is ~10-50¢ of Gemini.

### Cost

~300 LOC Python (most of it prompt templates). Two new optional deps.

---

## E5. Full spec-integrity tracker with auto-restore

### Pain it addresses

The MVP has `spec_check` as a **gate** — it detects drift and fails the round. That's sufficient for single-function runs. But for multi-function / multi-round runs, a mid-session drift in round 3 blows away work from round 1, and the agent has to learn the lesson again.

Numina's statement tracker goes further: detects drift AND **auto-restores** the original signatures before the next round. The agent is protected from itself — the restoration is silent, transparent, and logged as a `[warn]`. Also distinguishes allowed changes (added new statements) from forbidden changes (modified/removed existing).

### Trigger

Add when `spec_check` gates are firing often (say, >1 per 10 rounds) — it means the agent keeps trying to weaken specs and a hard fail every time is wasting budget. Or when you're running campaigns (E3) where one target's spec drift shouldn't tank the whole campaign.

### Design sketch

Extend existing `skills/spec_check.py`:

```python
# New entry points:
spec_check.py --snapshot <file> --out <snapshot.json>     # (MVP already has)
spec_check.py --verify <file> --against <snapshot.json>   # (MVP already has) — gate mode
spec_check.py --restore <file> --to <snapshot.json>       # NEW: rewrite file to original sigs
spec_check.py --diff <file> --against <snapshot.json> --category {modified,added,removed}  # NEW
```

Classification (per Numina):
- **modified** — existing `fn foo(...) ensures P { ... }` where `P` changed → FORBIDDEN, restore
- **removed** — signature gone entirely → FORBIDDEN, restore
- **added** — new `fn bar(...)` appears that didn't exist before → ALLOWED (might be a helper lemma the agent legitimately introduced)

Logic for restore: AST-level (use `syn` via a small Python wrapper, or a targeted brace-matcher with regex). Preserves agent's body edits; only restores the `fn` header + `requires` + `ensures` + `decreases`.

### Integration points

- `run.py` calls `spec_check --restore` between rounds when drift detected (instead of failing the task).
- `round_N.json` gains `spec_restored: bool` and `spec_restore_details: [...]`.
- The LLM is told in the next round's prompt: "Note: your edits to function X's signature were reverted. Prove it as specified, do not modify the spec."

### Cost

~150 LOC on top of the MVP gate version. Key risk: the restore primitive must be correct — an incorrect restore could corrupt a file mid-run. Needs careful tests, ideally a dry-run mode (`--restore --dry-run`) that emits a patch without applying.

---

## E6. Spec-inference experiment mode (spec-proof / proof-only)

**STATUS: built on the `spec_gen` branch** (`run.py --experiment-mode`), not in `main`. Listed here so the *pattern* — varying *what the task is*, not just how hard the agent tries — has a documented home and trigger, the way the other five do.

### Pain it addresses

The MVP fixes the specs and asks the agent to fill `admit()`s. But a large part of the real cost of Verus-izing a codebase is writing the fn-header specs (`requires` / `ensures` / `decreases`) in the first place. The MVP had no way to exercise or measure the agent on *spec reconstruction* as a capability distinct from proof construction.

### Trigger

Add when you want to evaluate or improve spec-writing separately from proof-writing — e.g. a benchmark that strips fn-header specs from a known-good module and measures whether the agent can re-derive them so a fixed higher-level anchor still verifies.

### Design sketch

Two modes selected by `--experiment-mode` (requires `--experiment-allow-edit` to name the dep files the agent may edit):

- `spec-proof` — dep fns have their fn-header specs stripped; the agent infers them (guided by `skills/infer_verus_spec/`) so a fixed anchor verifies.
- `proof-only` — specs are fixed; the agent only adds proof scaffolding to dep bodies seeded with `admit()`.

`skills/strip_specs.py` produces the stripped inputs; `build_experiment_block` in `run.py` injects the mode-specific prompt addendum. The spec-drift gate still protects the *anchor*; the `--experiment-allow-edit` files are exempt by design (that is the whole point of the mode).

### Cost

Already built: ~235 LOC in `run.py` (experiment block + allow-edit gate plumbing) + the `infer_verus_spec` doc skill + `strip_specs.py` (~456 LOC). The new axis is the cost — it widens "what success means" beyond the MVP's single `verus_okay` + zero-admit definition, so eval comparisons across modes are not apples-to-apples.

---

## Priority if you add them

If forced to rank, based on expected ROI from a prior baseline's post-mortem data:

1. **E5 full spec tracker** — cheap and defensive; a prior baseline's `external_body` bypass experiments are exactly this pain
2. **E3 CHECKLIST** — multiplier for debugging / campaign management
3. **E1 multi-level cascade** — unlocks the off-target / large-body cases (biggest single-level blocker)
4. **E4 informal prover** — biggest potential lift (paper: 4/12 → 11/12) but only for a specific pain pattern
5. **E2 subagents** — highest complexity, smallest expected delta (paper: 11/12 → 12/12 — one problem), only needed for context-degradation at scale

But again — **don't add any until the MVP exhibits the symptom they solve**. The prior baseline's feature post-mortem is the cautionary tale.
