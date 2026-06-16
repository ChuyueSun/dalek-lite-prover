#!/usr/bin/env python3
"""Specification integrity gate.

Snapshots function signatures + requires/ensures/decreases before the
agent runs; verifies the snapshot after. Failure = the agent weakened a
spec and must not be allowed to claim success.

Scope of "signature" for this check:
    - `fn` header (name, params, return type)
    - `requires` clauses (textual; normalized whitespace)
    - `ensures` clauses
    - `decreases` clause
    - `#[verifier::external_body]` attribute (forbidden to *add*)

What's allowed to change freely: the function body.

Usage:
    python skills/spec_check.py snapshot <file.rs> --out <snapshot.json>
    python skills/spec_check.py verify   <file.rs> --against <snapshot.json>

`verify` exits 0 if all known sigs preserved, 1 otherwise.
"""
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("spec_check")


# Find each `pub? (broadcast)? (open|closed)? (proof|spec|exec)? fn NAME` and
# capture through the end of any `requires`/`ensures`/`decreases` blocks,
# stopping at the first `{` that opens the body. Simple brace-matching; we
# accept a small risk of misparse on esoteric code and log when that happens.
_FN_START_RE = re.compile(
    r"(?P<attr>(?:#\[[^\]]+\]\s*)*)"
    r"(?P<vis>pub(?:\s*\([^)]+\))?\s+)?"
    r"(?P<broadcast>broadcast\s+)?"
    r"(?P<openness>(?:open|closed)\s+)?"
    r"(?P<mode>(?:proof|spec|exec)\s+)?"
    r"fn\s+(?P<name>\w+)",
    re.MULTILINE,
)


# Attributes that tune verification budget rather than the proof obligation.
# Adding/changing these is NOT a spec change — the function still has to
# meet the same requires/ensures. The agent must be able to bump them on
# hard files (the round-10 ristretto narrative documented this need).
_BUDGET_ATTR_RE = re.compile(
    r"#\[\s*verifier::(?:rlimit|spinoff_prover|integer_ring|nonlinear)\s*\([^)]*\)\s*\]"
)


def _strip_budget_attrs(s: str) -> str:
    """Remove budget-tuning attributes so they don't count as drift."""
    return _BUDGET_ATTR_RE.sub("", s)


def _extract_sigs(text: str) -> dict[str, dict]:
    """Return {name: {header, requires, ensures, decreases, attrs, mode,
                      external_body, line}}.

    Note: `header` excludes budget-tuning attributes
    (`#[verifier::rlimit(N)]`, etc.) so the agent can adjust them without
    tripping the drift gate. Other `#[verifier::*]` attrs remain captured.
    """
    sigs: dict[str, dict] = {}
    for m in _FN_START_RE.finditer(text):
        name = m.group("name")
        start = m.start()
        header_end = _find_header_end(text, m.end())
        if header_end is None:
            logger.warning("spec_check: could not find header end for %s", name)
            continue
        header = text[start:header_end]
        line = text.count("\n", 0, start) + 1
        attrs = m.group("attr") or ""
        sigs[name] = {
            "header": _normalize(_strip_budget_attrs(header)),
            "requires": _section(header, "requires"),
            "ensures": _section(header, "ensures"),
            "decreases": _section(header, "decreases"),
            "mode": (m.group("mode") or "").strip(),
            "external_body": "external_body" in attrs,
            "line": line,
        }
    return sigs


