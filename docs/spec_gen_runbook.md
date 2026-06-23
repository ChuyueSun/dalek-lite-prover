# Spec-Gen Runbook

This guide describes the public-repo surface for spec-strip and
proof-reconstruction experiments. The shipped tools are:

- `peel.py` — deterministic peel-depth builder, including isolated worktrees.
- `skills/strip_specs.py` — low-level strip/delete/proof-strip primitive.
- `admit.py` — proof-body admission primitive and worktree helper.
- `run.py` — proof agent driver, with `--experiment-mode` and
  `--experiment-allow-edit` for reconstruction runs.

This repository does not ship the private launcher scripts or preset manifest
directory from the research branch. Treat manifests as small JSON files you
author locally for the experiment you want to run.

## Peel Model

Peel expresses an experiment as a depth plus per-file targets:

| Depth | Shell | Removes | Primitive |
|:---:|---|---|---|
| P1 | proofs | `proof fn` bodies or inline proof blocks | `admit.py` or `skills/strip_specs.py --strip-proof-fn` |
| P2 | lemmas | named helper lemmas | `skills/strip_specs.py --delete-fn` |
| P3 | specs | named `spec fn` definitions | `skills/strip_specs.py --delete-fn` |
| P4 | contract | named `requires`/`ensures`/`decreases` clauses | `skills/strip_specs.py --strip-fn` |

The frozen floor is exec code plus every `axiom_*`, `assume`, and
`#[verifier::external_body]`. The proof-strip path structurally skips
`axiom_*`, and `peel.py` refuses unpinned P4 contract strips or P3 spec deletes.

## A Manifest

Create a small JSON file for each experiment:

```json
{
  "name": "decompress-bridge-full",
  "depth": 2,
  "experiment_mode": "bridge-full",
  "pin": "proof",
  "target": "curve25519-dalek/src/edwards.rs",
  "files": [
    {
      "path": "curve25519-dalek/src/lemmas/edwards_lemmas/decompress_lemmas.rs",
      "proof_op": "none",
      "lemmas": ["lemma_decompress_valid_branch"]
    }
  ]
}
```

Every listed file is part of the editable reconstruction surface. Files not
listed remain frozen by convention and should not be passed to
`--experiment-allow-edit`.

Preview a manifest without touching files:

```bash
python3 peel.py --manifest /tmp/decompress.json --depth 2 --surface
```

For directory-cut experiments, generate a starter manifest and edit it:

```bash
python3 peel.py --classify /path/to/dalek-lite/curve25519-dalek > /tmp/field_floor.json
```

Add the target, depth, experiment mode, and pin fields before running it.

## Build A Peeled Worktree

Run peel against a clean dalek-lite git repo. `--gitroot` is the dalek-lite
workspace repository root, not this harness repository.

```bash
python3 peel.py \
  --worktree /tmp/dalek-peel-wt \
  --gitroot /path/to/dalek-lite \
  --ref main \
  --manifest /tmp/decompress.json \
  --depth 2
```

The JSON output includes:

- `project`: the Cargo member to pass to `run.py --project`.
- `editable_files`: paths that should be passed to `--experiment-allow-edit`.
- `experiment_mode`: copied from the manifest.

Remove the worktree when done:

```bash
python3 peel.py --worktree /tmp/dalek-peel-wt --gitroot /path/to/dalek-lite --remove
```

## Run The Agent

After building the peeled worktree, run `run.py` directly. Substitute values from
the `peel.py` JSON output:

```bash
python3 run.py /tmp/dalek-peel-wt/curve25519-dalek/src/edwards.rs \
  --project /tmp/dalek-peel-wt/curve25519-dalek \
  --experiment-mode bridge-full \
  --experiment-allow-edit \
    /tmp/dalek-peel-wt/curve25519-dalek/src/lemmas/edwards_lemmas/decompress_lemmas.rs \
  --rounds 10 \
  --run-id peel_bf_001
```

For `spec-proof` mode the runner disables the spec gate automatically because
the agent is expected to rewrite specs. For the frozen-spec modes, the spec gate
stays on and now checks existing `spec fn` bodies via `--check-spec-defs`.

## Low-Level Commands

Use the primitive tools when debugging a cut by hand.

Strip a function's inline proof while keeping its contract and exec body:

```bash
python3 skills/strip_specs.py "$P/src/edwards.rs" \
  --in-place \
  --strip-proof-fn decompress
```

Delete whole helper lemmas:

```bash
python3 skills/strip_specs.py "$P/src/lemmas/edwards_lemmas/decompress_lemmas.rs" \
  --in-place \
  --delete-fn lemma_decompress_valid_branch \
  --delete-fn lemma_to_edwards_correctness
```

Strip all function-header clauses in a file:

```bash
python3 skills/strip_specs.py "$DEP" --in-place
```

Strip header clauses from one function:

```bash
python3 skills/strip_specs.py "$DEP" --in-place --strip-fn decompress
```

Admit proof bodies:

```bash
python3 admit.py "$DEP" --in-place --mode fn-bodies
```

## Soundness Checklist

Before launching a reconstruction run:

- Start from a clean proven dalek-lite checkout.
- List only the intended editable files in the manifest.
- Declare a pin for any P4 contract strip or P3 spec-delete cut.
- Keep frozen files out of `--experiment-allow-edit`.
- Use the exact `project` path returned by `peel.py`.

After a run claims success:

- Check `result.json` for `end_reason: COMPLETE`.
- Confirm `verus_okay` is true and no actionable `admit()` remains.
- Inspect `round_N.json` for empty `spec_drift`.
- For frozen-spec experiments, trust only runs where the spec-definition gate
  stayed on.

## Tests

The deterministic peel transforms and frozen-surface oracle are pinned by:

```bash
python3 -m unittest tests.test_peel
```

Full stdlib unittest discovery:

```bash
python3 -m unittest discover -s tests
```
