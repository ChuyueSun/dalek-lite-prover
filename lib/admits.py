"""Axiom-aware admit() counting and inventory, plus admit-skeleton creation.

Single source of truth for the question "how many actionable admits
are in this file?" Both the harness's COMPLETE gate and the
`admit_inventory` CLI flow through here, so they cannot disagree.

Also the inverse direction — turning proven source into an `admit()`
skeleton. `admit_proof_fn_bodies` / `admit_proof_blocks` (and their brace
helpers `find_proof_fn_body_brace` / `find_matching_brace`) implement a
mode-aware admitter that builds the `eval/admitted-*` starting states fed
to `run.py --admitted-ref`. It admits ONLY `proof fn` bodies
and inline `proof { ... }` blocks; it skips `axiom_*` (trusted), leaves
`spec fn` definitions intact, and preserves all exec code. This is
deliberately NOT `code_utils.strip_fn_body_to_admit`, which is mode-blind
and would wipe an exec fn's body wholesale.

The state machine and its two regexes are ported verbatim from
`run.py::_count_llm_target_admits` (which was added in PR #1 and is
pinned by tests/test_admits.py). DO NOT reimplement the algorithm
elsewhere — call `count_non_axiom` / `classify_admit_lines` /
`inventory_file` from this module.

The algorithm:

    Three-state walk so indented/nested axiom_* bodies are handled
    correctly without misreading `ensures ({ ... })` clauses as the
    function body:
      outside → sig   on `proof fn axiom_*` header
      sig → body      on a standalone `{` line (the body opener)
      body → outside  when brace depth returns to 0 inside the body
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable


_AXIOM_FN_RE = re.compile(
    r"^\s*((pub|broadcast|open|closed)\s+)*proof\s+fn\s+axiom_"
)

# Same header shape, but captures the full `axiom_*` name. Used by the
# harness's axiom-integrity gate (see run.py) to diff the set of axiom
# declarations against a pre-run baseline.
_AXIOM_FN_NAME_RE = re.compile(
    r"^\s*(?:(?:pub|broadcast|open|closed)\s+)*proof\s+fn\s+(axiom_\w+)",
    re.MULTILINE,
)

# A line whose `{` ends the line preceded by start-of-line / whitespace /
# `,` / `)`. Matches both standalone `{` lines (the canonical Verus body
# opener) and same-line forms like `ensures e, {` or `) {`. Crucially
# does NOT match inline `({` patterns in `ensures` / `requires` clauses,
# where `{` is preceded by `(` — those keep us in the signature state.
_BODY_OPEN_RE = re.compile(r"(?:[\s,)]|^)\s*\{\s*$")


def _brace_delta(s: str) -> int:
    """Net brace depth contributed by `s`: count of `{` minus count of `}`."""
    return s.count("{") - s.count("}")


def classify_admit_lines(text: str) -> dict:
    """Walk `text` line by line, returning the 1-indexed line numbers
    of `admit()` calls partitioned by whether they live inside a
    `proof fn axiom_*` body.

    Returns: {"non_axiom_lines": list[int], "axiom_lines": list[int]}.
    """
    non_axiom: list[int] = []
    axiom: list[int] = []
    state = "outside"
    depth = 0
    for idx, line in enumerate(text.splitlines(), start=1):
        code = line.split("//", 1)[0]
        if state == "outside":
            if _AXIOM_FN_RE.match(line):
                state = "sig"
                # Edge case: signature and body opener on same line
                # (e.g. `pub proof fn axiom_foo() { admit() }`). Skip
                # the admit-count step for this line — matches
                # _count_llm_target_admits's silent behavior.
                if "{" in code:
                    depth = _brace_delta(code)
                    state = "body" if depth > 0 else "outside"
            elif "admit()" in code:
                non_axiom.append(idx)
        elif state == "sig":
            if _BODY_OPEN_RE.search(code):
                state = "body"
                # Net `{` − `}` on this line is the starting body depth.
                depth = _brace_delta(code)
                if depth <= 0:
                    # Body opened and closed on the same line (rare).
                    state = "outside"
                    depth = 0
        elif state == "body":
            if "admit()" in code:
                axiom.append(idx)
            depth += _brace_delta(code)
            if depth <= 0:
                state = "outside"
                depth = 0
    return {"non_axiom_lines": non_axiom, "axiom_lines": axiom}


def axiom_fn_names(text: str) -> set[str]:
    """Names of `proof fn axiom_*` declarations in `text`.

    The COMPLETE gate excludes `admit()` inside `proof fn axiom_*` bodies
    (axioms-by-convention). That exclusion is a fake-green vector: an agent
    could route a proof through a NEW `proof fn axiom_cheat() { admit() }`
    and the counter would silently ignore it. The harness diffs this set
    against a pre-run baseline so any agent-introduced axiom is caught.
    """
    return {m.group(1) for m in _AXIOM_FN_NAME_RE.finditer(text)}


def count_non_axiom(text: str) -> int:
    """Count `admit()` lines outside `proof fn axiom_*` bodies.

    Drop-in replacement for run.py's former local
    `_count_llm_target_admits` — same algorithm, same answers.
    """
    return len(classify_admit_lines(text)["non_axiom_lines"])


# Header of any (non-axiom) proof fn, capturing its name.
_PROOF_FN_NAME_RE = re.compile(
    r"^\s*(?:(?:pub(?:\([^)]*\))?|broadcast|open|closed)\s+)*proof\s+fn\s+(\w+)",
    re.M,
)


def proof_fn_bodies(text: str) -> list[tuple[str, int, int]]:
    """Return `(name, body_open, body_close)` char offsets for every proof fn.

    Reuses the same body-brace finder the admit-skeleton tooling uses, so it
    handles `requires`/`ensures` clauses with braces correctly. `axiom_*` fns
    are included here (callers filter by name if they want only provable ones)."""
    out: list[tuple[str, int, int]] = []
    for m in _PROOF_FN_NAME_RE.finditer(text):
        bo = find_proof_fn_body_brace(text, m.start())
        if bo is None:
            continue
        bc = find_matching_brace(text, bo)
        if bc is None:
            continue
        out.append((m.group(1), bo, bc))
    return out


def proof_fn_admit_counts(text: str) -> dict[str, int]:
    """Map each non-axiom proof fn name → its count of non-comment `admit()`.

    Only fns with ≥1 admit are returned — i.e. the unfinished proof obligations,
    the atomic units a decomposition splits across sub-targets. `axiom_*` fns are
    excluded (their admits are trusted, not work)."""
    counts: dict[str, int] = {}
    for name, bo, bc in proof_fn_bodies(text):
        if name.startswith("axiom_"):
            continue
        body = text[bo:bc + 1]
        n = sum(1 for ln in body.splitlines() if "admit()" in ln.split("//", 1)[0])
        if n:
            counts[name] = n
    return counts


def count_non_axiom_in_fns(text: str, names: set[str] | list[str]) -> int:
    """Count non-comment `admit()` only within the bodies of the named fns.

    The scoped version of `count_non_axiom`, used by run.py's `--only-fns`: when
    a sub-target is responsible for just a subset of a file's proof fns, the
    COMPLETE gate must ignore admits in the OTHER fns (they stay admitted and
    supply their `ensures` to callers — see design §A5.2)."""
    names = set(names)
    total = 0
    for name, bo, bc in proof_fn_bodies(text):
        if name not in names or name.startswith("axiom_"):
            continue
        body = text[bo:bc + 1]
        total += sum(1 for ln in body.splitlines()
                     if "admit()" in ln.split("//", 1)[0])
    return total


def inventory_file(path: Path) -> dict:
    """Return the JSON shape used by skills/admit_inventory.py for a
    single file. Per-admit entries carry `{file, line}` only — the
    state machine does not track function names (a previous attempt
    using a heuristic Rust parser was dropped — see commit history)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    cls = classify_admit_lines(text)
    file_str = str(path)
    return {
        "file": file_str,
        "non_axiom_admits": [{"file": file_str, "line": ln}
                             for ln in cls["non_axiom_lines"]],
        "axiom_admits": [{"file": file_str, "line": ln}
                         for ln in cls["axiom_lines"]],
        "non_axiom_count": len(cls["non_axiom_lines"]),
        "axiom_count": len(cls["axiom_lines"]),
    }


