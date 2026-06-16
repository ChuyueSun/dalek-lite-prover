#!/usr/bin/env python3
"""Create the admit() skeleton from proven Verus source (init-time tool).

Run by `launch.sh --admit` to build a target's admitted *starting state*
in place. Wraps the mode-aware admitter in
`lib.admits`: it admits ONLY `proof fn` bodies and inline `proof { ... }`
blocks, skips `axiom_*` (trusted), and leaves `spec fn` definitions and all
exec code intact. (Contrast the mode-blind `strip_fn_body_to_admit` — see
lib/admits.py.)

This is harness/init tooling, NOT an agent skill (the proof agent never
calls it during a round), so it lives at the top level next to `run.py` /
`launch.sh` rather than under `skills/`.

It has two modes:

  * File mode (default) — admit one .rs file in place / to --out.
  * Worktree mode (--worktree) — `create_admit_worktree`: check out the
    project repo into an isolated git worktree holding the admit() starting
    state, optionally building the skeleton from proven source.

Modes (the body pass, used by both):
  fn-bodies     replace `proof fn` bodies with an admit() skeleton
                (for lemmas/ and specs/ files)
  proof-blocks  hollow inline `proof { ... }` blocks to `{ admit(); }`
                (for exec files)
  both          apply both passes
  auto          (default) fn-bodies when the path is under lemmas/ or
                specs/, else proof-blocks

Usage:
    # File mode
    python admit.py <file.rs> --in-place
    python admit.py <file.rs> --out <out.rs> --mode fn-bodies

    # Worktree mode — check out a pre-admitted ref (skeleton already committed)
    python admit.py --worktree /tmp/wt --gitroot /path/to/dalek-lite \
        --ref eval/admitted-start

    # Worktree mode — build the skeleton from proven source (--detach is implicit;
    # --admit-target admits that file in place after checkout, repeatable)
    python admit.py --worktree /tmp/wt --gitroot /path/to/dalek-lite --ref main \
        --admit-target curve25519-dalek/src/edwards.rs

    # Tear the worktree down
    python admit.py --worktree /tmp/wt --gitroot /path/to/dalek-lite --remove

Output (stdout): JSON summary
    file mode:     {"okay": true, "file": ..., "mode": ..., "changed": ...,
                    "raw_admits_before": N, "raw_admits_after": M, ...}
    worktree mode: {"okay": true, "worktree": ..., "ref": ..., "project": ...,
                    "admitted": [{"file": ..., "raw_admits_after": M}, ...]}
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.admits import (  # noqa: E402
    admit_proof_blocks,
    admit_proof_fn_bodies,
    count_non_axiom,
)

MODES = ("auto", "fn-bodies", "proof-blocks", "both")


def resolve_mode(mode: str, path: Path) -> str:
    """Map `auto` to fn-bodies / proof-blocks by path: lemmas//specs/
    files get the fn-body pass, exec files get the proof-block pass."""
    if mode != "auto":
        return mode
    p = str(path).replace(os.sep, "/")
    return "fn-bodies" if ("/lemmas/" in p or "/specs/" in p) else "proof-blocks"


def admit_text(text: str, mode: str) -> str:
    """Apply the resolved (non-auto) admitter mode to `text`."""
    if mode == "fn-bodies":
        return admit_proof_fn_bodies(text)
    if mode == "proof-blocks":
        return admit_proof_blocks(text)
    if mode == "both":
        return admit_proof_blocks(admit_proof_fn_bodies(text))
    raise ValueError(f"unknown mode: {mode!r}")


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run a git command, return stripped stdout, raise on non-zero."""
    out = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def create_admit_worktree(
    gitroot: Path,
    ref: str,
    dest: Path,
    admit_targets: list[Path] | None = None,
    mode: str = "auto",
) -> dict:
    """Check out the project repo into an isolated worktree at the admit()
    starting state, optionally building the skeleton from proven source.

    - `git worktree add --detach <dest> <ref>`. `--detach` is deliberate: it
      lets `ref` be a branch the primary checkout already holds (e.g. `main`),
      which a non-detached add rejects with "main is already used by worktree".
    - When `ref` is an already-admitted ref (e.g. `eval/admitted-start`) the
      skeleton is already committed — pass no `admit_targets`. When `ref` is
      proven source (e.g. `main`), pass the files to admit in place; each is run
      through the same body pass as file mode (`resolve_mode` + `admit_text`).

    `dest` is idempotent: a stale worktree there (crashed prior run) is removed
    first. Paths in `admit_targets` are relative to the worktree (or absolute
    inside it). Returns a JSON-able summary. Tear down with
    `remove_admit_worktree` (or `python admit.py --worktree <dest> --remove`)."""
    gitroot = Path(_git("rev-parse", "--show-toplevel", cwd=gitroot))
    dest = dest.resolve()

    # Defensive: clear a stale worktree from a crashed prior run before adding.
    if dest.exists():
        try:
            _git("worktree", "remove", "--force", str(dest), cwd=gitroot)
        except RuntimeError:
            shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    _git("worktree", "add", "--detach", str(dest), ref, cwd=gitroot)

    admitted: list[dict] = []
    for t in (admit_targets or []):
        f = t if t.is_absolute() else (dest / t)
        if not f.exists():
            raise FileNotFoundError(f"--admit-target not in worktree: {f}")
        text = f.read_text()
        m = resolve_mode(mode, f)
        new = admit_text(text, m)
        f.write_text(new)
        admitted.append({
            "file": str(f),
            "mode": m,
            "changed": new != text,
            "raw_admits_before": text.count("admit()"),
            "raw_admits_after": new.count("admit()"),
        })

    # Convenience: surface the Cargo workspace member (the run.py --project).
    member = dest / "curve25519-dalek"
    project = member if (member / "Cargo.toml").exists() else dest

    return {
        "okay": True,
        "worktree": str(dest),
        "ref": ref,
        "project": str(project),
        "admitted": admitted,
    }


