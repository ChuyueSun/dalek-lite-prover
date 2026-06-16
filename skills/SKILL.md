---
name: dalek-lite-mvp
description: "Exact flags, invocation syntax, and tactical notes for the dalek-lite proof CLIs: verus_check, spec_check, admit_inventory, search_semantic/module/macro/proven."
---

# Dalek-Lite MVP — Skills

CLIs the proof agent invokes via Bash. Each returns JSON on stdout
(see per-skill notes for the exceptions) and logs to `$CLI_LOG_PATH`
(set by `run.py` per task).

| Skill | Purpose | First thing to know |
|---|---|---|
| `verus_check.py` | Run `cargo verus --verify-module` | Source of truth for "did it verify" |
| `spec_check.py` | Snapshot / verify signature integrity | Agent edits that weaken specs fail the round |
| `admit_inventory.py` | Count actionable vs axiom admits and list their line numbers | `non_axiom_count == 0` is the COMPLETE condition; comments and `axiom_*` bodies are filtered out |
| `search_semantic.py` | Keyword/substring search over catalog (incl. vstd) | First try for "I'm looking for something about X" |
| `search_module.py` | List all sigs from one module (incl. vstd modules) | Use after seeing `use crate::...` or `use vstd::...` |
| `search_macro.py` | Expand `lemma_*!` macro families | Use when semantic search misses generated lemmas |
| `search_proven.py` | Query ProvenRegistry | Check if a lemma was proven earlier in the campaign |
| `diff_view.py` | Render admitted→final→truth diff as markdown | Diagnostic, not agent-facing; emits markdown, not JSON |
| `strip_specs.py` | Strip fn-header specs to build spec-inference eval inputs | Harness/eval tool, not used during a proof round |

The `infer_verus_spec/` directory is a documentation-only skill (no CLI) —
guidance for reconstructing stripped fn-header specs.

---

## Detailed reference (agent-facing skills)

**This section is the single source of truth for skill flags and examples.**
`prompt.md` carries only a one-line index and points here; keep the two in sync
(index terse, detail here). Each file also has `-h`.

In the commands below, substitute the concrete per-run paths printed in
`prompt.md`'s **## Target** block for the `<UPPER_CASE>` tokens — e.g. plug the
real catalog-cache path in for `<CATALOG_CACHE>`, and append the `<VSTD_FLAG>`
value (` --vstd-root …`, or nothing if vstd isn't indexed) to `search_*` calls.

### Verification

- `python skills/verus_check.py <TARGET> --project <PROJECT_ROOT>`
  Runs `cargo verus ... --verify-module` on the target module. JSON with
  `okay`, `messages[]`, `failed_declarations[]`. Call frequently — fast.

  **`--rlimit FLOAT`** is forwarded to verus (SMT resource limit in
  roughly-seconds). Default is verus's built-in (~10). For long exec-mode
  functions where verus hits a per-fn rlimit before your proof gets feedback,
  bump it: `--rlimit 80` (or higher). If you see `"resource limit exceeded"` in
  the error messages, this is the first lever to try — much cheaper than
  restructuring the proof.

- `python skills/spec_check.py verify <TARGET> --against <SPEC_SNAPSHOT>`
  Detect whether you've modified any original spec. Call before declaring
  COMPLETE. (The `snapshot` and `list-siblings` subcommands are harness-driven.)

- `python skills/admit_inventory.py <TARGET> [--siblings <helper.rs> ...]`
  Count actionable admits in the target (and any sibling helpers you changed).
  Returns `non_axiom_count`, `axiom_count`, plus per-line entries.
  `non_axiom_count == 0` is the COMPLETE condition. Comments and
  `proof fn axiom_*` bodies are filtered out automatically. Pass `--siblings`
  for any helper files you added so their non-axiom admits are counted too.

### Search (use aggressively when you need a lemma)

**The catalog indexes BOTH the project source AND vstd.** You can query vstd
modules (e.g. `vstd::arithmetic::mul`, `vstd::arithmetic::power2`, `vstd::bits`)
directly — no need to grep the vstd tree manually for lemma *names*.

- `python skills/search_semantic.py "<natural language query>" --project <PROJECT_ROOT> --catalog-cache <CATALOG_CACHE><VSTD_FLAG> -n 5`
  First thing to try when you don't know the exact name. Returns hits from BOTH
  dalek-lite source AND vstd. Examples:
  - `"pow2 adds and multiplies"` → finds `lemma_pow2_adds` in `vstd::arithmetic::power2`
  - `"distributive multiplication"` → finds `lemma_mul_is_distributive_add` in `vstd::arithmetic::mul`
  - `"field element bounded by prime"` → finds dalek-lite local lemmas

- `python skills/search_module.py "<module>" --project <PROJECT_ROOT> --catalog-cache <CATALOG_CACHE><VSTD_FLAG>`
  List every public signature in one module. Works for both project and vstd
  modules. Examples:
  - `"crate::lemmas::common_lemmas::pow_lemmas"` — pre-built dalek lemmas
  - `"vstd::arithmetic::mul"` — multiplication lemmas + broadcast groups
  - `"vstd::arithmetic::power2"` — pow2 lemmas
  Use after spotting a `use crate::foo::bar::*` or `use vstd::...` line.

- `python skills/search_macro.py --name-prefix lemma_u8_pow2 --project <PROJECT_ROOT> --catalog-cache <CATALOG_CACHE><VSTD_FLAG>`
  Many lemmas are generated by `lemma_*!(NAME, TYPE)` macro invocations and
  won't show up via grep of the source. This skill exposes those. Use when
  semantic search didn't find what you expected.

**Tactical-use note**: `search_*` returns signatures only — not doc comments or
proof bodies. If you need to see HOW a lemma is used or read its `///` docstring
for context, fall back to `Read` on the file or `grep -B2` for nearby context.

- `python skills/search_proven.py --results <RESULTS_ROOT> --name lemma_foo`
  Check whether a lemma you're about to call was proven earlier in this
  campaign. Prefer verified-in-registry lemmas over unverified ones.