def inventory_files(paths: Iterable[Path]) -> dict:
    """Aggregate `inventory_file` across multiple paths."""
    files: list[dict] = []
    non_axiom: list[dict] = []
    axiom: list[dict] = []
    for path in paths:
        inv = inventory_file(Path(path))
        files.append(inv)
        non_axiom.extend(inv["non_axiom_admits"])
        axiom.extend(inv["axiom_admits"])
    return {
        "okay_for_complete": len(non_axiom) == 0,
        "non_axiom_count": len(non_axiom),
        "axiom_count": len(axiom),
        "non_axiom_admits": non_axiom,
        "axiom_admits": axiom,
        "files": files,
    }


# ---------- admit-skeleton creation (mode-aware) ------------------------
# The inverse of counting: turn proven source into an `admit()` skeleton,
# preserving signatures + requires/ensures/decreases. The mode-aware
# admitter that builds the admitted starting state. Mode-aware on purpose:
# only `proof fn`
# bodies and inline `proof { ... }` blocks are admitted; `axiom_*` fns are
# skipped (trusted), and `spec fn` definitions plus all exec code are left
# intact. Contrast `code_utils.strip_fn_body_to_admit`, which is mode-blind
# and would replace an exec fn's executable body with `admit()`.

# Matches `proof fn name`, `pub proof fn name`, `pub(crate) proof fn name`.
_PROOF_FN_RE = re.compile(r"(?:pub(?:\s*\([^)]*\))?\s+)?proof\s+fn\s+\w+")

