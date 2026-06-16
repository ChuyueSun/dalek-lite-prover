#!/usr/bin/env python3
"""Run cargo verus on a target module. Return axle-compatible JSON.

Usage:
    python skills/verus_check.py <file.rs> [--project <cargo_root>] [--module <name>]

Output (stdout):
    {"okay": bool, "messages": [{file, line, column, severity, data}],
     "failed_declarations": [name, ...]}

Exit code mirrors cargo verus (0 = okay, non-zero = failed).
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(os.environ.get(
        "CLI_LOG_PATH", Path(__file__).parents[1] / "cli.log")))],
)
logger = logging.getLogger("verus_check")


# Pattern: "error: message" + "  --> path/file.rs:LINE:COL" pairs
_ERR_HEADER_RE = re.compile(r"^(?P<severity>error|warning|note)(?:\[[^\]]+\])?:\s*(?P<msg>.+)")
_ERR_LOC_RE = re.compile(r"^\s*-->\s*(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)")
_FAILED_DECL_RE = re.compile(r"error:\s*(?:precondition|postcondition|assertion|invariant)[^`]*`(?P<name>[^`]+)`")


def find_cargo_root(target: Path) -> Path:
    p = target.parent if target.is_file() else target
    while p != p.parent:
        if (p / "Cargo.toml").exists():
            return p
        p = p.parent
    return target.parent


def parse_diagnostics(stderr: str) -> list[dict]:
    messages: list[dict] = []
    lines = stderr.splitlines()
    i = 0
    while i < len(lines):
        m = _ERR_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        sev, msg = m.group("severity"), m.group("msg")
        file_, line, col = None, 0, 0
        # Look at next few lines for a location pointer
        for j in range(i + 1, min(i + 6, len(lines))):
            lm = _ERR_LOC_RE.match(lines[j])
            if lm:
                file_, line, col = lm.group("file"), int(lm.group("line")), int(lm.group("col"))
                break
        messages.append({
            "file": file_ or "",
            "line": line,
            "column": col,
            "severity": sev,
            "data": msg,
        })
        i += 1
    return messages


def extract_failed_declarations(stderr: str) -> list[str]:
    names = set()
    for m in _FAILED_DECL_RE.finditer(stderr):
        names.add(m.group("name"))
    return sorted(names)


def derive_module(target: Path, project_root: Path) -> str:
    rel = target.resolve().relative_to(project_root.resolve())
    parts = list(rel.parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts:
        parts[-1] = parts[-1].removesuffix(".rs")
    if parts and parts[-1] in ("mod", "lib"):
        parts = parts[:-1]
    return "::".join(parts)


def run(target: Path, project_root: Path, module: str | None, timeout: int,
        rlimit: float | None = None) -> dict:
    import os, signal as _signal
    mod = module or derive_module(target, project_root)
    # `cargo verus verify` runs `cargo build` and passes post-`--` args to verus.
    #
    # We use `--verify-module M` (NOT `--verify-only-module M`). The latter
    # checks ONLY the top-level module M — sub-modules like `M::decompress`,
    # `M::tests`, and any `mod X { }` blocks inside the file are SILENTLY
    # SKIPPED. This was a real harness bug: ristretto.rs has `mod decompress`
    # holding step_1/step_2; their proofs went unverified for the entire
    # campaign because `--verify-only-module ristretto` excluded them.
    # `--verify-module M` includes M and all its descendants.
    cmd = ["cargo", "verus", "verify"]
    # If project_root is inside a Cargo workspace, scope verification to the
    # member package so other workspace deps (e.g. vstd) aren't re-verified
    # and won't reject --verify-module that points to a member-local module.
    try:
        cargo_toml = (project_root / "Cargo.toml").read_text()
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', cargo_toml, re.M)
        pkg_name = m.group(1) if m else None
        parent_toml = project_root.parent / "Cargo.toml"
        if pkg_name and parent_toml.exists() and "[workspace]" in parent_toml.read_text():
            cmd += ["-p", pkg_name]
    except Exception:
        pass
    verus_args: list[str] = []
    if mod:
        verus_args += ["--verify-module", mod]
    if rlimit is not None:
        verus_args += ["--rlimit", str(rlimit)]
    if verus_args:
        cmd += ["--"] + verus_args

    logger.info("verus_check: cmd=%s cwd=%s", " ".join(cmd), project_root)
    # Use Popen + start_new_session=True so cargo verus + rust_verify + z3 all
    # share a process group we can SIGKILL together. subprocess.run's timeout
    # only kills the direct child (cargo verus), leaving z3 orphaned.
    proc = subprocess.Popen(
        cmd, cwd=str(project_root),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the entire process group (cargo verus, rust_verify, z3, ...)
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return {
            "okay": False,
            "messages": [{
                "file": str(target), "line": 0, "column": 0,
                "severity": "error",
                "data": (f"verus timed out after {timeout}s and was killed "
                         f"(cargo + z3 + rust_verify). Proof likely too complex "
                         f"for SMT — split into smaller lemmas with explicit "
                         f"intermediate `assert(...) by (...)` steps."),
            }],
            "warning_count": 0,
            "failed_declarations": [],
            "returncode": -9,
            "stderr_tail": "",
        }

    # `proc.communicate()` already captured stdout/stderr above
    stderr = stderr or ""
    all_messages = parse_diagnostics(stderr)
    errors = [m for m in all_messages if m["severity"] == "error"]
    warnings = [m for m in all_messages if m["severity"] == "warning"]
    has_error = bool(errors) or proc.returncode != 0
    return {
        "okay": not has_error,
        # `messages` holds only real errors — what the agent needs to act on
        "messages": errors,
        "warning_count": len(warnings),
        "failed_declarations": extract_failed_declarations(stderr),
        "returncode": proc.returncode,
        "stderr_tail": stderr[-4000:] if stderr else "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run cargo verus; emit JSON")
    ap.add_argument("target", type=Path)
    ap.add_argument("--project", type=Path, default=None,
                    help="Cargo project root (auto-detected if omitted)")
    ap.add_argument("--module", help="Override the --verify-only-module argument")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--rlimit", type=float, default=None,
                    help="Pass --rlimit FLOAT to verus (SMT resource limit, "
                         "roughly seconds). Default 10. Increase for "
                         "complex proofs that hit per-fn rlimit ceilings.")
    args = ap.parse_args()

    target = args.target.resolve()
    if not target.exists():
        print(json.dumps({
            "okay": False,
            "messages": [{"severity": "error", "data": f"File not found: {target}",
                          "line": 0, "column": 0, "file": str(target)}],
            "failed_declarations": [],
        }), flush=True)
        sys.exit(1)

    project = (args.project or find_cargo_root(target)).resolve()
    logger.info("verus_check target=%s project=%s module=%s",
                target, project, args.module)

    result = run(target, project, args.module, args.timeout, args.rlimit)
    logger.info("verus_check result: okay=%s errors=%d",
                result["okay"],
                sum(1 for m in result["messages"] if m["severity"] == "error"))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["okay"] else 1)


if __name__ == "__main__":
    main()
