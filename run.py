#!/usr/bin/env python3
"""Dalek-Lite MVP driver.

Loop:
    claude -p --verbose --output-format stream-json <prompt>
    round:
        - save raw NDJSON
        - parse END_REASON from result text
        - run spec_check verify (gate: any drift = failed round)
        - run verus_check (source of truth: verus_okay)
        - record round_N.json
    continue with `claude -c` until COMPLETE | LIMIT | NEEDS_DECOMP | max_rounds

NEEDS_DECOMP is an escalation: the agent declares the proof is blocked on
missing infrastructure (a helper lemma/chain that doesn't exist, or a
sub-lemma split) rather than grinding to the time limit. The loop breaks on
it, the label is preserved into result.json / failure_memory, and a fresh
run_task on the same target gives the retry +2 rounds, 1.5x wall-clock, and a
"build the named infrastructure first" directive.

Usage:
    python run.py <target.rs> [--project <cargo_root>] [--rounds 5]
                              [--run-id <id>] [--results <dir>]
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).parent))
from lib import atomic_json, discovery_brief, failure_memory, results  # noqa: E402
from lib.admits import count_non_axiom as _count_llm_target_admits  # noqa: E402
from lib.admits import count_non_axiom_in_fns as _count_admits_in_fns  # noqa: E402
from lib.admits import find_matching_brace, find_proof_fn_body_brace  # noqa: E402
from lib.admits import axiom_fn_names  # noqa: E402
from lib.results import RoundResult, TaskResult, task_dir, write_json  # noqa: E402


HERE = Path(__file__).parent.resolve()
PROMPT_TEMPLATE = HERE / "prompt.md"

def _make_agent_cwd(label: str = "") -> Path:
    """Create a fresh per-task scratch cwd for the claude subprocess and return
    it (call ONCE per task, then reuse for every round). Used as the claude
    subprocess cwd so Claude Code does not inject HERE's CLAUDE.md into the proof
    agent's context: Claude Code auto-loads CLAUDE.md by walking UP the cwd
    ancestry and injects it into EVERY request as a `# claudeMd`
    <system-reminder> block. HERE's CLAUDE.md is the harness operator/dev doc
    (~7.5k tokens) — pure noise for the agent, which only needs the rendered
    prompt + skills. So we launch from a dir OUTSIDE the repo that symlinks
    `skills/` + `lib/`, keeping the prompt's relative `python skills/<name>.py` /
    `Read skills/SKILL.md` (and the skills' `from lib import ...`) working, while
    every other path the prompt substitutes is already absolute (.resolve()d).

    A FRESH per-task tempdir (tempfile.mkdtemp), not a shared global path: one
    shared dir is a footgun under the documented parallel-worktree fan-out — two
    runs from different checkouts would flap each other's symlinks mid-round, so
    an agent could execute the OTHER checkout's skill code. mkdtemp gives each
    task its own collision-free dir; reusing it across the task's rounds keeps
    the cwd's session-project slug stable so `--resume` finds the session.

    On ANY setup failure, fall back to HERE (the old, proven cwd) with a LOUD
    warning rather than silently returning a dir missing the skills link — a
    missing link 404s every relative `python skills/<name>.py` the agent runs,
    silently degrading the whole task (the harness's own absolute-path gates
    still run, so it burns budget without false-greening)."""
    try:
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:60]
        cwd = Path(tempfile.mkdtemp(prefix=f"dalek_agent_cwd_{slug}_"))
        for name in ("skills", "lib"):
            (cwd / name).symlink_to(HERE / name, target_is_directory=True)
        atexit.register(shutil.rmtree, cwd, ignore_errors=True)
        return cwd
    except OSError as e:
        print(f"[run] WARNING: could not build agent scratch cwd ({e}); "
              f"falling back to cwd=HERE — the harness CLAUDE.md (~7.5k tokens) "
              f"will be injected into agent context for this task.", flush=True)
        return HERE
# Conditionally-injected guidance: the "Decompose hard admits" section. Only
# carried in the prompt for targets that actually have a hard function (see
# `target_needs_decompose`); empty otherwise, to keep the eager prompt lean.
DECOMPOSE_TEMPLATE = HERE / "prompt_decompose.md"

# Scope the spawned proof agent's toolset to only file + shell + subagent tools.
# The dalek-lite proof CLIs are run via Bash, and the skill reference is read on
# demand from `skills/SKILL.md` (a plain file). Everything else is stripped to
# keep the system prompt lean and off unrelated capabilities.
#
# `--tools` is the flag that actually filters tool *availability*; it also
# excludes MCP tools (they aren't in the built-in set). `--allowedTools` is
# only a *permission* allowlist and is a no-op under
# `--permission-mode bypassPermissions`, so it does NOT shrink the toolset.
# `--strict-mcp-config` (with no `--mcp-config`) deterministically loads zero
# MCP servers so connected ones (Gmail/Calendar/Drive) never leak in.
# `--disable-slash-commands` drops ALL discovered skills/slash-commands. We do
# NOT use native skills: enabling the `Skill` tool exposed 14 skills (1 project +
# 13 inherited user-global/built-in noise) with no flag to scope to just ours,
# and in a real run the native skill never fired — the lean prompt index carried
# the proof round. So: zero skill noise, and the agent `Read`s `skills/SKILL.md`
# for exact flags when it needs them.
# `Task` and `Agent` are two aliases for the SAME subagent tool, and `--tools`
# matches either. (Verified live on 2.1.128: `--tools Bash,Task` and
# `--tools Bash,Agent` both retain the subagent tool; `--tools Bash` alone drops
# it.) The CLI's init metadata labels it `Task`; the API request body and
# tool_use blocks label it `Agent`. Listing both here is harmless — they dedup
# to one tool, so these 7 names yield 6 built-ins. Grep/Glob are intentionally
# absent: this Claude Code build doesn't expose them as separate tools, and the
# agent greps via Bash (prompt.md already says "or raw grep").
AGENT_TOOL_FLAGS = [
    "--tools", "Bash,Read,Edit,Write,TodoWrite,Task,Agent",
    "--strict-mcp-config",
    "--disable-slash-commands",
]
END_REASON_RE = re.compile(
    r"(?m)^\s*END_REASON:(COMPLETE|LIMIT|NEEDS_DECOMP)\s*$", re.I)


# ----------------- helpers -----------------

# Match a `proof fn axiom_*` header. Admits inside such functions are
# axioms-by-convention (e.g. precomputed-table validity, primality, etc.)
# that cannot be discharged by SMT and are intentionally left as `admit()`.
# They must NOT count toward the "admits remaining" gate, or those files
# will be permanently LIMITed.
def _rejection_continue_msg(verus_okay: bool, admits_left: int) -> str:
    """Build the continuation message when a previous round's
    `END_REASON:COMPLETE` is overridden by the harness's final-state
    gate. Pure function so it can be unit-tested; the loop below
    prepends its output to the round-history block on the next round."""
    return (
        f"Your previous END_REASON:COMPLETE was rejected: "
        f"verus_okay={verus_okay}, non-axiom admits remaining="
        f"{admits_left}. Re-run `verus_check`, locate any remaining "
        f"`admit()` outside `proof fn axiom_*` bodies, fix them, "
        f"then declare COMPLETE again — or emit END_REASON:LIMIT "
        f"if you cannot."
    )


def _final_end_reason(done_for_real: bool, loop_end_reason: Optional[str]) -> str:
    """Resolve the task's recorded end_reason from the final-state gate.

    Pure function so the decision table can be unit-tested; the loop below
    just feeds it `done_for_real` (verus okay AND no hard admits remain) and
    the agent's self-declared `loop_end_reason`.

    Priority:
      1. RATE_LIMITED ⇒ preserved, ABOVE the done_for_real promotion. A 429
         halt means the round never really ran; even a trivial (zero-hard-
         admit) target that verus accepts must NOT be promoted to COMPLETE
         off it — otherwise the throttle is masked and the launcher won't
         halt. Recording RATE_LIMITED keeps it out of proven_registry so a
         later --skip-existing re-run picks it back up honestly.
      2. `done_for_real` ⇒ COMPLETE, regardless of what the agent claimed.
         Promotes an over-cautious LIMIT (only intentional axioms left) and a
         NEEDS_DECOMP the agent actually discharged before escalating.
      3. NEEDS_DECOMP ⇒ preserved (Feature2). A distinct, machine-countable
         "needs missing infrastructure" escalation — not silently flattened
         into LIMIT, so a retry can detect it and bump its budget.
      4. anything else ⇒ LIMIT (COMPLETE claimed but evidence disagrees, or
         an honest LIMIT).

    Exception (highest priority): a cheating signal — SPEC_DRIFT (a frozen
    spec was weakened), AXIOM_DRIFT (a new `proof fn axiom_*` was injected),
    or TOOLING_DRIFT (the agent edited the harness's own verification skills
    under skills/ + lib/) — is NEVER promoted to COMPLETE, even when verus is
    green and no hard admits remain. Weakening a spec / injecting an axiom /
    doctoring a verification skill is *precisely* how an agent makes verus
    pass without a real proof, so a green final state is not evidence of
    done — it's evidence the cheat worked."""
    if (loop_end_reason or "").upper() == "RATE_LIMITED":
        return "RATE_LIMITED"
    lr = (loop_end_reason or "").upper()
    if lr in ("SPEC_DRIFT", "AXIOM_DRIFT", "TOOLING_DRIFT", "SIBLING_VERUS_FAIL"):
        # Terminal: never promoted to COMPLETE even when the target locally
        # verifies. The cheat-class drifts make verus pass without a real
        # proof; SIBLING_VERUS_FAIL means a sibling/top-level module the agent
        # touched no longer verifies — a target-only green is not done.
        return lr
    if done_for_real:
        return "COMPLETE"
    if lr == "NEEDS_DECOMP":
        return "NEEDS_DECOMP"
    return "LIMIT"


# The axiom-aware admit counter `_count_llm_target_admits` is now
# imported from `lib.admits` (see top-of-file import). Same algorithm,
# pinned by tests/test_admits.py — kept aliased to the old name so
# existing callers in this file don't need to change.


def _count_gate_admits(target: Path, allow_edit: Optional[list[Path]]) -> int:
    """Count non-axiom `admit()` calls across the target plus any
    experiment_allow_edit files. Used by the COMPLETE gate so an agent
    cannot declare done while admit() placeholders remain in dep file
    bodies (relevant for proof-only mode, whose baseline seeds them)."""
    total = _count_llm_target_admits(target.read_text())
    for dep in (allow_edit or []):
        try:
            total += _count_llm_target_admits(dep.read_text())
        except OSError:
            pass
    return total


def find_cargo_root(target: Path) -> Path:
    p = target.parent if target.is_file() else target
    while p != p.parent:
        if (p / "Cargo.toml").exists():
            return p
        p = p.parent
    return target.parent


def module_path_of(target: Path, project: Path) -> str:
    rel = target.resolve().relative_to(project.resolve())
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts:
        parts[-1] = parts[-1].removesuffix(".rs")
    if parts and parts[-1] in ("mod", "lib"):
        parts = parts[:-1]
    return "::".join(parts)


def target_needs_decompose(target: Path) -> bool:
    """Mirror of the prompt's "Decompose hard admits" trigger.

    That guidance only earns its eager cost when the target has an `admit()`
    to fill AND some function is "hard" — a body spanning >100 source lines, or
    an `ensures` clause with >=3 top-level `&&` conjuncts. The check is
    file-level and deliberately *lenient* (it doesn't insist the hard function
    be the one holding the admit): a false positive merely shows ~57 extra
    lines, whereas a false negative drops the guidance on a genuinely hard
    proof — the worse outcome. Reuses the battle-tested brace helpers from
    `lib.admits` so `ensures ({ ... })` clauses aren't misread as fn bodies.
    """
    try:
        text = target.read_text()
    except OSError:
        return False
    if "admit()" not in text:
        return False
    # Signal 1: any `ensures` clause with >=3 top-level conjuncts (>=2 `&&`).
    for m in re.finditer(r"\bensures\b", text):
        body = find_proof_fn_body_brace(text, m.start())
        clause = text[m.end(): body] if body else text[m.end():m.end() + 2000]
        dec = clause.find("decreases")          # ensures ends at decreases/body
        if dec != -1:
            clause = clause[:dec]
        if clause.count("&&") >= 2:
            return True
    # Signal 2: any function body spanning >100 source lines.
    for m in re.finditer(r"\bfn\s+\w+", text):
        body = find_proof_fn_body_brace(text, m.start())
        if body is None:
            continue
        end = find_matching_brace(text, body)
        if end is not None and text.count("\n", body, end) > 100:
            return True
    return False


def render_prompt(
    target: Path, project: Path, module: str,
    spec_snapshot: Path, catalog_cache: Path,
    results_root: Path, failure_block: str,
    vstd_root: Optional[Path] = None,
    experiment_block: str = "",
    decompose_block: str = "",
) -> str:
    template = PROMPT_TEMPLATE.read_text()
    vstd_flag = f" --vstd-root {vstd_root}" if vstd_root else ""
    return (
        template
        .replace("{TARGET_PATH}", str(target))
        .replace("{PROJECT_ROOT}", str(project))
        .replace("{MODULE_PATH}", module)
        .replace("{SPEC_SNAPSHOT}", str(spec_snapshot))
        .replace("{CATALOG_CACHE}", str(catalog_cache))
        .replace("{RESULTS_ROOT}", str(results_root))
        .replace("{VSTD_FLAG}", vstd_flag)
        .replace("{FAILURE_MEMORY_BLOCK}", failure_block or
                 "_(none — this is a fresh attempt on this function)_")
        .replace("{EXPERIMENT_MODE_BLOCK}", experiment_block)
        .replace("{DECOMPOSE_BLOCK}", decompose_block)
    )


def classify_remaining_admits(target: Path) -> dict:
    """For each `admit()` in `target`, classify as 'intentional' or 'hard'.

    Intentional signals (any one suffices):
      - File basename is `axioms.rs`
      - Enclosing fn name starts with `axiom_`
      - The docstring/comment block within ~20 lines above the enclosing
        fn contains `Axiom:` or `/// Axiom`
      - File path contains `core_assumes` or basename matches the
        documented axiom-file patterns (primality_specs, proba_specs,
        curve_equation_lemmas)

    Returns {'total': N, 'intentional': K, 'hard': N-K, 'detail': [...]}
    where `detail` is per-admit (line, enclosing_fn, classification, reason).
    """
    try:
        text = target.read_text()
    except OSError:
        return {"total": 0, "intentional": 0, "hard": 0, "detail": []}

    if "admit()" not in text:
        return {"total": 0, "intentional": 0, "hard": 0, "detail": []}

    # curve_equation_lemmas is REMOVED from this set per user direction:
    # the file contains a mix of `axiom_*` foundational propositions and
    # `lemma_*` derivable propositions. Per-fn signals (axiom_* prefix,
    # "Axiom:" docstring) suffice to classify the axiom_* ones; lemma_*
    # ones are now classified `hard` so the harness pursues them.
    is_axiom_file = (
        target.name == "axioms.rs"
        or target.stem in {"core_assumes", "primality_specs", "proba_specs"}
    )

    fn_ranges = _fn_ranges_in_file(target)
    lines = text.splitlines()
    admit_lines = [i + 1 for i, ln in enumerate(lines) if "admit()" in ln]

    def find_fn(ln: int) -> Optional[tuple[str, int, int]]:
        best = None
        for name, s, e in fn_ranges:
            if s <= ln <= e and (best is None or s > best[1]):
                best = (name, s, e)
        if best is not None:
            return best
        # Fallback: the brace-walking parser may have skipped this fn
        # (e.g. unusual body, macros). Do a simple backward scan for
        # the most-recent `(pub )?(proof )?fn NAME` header above the
        # admit line. Less precise but more robust.
        header_re = re.compile(
            r"^[ \t]*(?:pub(?:\([^)]+\))?\s+)?(?:broadcast\s+)?"
            r"(?:open\s+|closed\s+)?(?:proof|spec|exec)?\s*"
            r"fn\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        for i in range(ln - 1, max(ln - 200, -1), -1):
            if i >= len(lines):
                continue
            m = header_re.match(lines[i])
            if m:
                return (m.group(1), i + 1, ln)
        return None

    detail: list[dict] = []
    for ln in admit_lines:
        enc = find_fn(ln)
        # A `lemma_*` fn with an admit() is an unfinished proof, never an
        # intentional axiom — pursue it regardless of filename/docstring.
        # Without this, a `lemma_*` obligation living in axioms.rs (or under
        # an "Axiom:" docstring) gets mis-classified intentional. Keeps this
        # consistent with _count_llm_target_admits (excludes only axiom_*).
        if enc is not None and enc[0] and enc[0].startswith("lemma_"):
            detail.append({
                "line": ln, "function": enc[0],
                "classification": "hard",
                "reason": "lemma_ fn (unfinished proof, pursued)",
            })
            continue
        reason = None
        if is_axiom_file:
            reason = f"file basename {target.name}"
        if enc is not None:
            name, s, e = enc
            if name.startswith("axiom_"):
                reason = reason or f"fn name '{name}' starts with axiom_"
            # Check ~20 lines above the fn header for "Axiom:" in comments
            window_start = max(s - 20, 0)
            window = "\n".join(lines[window_start:s])
            if "Axiom:" in window or "/// Axiom" in window:
                reason = reason or "docstring contains 'Axiom:'"
        detail.append({
            "line": ln,
            "function": enc[0] if enc else None,
            "classification": "intentional" if reason else "hard",
            "reason": reason or "not flagged",
        })

    intentional = sum(1 for d in detail if d["classification"] == "intentional")
    return {
        "total": len(detail),
        "intentional": intentional,
        "hard": len(detail) - intentional,
        "detail": detail,
    }


def _iter_assistant_blocks(raw_out: Path) -> Iterator[dict]:
    """Yield each content block of every assistant message in a round jsonl.

    Shared by the round-stream counters below: both classify assistant
    tool_use/text blocks from `claude_raw/round_N.jsonl`. Malformed lines and
    non-dict blocks are skipped; yields nothing if the file is missing or
    unreadable.
    """
    try:
        with open(raw_out) as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("type") != "assistant":
                        continue
                    for c in (e.get("message", {}).get("content") or []):
                        if isinstance(c, dict):
                            yield c
                except (json.JSONDecodeError, AttributeError):
                    continue
    except OSError:
        return


def count_agent_actions(raw_out: Path) -> int:
    """Count productive agent actions in the round's raw event stream.

    Reads `claude_raw/round_N.jsonl` and counts assistant tool_uses and text
    blocks. Used as a productivity signal that survives SIGKILL (when the
    final `result` event — and thus `claude_usage` — is missing).

    Returns 0 if file missing or unreadable.
    """
    return sum(1 for c in _iter_assistant_blocks(raw_out)
               if c.get("type") in ("tool_use", "text"))


def count_agent_delegations(raw_out: Path) -> int:
    """Count `Agent` (subagent) tool-uses in the round's raw event stream.

    Reads `claude_raw/round_N.jsonl` and counts assistant tool_use blocks
    whose tool name is `Agent` (the literal name the subagent-spawning tool
    emits in the headless `claude -p` stream; the older `Task` wording is
    tolerated). This is the read-only-offload metric — it tells us whether
    the prompt's read-delegation framing actually induces delegation, which
    prompt-only encouragement historically never did (0 spawns across 7
    rounds; see docs/extension_spec.md E2). Returns 0 if file missing.
    """
    return sum(1 for c in _iter_assistant_blocks(raw_out)
               if c.get("type") == "tool_use"
               and c.get("name") in ("Agent", "Task"))


def snapshot_files(files: list[Path], dest_dir: Path) -> None:
    """Copy each file into dest_dir, preserving relative path structure.

    Used to record per-round state of target + sibling helpers so we can
    diff and surface what the previous round attempted.
    """
    import shutil
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f.exists():
            continue
        # Flatten by basename — files originate from different dirs so we
        # also include a short prefix to disambiguate. Keep the .rs suffix.
        out = dest_dir / f.name
        if out.exists():
            # Disambiguate by parent-dir name (e.g. elligator_lemmas.rs vs
            # axioms.rs both live under ristretto_lemmas/, but different
            # areas can collide too — use parent for safety).
            out = dest_dir / f"{f.parent.name}__{f.name}"
        shutil.copy2(f, out)


def _file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# Map a `lemmas/<area>_lemmas/...` sibling to the top-level module(s) that
# depend on that area. Used by the sibling-verify gate: when the agent edits
# a sibling helper, `--verify-module <TARGET>` won't re-check the top-level
# module that consumes it, so we re-verify these explicitly. Keep adjacent to
# LAYER_SETS in run_layer.py conceptually — add an entry if a new area lands.
_AREA_TOP_LEVEL: dict[str, list[str]] = {
    "field": ["field", "backend::serial::u64::field"],
    "scalar": ["scalar", "backend::serial::u64::scalar"],
    "edwards": ["edwards"],
    "montgomery": ["montgomery"],
    "ristretto": ["ristretto"],
}


def _area_top_level_modules(sib_path: Path) -> list[str]:
    """Top-level module(s) depending on the area of a `lemmas/<area>_lemmas`
    sibling. Returns [] for areas with no top-level consumer (e.g.
    common_lemmas) or paths outside a lemmas/ tree."""
    parts = sib_path.parts
    if "lemmas" not in parts:
        return []
    i = parts.index("lemmas")
    if i + 1 >= len(parts):
        return []
    comp = parts[i + 1]  # e.g. 'field_lemmas', 'scalar_lemmas.rs', 'edwards_lemmas'
    for area, mods in _AREA_TOP_LEVEL.items():
        if comp.startswith(area):
            return mods
    return []


_FN_HEADER_RE = re.compile(
    r"^[ \t]*(?:#\[[^\]]+\]\s*)*"
    r"(?:pub(?:\s*\([^)]+\))?\s+)?"
    r"(?:broadcast\s+)?"
    r"(?:open\s+|closed\s+)?"
    r"(?:proof|spec|exec)?\s*"
    r"fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)


def _fn_ranges_in_file(file_path: Path) -> list[tuple[str, int, int]]:
    """Return [(fn_name, start_line, end_line), ...] for a Rust file.

    Walks brace depth from each `fn` header to find the closing `}`.
    Used to filter diff hunks by enclosing function in the round-history
    block: hunks inside a fn that no longer contains `admit()` get
    dropped, because their verified state is already encoded in the file
    the agent reads fresh each round.

    Best-effort: skips fns the parser can't bracket-match cleanly.
    Handles `//` line comments, `/* */` block comments, and `"..."`
    string literals while walking. Char literals and lifetimes are not
    handled — Rust's `'a` lifetimes vs `'a'` char literals are ambiguous
    without a real lexer, so we ignore single-quote entirely.
    """
    try:
        text = file_path.read_text()
    except OSError:
        return []
    ranges: list[tuple[str, int, int]] = []
    for m in _FN_HEADER_RE.finditer(text):
        name = m.group("name")
        start_pos = m.start()
        start_line = text.count("\n", 0, start_pos) + 1
        # Find the first `{` after the header (skip requires/ensures/etc).
        # Then walk balanced braces to the matching `}`.
        #
        # `sig_depth` tracks `(...)`/`[...]` nesting so a `;` *inside* the
        # signature is not mistaken for a forward-declaration terminator. Rust
        # array types (`[u64; 5]`, `[u8; 32]`) embed a `;` — without this the
        # parser bailed on every fn with an array-typed param/return, which is
        # most of the field lemmas (e.g. all of mul_lemmas.rs).
        i = m.end()
        brace_start = -1
        sig_depth = 0
        in_str = False
        lc = False  # line comment
        bc = False  # block comment
        while i < len(text):
            c = text[i]
            if lc:
                if c == "\n":
                    lc = False
            elif bc:
                if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                    bc = False
                    i += 1
            elif in_str:
                if c == "\\" and i + 1 < len(text):
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                lc = True
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                bc = True
                i += 1
            elif c == '"':
                in_str = True
            elif c in "([":
                sig_depth += 1
            elif c in ")]":
                sig_depth -= 1
            elif c == "{" and sig_depth == 0:
                brace_start = i
                break
            elif c == ";" and sig_depth == 0:
                # Forward declaration (no body) — skip.
                brace_start = -2
                break
            i += 1
        if brace_start < 0:
            continue
        # Walk balanced braces from brace_start.
        depth = 1
        i = brace_start + 1
        in_str = False
        lc = False
        bc = False
        while i < len(text) and depth > 0:
            c = text[i]
            if lc:
                if c == "\n":
                    lc = False
            elif bc:
                if c == "*" and i + 1 < len(text) and text[i + 1] == "/":
                    bc = False
                    i += 1
            elif in_str:
                if c == "\\" and i + 1 < len(text):
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "/":
                lc = True
            elif c == "/" and i + 1 < len(text) and text[i + 1] == "*":
                bc = True
                i += 1
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        end_line = text.count("\n", 0, i) + 1
        ranges.append((name, start_line, end_line))
    return ranges


def _in_progress_fns(file_path: Path) -> set[str]:
    """Return set of fn names in `file_path` whose body still contains `admit()`."""
    try:
        text = file_path.read_text()
    except OSError:
        return set()
    if "admit()" not in text:
        return set()
    ranges = _fn_ranges_in_file(file_path)
    if not ranges:
        return set()
    # For each admit() occurrence, find enclosing fn by line number.
    lines = text.splitlines()
    admit_lines = [i + 1 for i, ln in enumerate(lines) if "admit()" in ln]
    out: set[str] = set()
    for ln in admit_lines:
        # Innermost fn containing ln (most-recently-started before ln).
        best: Optional[tuple[str, int, int]] = None
        for name, s, e in ranges:
            if s <= ln <= e:
                if best is None or s > best[1]:
                    best = (name, s, e)
        if best is not None:
            out.add(best[0])
    return out


_HUNK_HEADER_RE = re.compile(
    r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@",
    re.MULTILINE,
)


def _split_diff_into_hunks(diff_text: str) -> tuple[str, list[tuple[int, int, str]]]:
    """Split a unified-diff text into (header, [(new_start, new_end, hunk_text), ...]).

    The header is the leading `---`/`+++` lines (kept verbatim). Each hunk
    starts at a `@@ ... @@` line and runs until the next `@@` or EOF.
    new_start / new_end are 1-indexed line numbers in the NEW (post-edit)
    file, derived from the `+L,n` portion of the hunk header.
    """
    lines = diff_text.splitlines(keepends=True)
    # Header = lines before the first @@ header.
    first_hunk = None
    for i, ln in enumerate(lines):
        if ln.startswith("@@"):
            first_hunk = i
            break
    if first_hunk is None:
        # No hunks
        return diff_text, []
    header = "".join(lines[:first_hunk])
    hunks: list[tuple[int, int, str]] = []
    cur_start: Optional[int] = None
    cur_end: Optional[int] = None
    cur_buf: list[str] = []
    for ln in lines[first_hunk:]:
        m = _HUNK_HEADER_RE.match(ln)
        if m:
            if cur_buf:
                hunks.append((cur_start, cur_end, "".join(cur_buf)))
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) else 1
            cur_start = new_start
            cur_end = new_start + max(new_count - 1, 0)
            cur_buf = [ln]
        else:
            cur_buf.append(ln)
    if cur_buf:
        hunks.append((cur_start, cur_end, "".join(cur_buf)))
    return header, hunks


def _enclosing_fn(ranges: list[tuple[str, int, int]], line: int) -> Optional[str]:
    """Return innermost fn name containing `line`, or None."""
    best: Optional[tuple[str, int, int]] = None
    for name, s, e in ranges:
        if s <= line <= e:
            if best is None or s > best[1]:
                best = (name, s, e)
    return best[0] if best else None


def _filter_diff_to_in_progress(
    diff_text: str,
    fn_ranges: list[tuple[str, int, int]],
    in_progress_fns: set[str],
) -> str:
    """Drop diff hunks whose enclosing fn is fully verified. Keep hunks
    whose enclosing fn is admit-bearing or undeterminable. Returns the
    filtered diff text (empty string if nothing survives)."""
    header, hunks = _split_diff_into_hunks(diff_text)
    if not hunks:
        return diff_text
    kept_blobs: list[str] = []
    for new_start, new_end, blob in hunks:
        # Probe a few lines spanning the hunk to find an enclosing fn.
        probes = [new_start, (new_start + new_end) // 2, new_end]
        encl = None
        for p in probes:
            encl = _enclosing_fn(fn_ranges, p)
            if encl is not None:
                break
        if encl is None:
            # Outside any fn (e.g. impl-level, module-level) — keep.
            kept_blobs.append(blob)
            continue
        if encl in in_progress_fns:
            kept_blobs.append(blob)
        # else: drop — fn no longer has admit(), verified work
    if not kept_blobs:
        return ""
    return header + "".join(kept_blobs)


def _loc_in_target(msg_file: str, target: Path) -> bool:
    """True if a Verus diagnostic location (`curve25519-dalek/src/.../x.rs`,
    relative to the cargo workspace root) points at `target`. Verus prints
    workspace-relative paths; `target` is whatever run.py was handed. Match by
    path suffix in either direction so a bare basename collision (two
    `mul_lemmas.rs` in different dirs) does not produce a false hit."""
    if not msg_file:
        return False
    mf = msg_file.replace("\\", "/")
    try:
        tp = str(target.resolve()).replace("\\", "/")
    except OSError:
        tp = str(target).replace("\\", "/")
    return tp.endswith(mf) or mf.endswith(tp)


def _extract_near_miss(target: Path, failed_decls: list[str],
                       error_locs: Optional[list[tuple[str, int]]] = None,
                       max_decls: int = 3, max_lines_per: int = 70,
                       ) -> tuple[list[str], str]:
    """Feature 1 — pull the source of the declarations Verus rejected, from
    the target file as it stands at the end of a failed run (the agent's
    near-miss attempt). Stored into failure memory so the next attempt starts
    from the code that almost worked rather than from raw stderr alone.

    Returns `(resolved_fn_names, source)`. Resolution is best-effort against
    `_fn_ranges_in_file`:
      1. exact / last-`::`-segment name match on `failed_decls`;
      2. line-number fallback — map each Verus error location in `target` to
         its enclosing fn.
    The fallback matters because on current Verus the parsed
    `failed_declarations` are unusable for this (a precondition/postcondition
    failure prints a `file:line` location but no fn name in backticks, so the
    name regex captures the crate name or nothing). The reliable signal is the
    error line, which `_fn_ranges_in_file` brackets back to a fn.

    Returns `([], "")` when nothing maps to a parseable fn — never wrong code."""
    try:
        lines = target.read_text().splitlines()
    except OSError:
        return [], ""
    fn_ranges = _fn_ranges_in_file(target)               # [(name, s, e), ...]
    ranges = {name: (s, e) for name, s, e in fn_ranges}
    ordered: list[str] = []                              # resolved names, in order
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)

    # 1. name match (handles Verus builds / cases that do emit a fn name)
    for decl in failed_decls or []:
        short = decl.split("::")[-1]
        _add(decl if decl in ranges else (short if short in ranges else ""))
    # 2. line-number fallback (the reliable path on current Verus output)
    for mf, ln in (error_locs or []):
        if not ln or not _loc_in_target(mf, target):
            continue
        for name, s, e in fn_ranges:
            if s <= ln <= e:
                _add(name)
                break

    chunks: list[str] = []
    names: list[str] = []
    for name in ordered:
        s, e = ranges[name]
        body = lines[s - 1:e]
        if len(body) > max_lines_per:
            body = body[:max_lines_per] + ["    // ... (truncated)"]
        chunks.append("\n".join(body))
        names.append(name)
        if len(chunks) >= max_decls:
            break
    return names, "\n\n".join(chunks)


def build_round_history_block(
    tdir: Path, round_num: int, max_recent_rounds: int = 2,
    target: Optional[Path] = None, since_round: int = 1,
) -> str:
    """Render a markdown block summarizing the previous 1-2 rounds:
    file diffs and verus_check errors. Empty string before round 2.

    Reads:
      - tdir / "snapshots" / "round_<N-1>" / *.rs  (snapshot at end of round N-1)
      - tdir / "snapshots" / "round_<N-2>" / *.rs  (snapshot at end of round N-2, or "round_0" for pre-round-1 baseline)
      - tdir / "round_<N>.json"                    (verus + spec results)
    """
    if round_num <= 1:
        return ""

    import difflib
    snapshots_root = tdir / "snapshots"
    # Clip history range to current session (post-reset) and the recent
    # window. since_round=1 means "all rounds so far"; reset bumps it.
    start_inclusive = max(since_round, round_num - max_recent_rounds, 1)
    history_rounds = list(range(start_inclusive, round_num))

    # Compute the in-progress fn set + line ranges from the LIVE target.
    # Used to drop diff hunks for fully-verified fns (Lever 1).
    in_progress: set[str] = set()
    fn_ranges: list[tuple[str, int, int]] = []
    target_name: Optional[str] = None
    if target is not None and target.exists():
        in_progress = _in_progress_fns(target)
        fn_ranges = _fn_ranges_in_file(target)
        target_name = target.name

    sections: list[str] = []
    for r in history_rounds:
        prev_dir = snapshots_root / f"round_{r - 1}"  # state at END of round r-1 (= start of round r)
        cur_dir = snapshots_root / f"round_{r}"      # state at END of round r
        if not (prev_dir.exists() and cur_dir.exists()):
            continue
        round_json = tdir / f"round_{r}.json"
        verus_okay = None
        verus_errors: list[str] = []
        end_reason = None
        try:
            rr = json.loads(round_json.read_text())
            verus_okay = rr.get("verus_okay")
            end_reason = rr.get("end_reason")
            verus_errors = [
                m.get("message", "")[:400] if isinstance(m, dict) else str(m)[:400]
                for m in (rr.get("verus_errors") or [])[:5]
            ]
        except (OSError, json.JSONDecodeError):
            pass

        # Diff each pair of files present in both dirs
        file_diffs: list[str] = []
        all_filtered_empty = True
        cur_files = sorted(cur_dir.glob("*.rs"))
        for cf in cur_files:
            pf = prev_dir / cf.name
            if not pf.exists():
                continue
            try:
                a = pf.read_text().splitlines(keepends=True)
                b = cf.read_text().splitlines(keepends=True)
            except OSError:
                continue
            diff = list(difflib.unified_diff(
                a, b, fromfile=f"round_{r-1}/{cf.name}",
                tofile=f"round_{r}/{cf.name}", n=3,
            ))
            if not diff:
                continue
            blob = "".join(diff)
            # Lever 1 filter: for the target file, drop hunks whose
            # enclosing fn no longer has admit() (verified). Sibling
            # files keep all hunks (new helper lemmas are usually small
            # and relevant to active work). When target_name is unset
            # (no target passed in), no filtering happens.
            if target_name is not None and cf.name == target_name and fn_ranges:
                filtered = _filter_diff_to_in_progress(blob, fn_ranges, in_progress)
                if filtered:
                    blob = filtered
                    all_filtered_empty = False
                else:
                    # Whole file's diff filtered to verified-work-only — skip.
                    continue
            else:
                all_filtered_empty = False
            # Cap each file's diff at ~3000 chars to bound prompt growth
            if len(blob) > 3000:
                blob = blob[:3000] + "\n... (diff truncated, full state on disk)\n"
            file_diffs.append(blob)

        # Filter verus errors to those pointing at in-progress fns (for
        # the target file). Sibling-file errors are always kept.
        if target_name is not None and fn_ranges:
            def _err_relevant(e: str) -> bool:
                # `e` is a stringified error. Try to parse "file:line".
                m = re.search(r"([\w./-]+):(\d+)", e)
                if not m:
                    return True
                fname, lstr = m.group(1), m.group(2)
                if not fname.endswith(target_name):
                    return True  # sibling/other-file error: keep
                line = int(lstr)
                encl = _enclosing_fn(fn_ranges, line)
                return encl is None or encl in in_progress
            verus_errors = [e for e in verus_errors if _err_relevant(e)]

        # Render section. If file_diffs is empty AFTER filtering AND
        # there were edits originally, note that. Otherwise standard
        # "no edits" message.
        filter_applied = target_name is not None and bool(fn_ranges)
        diff_note: str
        if file_diffs:
            diff_note = (
                "Edits in this round (filtered to in-progress fns):"
                if filter_applied else
                "Edits in this round:"
            )
        elif all_filtered_empty and filter_applied:
            diff_note = "_Edits this round were inside now-verified fns; nothing in-progress to show._"
        else:
            diff_note = "_No file edits in this round_"

        sections.append("\n".join([
            f"### Round {r} — verus_okay={verus_okay}, end_reason={end_reason}",
            diff_note,
            *([f"```diff\n{d}```" for d in file_diffs] if file_diffs else []),
            ("Verus errors (first 5):" if verus_errors else ""),
            *([f"```\n{e}\n```" for e in verus_errors] if verus_errors else []),
            "",
        ]))

    if not sections:
        return ""

    return "\n".join([
        "## Round history (last {} round(s))".format(len(sections)),
        "",
        "What follows is a diff of YOUR previous edits and the Verus errors",
        "that resulted. If a round's edits failed `verus_check` and were",
        "reverted, do NOT repeat the same approach — either define the",
        "missing helper (in the target file, or in a sibling per rule 4),",
        "or try a different decomposition.",
        "",
        *sections,
    ])


def build_experiment_block(
    target: Path, allow_edit: list[Path], mode: str = "spec-proof"
) -> str:
    """Render the experiment-mode prompt addendum. Empty string if not in
    experiment mode (no allow_edit paths).

    `mode` selects which experimental setup the agent is in:
      - "spec-proof": dep fns have no Verus specs; agent infers
        requires/ensures/decreases AND adds proof scaffolding. Helper
        lemmas may be added.
      - "proof-only": specs are fixed; agent only adds proof scaffolding
        inside existing fn/lemma signatures. No new lemmas, no new
        axioms, no new lemma skeletons.
    """
    if not allow_edit:
        return ""
    bullets = "\n".join(f"- `{p}`" for p in allow_edit)
    if mode == "proof-only":
        return f"""## EXPERIMENT MODE — read this first

This is constrained admit-filling. The standard rules below mostly
apply; this section adjusts them.

**Anchor:** `{target}` — read-only. Specs and proof body intact.

**Dependency files (you edit these — proof scaffolding only):** these
files compile but verus_check fails on them. Postconditions don't
follow from bodies, callsite preconditions aren't established, loops
have no decreases.

{bullets}

**All Rust code is correct as written, and all Verus specs (fn
headers, lemma signatures, `requires` / `ensures` / `decreases`) are
complete and fixed.** Your edits only add proof scaffolding inside fn
bodies — loop `invariant`s and loop-level `decreases`, `assert(...)`,
`assert(...) by (existing_lemma(...))`, `proof {{ ... }}` blocks, ghost
bindings.

**Your job:** add the proof scaffolding needed to close the existing
specs. Run `verus_check` on each dep file; the errors point at every
missing piece.

**Hard constraints — work within the existing skeleton:**
- **No new `proof fn` declarations.** No new helper lemmas, no new
  lemma skeletons (even with `admit()` body), no new axioms (no new
  `proof fn axiom_*`). The lemma library is exactly what's currently
  on disk — use it, don't extend it.
- **No fn-header changes anywhere.** Do not modify any `requires` /
  `ensures` / `decreases`, on any fn. Spec drift fails the round.
- **Do not modify any Rust code.** Exec bodies, exec fn signatures,
  types, and imports are correct as written.
- No `admit()`, no `assume(...)`, no `#[verifier::external_body]`.
- Edit only the dep files listed above.

**If a postcondition appears unprovable from the body within the
existing lemma library, emit `END_REASON:LIMIT`** — do not relax the
spec, do not add an axiom, do not invent a helper.

**Verification:** run `verus_check` on the anchor AND on each dep file
separately. `END_REASON:COMPLETE` only when every check passes.
"""
    # default: spec-proof
    return f"""## EXPERIMENT MODE — read this first

This is a spec-reconstruction experiment, not the usual admit-filling
workflow. Read this section before the standard rules below; where they
conflict, this section wins.

**Anchor:** `{target}` — read-only. It has the top-level pub fn's
`requires` / `ensures` (the user-visible API contract). This and the
standard library specs are the only Verus specs you may treat as given.

**Dependency files (you edit these — Verus annotations only):**
intermediate functions in the call chain below the anchor have no
Verus specs — bare Rust fn headers, with bodies that compile but do
not verify.

{bullets}

**All Rust code in these files is correct as written.** Do not modify
any exec body, exec fn signature, type definition, or `use` import.
Your edits only add or modify Verus annotations: `requires` / `ensures`
/ `decreases` on fn headers, loop `invariant`s and loop-level
`decreases`, `assert(...)`, `assert(...) by (...)`, `proof {{ ... }}`
blocks, ghost bindings, and helper `proof fn lemma_*` declarations.

**Your job:** given (i) the anchor's contract, (ii) the Rust bodies in
front of you, and (iii) the standard library spec vocabulary, infer
`requires` / `ensures` / `decreases` on each intermediate function —
strong enough that the anchor's proof discharges, weak enough that the
function body proves them. Add the proof scaffolding needed to close
those specs.

**You have full creative freedom over proof structure.** All Verus
proof forms are permitted: helper lemmas, broadcast use, assert-by,
reveal_with_fuel, ghost variables, custom lemma libraries.
**Helper-lemma refactoring for maximum reuse is strongly encouraged.**
If multiple obligations share an algebraic identity, lift it into a
reusable lemma and reuse.

**Rules (override the standard rules below):**
- Edit only the dependency files listed above. The anchor and unrelated
  siblings stay untouched.
- Do not modify any Rust code in the dep files — exec bodies, exec fn
  signatures, types, and imports are correct as written.
- No `admit()`, `assume(...)`, or `#[verifier::external_body]`.
- The in-loop spec-integrity gate is disabled for this run. Post-hoc
  analysis compares your specs to alternative phrasings.
- **Run `verus_check` on the anchor AND on each dependency file
  separately** — `--verify-only-module` is per-module, so checking only
  the anchor won't surface errors inside a dep fn's proof body. All
  checks must return `okay: true` before declaring done.
- `END_REASON:COMPLETE` only when every check passes.
"""


def run_subskill(cmd: list[str], env: dict, cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Run a skill CLI; capture stdout/stderr as strings."""
    proc = subprocess.run(
        cmd, env=env, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ----------------- the round -----------------

# Module-level handle to the live claude subprocess so a SIGTERM to run.py
# can propagate the kill to the whole process group. Without this, killing
# run.py orphans claude and any subprocesses it spawned (cargo verus, z3,
# Monitor poll loops, ...).
_LIVE_PROC: Optional[subprocess.Popen] = None

# Module-level handle to the optional --wire-log proxy (wire_proxy.py). Tracked
# so it is killed on signal and at interpreter exit — otherwise its serve_forever
# loop would outlive run.py as an orphan, exactly like the claude tree above.
_WIRE_PROC: Optional[subprocess.Popen] = None


def _install_signal_handler() -> None:
    import signal as _signal
    import os

    def _handler(signum, _frame):
        global _LIVE_PROC
        proc = _LIVE_PROC
        if proc is not None and proc.poll() is None:
            print(f"\n[run] received signal {signum} — killing claude process group {proc.pid}",
                  flush=True)
            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass
        _kill_wire_proxy()
        # Re-raise default behavior so run.py exits
        raise SystemExit(128 + signum)

    for sig in (_signal.SIGTERM, _signal.SIGINT, _signal.SIGHUP):
        _signal.signal(sig, _handler)


def _start_wire_proxy(claude_raw_dir: Path, env: dict) -> None:
    """Route the claude subprocess through a localhost logging proxy via
    ANTHROPIC_BASE_URL — the only API-capture method that works with the
    native-binary claude (claude-trace's JS-patching approach is dead on v2.x).

    Best-effort: on ANY failure we warn and leave `env` untouched, so the run
    proceeds normally straight to api.anthropic.com — wire logging must never
    fail a proof round. The proxy writes claude_raw/wire_{prefixes,requests}.jsonl
    (full system prompt + tool schemas once, then per-turn message deltas).
    """
    global _WIRE_PROC
    import atexit
    import socket
    import time
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        claude_raw_dir.mkdir(parents=True, exist_ok=True)
        log = open(claude_raw_dir / "wire_proxy.log", "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(HERE / "wire_proxy.py"), str(port),
             str(claude_raw_dir)],
            stdout=log, stderr=subprocess.STDOUT,
        )
        for _ in range(100):                      # wait for the listener to bind
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("listener did not bind within ~5s")
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        _WIRE_PROC = proc
        atexit.register(_kill_wire_proxy)
        print(f"[run] --wire-log: proxy on 127.0.0.1:{port} -> "
              f"{claude_raw_dir}/wire_*.jsonl", flush=True)
    except Exception as e:
        print(f"[run] --wire-log: could not start proxy ({e}); continuing "
              f"without wire logging", flush=True)


def _kill_wire_proxy() -> None:
    global _WIRE_PROC
    proc = _WIRE_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    _WIRE_PROC = None


def run_claude_round(
    prompt: str,
    cwd: Path,
    env: dict,
    raw_out: Path,
    session_id: str,
    resume: bool,
    model: Optional[str] = None,
    deadline_seconds: Optional[float] = None,
    continue_message: Optional[str] = None,
) -> tuple[Optional[str], int, dict]:
    """Invoke `claude -p` (fresh session pinned to `session_id`) or
    `claude --resume <session_id> -p` (continue THAT specific session).
    Stream NDJSON to `raw_out`. Parse END_REASON from the final
    `type:"result"` line. Return (end_reason, returncode, claude_result_dict).

    The session is identified explicitly by UUID rather than via `-c`'s
    "most recent session in this directory" lookup. `-c` is mtime-based
    and globally scoped to the OAuth user, so a concurrent interactive
    Claude Code session in the same project dir would always win the
    tiebreaker and quietly hijack the harness's continuation rounds.
    See: investigation of curve_eq_20260518 — 6 of 10 rounds were
    re-routed to the user's interactive session because of that.

    `deadline_seconds`: if set, SIGKILL the entire process group when the
    deadline expires. Catches the case where the agent spawns background
    subprocesses (Monitor + sleep loops, async cargo verus + pkill chains)
    that hold the claude -p process alive forever.

    `start_new_session=True`: puts claude (+ all its descendants) in a
    fresh process group so we can kill the whole tree at once.
    """
    import os, signal as _signal
    if resume:
        cmd = ["claude", "--resume", session_id, "-p",
               "--verbose", "--output-format", "stream-json",
               "--permission-mode", "bypassPermissions",
               *AGENT_TOOL_FLAGS]
        if model:
            cmd += ["--model", model]
        # The trailing arg becomes the next user message. Default to a
        # bare "continue"; callers may pass a richer message (e.g.
        # structured round-history feedback) to nudge the agent.
        cmd += [continue_message or "continue"]
    else:
        cmd = ["claude", "-p", "--session-id", session_id,
               "--verbose", "--output-format", "stream-json",
               "--permission-mode", "bypassPermissions",
               *AGENT_TOOL_FLAGS]
        if model:
            cmd += ["--model", model]
        cmd += [prompt]

    raw_out.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run] claude subprocess → {raw_out}", flush=True)
    global _LIVE_PROC
    with open(raw_out, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(
            cmd, stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, env=env, cwd=str(cwd),
            start_new_session=True,
        )
        _LIVE_PROC = proc
        # Enforce the deadline against the WALL clock (time.time), polling in
        # short slices. proc.wait(timeout=...) alone counts down against
        # time.monotonic(), which freezes during macOS sleep — once let a
        # round run 7.8h past a 90-min budget on a sleeping laptop.
        wall_deadline = (time.time() + deadline_seconds) if deadline_seconds else None
        while True:
            try:
                proc.wait(timeout=(30 if wall_deadline else None))
                break
            except subprocess.TimeoutExpired:
                if wall_deadline and time.time() >= wall_deadline:
                    print(f"[run] deadline ({deadline_seconds:.0f}s) exceeded — "
                          f"killing claude process group {proc.pid}", flush=True)
                    try:
                        os.killpg(proc.pid, _signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    break
        # Post-completion cleanup: claude may have left background bash
        # children (Monitor poll loops, etc.). If anything is still alive
        # after the main process returned, kill the whole group.
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        _LIVE_PROC = None

    # Parse the final result line.
    last = ""
    try:
        with open(raw_out, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    last = line.strip()
    except OSError:
        pass

    result_dict: dict = {}
    end_reason: Optional[str] = None
    if last:
        try:
            parsed = json.loads(last)
            if parsed.get("type") == "result":
                result_dict = parsed
                text = parsed.get("result", "")
                m = END_REASON_RE.search(text)
                if m:
                    end_reason = m.group(1).upper()
        except json.JSONDecodeError:
            pass

    return end_reason, proc.returncode, result_dict


# ----------------- the task -----------------

def run_task(
    target: Path,
    project: Path,
    run_id: str,
    results_root: Path,
    max_rounds: int,
    model: Optional[str] = None,
    vstd_root: Optional[Path] = None,
    admitted_ref: Optional[str] = None,
    truth_ref: str = "main",
    max_task_minutes: float = 45.0,
    round_minutes: Optional[float] = None,
    only_fns: Optional[list[str]] = None,
    skip_failure_memory: bool = False,
    verus_rlimit: Optional[float] = None,
    auto_reset: bool = True,
    max_auto_resets: int = 3,
    stall_max_duration_sec: float = 180.0,
    bloat_threshold_tokens: int = 200_000,
    experiment_allow_edit: Optional[list[Path]] = None,
    experiment_mode: str = "spec-proof",
    no_spec_gate: bool = False,
    wire_log: bool = False,
    sibling_verify: bool = True,
) -> TaskResult:
    target = target.resolve()
    project = project.resolve()
    module = module_path_of(target, project)
    target_id = results.target_id_from_path(target)
    tdir = task_dir(results_root, run_id, target_id)
    catalog_cache = results_root / "catalog_cache.json"

    # Scratch cwd for the claude subprocess, built once and reused for every
    # round of this task (stable cwd → stable session-project slug for
    # --resume). Keeps HERE's CLAUDE.md out of the agent's context. See
    # _make_agent_cwd for why it's a fresh per-task dir, not a shared global.
    agent_cwd = _make_agent_cwd(target_id)

    # Per-task isolated CLI log
    env = os.environ.copy()
    env["CLI_LOG_PATH"] = str(tdir / "cli.log")

    # When running under Claude Code (CLAUDECODE=1), strip ANTHROPIC_API_KEY
    # so the spawned `claude -p` subprocess falls back to the user's logged-in
    # session auth. Otherwise an inherited (possibly stale) env-var key gets
    # rejected and every round fails with "Invalid API key" instantly.
    if env.get("CLAUDECODE") == "1":
        env.pop("ANTHROPIC_API_KEY", None)

    # --wire-log: capture the full API request bodies (system prompt + tool
    # schemas + skills + per-turn context growth) that stream-json never sees,
    # by routing claude through a localhost logging proxy. Subagents inherit
    # ANTHROPIC_BASE_URL via env, so their traffic is captured too. Best-effort.
    if wire_log:
        _start_wire_proxy(tdir / "claude_raw", env)

    # Discover sibling helpers in scope (rule 4 relaxation: the agent may
    # append new lemmas to siblings under lemmas/<area>_lemmas/). Empty
    # for tasks whose target has no recognized helper area.
    siblings: list[Path] = []
    try:
        rc_sib, sib_stdout, _ = run_subskill(
            [sys.executable, str(HERE / "skills" / "spec_check.py"),
             "list-siblings", str(target), "--project", str(project)],
            env=env,
        )
        if rc_sib == 0:
            siblings = [Path(p) for p in json.loads(sib_stdout).get("siblings", [])]
    except (json.JSONDecodeError, OSError):
        siblings = []

    if siblings:
        print(f"[run] siblings in scope ({len(siblings)}):", flush=True)
        for s in siblings:
            print(f"[run]   {s}", flush=True)

    # Snapshot specs (baseline for integrity gate) — covers target + siblings
    spec_snapshot = tdir / "spec_snapshot.json"
    snap_cmd = [sys.executable, str(HERE / "skills" / "spec_check.py"),
                "snapshot", str(target), "--out", str(spec_snapshot)]
    if siblings:
        snap_cmd += ["--siblings"] + [str(s) for s in siblings]
    rc, _, _ = run_subskill(snap_cmd, env=env)
    if rc != 0:
        return TaskResult(
            task_id=target_id, run_id=run_id, target_path=str(target),
            module_path=module, success=False, end_reason="ERROR",
            rounds_used=0, duration_seconds=0.0,
            error_message="spec_check snapshot failed",
        )

    # Pull prior failures → prompt block. Skippable for runs where prior
    # records predate prompt/harness improvements and would prime the
    # agent to give up.
    if skip_failure_memory:
        prior = []
        print("[run] failure_memory: SKIPPED (--no-failure-memory)", flush=True)
    else:
        prior = failure_memory.query(results_root, module, target_id)
    failure_block = failure_memory.as_prompt_block(prior)

    # Feature 3: prepend the cross-round discovery brief (files/searches a
    # prior attempt on this target already explored) so the retry doesn't
    # re-walk the tree. Injected regardless of failure-memory skip.
    brief_block = discovery_brief.load_block(results_root, target_id)
    if brief_block:
        print("[run] Feature3: injecting prior discovery brief", flush=True)
        failure_block = (
            "### Prior exploration map (discovery brief)\n\n"
            + brief_block + "\n\n" + failure_block
        )

    # Feature2 — escalation retry. If a prior attempt on this target declared
    # END_REASON:NEEDS_DECOMP ("this proof needs missing infrastructure"), give
    # the retry more room and a directive to build that infrastructure FIRST.
    # The escalation is "surprisingly informative" (AutoformBot): it tells us
    # the bottleneck is a missing lemma/chain, not merely a hard-but-tractable
    # proof, so widening the budget and front-loading the build is the right
    # response. NOTE: this mutates max_rounds / max_task_minutes BEFORE the
    # round loop reads them (first reads at the `for round_num` loop and the
    # remaining-budget calc, both well below here), so the bump takes effect.
    # Prepended AFTER the discovery brief so the build-first directive lands at
    # the very top, then the exploration map, then the raw failure records.
    prior_decomp = [r for r in prior
                    if (r.end_reason or "").upper() == "NEEDS_DECOMP"]
    if prior_decomp:
        max_rounds += 2
        max_task_minutes *= 1.5
        directive = (
            "## Escalation follow-up — prior attempt declared NEEDS_DECOMP\n\n"
            "A prior attempt escalated this target as needing **missing "
            "infrastructure** (a helper lemma / lemma-chain that does not "
            "exist, or a sub-lemma split). **Build that infrastructure FIRST**: "
            "define the missing helper lemma(s) in the target or a sibling "
            "`lemmas/<area>_lemmas/*.rs` file, verify them in isolation, THEN "
            "use them to discharge the admit(s). Read the prior error(s) below "
            "for what was reported missing. Do NOT re-escalate without having "
            "attempted to build the named infrastructure.\n"
        )
        failure_block = directive + ("\n" + failure_block if failure_block else "")
        print(f"[run] Feature2: prior NEEDS_DECOMP on {target_id} "
              f"({len(prior_decomp)} record(s)) — retry budget bumped to "
              f"rounds={max_rounds}, max_task_minutes={max_task_minutes:.0f} "
              f"+ build-infrastructure-first directive prepended", flush=True)

    experiment_block = build_experiment_block(
        target, experiment_allow_edit or [], mode=experiment_mode,
    )
    # Conditionally inject the "Decompose hard admits" guidance only for hard
    # targets, so easy targets don't carry its ~57 eager lines every round.
    if target_needs_decompose(target):
        decompose_block = DECOMPOSE_TEMPLATE.read_text().rstrip()
        print("[run] injecting Decompose-hard-admits guidance (hard target)",
              flush=True)
    else:
        decompose_block = ""
        print("[run] Decompose guidance omitted (no hard function detected)",
              flush=True)
    prompt = render_prompt(
        target=target, project=project, module=module,
        spec_snapshot=spec_snapshot, catalog_cache=catalog_cache,
        results_root=results_root, failure_block=failure_block,
        vstd_root=vstd_root, experiment_block=experiment_block,
        decompose_block=decompose_block,
    )

    # Save the rendered prompt for reproducibility
    (tdir / "prompt_rendered.md").write_text(prompt)

    # Per-round file snapshots so we can diff "what the previous round
    # tried" and surface it back to the agent. "round_0" captures the
    # baseline before any agent edits.
    snapshots_root = tdir / "snapshots"
    snapshot_files([target, *siblings], snapshots_root / "round_0")

    start = datetime.now()
    round_results: list[RoundResult] = []
    end_reason: Optional[str] = None
    last_verus_err = ""
    last_failed_decls: list[str] = []   # Feature 1: decls verus rejected last round
    last_failed_locs: list[tuple[str, int]] = []  # Feature 1: (file, line) of those errors
    # Continuation message used on round 2+. Updated below when the
    # previous round's COMPLETE was rejected, so the agent sees the
    # specific reason (verus failure or admits remaining) prepended to
    # the round-history block instead of silently re-trying the same path.
    next_continue_msg = "continue"

    # Lever 2 — auto-reset bookkeeping. Default values mean no reset;
    # populated and consumed by the after-round decision below.
    session_start_round = 1
    fresh_next_round = False
    auto_resets_used = 0
    session_cc_tokens = 0
    reset_round_starts: list[int] = []
    # Last round number whose end-state was verus_okay (used as the
    # rollback target if the budget exhausts mid-fix and leaves the
    # file in a broken state). 0 = the pre-round baseline.
    last_good_snapshot_round = 0

    # Explicit session id for this task. Used with `--session-id <uuid>` on
    # the first round (pins the new session to this UUID) and `--resume
    # <uuid>` on subsequent rounds (continues exactly this session, not
    # whatever `claude -c` picks as "most recent"). Regenerated on each
    # Lever 2 auto-reset.
    task_session_id = str(uuid.uuid4())

    def _admit_count() -> int:
        try:
            text = target.read_text()
        except OSError:
            return -1
        # --only-fns (decomposition sub-target): the COMPLETE gate counts admits
        # only inside this group's fns; the other fns stay admitted and supply
        # their `ensures` to callers (design §A5.2). Otherwise count file-wide.
        if only_fns:
            return _count_admits_in_fns(text, only_fns)
        return _count_llm_target_admits(text)

    # Axiom-integrity gate: snapshot the set of `proof fn axiom_*` names
    # across every file the agent may touch (target + siblings + any
    # experiment allow-edit deps). The COMPLETE counter excludes admits
    # inside axiom_* bodies, so a NEW axiom_* is a fake-green vector — the
    # agent could discharge a proof obligation through a fresh
    # `proof fn axiom_cheat() { admit() }`. Any name not in this baseline,
    # appearing later, fails the round like spec drift.
    axiom_scope_files = [target, *siblings, *(experiment_allow_edit or [])]

    def _axiom_names() -> set[str]:
        names: set[str] = set()
        for f in axiom_scope_files:
            try:
                names |= axiom_fn_names(f.read_text())
            except OSError:
                pass
        return names

    baseline_axioms = _axiom_names()

    # Tooling-integrity gate: the harness's own verification skills are
    # re-read from disk every round — verus_check.py / spec_check.py run as
    # subprocesses in the round loop below, and the agent invokes the rest via
    # Bash. The proof agent reaches THIS repo's skills/ + lib/ through symlinks
    # in its scratch cwd (see _make_agent_cwd) and holds Edit/Write/Bash under
    # `--permission-mode bypassPermissions`, so it CAN rewrite a skill to always
    # return okay=true — the symlinks point at the real files, and this gate
    # hashes those same absolute HERE paths, so the edit is still caught. It is
    # an undetected fake-green vector otherwise: the
    # same cheat class the spec- and axiom-integrity gates exist to stop. (A
    # verus_check.py edit actually happened mid-run on this branch — that one
    # was a correct fix, but the hole is real.) Snapshot a content hash of
    # every tooling file under skills/ + lib/ at run start and diff after each
    # round; any add / edit / delete fails the round like SPEC_DRIFT. Note
    # tool-scoping (AGENT_TOOL_FLAGS) can't close this on its own: Bash is a
    # write primitive and `--allowedTools` is a no-op under bypassPermissions.
    def _tooling_digest() -> dict[str, str]:
        digest: dict[str, str] = {}
        for f in [*HERE.glob("skills/**/*.py"), *HERE.glob("lib/**/*.py")]:
            if "__pycache__" in f.parts:
                continue
            rel = str(f.relative_to(HERE))
            try:
                digest[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                digest[rel] = "MISSING"
        return digest

    baseline_tooling = _tooling_digest()

    def _tooling_changed() -> list[str]:
        """Tooling files (skills/ + lib/) whose content differs from the
        pre-run baseline — added, edited, or deleted. Empty = intact."""
        current = _tooling_digest()
        return sorted(
            k for k in (baseline_tooling.keys() | current.keys())
            if baseline_tooling.get(k) != current.get(k)
        )

    # Sibling-verify gate: track each sibling's content hash so we can
    # re-verify only the ones the agent actually modified each round.
    sibling_hashes: dict[str, str] = {}
    for s in siblings:
        try:
            sibling_hashes[str(s)] = _file_hash(s)
        except OSError:
            pass

    for round_num in range(1, max_rounds + 1):
        print("=" * 60, flush=True)
        print(f"[run] round {round_num}/{max_rounds}", flush=True)
        admits_start = _admit_count()
        round_start = time.time()
        raw_out = tdir / "claude_raw" / f"round_{round_num}.jsonl"

        # cwd = agent_cwd (a scratch dir outside the repo, symlinking skills/ +
        # lib/) so Claude Code does not inject HERE's CLAUDE.md; the prompt's
        # relative `skills/*.py` invocations resolve through the symlink. The
        # agent edits the target via absolute path; skills receive absolute
        # --project to find the Cargo root.
        # Compute remaining budget (wall-clock cap distributed across rounds).
        # If less than one productive round (~60s) remains, stop the loop
        # *before* invoking claude — otherwise we'd hand it a 60s deadline,
        # SIGKILL it, record a phantom rc=-9 / output_tokens=0 round, and
        # come back to do the same thing again next iteration. (Pre-fix
        # those zombie rounds ran past `max_task_minutes` and polluted
        # `round_results`; see docs/diagnostics.md.)
        elapsed = (datetime.now() - start).total_seconds()
        remaining_s = max_task_minutes * 60 - elapsed
        if remaining_s < 60.0:
            print(f"[run] budget exhausted "
                  f"(elapsed={elapsed:.0f}s ≥ {max_task_minutes*60:.0f}s, "
                  f"remaining={remaining_s:.0f}s < 60s) — stopping loop",
                  flush=True)
            break

        # Bail-out guard: if remaining budget is <5 min, no productive
        # agent work is possible (claude will be SIGKILL'd at ~60s by
        # the deadline, the floor). Two sub-cases:
        #
        #   (a) Current file state is broken (verus_okay=False on last
        #       round). Roll back to the latest verus_okay snapshot so
        #       we don't leave the file worse than admitted-start.
        #   (b) Current file state is clean. No rollback needed; just
        #       break out of the loop. Avoids burning N×60s rounds at
        #       end-of-budget with zero productive work.
        budget_exhausted = (max_task_minutes * 60 - elapsed) < 5 * 60
        last_verus_failed = bool(round_results) and not round_results[-1].verus_okay
        if budget_exhausted:
            if last_verus_failed:
                snap_dir = snapshots_root / f"round_{last_good_snapshot_round}"
                rollback_files = [target, *siblings]
                if snap_dir.exists():
                    print(f"[run] budget exhausted with broken file state — "
                          f"rolling back to snapshots/round_{last_good_snapshot_round}/",
                          flush=True)
                    import shutil
                    for f in rollback_files:
                        snap_f = snap_dir / f.name
                        if not snap_f.exists():
                            snap_f = snap_dir / f"{f.parent.name}__{f.name}"
                        if snap_f.exists():
                            shutil.copy2(snap_f, f)
                else:
                    print(f"[run] budget exhausted with broken file state, but no "
                          f"snapshot to roll back to — file remains broken.",
                          flush=True)
                print(f"[run] bailing out of round loop early (budget < 5 min, "
                      f"file not verus_okay). end_reason=LIMIT", flush=True)
            else:
                print(f"[run] budget exhausted (remaining < 5 min); file is in "
                      f"verus_okay state with {_admit_count()} admits remaining. "
                      f"Bailing out of round loop to avoid wasted 60s rounds. "
                      f"end_reason=LIMIT", flush=True)
            break

        # For round 2+: assemble a "round history" message containing diffs
        # of the previous round(s) plus their verus errors. Delivered as
        # the next user message via claude -c -p <msg>. If the previous
        # round's END_REASON:COMPLETE was rejected by the harness's
        # final-state gate, prepend the specific rejection reason so the
        # agent doesn't silently retry the same self-declared COMPLETE.
        continue_message: Optional[str] = None
        if round_num > 1:
            history = build_round_history_block(
                tdir, round_num, target=target,
                since_round=session_start_round,
            )
            parts: list[str] = []
            if next_continue_msg != "continue":
                parts.append(next_continue_msg)
            if history:
                parts.append(
                    "Harness feedback for this round:\n\n" + history +
                    "\nContinue working on remaining admits."
                )
            if parts:
                continue_message = "\n\n".join(parts)
                (tdir / f"round_history_{round_num}.md").write_text(continue_message)
            else:
                continue_message = "continue"

        # Lever 2: when fresh_next_round is set, mint a NEW session id and
        # start fresh (no `--resume`). File state on disk is preserved.
        use_resume = (round_num > 1 and not fresh_next_round)
        if fresh_next_round:
            task_session_id = str(uuid.uuid4())
            print(f"[run] starting FRESH claude session "
                  f"(auto-reset #{auto_resets_used}, session_id={task_session_id})",
                  flush=True)
        # Per-round wall-clock cap. With --round-minutes, each round gets at
        # most that slice (capped by the remaining task budget), so `rounds`
        # behaves as "number of continuation attempts": a round cut by its
        # deadline (LIMIT) leaves budget for the next one. Without it, the round
        # gets the whole remaining budget (legacy behaviour — round 1 can eat
        # it all).
        round_deadline = remaining_s
        if round_minutes:
            round_deadline = min(round_minutes * 60.0, remaining_s)
        reason, rc, claude_result = run_claude_round(
            prompt=prompt,
            cwd=agent_cwd, env=env, raw_out=raw_out,
            session_id=task_session_id,
            resume=use_resume,
            model=model,
            deadline_seconds=round_deadline,
            continue_message=continue_message if use_resume else None,
        )
        duration = time.time() - round_start
        fresh_next_round = False

        # A round cut short by its per-round deadline (claude SIGKILLed mid-work
        # → no END_REASON, negative rc) resumes a half-streamed session badly.
        # Reset context for the next attempt: mint a fresh session that reads
        # the partial proof from disk + the round-history feedback, rather than
        # --resume the killed one. This is what makes "LIMIT → reset & continue,
        # bounded by `rounds`" work.
        if round_minutes and reason is None and rc is not None and rc < 0:
            fresh_next_round = True
            print(f"[run] round {round_num} hit its {round_deadline:.0f}s cap — "
                  f"resetting context for the next continuation round", flush=True)

        # Deterministic rate-limit halt. A 429 means the API rejected the
        # request outright (5-hour session limit, quota exhausted, overage
        # disabled). Unlike the heuristic RATE_LIMIT_OR_HANG guard below
        # (which needs duration > 300), a rejection is instant (~2s) and
        # carries an explicit status, so the heuristic never catches it —
        # the auto-reset machinery instead reads the instant no-op as a
        # "stall," burns every remaining round, and exits as a plausible
        # LIMIT. Catch it here BEFORE the verus gate so a zero-hard-admit
        # target can't be stamped COMPLETE off a round the agent never ran,
        # and so the whole sweep can stop (every later round/target would be
        # rejected too until the window resets).
        if (claude_result.get("is_error")
                and claude_result.get("api_error_status") == 429):
            msg = claude_result.get("result", "rate limited")
            print(f"[run] round {round_num}: API rejected with 429 "
                  f"({msg!r}) — no work possible until the quota window "
                  f"resets. Aborting run (RATE_LIMITED).", flush=True)
            end_reason = "RATE_LIMITED"
            break

        # Spec drift gate (skipped in experiment mode — agent is expected to
        # add specs back to dependency files; snapshot above is still kept
        # for post-hoc analysis against the original).
        if no_spec_gate:
            spec_drift = []
        else:
            # When the gate is on, specs are frozen — so freeze spec fn
            # DEFINITIONS (bodies) too, not just headers. Otherwise a spec fn
            # co-located in an editable file (e.g. edwards.rs's open spec fns,
            # or the lemma files in --strip-to-fields) could be redefined to
            # hollow out a frozen contract without tripping a header check.
            # Folds into spec_drift → SPEC_DRIFT (non-promotable).
            rc_spec, spec_stdout, _ = run_subskill(
                [sys.executable, str(HERE / "skills" / "spec_check.py"),
                 "verify", str(target), "--against", str(spec_snapshot),
                 "--check-spec-defs"],
                env=env,
            )
            try:
                spec_drift = json.loads(spec_stdout).get("drift", [])
            except json.JSONDecodeError:
                spec_drift = []

        # Verus check on the target (anchor) module.
        verus_cmd = [sys.executable, str(HERE / "skills" / "verus_check.py"),
                     str(target), "--project", str(project)]
        if verus_rlimit is not None:
            verus_cmd += ["--rlimit", str(verus_rlimit)]
        rc_verus, verus_stdout, _ = run_subskill(verus_cmd, env=env)
        try:
            verus_result = json.loads(verus_stdout)
        except json.JSONDecodeError:
            verus_result = {"okay": False, "messages": []}

        # In experiment mode the prompt instructs the agent to drive
        # verus_check on each dep file separately (`--verify-only-module`
        # is per-module, so the anchor check alone can't see errors in
        # dep proof bodies). Mirror that on the harness side so the
        # `verus_okay` signal that gates COMPLETE reflects the same truth
        # the agent is being asked to verify. Without this the loop
        # cannot exit early: an honest LIMIT can never flip to COMPLETE.
        for dep in (experiment_allow_edit or []):
            dep_cmd = [sys.executable, str(HERE / "skills" / "verus_check.py"),
                       str(dep), "--project", str(project)]
            if verus_rlimit is not None:
                dep_cmd += ["--rlimit", str(verus_rlimit)]
            rc_dep, dep_stdout, _ = run_subskill(dep_cmd, env=env)
            try:
                dep_result = json.loads(dep_stdout)
            except json.JSONDecodeError:
                dep_result = {"okay": False, "messages": []}
            if not dep_result.get("okay", False):
                verus_result["okay"] = False
            verus_result.setdefault("messages", []).extend(
                dep_result.get("messages", [])[:5]
            )

        last_verus_err = ("\n".join(m.get("data", "") for m in verus_result.get("messages", []))
                          + "\n" + (verus_result.get("stderr_tail", "") or "")).strip()
        last_failed_decls = verus_result.get("failed_declarations", []) or []  # Feature 1
        # Feature 1: error (file, line) locations — the reliable signal for
        # mapping a failure back to its fn body (see _extract_near_miss).
        last_failed_locs = [(m.get("file", ""), m.get("line", 0))
                            for m in verus_result.get("messages", [])
                            if m.get("line")]

        claude_usage = {}
        if claude_result:
            u = claude_result.get("usage") or {}
            claude_usage = {
                "input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
                "total_cost_usd": claude_result.get("total_cost_usd", 0.0),
            }

        rr = RoundResult(
            round_number=round_num,
            end_reason=reason,
            returncode=rc,
            duration_seconds=duration,
            verus_okay=verus_result.get("okay", False),
            verus_errors=verus_result.get("messages", [])[:20],
            spec_drift=spec_drift,
            claude_usage=claude_usage,
            agent_delegations=count_agent_delegations(raw_out),
        )
        round_results.append(rr)
        write_json(tdir / f"round_{round_num}.json", rr)

        # Capture end-of-round file state for next round's history diff.
        snapshot_files([target, *siblings],
                       snapshots_root / f"round_{round_num}")

        # If this round ended with a verifying file, mark it as the
        # rollback target in case a future round runs out of budget
        # while leaving the file broken.
        if rr.verus_okay:
            last_good_snapshot_round = round_num

        # Sibling-verify gate. The per-round verus check above covers only
        # the TARGET module. Rule 4 lets the agent edit sibling helpers under
        # lemmas/<area>_lemmas/; a bad sibling edit can break that sibling's
        # OWN verification, or break a top-level module that consumes it,
        # without the target-only check noticing. Re-verify each sibling
        # modified this round plus its area's top-level module(s).
        sibling_fail: list[dict] = []
        if sibling_verify:
            modified_sibs = []
            for s in siblings:
                try:
                    h = _file_hash(s)
                except OSError:
                    continue
                if h != sibling_hashes.get(str(s)):
                    modified_sibs.append(s)
                    sibling_hashes[str(s)] = h
            reverify: set[Path] = set()
            for s in modified_sibs:
                reverify.add(s)
                for mod in _area_top_level_modules(s):
                    tl = project / "src" / (mod.replace("::", "/") + ".rs")
                    if tl.exists():
                        reverify.add(tl)
            reverify.discard(target)  # target already checked above
            for f in sorted(reverify):
                sv_cmd = [sys.executable, str(HERE / "skills" / "verus_check.py"),
                          str(f), "--project", str(project)]
                if verus_rlimit is not None:
                    sv_cmd += ["--rlimit", str(verus_rlimit)]
                _, sv_out, _ = run_subskill(sv_cmd, env=env)
                try:
                    sv_res = json.loads(sv_out)
                except json.JSONDecodeError:
                    sv_res = {"okay": False, "messages": []}
                if not sv_res.get("okay"):
                    sibling_fail.append({
                        "file": str(f.relative_to(project)),
                        "errors": sv_res.get("messages", [])[:5],
                    })
            if sibling_fail:
                print(f"[run] round {round_num}: sibling re-verify FAILED for "
                      f"{[d['file'] for d in sibling_fail]} — "
                      f"end_reason=SIBLING_VERUS_FAIL", flush=True)

        admits_end = _admit_count()
        admits_delta = (admits_start - admits_end) if (admits_start >= 0 and admits_end >= 0) else 0

        # Lever 2: update per-session token counter
        cc_this_round = claude_usage.get("cache_creation_input_tokens", 0)
        session_cc_tokens += cc_this_round

        print(f"[run] round {round_num}: end_reason={reason} "
              f"verus_okay={rr.verus_okay} spec_drift={len(spec_drift)} "
              f"admits {admits_start}→{admits_end} (Δ{-admits_delta if admits_delta else 0}) "
              f"cc_tokens={cc_this_round/1000:.0f}k (session_cum={session_cc_tokens/1000:.0f}k) "
              f"agent_delegations={rr.agent_delegations}",
              flush=True)

        # Rate-limit / hang detection. We want to bail out when the agent
        # literally did nothing in this round (likely the Claude API is
        # throttling, or the subprocess hung). The correct productivity
        # signal is "did the agent perform any tool_use or emit any text"
        # — counted directly from the raw jsonl. `cc_tokens` is NOT
        # reliable because it's 0 whenever the agent gets SIGKILL'd before
        # emitting a final `result` event, which happens routinely when
        # the wall-deadline fires mid-work.
        agent_actions = count_agent_actions(raw_out)
        if agent_actions == 0 and duration > 300 and rr.end_reason is None:
            print(f"[run] round {round_num}: agent performed 0 actions in {duration:.0f}s — "
                  f"likely rate-limited or hung. Bailing out of round loop. "
                  f"end_reason=RATE_LIMIT_OR_HANG", flush=True)
            # Roll back if the file is now broken (otherwise leave as-is).
            if not rr.verus_okay:
                snap_dir = snapshots_root / f"round_{last_good_snapshot_round}"
                if snap_dir.exists():
                    import shutil as _shutil
                    for f in [target, *siblings]:
                        snap_f = snap_dir / f.name
                        if not snap_f.exists():
                            snap_f = snap_dir / f"{f.parent.name}__{f.name}"
                        if snap_f.exists():
                            _shutil.copy2(snap_f, f)
                    print(f"[run] rolled back to snapshots/round_{last_good_snapshot_round}/",
                          flush=True)
            end_reason = "RATE_LIMIT_OR_HANG"
            break

        # Lever 2: auto-reset decision. Evaluate AFTER the round but BEFORE
        # the early-exit decision below (COMPLETE/SPEC_DRIFT should still
        # short-circuit). We only reset if we'd otherwise continue.
        if auto_reset and auto_resets_used < max_auto_resets and round_num < max_rounds:
            # Stall signal: last 2 rounds in current session, zero fills + short.
            stall = False
            session_rounds = [rr_ for rr_ in round_results
                              if rr_.round_number >= session_start_round]
            if len(session_rounds) >= 2:
                # Need admits_delta per round — derive from snapshots so we don't
                # have to plumb it through RoundResult.
                def _admits_in_snap(n: int) -> int:
                    f = snapshots_root / f"round_{n}" / target.name
                    try:
                        return _count_llm_target_admits(f.read_text()) if f.exists() else -1
                    except OSError:
                        return -1
                last_two = session_rounds[-2:]
                d1_start = _admits_in_snap(last_two[0].round_number - 1)
                d1_end = _admits_in_snap(last_two[0].round_number)
                d2_start = _admits_in_snap(last_two[1].round_number - 1)
                d2_end = _admits_in_snap(last_two[1].round_number)
                if (d1_start == d1_end and d2_start == d2_end and
                    d1_start >= 0 and d2_start >= 0 and
                    last_two[0].duration_seconds < stall_max_duration_sec and
                    last_two[1].duration_seconds < stall_max_duration_sec):
                    stall = True

            # Bloat signal: cumulative cache_creation past threshold
            bloat = session_cc_tokens > bloat_threshold_tokens

            if stall or bloat:
                reason_str = []
                if stall: reason_str.append(
                    f"stall (rounds {round_num-1},{round_num}: 0 fills, "
                    f"dur<{stall_max_duration_sec/60:.0f}min)")
                if bloat: reason_str.append(
                    f"bloat (session_cc={session_cc_tokens/1000:.0f}k>"
                    f"{bloat_threshold_tokens/1000:.0f}k)")
                print(f"[run] auto-reset: round {round_num+1} → fresh session. "
                      f"reason={'; '.join(reason_str)}. "
                      f"resets_used={auto_resets_used+1}/{max_auto_resets}", flush=True)
                fresh_next_round = True
                session_start_round = round_num + 1
                session_cc_tokens = 0
                auto_resets_used += 1
                reset_round_starts.append(round_num + 1)

        # Decision
        changed_tooling = _tooling_changed()
        if changed_tooling:
            # Hard fail: the agent altered the harness's own verification
            # tooling. A doctored verus_check / spec_check / admit counter can
            # fake a green, which means THIS round's verus_okay & spec_drift
            # signals are themselves untrustworthy — so this is checked first,
            # before any gate that consumed those signals. Same cheat class as
            # spec / axiom drift: break and record it.
            print(f"[run] TOOLING_DRIFT: agent modified harness tooling "
                  f"{changed_tooling} — a verification skill that always "
                  f"returns okay=true is a fake-green vector. Failing round.",
                  flush=True)
            end_reason = "TOOLING_DRIFT"
            break
        if spec_drift:
            # Hard fail: specs weakened. Don't continue — next round
            # won't help until we make the agent stop cheating.
            end_reason = "SPEC_DRIFT"
            break
        new_axioms = _axiom_names() - baseline_axioms
        if new_axioms:
            # Hard fail: agent introduced a new `proof fn axiom_*`. Admits
            # inside it are silently excluded from the COMPLETE count, so
            # this bypasses the anti-admit gate. Same class of cheat as
            # spec drift — break and record it.
            print(f"[run] AXIOM_DRIFT: agent introduced new axiom declaration(s) "
                  f"{sorted(new_axioms)} — admits inside axiom_* bodies are "
                  f"excluded from the COMPLETE count, so this is a fake-green "
                  f"vector. Failing round.", flush=True)
            end_reason = "AXIOM_DRIFT"
            break
        if sibling_fail:
            # The agent broke a sibling helper (or a top-level module that
            # consumes it). The per-round verus check covers only the TARGET
            # module, so a target-only COMPLETE here would be a false green.
            # Checked after the cheat-class gates (TOOLING/SPEC/AXIOM drift),
            # whose doctored-tooling / weakened-spec signals would make this
            # re-verify itself untrustworthy. Break and record (parity with
            # SPEC_DRIFT); the per-file failure detail was already logged.
            end_reason = "SIBLING_VERUS_FAIL"
            break
        if reason == "NEEDS_DECOMP":
            # Feature2: the agent escalated — the proof needs missing
            # infrastructure. The whole point of the escalation is to stop
            # grinding the session to the time limit, so break now and record
            # the label. A fresh run_task (e.g. a run_layer re-run) will detect
            # the NEEDS_DECOMP record and retry with a bumped budget + a
            # build-infrastructure-first directive (see top of run_task).
            end_reason = "NEEDS_DECOMP"
            break
        admits_left = _count_gate_admits(target, experiment_allow_edit)
        if reason == "COMPLETE" and rr.verus_okay and admits_left == 0:
            end_reason = "COMPLETE"
            break
        if reason == "COMPLETE" and (not rr.verus_okay or admits_left > 0):
            # Agent claimed done but evidence disagrees. Treat as LIMIT and
            # continue, and tell the agent WHY on the next round so it
            # doesn't just retry the same self-declared COMPLETE.
            print(f"[run] agent claimed COMPLETE but verus_okay={rr.verus_okay} "
                  f"admits_left={admits_left} — continuing", flush=True)
            reason = None
            next_continue_msg = _rejection_continue_msg(rr.verus_okay, admits_left)
        else:
            next_continue_msg = "continue"
        # Otherwise: LIMIT or None → next round continues the session.

    duration_total = (datetime.now() - start).total_seconds()

    # Final state — verus must pass AND no admit() may remain UNLESS
    # remaining admits are documented as intentional axioms (M4 metric).
    # `admit()` makes Verus accept any postcondition trivially, so
    # verus_okay alone is not sufficient evidence of "done."
    try:
        admits_remaining = _count_gate_admits(target, experiment_allow_edit)
    except OSError:
        admits_remaining = -1
    last_round_okay = bool(round_results and round_results[-1].verus_okay)

    # Classify remaining admits: intentional axioms vs hard tail.
    admit_classification = classify_remaining_admits(target)
    intentional_axioms = admit_classification["intentional"]
    hard_remaining = admit_classification["hard"]

    # Axiom-integrity: any agent-introduced `proof fn axiom_*` (vs the
    # pre-run baseline) is a fake-green vector — fold it into the loop
    # end_reason so the final gate can't promote it to COMPLETE even if
    # the loop ended without the per-round check firing (e.g. budget bail).
    final_new_axioms = _axiom_names() - baseline_axioms
    if final_new_axioms and (end_reason or "").upper() != "SPEC_DRIFT":
        end_reason = "AXIOM_DRIFT"

    # Tooling-integrity: any agent edit to the harness's own verification
    # skills (vs the pre-run baseline) is a fake-green vector — fold it into
    # the loop end_reason so a budget-bail / deadline exit that never reached
    # the per-round decision block still can't be promoted to COMPLETE. Does
    # not clobber an already-recorded spec/axiom cheat label.
    final_changed_tooling = _tooling_changed()
    if final_changed_tooling and (end_reason or "").upper() not in (
            "SPEC_DRIFT", "AXIOM_DRIFT"):
        end_reason = "TOOLING_DRIFT"

    # Success criterion: verus okay AND no LLM-target admit remains, with no
    # integrity cheat. Key on `admits_remaining` (== _count_gate_admits, the
    # SAME strict counter the per-round COMPLETE gate uses) as the single
    # source of truth — NOT classify_remaining_admits["hard"], whose extra
    # heuristics (axioms.rs filename, "Axiom:" docstring) can mis-flag a real
    # `lemma_*` obligation as intentional and promote a never-proved module to
    # COMPLETE (false green). classify_remaining_admits is kept for the
    # result.json detail only. (admits_remaining == -1 on a read error ⇒ not
    # done, which is the safe direction.)
    done_for_real = (
        last_round_okay and admits_remaining == 0
        and not final_new_axioms and not final_changed_tooling
    )

    # Final-state gate (pure decision in `_final_end_reason`, unit-tested):
    # RATE_LIMITED (429 halt) is preserved above all; else done_for_real ⇒
    # COMPLETE; else NEEDS_DECOMP is preserved (Feature2); else LIMIT.
    final_end_reason = _final_end_reason(done_for_real, end_reason)

    success = final_end_reason == "COMPLETE"
    if admits_remaining > 0:
        kind = "all intentional" if hard_remaining == 0 else f"{hard_remaining} hard + {intentional_axioms} intentional"
        print(f"[info] Final state: end_reason={final_end_reason} "
              f"admits_remaining={admits_remaining} ({kind})")

    task_result = TaskResult(
        task_id=target_id, run_id=run_id,
        target_path=str(target), module_path=module,
        success=success, end_reason=final_end_reason,
        rounds_used=len(round_results),
        duration_seconds=duration_total,
        round_results=round_results,
        reset_round_starts=reset_round_starts,
        admit_classification=admit_classification,
    )
    write_json(tdir / "result.json", task_result)

    # Feature 3: mine this run's trace into a discovery brief for the next
    # attempt on this target (persisted whether or not it succeeded).
    try:
        discovery_brief.update(results_root, target_id, tdir, project)
    except Exception as e:  # never let brief-mining break a run
        print(f"[run] Feature3: discovery_brief.update failed: {e!r}", flush=True)

    # Record to failure memory on non-success
    if not success:
        # Feature 1: resolve the rejected decls to fn bodies. The resolved
        # names are the real fns (line-matched on current Verus, since the
        # parsed failed_declarations are unreliable), so store those rather
        # than the raw parse — which on current Verus is the crate-name junk
        # the name regex captures, never an actual fn.
        nm_names, nm_source = _extract_near_miss(
            target, last_failed_decls, last_failed_locs)
        failure_memory.record(
            results_root=results_root, run_id=run_id,
            module=module, function=target_id,
            rounds_used=len(round_results),
            final_error=last_verus_err,
            end_reason=final_end_reason,
            failed_decls=nm_names,                                 # Feature 1
            near_miss=nm_source,                                   # Feature 1
        )

    # Append to proven registry on success: we record the target file stem,
    # not individual fns (MVP has no per-fn tracking yet; that's extension E3).
    if success:
        reg_path = results_root / "proven_registry.json"
        # Locked read-modify-write: concurrent successes must not clobber each
        # other's entries (Phase 0, docs/parallel_orchestration_design.md).
        with atomic_json.locked_update(reg_path, {"proven": []}) as existing:
            existing.setdefault("proven", []).append({
                "name": target_id,
                "module": module,
                "file": str(target.relative_to(project)) if target.is_relative_to(project) else str(target),
                "run_id": run_id,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })

    # Emit diff.md — admitted vs final vs ground-truth
    if admitted_ref:
        diff_path = tdir / "diff.md"
        rc_diff, _, stderr_diff = run_subskill(
            [sys.executable, str(HERE / "skills" / "diff_view.py"),
             str(target),
             "--admitted-ref", admitted_ref,
             "--truth-ref", truth_ref,
             "--out", str(diff_path)],
            env=env,
        )
        if rc_diff == 0:
            print(f"[info] diff written to {diff_path}")
        else:
            print(f"[warn] diff_view failed (rc={rc_diff}): {stderr_diff[:500]}")

    _print_summary(task_result)
    return task_result


def _print_summary(result: TaskResult) -> None:
    print("\n" + "=" * 60)
    print(f"Task: {result.task_id}")
    print(f"Status: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"End reason: {result.end_reason}")
    print(f"Rounds: {result.rounds_used}")
    print(f"Duration: {result.duration_seconds:.1f}s")
    if result.round_results:
        last = result.round_results[-1]
        print(f"Final verus_okay: {last.verus_okay}")
        print(f"Final error count: {len(last.verus_errors)}")
    print("=" * 60)


# ----------------- CLI -----------------

def main() -> int:
    _install_signal_handler()
    ap = argparse.ArgumentParser()
    ap.add_argument("target", type=Path, help="Target .rs file (must live inside a Cargo project)")
    ap.add_argument("--project", type=Path, default=None,
                    help="Cargo project root (auto-detected from target if omitted)")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--results", type=Path, default=Path("results"))
    ap.add_argument("--model", default=None,
                    help="Claude model (alias 'sonnet'/'opus'/'haiku' or full id). "
                         "Default: whatever claude-code is configured to use.")
    ap.add_argument("--vstd-root", type=Path, default=None,
                    help="Path to Verus's vstd source to index alongside the "
                         "project. Example: /path/to/verus/vstd")
    ap.add_argument("--admitted-ref", default=None,
                    help="Git ref of the admitted baseline (e.g. "
                         "eval/admitted-layerA-debug). Enables diff.md generation.")
    ap.add_argument("--truth-ref", default="main",
                    help="Git ref of the ground-truth version for the diff. "
                         "Default: main")
    ap.add_argument("--max-task-minutes", type=float, default=None,
                    help="Wall-clock cap in minutes. SIGKILL the claude "
                         "process group if exceeded. If omitted, the budget "
                         "scales with the number of admit() in the target "
                         "file: max(20, 1.5 * num_admits). Empirically derived "
                         "from Layer A/B/C runs across 29 modules.")
    ap.add_argument("--only-fns", default=None,
                    help="Comma-separated proof-fn names: the COMPLETE gate "
                         "counts admits only inside these fns (decomposition "
                         "sub-target, §A5.2). Other fns stay admitted and supply "
                         "their ensures. Use with decompose.py partition.")
    ap.add_argument("--round-minutes", type=float, default=None,
                    help="Per-round wall-clock cap (minutes), bounded by "
                         "--max-task-minutes. With it, `rounds` acts as the "
                         "number of continuation attempts: a round cut by its "
                         "cap (LIMIT) resets context (fresh session reading the "
                         "partial proof from disk) and the next round continues. "
                         "Omitted → each round gets the whole remaining budget.")
    ap.add_argument("--budget-min-floor", type=float, default=20.0,
                    help="Minimum auto-budget when --max-task-minutes is "
                         "omitted. Default: 20 min.")
    ap.add_argument("--budget-min-per-admit", type=float, default=1.5,
                    help="Minutes per admit() for auto-budget. Default: 1.5.")
    ap.add_argument("--no-failure-memory", action="store_true",
                    help="Skip rendering prior failure_memory records into "
                         "the prompt for this run. Useful when prior records "
                         "predate prompt/harness improvements and would prime "
                         "the agent to give up.")
    ap.add_argument("--verus-rlimit", type=float, default=80.0,
                    help="Pass --rlimit FLOAT to all harness-level "
                         "verus_check invocations. Increases the per-fn SMT "
                         "resource limit. Default 80 (Verus's own default "
                         "is ~10, which empirically rlimits out on any "
                         "non-trivial exec function — ristretto.rs's "
                         "compress, double_and_compress_batch_verus, etc.).")
    ap.add_argument("--auto-reset", dest="auto_reset", action="store_true", default=True,
                    help="Auto-reset claude session on stall or context "
                         "bloat. Default: on.")
    ap.add_argument("--no-auto-reset", dest="auto_reset", action="store_false",
                    help="Disable auto-reset (keep -c continuation throughout).")
    ap.add_argument("--max-auto-resets", type=int, default=3,
                    help="Cap on auto-resets per task. Default: 3.")
    ap.add_argument("--stall-max-duration-sec", type=float, default=180.0,
                    help="Round shorter than this counts toward stall "
                         "detection. Default: 180 (3 min).")
    ap.add_argument("--bloat-threshold-tokens", type=int, default=200_000,
                    help="Cumulative cache_creation tokens per session past "
                         "this triggers preemptive reset. Default: 200000 "
                         "(lowered from 300000: context degradation is the "
                         "dominant failure mode, so shed the session sooner; "
                         "subagent delegation — see prompt.md — keeps the "
                         "parent context lean between resets).")
    ap.add_argument("--no-spec-gate", action="store_true",
                    help="Skip the in-loop spec_check verify (snapshot still "
                         "taken for post-hoc diff). Used by the spec-"
                         "reconstruction experiment, where the agent is "
                         "expected to ADD specs back to dependency files.")
    ap.add_argument("--experiment-allow-edit", type=Path, nargs="+", default=None,
                    help="Dependency file(s) the agent may edit. Renders an "
                         "experiment-mode block into the prompt that "
                         "overrides rule 4 (edit only target) for these "
                         "files. Required for --experiment-mode.")
    ap.add_argument("--experiment-mode",
                    choices=["spec-proof", "proof-only"],
                    default="spec-proof",
                    help="Which experiment shape to run. "
                         "'spec-proof' (default): dep fns have no Verus "
                         "specs; agent infers requires/ensures/decreases AND "
                         "adds proof scaffolding; helper lemmas allowed; "
                         "in-loop spec-integrity gate disabled. "
                         "'proof-only': specs and lemma library are frozen; "
                         "agent only adds proof scaffolding inside existing "
                         "fn bodies; spec-integrity gate stays ON so any "
                         "fn-header edit fails the round.")
    ap.add_argument("--wire-log", action="store_true",
                    help="Route the claude subprocess through a localhost "
                         "logging proxy (wire_proxy.py) via ANTHROPIC_BASE_URL, "
                         "capturing the full API request bodies into "
                         "claude_raw/wire_*.jsonl: system prompt, tool JSON "
                         "schemas, skills, and per-turn context growth — none "
                         "of which appear in the stream-json logs. Best-effort: "
                         "a proxy failure falls back to the direct API and "
                         "never fails the run. Off by default.")
    ap.add_argument("--sibling-verify", dest="sibling_verify",
                    action="store_true", default=True,
                    help="After each round, re-verify any sibling helper the "
                         "agent modified plus its area's top-level module. "
                         "Catches sibling edits that break transitive "
                         "verification. Default: on.")
    ap.add_argument("--no-sibling-verify", dest="sibling_verify",
                    action="store_false",
                    help="Disable the per-round sibling re-verify gate "
                         "(only the target module is checked).")
    args = ap.parse_args()
    if args.experiment_allow_edit:
        # spec-proof: agent rewrites specs, so the snapshot-vs-current gate
        # would always fail. proof-only: gate must stay ON since headers are
        # frozen and any drift = cheating.
        if args.experiment_mode == "spec-proof":
            args.no_spec_gate = True
        # proof-only: leave --no-spec-gate at whatever the user set (default
        # False), so the gate runs and catches header edits.

    target = args.target.resolve()
    if not target.exists():
        print(f"[error] target not found: {target}", file=sys.stderr)
        return 1
    project = (args.project or find_cargo_root(target)).resolve()
    run_id = args.run_id or results.run_id_new()
    results_root = args.results.resolve()
    results_root.mkdir(parents=True, exist_ok=True)

    # Budget: explicit override; else, if a per-round cap is set, the task
    # budget is just rounds × round_minutes (the natural ceiling of "N
    # continuation attempts of M minutes each" — no separate smaller cap that
    # would throttle the rounds); else auto-scale by admit count.
    if args.max_task_minutes is not None:
        max_minutes = args.max_task_minutes
        budget_source = "explicit"
    elif args.round_minutes is not None:
        max_minutes = args.round_minutes * args.rounds
        budget_source = f"rounds×round_minutes ({args.rounds} × {args.round_minutes:.0f})"
    else:
        try:
            # Count NON-AXIOM admits only — the same axiom-aware counter the
            # COMPLETE gate uses (_count_gate_admits / _admit_count). A raw
            # .count("admit()") over-counts: it includes admit()s inside
            # `proof fn axiom_*` bodies (allowed to stay) and any in
            # comments/strings, inflating the auto budget.
            num_admits = _count_llm_target_admits(target.read_text())
        except OSError:
            num_admits = 0
        auto = max(args.budget_min_floor, args.budget_min_per_admit * num_admits)
        max_minutes = auto
        budget_source = (
            f"auto (max({args.budget_min_floor}, "
            f"{args.budget_min_per_admit} * {num_admits} admits) = {auto:.0f})"
        )

    print(f"[run] target   = {target}")
    print(f"[run] project  = {project}")
    print(f"[run] run_id   = {run_id}")
    print(f"[run] results  = {results_root}")
    print(f"[run] rounds   = {args.rounds}")
    print(f"[run] budget   = {max_minutes:.1f} min  ({budget_source})")
    print(f"[run] pid      = {os.getpid()}  (Ctrl-C or kill -TERM {os.getpid()} to stop)")

    result = run_task(
        target=target, project=project,
        run_id=run_id, results_root=results_root,
        max_rounds=args.rounds,
        model=args.model,
        vstd_root=args.vstd_root.resolve() if args.vstd_root else None,
        admitted_ref=args.admitted_ref,
        truth_ref=args.truth_ref,
        max_task_minutes=max_minutes,
        round_minutes=args.round_minutes,
        only_fns=[s for s in (args.only_fns or "").split(",") if s.strip()] or None,
        skip_failure_memory=args.no_failure_memory,
        verus_rlimit=args.verus_rlimit,
        auto_reset=args.auto_reset,
        max_auto_resets=args.max_auto_resets,
        stall_max_duration_sec=args.stall_max_duration_sec,
        bloat_threshold_tokens=args.bloat_threshold_tokens,
        experiment_allow_edit=[p.resolve() for p in (args.experiment_allow_edit or [])],
        experiment_mode=args.experiment_mode,
        no_spec_gate=args.no_spec_gate,
        wire_log=args.wire_log,
        sibling_verify=args.sibling_verify,
    )
    # Distinct exit code 42 on a 429 halt so a batch launcher (launch.sh) can
    # tell "this target failed" (rc 1) from "the quota window is exhausted,
    # stop the whole sweep" (rc 42) — every later target would just be
    # rejected too until the window resets.
    if result.end_reason == "RATE_LIMITED":
        return 42
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