# Matches `proof {` opening an inline proof block inside an exec function.
_PROOF_BLOCK_RE = re.compile(r"\bproof\s*\{")


def find_matching_brace(code: str, brace_pos: int) -> int | None:
    """Find the matching closing brace for an opening brace at *brace_pos*."""
    depth = 0
    for i in range(brace_pos, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def find_proof_fn_body_brace(code: str, fn_start: int) -> int | None:
    """Find the opening brace of a proof fn body in Verus source.

    Unlike a naive "first `{` at paren depth 0" scan, this handles Verus
    `requires`/`ensures` clauses whose expressions contain braces at paren
    depth 0 — e.g. `if (cond) { expr } else { expr }`,
    `forall|k| ... ==> { expr }`, or `(expr) by { ... }`.

    Heuristic: the body `{` appears on a line where the text before it is
    either (a) only whitespace (standalone brace after clauses), or (b)
    part of the `fn` signature line (simple one-liner). Clause-internal
    braces are preceded by keywords or operators on the same line and are
    skipped.
    """
    paren_depth = 0
    for i in range(fn_start, len(code)):
        ch = code[i]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == "{" and paren_depth == 0:
            line_start = code.rfind("\n", 0, i) + 1
            prefix = code[line_start:i].strip()
            # Body brace: empty line (after clauses) or fn signature line.
            if prefix == "" or re.search(r"\bfn\b", prefix):
                return i
    return None


_INT_RETURN_TYPES = ("int", "nat", "u64", "u32", "u16", "u8",
                     "i64", "i32", "usize")


def _admit_body_for_return(sig: str) -> str:
    """A type-correct `admit()` body for a proof fn with signature `sig`.

    Verus proof fns return `()` by default (→ bare `admit()`); a few
    return `(name: Type)`, which needs a trailing value so the body still
    type-checks (`bool` → `true`, an integer type → `0`).

    Boundary (documented & kept): only the *named* return form
    `-> (name: Type)` is recognised — the Verus convention for proof fns
    that return a value. An *unnamed* `-> bool` falls through to a bare
    `admit()` with no trailing value. This is intentional: `admit()`
    assumes `false`, so SMT accepts the body regardless of the return
    type, and real Verus proof fns use the named form. Pinned by
    `tests/test_admits.py::AdmitProofFnBodies.test_unnamed_return_falls_through`.
    """
    m = re.search(r"->\s*\(\s*\w+\s*:\s*(\w+)", sig)
    ret = m.group(1) if m else None
    if ret == "bool":
        return "{\n    admit();\n    true\n}"
    if ret in _INT_RETURN_TYPES:
        return "{\n    admit();\n    0\n}"
    return "{\n    admit()\n}"


def _splice_edits(code: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply `(start, end, replacement)` edits to `code` (`end` exclusive).

    Edits are collected against the *original* `code` and must be ascending
    and non-overlapping — which holds here because regex matches are walked
    left-to-right and each edit spans a single fn body / proof block.
    Collecting first and splicing once avoids per-edit offset bookkeeping.
    """
    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        out.append(code[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(code[cursor:])
    return "".join(out)


def admit_proof_fn_bodies(code: str) -> str:
    """Replace every `proof fn` body with an `admit()` skeleton.

    Preserves signatures + requires/ensures/decreases; only the body (the
    outermost brace pair at paren depth 0) is replaced. `axiom_*` fns keep
    their bodies (trusted axioms); `spec fn` / `exec fn` bodies are not
    matched at all. The admit body is made return-type-correct (see
    `_admit_body_for_return`) so the result still type-checks.
    """
    edits: list[tuple[int, int, str]] = []
    for match in _PROOF_FN_RE.finditer(code):
        name = re.search(r"\bfn\s+(\w+)", match.group(0))
        if name and name.group(1).startswith("axiom_"):
            continue  # trusted axioms keep their bodies
        body_brace = find_proof_fn_body_brace(code, match.start())
        if body_brace is None:
            continue
        closing = find_matching_brace(code, body_brace)
        if closing is None:
            continue
        new_body = _admit_body_for_return(code[match.start():body_brace])
        edits.append((body_brace, closing + 1, new_body))
    return _splice_edits(code, edits)


def admit_proof_blocks(code: str) -> str:
    """Replace the contents of inline `proof { ... }` blocks with admit().

    These appear inside exec functions between statements. Only the proof
    scaffolding is hollowed out (`{ admit(); }`); the surrounding exec code
    is preserved.
    """
    edits: list[tuple[int, int, str]] = []
    for match in _PROOF_BLOCK_RE.finditer(code):
        brace = code.index("{", match.start())
        closing = find_matching_brace(code, brace)
        if closing is None:
            continue
        edits.append((brace, closing + 1, "{ admit(); }"))
    return _splice_edits(code, edits)