def remove_admit_worktree(gitroot: Path, dest: Path) -> None:
    """Tear down a worktree created by `create_admit_worktree`
    (`git worktree remove --force`, falling back to rmtree)."""
    gitroot = Path(_git("rev-parse", "--show-toplevel", cwd=gitroot))
    try:
        _git("worktree", "remove", "--force", str(dest.resolve()), cwd=gitroot)
    except RuntimeError:
        shutil.rmtree(Path(dest), ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Create the admit() skeleton from proven Verus source "
                    "(file mode), or an admit-skeleton git worktree (--worktree).")
    # File mode (default): target is optional so --worktree can stand alone.
    ap.add_argument("target", type=Path, nargs="?",
                    help="Target .rs file to admit (file mode)")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--out", type=Path, help="Write output here (file mode)")
    grp.add_argument("--in-place", action="store_true",
                     help="Overwrite the target file in place (file mode)")
    ap.add_argument("--mode", choices=MODES, default="auto",
                    help="Which admitter pass(es) to apply (default: auto)")
    # Worktree mode (create_admit_worktree):
    wt = ap.add_argument_group("worktree mode (--worktree)")
    wt.add_argument("--worktree", type=Path, metavar="DEST",
                    help="Create (or --remove) an admit-skeleton git worktree at "
                         "DEST instead of admitting a single file.")
    wt.add_argument("--gitroot", type=Path, metavar="REPO",
                    help="Project git repo to worktree from (e.g. the dalek-lite "
                         "root). Required with --worktree.")
    wt.add_argument("--ref", default="eval/admitted-start",
                    help="Commit/branch the worktree checks out "
                         "(default: eval/admitted-start).")
    wt.add_argument("--admit-target", type=Path, action="append", default=[],
                    metavar="REL",
                    help="After checkout, admit this file in place (repeatable). "
                         "Use when --ref is proven source (e.g. main).")
    wt.add_argument("--remove", action="store_true",
                    help="Remove the worktree at DEST instead of creating it.")
    args = ap.parse_args()

    # ── Worktree mode ────────────────────────────────────────────────────────
    if args.worktree is not None:
        if args.gitroot is None:
            ap.error("--worktree requires --gitroot (the project repo root)")
        try:
            if args.remove:
                remove_admit_worktree(args.gitroot, args.worktree)
                print(json.dumps({"okay": True, "removed": str(args.worktree)},
                                 indent=2))
                return 0
            summary = create_admit_worktree(
                args.gitroot, args.ref, args.worktree,
                admit_targets=args.admit_target, mode=args.mode,
            )
            print(json.dumps(summary, indent=2))
            return 0
        except (RuntimeError, FileNotFoundError) as e:
            print(json.dumps({"okay": False, "error": str(e)}, indent=2))
            return 1

    # ── File mode (default) ──────────────────────────────────────────────────
    if args.target is None or not (args.in_place or args.out):
        ap.error("file mode requires <target> and one of --in-place / --out "
                 "(or use --worktree for worktree mode)")

    if not args.target.exists():
        print(json.dumps({"okay": False,
                          "error": f"target not found: {args.target}"}))
        return 1

    text = args.target.read_text()
    mode = resolve_mode(args.mode, args.target)
    new = admit_text(text, mode)
    dest = args.target if args.in_place else args.out
    dest.write_text(new)

    print(json.dumps({
        "okay": True,
        "file": str(args.target),
        "out": str(dest),
        "requested_mode": args.mode,
        "mode": mode,
        "changed": new != text,
        "bytes_before": len(text),
        "bytes_after": len(new),
        "raw_admits_before": text.count("admit()"),
        "raw_admits_after": new.count("admit()"),
        "non_axiom_admits_after": count_non_axiom(new),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
