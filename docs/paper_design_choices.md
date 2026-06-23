# Paper design choices — source of truth

This doc is the canonical catalog of **harness design choices the paper must
account for** — soundness-relevant mechanisms, rung/init-state constructions, and
known caveats. It exists so the paper-writing / fact-checking effort discovers the
ground truth *here* rather than reconstructing it from `run.py` each time, and so
findings about paper↔code mismatches survive across sessions.

**Scope:** durable design decisions and their soundness implications. Not run logs,
not eval numbers (those live in `docs/campaign_report.md` etc.). When you find a new
design choice the paper should reflect, append an entry here.

---

## 1. The cheat-class gate suite is **seven** gates, not five

The paper (abstract, intro, §3.4, §4.3, Fig. `fig:loop`, Table `tab:gates`,
App. B) describes a **five-gate** bank: admit-count, axiom-drift, spec-drift,
sibling-verus, tooling-drift. The code in `run.py` enforces **seven**: six
**terminal cheat-class `end_reason`s** that `_final_end_reason` never promotes to
COMPLETE — `SPEC_DRIFT`, `AXIOM_DRIFT`, `TOOLING_DRIFT`, `SIBLING_VERUS_FAIL`,
`GIT_RECOVERY`, `FROZEN_EDIT` ([run.py:188–195](../run.py)) — plus the
**admit-count** gate (a COMPLETE *precondition*: `done_for_real` requires zero hard
`admit()`s, not an `end_reason`). The two uncredited ones are both terminal drifts:

> **Audit note (verified against code 2026-06-21):** the six terminal `end_reason`s
> above are the complete set in `_final_end_reason` — confirmed by enumerating every
> `end_reason = "…"` assignment in `run.py`. What is *not* independently verifiable
> from this repo is the paper's side: there is no paper source tree here, so the
> claim "the paper lists exactly these five" rests on the prior summary, not a
> checked `.tex`. Confirm the five-gate enumeration in the paper before editing.

- **`GIT_RECOVERY`** — `detect_git_recovery()` ([run.py:508](../run.py)) scans every
  assistant `Bash` tool-use block each round and flags git commands that could
  surface the pre-strip original proof/lemmas from history: `git show HEAD:<path>`,
  `git restore`, `git checkout <ref> -- <path>`, `git log -p`/`-G`/`-S`,
  `git diff` vs HEAD (the removed `-` lines *are* the answer), `git cat-file -p`,
  `git stash show -p`, `git worktree add`. Metadata-only forms (`status`, `--stat`,
  `log --oneline`) are allowed. Wired into the round loop at
  [run.py:2371](../run.py); terminal via `_final_end_reason`
  ([run.py:188–195](../run.py)).
- **`FROZEN_EDIT`** — fails the round if the agent edits a file marked frozen in the
  spec-strip / no-api / bridge experiments ([run.py:2411, 2505](../run.py)). Also
  terminal.

**Why it's load-bearing (not just a missing list item).** The start state is a
stripped working tree whose `git HEAD` still holds the complete original proof. The
GIT_RECOVERY gate is what makes "no access to the human reference" true *inside a
single shared-history worktree* — without it the agent could `git show HEAD:` the
answer in one command.

- §6.5 *"An independent proof, not retrieval"* is **mechanically enforced** by
  GIT_RECOVERY, yet the paper attributes it to nothing. Citing the gate strengthens
  the result.
- §6.5's *"nothing but the `spec-drift` gate stands between the agent and a weakened
  guarantee"* is inaccurate as written — on reconstruction/retrieval the
  git-recovery gate is also standing there. True narrowly about spec *weakening*,
  but it reads as "spec-drift is the only active gate," which the code contradicts.
- §5/§6 *"fresh context with no access to the human reference"* — the git gate is how
  "no access" is operationalized.

