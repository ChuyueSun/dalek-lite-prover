# dalek-lite-mvp

A slim [Verus](https://github.com/verus-lang/verus) proof-synthesis agent. Its
one job: replace `admit()` calls in Verus-annotated Rust with real proofs that
pass `cargo verus`. One loop, a handful of skills, ~1k LOC of Python — the
anti-monolith.

The agent drives headless [Claude Code](https://docs.claude.com/claude-code) in
a round loop: snapshot the function signatures, render a prompt, let the model
edit proofs and call the search/verification skills, then gate the result on a
real Verus check. A target is `COMPLETE` only when Verus passes **and** zero
`admit()` remain (an `admit()` makes Verus accept any postcondition, so
`verus_okay` alone is not sufficient evidence).

See [`docs/mvp_spec.md`](docs/mvp_spec.md) for the full design and
[`docs/extension_spec.md`](docs/extension_spec.md) for deliberately deferred
features, each with a documented trigger.

## Layout

```
dalek-lite-mvp/
├── run.py              # the driver — one target per invocation
├── run_layer.py        # iterate a layer set sequentially
├── launch.sh           # arbitrary target lists; --detach for headless safety
├── admit.py            # build the admit() skeleton / isolated worktrees
├── replay.py           # pretty-print a raw Claude stream-json log
├── prompt.md           # the task prompt (template)
├── docs/
│   ├── mvp_spec.md         # what's in scope + design rationale
│   ├── extension_spec.md   # deferred features, each with a trigger
│   └── diagnostics.md      # failure-pattern catalog + detection commands
├── lib/                # support modules (not skills)
│   ├── catalog.py          # canonical symbol catalog (shared by search skills)
│   ├── admits.py           # axiom-aware admit counting + skeleton creation
│   ├── failure_memory.py   # per-function persistent failure records
│   └── results.py          # result-dir helpers + dataclasses
└── skills/             # CLIs the agent invokes via Bash
    ├── SKILL.md
    ├── verus_check.py      # run cargo verus, parse errors (source of truth)
    ├── spec_check.py       # snapshot + verify signatures (spec-drift gate)
    ├── admit_inventory.py  # classify remaining admits (axiom vs actionable)
    ├── search_semantic.py
    ├── search_module.py
    ├── search_macro.py
    └── search_proven.py
```

## Prerequisites

- Python 3.11+ (standard library only — no third-party deps)
- Claude Code CLI (`claude`) on `PATH`, authenticated
- Verus / `cargo verus` installed and on `PATH`
- A Verus-annotated Rust project with at least one `admit()` to fill in

## One-time setup: make skills discoverable

Claude Code auto-discovers skills under `.claude/skills/`. Symlink this
project's `skills/` there:

```bash
cd /path/to/dalek-lite-mvp
mkdir -p .claude/skills
ln -sfn "$(pwd)/skills" ".claude/skills/dalek-lite-mvp"
```

## Running it

### Single module

```bash
# Prove the admits in one file (auto-detects the Cargo root)
python run.py /path/to/project/curve25519-dalek/src/specs/field_specs.rs

# Budget + explicit run id + results dir
python run.py <target> --rounds 5 --run-id baseline_001 --results ./results

# Pick a model
python run.py <target> --model sonnet      # haiku | sonnet | opus | <model-id>

# Include vstd in the symbol catalog so skills find vstd lemmas
python run.py <target> --vstd-root /path/to/verus/vstd

# Override the project root (rarely needed; auto-detected from the target)
python run.py <target> --project /path/to/cargo/root
```

The target file must live inside a buildable Cargo project (an ancestor
directory has `Cargo.toml`); `run.py` detects it automatically.

### Layer sets (multiple modules, sequential)

```bash
python run_layer.py A --project /path/to/curve25519-dalek --rounds 5 \
    --run-id layerA_001
# resume after interruption (skips already-proven modules)
python run_layer.py A --project /path/to/curve25519-dalek \
    --run-id layerA_001 --skip-existing
```

A summary is written to `results/<run_id>/layer_summary.json`. Layer sets
A–D group the target modules by dependency depth; pass the letter as the
positional argument.

### Arbitrary target lists (`launch.sh`)

When the targets don't line up with a layer set — re-running prior failures,
mixing modules, per-target budgets — use `launch.sh` instead of a bash loop:

```bash
# Foreground, single target
./launch.sh --run-id rerun_001 --project /path/to/curve25519-dalek \
    --vstd-root /path/to/verus/vstd src/edwards.rs

# Background (detached), mixed result-dirs + per-target budgets via a file
cat > /tmp/targets <<'EOF'
results   | src/lemmas/field_lemmas/u64_5_as_nat_lemmas.rs | 60
results-C | src/edwards.rs                                  | 90
results-C | src/window.rs
EOF
./launch.sh --detach --run-id rerun_002 \
    --project /path/to/curve25519-dalek --vstd-root /path/to/verus/vstd \
    --targets-file /tmp/targets
tail -f launcher_rerun_002.log | grep --line-buffered '^MARKER'
```

`launch.sh` is sequential by design: parallel `run.py`s on one project worktree
contend on the cargo lock and race on the shared JSON state. To parallelise,
give each run its **own** worktree (see below) and its **own** `--results` dir.

> **`--detach` is required when launching from inside a sandboxed/headless agent
> environment.** Such environments often `killpg` their child process group on
> teardown, so a plain `nohup … & disown` dies between targets. `--detach`
> re-execs through `start_new_session=True` (POSIX `setsid`), reparenting the
> orchestrator out of that group. Foreground (no `--detach`) is fine for
> interactive shells.

### Isolated admitted worktrees

A run wants the target in its **admitted starting state** — `proof fn` bodies
replaced by `admit()`, with `spec fn` defs / exec code / `axiom_*` left intact —
inside an isolated checkout so the run never dirties your main tree. `admit.py`
builds this:

```bash
REPO=/path/to/dalek-lite          # the Cargo workspace git root

# Check out a ref into an isolated worktree and admit a file in place:
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --ref main \
    --admit-target curve25519-dalek/src/edwards.rs
python run.py /tmp/dalek-wt/curve25519-dalek/src/edwards.rs \
    --project /tmp/dalek-wt/curve25519-dalek
python admit.py --worktree /tmp/dalek-wt --gitroot "$REPO" --remove   # when done

# Or admit a file in place directly (resets any proofs already present —
# point it at a clean checkout):
python admit.py /path/to/curve25519-dalek/src/edwards.rs --in-place --mode auto
```

If the project is a Cargo **workspace**, add the worktree at the workspace git
root and pass the member crate as `--project`. On a brand-new worktree, warm
the build once (`cargo verus verify -p <crate>`, ~40s) before the first
module-scoped check, otherwise `--verify-module` can spuriously fail against an
uncompiled `vstd`.

## Output

```
results/<run_id>/<target_id>/
├── result.json          # success, end_reason, rounds_used, duration
├── round_N.json         # per round: verus_okay, errors, spec_drift, usage
├── prompt_rendered.md   # the exact prompt the model received (reproducibility)
├── spec_snapshot.json   # signature baseline (spec_check reference)
└── claude_raw/round_N.jsonl   # raw Claude stream-json

results/
├── failure_memory.json  # per-(module,function) prior failures, fed back on retry
└── proven_registry.json # cumulative list of proven targets
```

`jq` and `less` are the dashboard. Inspect a run with
`python replay.py results/<run_id>/<target_id>/claude_raw/round_1.jsonl`, or
follow the live skill log with `tail -f results/<run_id>/<target_id>/cli.log`.

## Integrity gates

The agent's incentive is to make Verus pass, and the cheapest way to fake that
is to weaken the problem. Three gates snapshot a baseline before the loop and
fail the round on any drift:

- **Spec drift** — every `fn` signature + `requires`/`ensures`/`decreases` is
  snapshotted; any change fails the round (`SPEC_DRIFT`).
- **Axiom drift** — the `COMPLETE` counter excludes `admit()` inside
  `axiom_*` bodies, so a *new* `axiom_*` would be a fake-green vector; the
  axiom-name set is pinned (`AXIOM_DRIFT`).
- **Tooling drift** — the verification skills are SHA-256'd before the loop; any
  edit to `skills/`+`lib/` fails the round (`TOOLING_DRIFT`).

The prompt also forbids `#[verifier::external_body]`, `assume(...)`, and new
`admit()` calls.

## Extending

- **A new search skill** — drop a file under `skills/` matching
  `search_semantic.py`: a CLI that prints JSON on stdout and logs to
  `$CLI_LOG_PATH`. Mention it in `prompt.md` and `skills/SKILL.md`. That is the
  entire extension protocol.
- **A new result field** — add it to the dataclasses in `lib/results.py`; it
  persists automatically.
- **Change behaviour** — edit `prompt.md`; `run.py` re-reads it each round.

See [`docs/extension_spec.md`](docs/extension_spec.md) for larger features that
are deferred until their trigger fires — don't build them on speculation. If a
fix needs changes in more than two files, that's a sign of drift; pause and
reconsider.

## License

MIT — see [LICENSE](LICENSE). This tool operates on, but does not include,
[curve25519-dalek](https://github.com/dalek-cryptography/curve25519-dalek),
which is independently licensed.