def _find_header_end(text: str, start: int) -> int | None:
    """Walk forward from `start` past `requires`/`ensures`/`decreases`
    blocks and stop at the `{` that opens the body or the `;` of an
    externless declaration.

    Handles:
    - `//` line comments (so apostrophes like "doesn't" in a comment
      don't get treated as char-literal openers)
    - `/* */` block comments
    - `"..."` string literals (only; bare `'` in code is ambiguous with
      Rust lifetimes, so we intentionally do NOT treat it as a quote)
    """
    i = start
    depth = 0
    in_str = False
    while i < len(text):
        c = text[i]
        # Skip line comments
        if not in_str and c == "/" and i + 1 < len(text) and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl == -1:
                return None
            i = nl + 1
            continue
        # Skip block comments
        if not in_str and c == "/" and i + 1 < len(text) and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close == -1:
                return None
            i = close + 2
            continue
        if in_str:
            if c == "\\" and i + 1 < len(text):
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(" or c == "[":
            depth += 1
        elif c == ")" or c == "]":
            depth -= 1
        elif c == "{" and depth == 0:
            return i
        elif c == ";" and depth == 0:
            return i + 1
        i += 1
    return None


def _section(header: str, keyword: str) -> str:
    """Extract `requires { ... }` (or `ensures` / `decreases`) as normalized text."""
    # Verus accepts both `requires P` and `requires P1, P2` forms. We match
    # `keyword` followed by either a `{ ... }` block or a comma-separated
    # expression until the next keyword.
    kw_re = re.compile(
        rf"\b{keyword}\b\s*(?P<body>\{{.*?\}}|[^;{{]*)",
        re.DOTALL,
    )
    m = kw_re.search(header)
    if not m:
        return ""
    return _normalize(m.group("body"))


def _normalize(s: str) -> str:
    return " ".join(s.split())


# ---------------- Sibling helper discovery ----------------

def discover_sibling_helpers(project: Path, target: Path) -> list[Path]:
    """Return sibling helper files that the agent may append new lemmas to.

    Convention (dalek-lite): for a target like
        <project>/src/<area>.rs            (e.g. ristretto.rs)
    the sibling helpers live in EITHER
        <project>/src/lemmas/<area>_lemmas/*.rs   (directory layout — ristretto, edwards, field, window)
    OR
        <project>/src/lemmas/<area>_lemmas*.rs    (loose-file layout — scalar, montgomery)
    or any combination of the two.

    For a target already INSIDE `lemmas/<area>_lemmas/`, the siblings are
    the OTHER files in the same directory.

    The target itself is never in the returned list. `mod.rs` is excluded.
    """
    target = target.resolve()
    project = project.resolve()
    src = project / "src"
    if not src.exists():
        return []

    siblings_set: set[Path] = set()

    # Case 1: target is INSIDE lemmas/<area>_lemmas/ — siblings are other
    # files in the same dir.
    try:
        rel = target.relative_to(src)
    except ValueError:
        return []
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "lemmas" and parts[1].endswith("_lemmas"):
        helper_dir = src / "lemmas" / parts[1]
        for f in sorted(helper_dir.glob("*.rs")):
            if f.resolve() != target and f.name != "mod.rs":
                siblings_set.add(f.resolve())
        return sorted(siblings_set)

    # Case 2: target is <area>.rs at src/ top level — siblings are
    # lemmas/<area>_lemmas/*.rs (directory) AND lemmas/<area>_lemmas*.rs
    # (loose files at the top of lemmas/).
    if len(parts) == 1 and rel.suffix == ".rs":
        area = rel.stem
        lemmas_root = src / "lemmas"
        if not lemmas_root.is_dir():
            return []

        # Liberal prefix match: ANY file or directory under lemmas/ whose
        # name starts with "<area>_" (or is named exactly "<area>_lemmas")
        # is a sibling helper module. Covers all observed layouts:
        #   ristretto.rs   → lemmas/ristretto_lemmas/*.rs       (dir)
        #   edwards.rs     → lemmas/edwards_lemmas/*.rs         (dir)
        #   field.rs       → lemmas/field_lemmas/*.rs           (dir)
        #   window.rs      → (no lemma helpers; specs only)
        #   scalar.rs      → lemmas/scalar_lemmas.rs, scalar_lemmas_extra.rs,
        #                    scalar_batch_invert_lemmas.rs,
        #                    scalar_montgomery_lemmas.rs       (loose files)
        #                  + lemmas/scalar_lemmas_/*.rs,
        #                    lemmas/scalar_byte_lemmas/*.rs    (dirs)
        #   montgomery.rs  → lemmas/montgomery_lemmas.rs,
        #                    montgomery_curve_lemmas.rs,
        #                    montgomery_pow_chain_lemmas.rs    (loose files)
        prefix = f"{area}_"
        for entry in sorted(lemmas_root.iterdir()):
            if entry.name == "mod.rs":
                continue
            if not entry.name.startswith(prefix):
                continue
            if entry.is_file() and entry.suffix == ".rs":
                siblings_set.add(entry.resolve())
            elif entry.is_dir():
                for f in sorted(entry.glob("*.rs")):
                    if f.name != "mod.rs":
                        siblings_set.add(f.resolve())

    return sorted(siblings_set)


# ---------------- Commands ----------------

def cmd_snapshot(args) -> int:
    files = [args.target] + list(args.siblings or [])
    snapshot = {"files": {}}
    total_sigs = 0
    for f in files:
        text = f.read_text()
        sigs = _extract_sigs(text)
        snapshot["files"][str(f)] = {"sigs": sigs}
        total_sigs += len(sigs)
    args.out.write_text(json.dumps(snapshot, indent=2))
    logger.info("spec_check snapshot: %d files / %d sigs → %s",
                len(files), total_sigs, args.out)
    return 0


def _verify_one(file_path: str, original: dict, current: dict) -> list[dict]:
    """Return drift entries for one file. Each entry carries a `file` key
    so callers can route by source file."""
    drift: list[dict] = []
    for name, orig in original.items():
        if name not in current:
            drift.append({"file": file_path, "function": name,
                          "change": "removed",
                          "original_line": orig.get("line")})
            continue
        cur = current[name]
        for field_ in ("header", "requires", "ensures", "decreases", "mode"):
            if orig[field_] != cur[field_]:
                drift.append({
                    "file": file_path,
                    "function": name,
                    "change": "modified",
                    "field": field_,
                    "original": orig[field_][:400],
                    "current": cur[field_][:400],
                    "line": cur.get("line"),
                })
        if cur["external_body"] and not orig["external_body"]:
            drift.append({
                "file": file_path, "function": name,
                "change": "external_body_added",
                "line": cur.get("line"),
            })
    return drift


def cmd_verify(args) -> int:
    try:
        snapshot = json.loads(args.against.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"okay": False, "error": f"bad snapshot: {e}"}))
        return 1

    # Back-compat: old snapshots have top-level "sigs", new ones have "files".
    if "sigs" in snapshot and "files" not in snapshot:
        files_map = {snapshot.get("file", str(args.target)):
                     {"sigs": snapshot["sigs"]}}
    else:
        files_map = snapshot.get("files", {})

    drift: list[dict] = []
    new_fns: dict[str, list[str]] = {}
    for file_path, entry in files_map.items():
        p = Path(file_path)
        if not p.exists():
            drift.append({"file": file_path, "change": "file_missing"})
            continue
        current = _extract_sigs(p.read_text())
        original = entry["sigs"]
        drift.extend(_verify_one(file_path, original, current))
        added = sorted(set(current) - set(original))
        if added:
            new_fns[file_path] = added

    result = {
        "okay": len(drift) == 0,
        "drift": drift,
        "new_functions": new_fns,
    }
    print(json.dumps(result, indent=2))
    logger.info("spec_check verify: okay=%s drift=%d files=%d",
                result["okay"], len(drift), len(files_map))
    return 0 if result["okay"] else 1


def cmd_list_siblings(args) -> int:
    siblings = discover_sibling_helpers(args.project, args.target)
    print(json.dumps({
        "target": str(args.target.resolve()),
        "siblings": [str(s) for s in siblings],
    }, indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("target", type=Path)
    snap.add_argument("--out", type=Path, required=True)
    snap.add_argument("--siblings", type=Path, nargs="*", default=[],
                      help="Additional files to snapshot alongside the target")
    snap.set_defaults(func=cmd_snapshot)

    ver = sub.add_parser("verify")
    ver.add_argument("target", type=Path)
    ver.add_argument("--against", type=Path, required=True)
    ver.set_defaults(func=cmd_verify)

    lst = sub.add_parser("list-siblings")
    lst.add_argument("target", type=Path)
    lst.add_argument("--project", type=Path, required=True)
    lst.set_defaults(func=cmd_list_siblings)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