**Fix:** add `git-recovery` (and `frozen-edit`) to the gate suite — prose in
§3.4/§4.3, a row in Table `tab:gates`, the `fig:loop` `bank` node, App. B. Have
§6.5 cite GIT_RECOVERY. Update every "five gates" / "five-gate bank" phrasing
(abstract, intro, §3, §4 caption) to seven. Leave campaign/eval numbers untouched.

---

## 2. Ristretto rung (`--no-ristretto-proof`) — pure proof-body strip + a soundness wrinkle

The spec-strip difficulty ladder was extended up one layer to RISTRETTO as a
pure proof-reconstruction run (`run.py --experiment-mode bridge-full`, target =
`ristretto.rs`, editable = ristretto.rs only). Facts from mapping
`CompressedRistretto::decompress`'s proof tree (2026-06-21):

1. **No dedicated deletable lemma layer.** Every lemma `decompress`/`step_1`/`step_2`
   calls lives in the FROZEN substrate (`field_lemmas/*`,
   `edwards_lemmas/curve_equation_lemmas.rs`, `specs/*`, vstd). The only
   `ristretto_lemmas/` references in the decompress path are two **axioms**
   (`axiom_ristretto_decode_on_curve`, `axiom_ristretto_decode_in_even_subgroup`
   in `lemmas/ristretto_lemmas/axioms.rs`) — un-deletable (axiom gate) and frozen.
   The other `ristretto_lemmas/` files (batch_compress, coset, elligator) serve
   `compress`/`batch_compress`/elligator, NOT decompress. → The rung is a **pure
   proof-body strip** (`--strip-proof-fn decompress step_1 step_2`, 325 proof
   lines), NO `--delete-fn`. Contrast the edwards rungs, which delete a real
   decompress lemma chain (see entry on the edwards `no_api_proof` analog).
2. **step_1 / step_2 are decompress's dedicated proof helpers** — `pub(super) fn`
   inside `mod decompress`, used only by decompress; the moral analog of edwards's
   `decompress_lemmas.rs`. Stripping all three = "reconstruct the ristretto
   decompress proof layer" with the whole edwards/field substrate frozen.
3. **SOUNDNESS WRINKLE — ristretto.rs carries its own `open spec fn`s** (unlike
   edwards.rs, which had zero, giving the no-api rung its "editable files contain
   zero spec fns" structural guarantee): `batch_state_limbs_bounded`,
   `batch_state_matches_point`, `from_spec`, `eq_spec`, `neg_spec`, `neg_req`,
   `ct_eq_req` — all seven present, scattered across the compress/From/Eq/Neg impls
   (verified at `ristretto.rs` 1435/1455/1482/2946/2973/2978/3301/3306, *not*
   clustered near one line). The spec gate snapshots fn HEADERS but NOT spec-fn
   bodies (verified — [`spec_check.py:15`](../skills/spec_check.py) "What's allowed
   to change freely: the function body"), so these editable spec-fn bodies are
   **NOT gate-frozen**. Mitigation: (a) decompress's contract references NONE of them
   (verified 2026-06-21 against the live worktree — its `ensures` vocab is exactly
   `spec_ristretto_decompress`, `edwards_point_as_nat`,
   `is_well_formed_edwards_point`, `is_in_even_subgroup`, all in frozen
   `specs/`), so the contract-under-test is
   structurally safe; (b) they're pinned by their own FROZEN consumers + the
   whole-crate verify; (c) post-run audit byte-identity. **Document this residual
   in any ristretto-rung claim** — it is weaker than the edwards rungs' structural
   guarantee.
4. **run.py needed a target-aware branch.** The generic `bridge-full` prompt block is
   hardwired to the edwards "decompress-path lemmas DELETED + Montgomery↔Edwards map"
   scenario, which misleads a ristretto agent (nothing is deleted). A
   `mode == "bridge-full" and target.stem == "ristretto"` branch in
   `build_experiment_block` renders a ristretto-tailored block (NO deletions,
   strip-only, leave other ristretto APIs alone). Edwards rungs fall through
   unchanged.

Init state (re-verified against the live worktree 2026-06-21): strip → whole-crate
`cargo verus verify` gave **2060 verified, 6 errors**, ALL confined to decompress
(`:388`) / step_1 (`:441`,`:445`) / step_2 (`:519`,`:522`,`:526`) — zero collateral
to lizard or frozen code. Baseline clean main = **2066/0**.

**Run executed — `ristretto_proof_001` (2026-06-21).** The rung was run end-to-end
(opus, 1 round, ~16.9 min, $7.97) and **COMPLETE**, then audited independently. All
runtime counts confirmed: reconstruction = **2063 verified, 0 errors** (3 below the
2066 baseline purely because the agent's proof uses 29 `assert … by` blocks vs gt's
64 — Verus counts each as a verification unit; identical guarantee). Soundness
wrinkle (3) was **never exercised**: every local `open spec fn` is byte-identical to
main, all three contracts are byte-identical, real exec code is byte-identical, only
`ristretto.rs` changed, 0 admit / assume / new-axiom / new-spec / git-recovery. The
agent added **no new helper lemmas** — it reconstructed all three proofs inline
against the frozen substrate. Full metrics + agent-vs-gt comparison:
[`results/ristretto_proof_001/ristretto/report.md`](../results/ristretto_proof_001/ristretto/report.md).

**Correction to a prior claim.** The no_api_proof report stated "the editable files
contain zero spec fns" as the structural-integrity basis. That is **imprecise**:
edwards.rs carries **17** `open spec fn`s, montgomery.rs **2**, ristretto.rs **7+**
(Eq/Add/Sub/From/Neg/well_formed trait specs). The actual guarantee for these rungs
is narrower: *no editable-file-local spec fn is referenced by any decompress-path
API contract* (verified), so the contract-under-test is safe; the local spec fns are
pinned by their own frozen consumers + the whole-crate verify, and audited
byte-identical post-run. Same residual as the ristretto wrinkle (3) above — it
applies to every rung whose editable set includes a top-level `.rs`.

---

## 3. Full-stack rung (`--no-fullstack-proof`) — all three decompress layers at once

The hardest rung: strip the ENTIRE decompress proof tree across all three API
layers and reconstruct simultaneously. = `no_api_proof` (edwards.rs::decompress +
montgomery.rs::to_edwards proofs stripped; 10 decompress-path lemmas deleted) ∪ the
ristretto rung (ristretto.rs decompress + step_1 + step_2 proofs stripped). **Five
editable files**; everything else frozen (the Montgomery↔Edwards map, every specs/*
vocabulary, ristretto_lemmas/* incl. axioms, field/number-theory substrate). Target
= ristretto.rs; `run.py --experiment-mode bridge-full`.

- **run.py prompt branch** (`build_experiment_block`): the pure-ristretto branch was
  gated to `len(allow_edit) == 1`; a new branch `target.stem == "ristretto" and
  len(allow_edit) > 1` renders a combined 3-layer prompt (enumerates the 5 stripped
  proofs + 10 deleted lemmas). Edwards-target and single-file-ristretto rungs are
  unaffected.
- **Same designed pinning as no_api_proof**: deleting `lemma_negation_preserves_curve`
  / `lemma_affine_to_extended_valid` makes the crate fail to *compile* (a FROZEN
  caller, `niels_addition_correctness.rs:248`, references them), forcing exact-name
  re-creation with a caller-strong contract.

**Run executed — `fullstack_proof_001` (2026-06-21).** opus, **1 round, ~63.9 min,
$27.71** (234k output tokens, 36M cache-read; a round-2 bloat reset was *scheduled*
but round 1 closed COMPLETE). **COMPLETE**, then audited independently. Verified at
the harness standard (rlimit 80) = **2063 / 0**. All five API contracts + every local
spec fn + all exec code byte-identical to main; only the 5 editable files changed;
0 admit (bar the 4 pre-existing group-law axioms, name-set unchanged) / assume /
new-axiom / new-spec / git-recovery; both forced-by-name lemmas re-created. The
agent's reconstruction is uniformly **leaner** than gt (every API proof shorter; net
−338 lines; step_2's `assert … by` blocks 56 → 8).

> **Caveat the paper should note — `montgomery::hash` rlimits out at the bare
> default rlimit.** At rlimit 80 (what `run.py --verus-rlimit 80` applies to gt AND
> agent, and what this crate needs for its heavy exec fns) the whole crate verifies
> 2063/0. At Verus's default rlimit (~10), ONE unrelated function —
> `montgomery.rs:260 fn hash`, a `Hash` impl — rlimits out. Verified non-issue:
> `hash` is **byte-identical to main** and the agent added no module-scope
> broadcast/global; definitive same-env A/B shows clean main = **2066/0 at default**
> (hash passes) while the agent tree = **2062/1**. So the more-compact reconstruction
> shifts crate-level Z3 resource accounting enough to tip an unchanged borderline
> function over the *default* budget — a resource artifact, NOT a weakening or
> unsound proof. It is the visible cost of a globally heavier (though line-leaner)
> proof tree. Full writeup:
> [`results/fullstack_proof_001/ristretto/report.md`](../results/fullstack_proof_001/ristretto/report.md).

---

## 4. Init-state construction is reconciled into one builder (`peel`) with an enforced pin rule

The rung init-states above were originally built by bespoke
`skills/strip_specs.py` / `admit.py` invocation sequences. They can now be
expressed as user-authored declarative peel manifests built by `peel.py`; the
resulting editable-file list is passed directly to `run.py`. **This is a
build-side change only — `run.py` and every gate in §1 are untouched; the
runtime soundness story is identical.** Two things here the paper should state
precisely:

- **The cut is now one totally-ordered axis (peel depth).** P1 proofs → P2 lemmas
  → P3 specs → P4 contract, strictly cumulative (depth N removes shells 1..N). The
  trusted floor (exec code + every `axiom_*`/`assume`/`external_body`) is never
  peeled, by construction — the shell ops only touch `proof fn` bodies (axiom-
  skipping) and the *named* lemmas/spec-fns/anchors in the manifest. The
  transform is deterministic and pinned by golden tests
  (`tests/test_peel.py` asserts hand-written exact output on real dalek
  fragments), so the init-state is reproducible byte-for-byte, not
  re-run-blessed.

- **The pin rule is an *enforced* soundness precondition at build time**
  (`peel._require_pin`). A cut is *self-pinning* — the agent cannot pass by
  weakening the guarantee — only while every artifact the frozen top-level
  contract is phrased in stays frozen. P1–P3 keep it frozen and self-pin. The two
  cuts that break it **refuse to build without a declared pin**: (a) **P4**
  strips the top-level contract itself; (b) any **P3 that deletes a `spec fn`**
  (e.g. the Montgomery↔Edwards bridge map) leaves the frozen contract phrased in
  an agent-reconstructed definition, so the contract alone under-determines it.
  In both cases peel demands `proof` (a retained frozen-and-proven consumer),
  `consumer:NAME`, or `oracle:REF`. This mechanizes, *at construction time*, the
  same discipline §2/§3's rungs got informally from "keep the consumer frozen":
  the `bridge-specs` rung (deletes the map) is exactly case (b), pinned by the
  frozen `montgomery::to_edwards` proof. **There is deliberately no depth→mode
  inference** — a monotone "strip shells 1..k" axis structurally cannot express
  "strip stratum k, keep 1..k-1 as the pin," so the experiment mode is declared
  in the manifest, not derived (see the long NOTE in `peel.py`).

The residual caveats from §2/§3 are unchanged by peel (it builds the same
trees): the editable-file-local `open spec fn`s are still pinned only by frozen
consumers + whole-crate verify + post-run byte-identity audit, not by the spec
gate. Build provenance for any rung is now reproducible via
`peel.py --surface --manifest <m> --depth <n>` (preview, no files touched).
