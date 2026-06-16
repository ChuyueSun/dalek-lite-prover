"""Tests for the canonical axiom-aware admit counter and the
inventory JSON shape used by `skills/admit_inventory.py`.

Replaces (and merges) the previous two files:
  - tests/test_count_admits.py (algorithm regression table)
  - tests/test_admit_inventory.py (JSON-shape + cross-check tests)

Single algorithm now lives in `lib.admits`. `run._count_llm_target_admits`
is an import alias of `lib.admits.count_non_axiom`, so callers of either
name run identical code — verified by `AliasIntegrity`.

Run: `python3 -m unittest tests.test_admits`
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.admits import (  # noqa: E402
    admit_proof_blocks,
    admit_proof_fn_bodies,
    classify_admit_lines,
    count_non_axiom,
    find_proof_fn_body_brace,
    inventory_file,
    inventory_files,
)


# ---------- algorithm regression table ----------------------------------
# Migrated verbatim from the previous tests/test_count_admits.py.
# These fixtures pin specific bugs found in PR review and during real
# agent runs. Don't delete cases here without understanding why each
# one was added — see comments on each block.

class AdmitCounterRegressionTable(unittest.TestCase):
    """Each case is (description, source, expected_non_axiom_count)."""

    CASES = [
        # --- basic ---
        ("bare admit() in non-axiom function",
         "pub proof fn lemma_normal() {\n    admit();\n}\n", 1),
        ("no admits at all",
         "pub proof fn lemma_x() {\n    let y = 1;\n}\n", 0),

        # --- regression: pippenger doc-comment bug ---
        ("doc comment mentioning admit() text does not count",
         "//! All proofs are done — no `admit()` remains.\n"
         "pub proof fn lemma_x() {\n    let y = 1;\n}\n", 0),
        ("inline `//` comment with admit() text does not count",
         "pub proof fn lemma_x() {\n    let y = 1;  // admit() once\n}\n", 0),

        # --- axiom-by-convention exclusion ---
        ("top-level axiom_* body's admit is excluded",
         "pub proof fn axiom_x()\n{\n    admit();\n}\n", 0),
        ("axiom_* admit excluded; later non-axiom admit counted",
         "pub proof fn axiom_x()\n{\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- regression: indented closing brace (P1) ---
        ("indented axiom inside impl block — closing } is indented",
         "impl X {\n    pub proof fn axiom_indented() {\n"
         "        admit()\n    }\n}\n"
         "pub proof fn lemma_outer() {\n    admit();\n}\n", 1),

        # --- regression: brace-counting confused by ensures ({...}) (P2 v1) ---
        # This is the case that broke a 2026-05 reimplementation of the
        # counter: the `{` inside `({ ... })` was mistaken for the body
        # opener. The _BODY_OPEN_RE end-of-line anchor is what prevents
        # this — do NOT loosen it.
        ("inline ensures ({...}) followed by standalone { body opener",
         "pub proof fn axiom_x()\n    ensures\n"
         "        ({ let z = 1; z == 1 }),\n{\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- regression: same-line body opener on final sig line (P2 v2) ---
        ("multi-line header, `ensures e, {` on its own line",
         "pub proof fn axiom_x(y: int)\n    ensures\n        y > 0, {\n"
         "    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),
        ("multi-line header with `) {` body opener",
         "pub proof fn axiom_x(\n    y: int,\n) {\n    admit();\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- body with nested {} blocks should not exit early ---
        ("axiom body with nested if{}",
         "pub proof fn axiom_x() {\n    if true {\n        admit()\n    }\n}\n"
         "pub proof fn lemma_y() {\n    admit();\n}\n", 1),

        # --- real-world Verus: array-type ; in args (must not confuse
        #     the counter — `;` is only a body-end signal when at top level)
        ("fn with `&[u8; 32]` arg — array-type `;` is harmless",
         "pub fn encode_253_bits(data: &[u8; 32]) -> Option<u8> {\n"
         "    admit();\n    None\n}\n", 1),
        ("axiom_* fn with array-type arg",
         "pub proof fn axiom_array_pkg(buf: &[u8; 64]) {\n"
         "    admit();\n}\n", 0),
    ]

    def test_table(self):
        for desc, src, want in self.CASES:
            with self.subTest(desc=desc):
                got = count_non_axiom(src)
                self.assertEqual(got, want, f"{desc}: got {got}, want {want}")


# ---------- inventory JSON shape ----------------------------------------

class InventoryJsonShape(unittest.TestCase):
    """The `skills/admit_inventory.py` CLI uses `inventory_file` /
    `inventory_files` from `lib.admits`. Pin the JSON shape callers
    (including the agent's prompt-guided reuse of `non_axiom_count`)
    depend on."""

    def test_inventory_classifies_axiom_and_non_axiom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "target.rs"
            p.write_text(
                "// admit() in comment ignored\n"
                "pub proof fn axiom_foundation() {\n    admit();\n}\n"
                "pub proof fn lemma_needed() {\n"
                "    // another admit() comment\n    admit();\n}\n"
            )
            inv = inventory_files([p])

        self.assertEqual(inv["non_axiom_count"], 1)
        self.assertEqual(inv["axiom_count"], 1)
        self.assertFalse(inv["okay_for_complete"])
        # Per-admit entries report file + line only (no fn name).
        self.assertEqual(
            set(inv["non_axiom_admits"][0].keys()), {"file", "line"})
        self.assertEqual(
            set(inv["axiom_admits"][0].keys()), {"file", "line"})

    def test_okay_when_only_axiom_admits_remain(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "target.rs"
            p.write_text("pub proof fn axiom_only() {\n    admit();\n}\n")
            inv = inventory_files([p])

        self.assertEqual(inv["non_axiom_count"], 0)
        self.assertEqual(inv["axiom_count"], 1)
        self.assertTrue(inv["okay_for_complete"])

    def test_line_numbers_are_one_indexed_and_point_at_admit(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "target.rs"
            p.write_text(
                "pub proof fn lemma_a() {\n"   # line 1
                "    admit();\n"                # line 2
                "}\n"                           # line 3
                "pub proof fn lemma_b() {\n"   # line 4
                "    let y = 1;\n"              # line 5
                "    admit();\n"                # line 6
                "}\n"                           # line 7
            )
            inv = inventory_file(p)
        self.assertEqual(
            [a["line"] for a in inv["non_axiom_admits"]], [2, 6])

    def test_classify_lines_returns_both_partitions(self):
        cls = classify_admit_lines(
            "pub proof fn axiom_a() {\n    admit();\n}\n"  # line 2 axiom
            "pub proof fn lemma_b() {\n    admit();\n}\n"  # line 5 non-axiom
        )
        self.assertEqual(cls["non_axiom_lines"], [5])
        self.assertEqual(cls["axiom_lines"], [2])


# ---------- alias integrity ---------------------------------------------

class AliasIntegrity(unittest.TestCase):
    """`run._count_llm_target_admits` is an import alias of
    `lib.admits.count_non_axiom`. Pin that this stays true — if someone
    accidentally reintroduces a local definition with the same name in
    run.py, this test catches the divergence on the first run."""

    def test_run_py_alias_is_lib_admits_count_non_axiom(self):
        from run import _count_llm_target_admits as run_counter
        self.assertIs(run_counter, count_non_axiom,
                      "run._count_llm_target_admits must be the same object "
                      "as lib.admits.count_non_axiom (drop any local "
                      "reimplementation that shadows the import)")


# ---------- real-file anchor tests --------------------------------------

class RealFileInvariants(unittest.TestCase):
    """Anchor tests against a live Verus worktree.

    Hard-coded counts go stale the moment an agent run mutates a file,
    which produced a confusing false failure during earlier patch work.
    Instead, this class tests *invariants* that hold regardless of
    intermediate file state:

      1. Counts are bounded: 0 ≤ count ≤ raw_grep_count('admit()').
      2. Files known to be pure-axiom or pure-doc-comment must return 0
         (these are stable properties of the file's structure, not of a
         mutable admit count).

    Worktree path resolution:
      1. `$DALEK_WORKTREE` environment variable, else
      2. first existing path from `_CANDIDATE_WORKTREES`, else
      3. skip the entire class (suite stays runnable in CI / fresh
         clones / unrelated machines).
    """

    _CANDIDATE_WORKTREES = (
        Path.home() / "dalek-lite/curve25519-dalek",
    )

    @classmethod
    def _resolve_worktree(cls) -> Path | None:
        env = os.environ.get("DALEK_WORKTREE")
        if env:
            p = Path(env).expanduser()
            return p if p.exists() else None
        for p in cls._CANDIDATE_WORKTREES:
            if p.exists():
                return p
        return None

    EXPECT_ZERO = (
        ("src/lemmas/edwards_lemmas/pippenger_lemmas.rs",
         "doc-comment mentions admit() — must not count"),
        ("src/specs/edwards_specs.rs",       "single axiom_*-bodied admit"),
        ("src/specs/window_specs.rs",        "single axiom_*-bodied admit"),
    )

    EXPECT_BOUNDED = (
        ("src/lemmas/edwards_lemmas/curve_equation_lemmas.rs",
         "mid-run count varies; bounds must always hold"),
    )

    def setUp(self):
        wt = self._resolve_worktree()
        if wt is None:
            env_hint = "set $DALEK_WORKTREE or place a worktree at one of: " + \
                       ", ".join(str(p) for p in self._CANDIDATE_WORKTREES)
            self.skipTest(f"no Verus worktree found ({env_hint})")
        self.worktree = wt

    @staticmethod
    def _raw_admit_count(text: str) -> int:
        return text.count("admit()")

    def test_zero_invariant(self):
        for rel, why in self.EXPECT_ZERO:
            with self.subTest(file=rel):
                f = self.worktree / rel
                if not f.exists():
                    self.skipTest(f"{rel} missing in this worktree")
                got = count_non_axiom(f.read_text())
                self.assertEqual(
                    got, 0,
                    f"{rel}: expected 0 LLM-target admits ({why}); got {got}")

    def test_bounded_invariant(self):
        files = [(rel, why) for rel, why in self.EXPECT_BOUNDED] + \
                [(rel, why) for rel, why in self.EXPECT_ZERO]
        for rel, why in files:
            with self.subTest(file=rel):
                f = self.worktree / rel
                if not f.exists():
                    self.skipTest(f"{rel} missing in this worktree")
                text = f.read_text()
                got = count_non_axiom(text)
                raw = self._raw_admit_count(text)
                self.assertGreaterEqual(
                    got, 0, f"{rel}: counter returned negative ({got})")
                self.assertLessEqual(
                    got, raw,
                    f"{rel}: counter ({got}) exceeds raw 'admit()' "
                    f"line count ({raw}) — over-counting bug")


# ---------- rejection continue message ----------------------------------

class RejectionContinueMsg(unittest.TestCase):
    """The continuation message sent on the next round when a previous
    `END_REASON:COMPLETE` is overridden. Pin that the message
    (a) actually changes from the default `"continue"`, and
    (b) names the specific rejection cause so the agent can act on it."""

    def test_verus_failing_with_admits_remaining(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=False, admits_left=1)
        self.assertNotEqual(msg, "continue")
        self.assertIn("rejected", msg)
        self.assertIn("verus_okay=False", msg)
        self.assertIn("admits remaining=1", msg)

    def test_verus_passing_but_admits_remaining(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=True, admits_left=2)
        self.assertIn("verus_okay=True", msg)
        self.assertIn("admits remaining=2", msg)

    def test_message_mentions_recovery_options(self):
        from run import _rejection_continue_msg
        msg = _rejection_continue_msg(verus_okay=False, admits_left=3)
        self.assertIn("verus_check", msg)
        self.assertIn("COMPLETE", msg)
        self.assertIn("LIMIT", msg)


# ---------- final-state gate / NEEDS_DECOMP escalation ------------------

class FinalEndReasonGate(unittest.TestCase):
    """`run._final_end_reason` resolves the recorded end_reason from the
    final-state gate. Pin the decision table — especially that NEEDS_DECOMP
    (the Feature2 escalation) is preserved when the proof did NOT actually
    finish, but is promoted to COMPLETE when it did. Demoting it to LIMIT
    would lose the "needs missing infrastructure" signal a retry relies on
    to bump its budget."""

    def test_done_for_real_is_always_complete(self):
        from run import _final_end_reason
        # Regardless of the agent's self-declared reason.
        for claimed in ("COMPLETE", "LIMIT", "NEEDS_DECOMP", None, ""):
            with self.subTest(claimed=claimed):
                self.assertEqual(
                    _final_end_reason(True, claimed), "COMPLETE")

    def test_needs_decomp_preserved_when_not_done(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "NEEDS_DECOMP"), "NEEDS_DECOMP")

    def test_needs_decomp_is_case_insensitive(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "needs_decomp"), "NEEDS_DECOMP")

    def test_unfinished_complete_claim_demoted_to_limit(self):
        from run import _final_end_reason
        # Agent claimed COMPLETE but evidence disagrees (not done_for_real).
        self.assertEqual(_final_end_reason(False, "COMPLETE"), "LIMIT")

    def test_honest_limit_and_missing_reason_are_limit(self):
        from run import _final_end_reason
        self.assertEqual(_final_end_reason(False, "LIMIT"), "LIMIT")
        self.assertEqual(_final_end_reason(False, None), "LIMIT")
        self.assertEqual(_final_end_reason(False, ""), "LIMIT")

    def test_rate_limited_preserved_when_not_done(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "RATE_LIMITED"), "RATE_LIMITED")

    def test_rate_limited_beats_done_for_real(self):
        from run import _final_end_reason
        # A 429 halt must NOT be promoted to COMPLETE even when the target
        # trivially verifies (zero hard admits) — otherwise the throttle is
        # masked, the launcher won't halt, and a trivial target lands in
        # proven_registry off a round the agent never ran.
        self.assertEqual(
            _final_end_reason(True, "RATE_LIMITED"), "RATE_LIMITED")

    def test_rate_limited_is_case_insensitive(self):
        from run import _final_end_reason
        self.assertEqual(
            _final_end_reason(False, "rate_limited"), "RATE_LIMITED")
    def test_drift_signals_never_promoted_to_complete(self):
        from run import _final_end_reason
        # Cheating signals win even when verus is green (done_for_real=True):
        # a weakened spec / injected axiom / doctored verification skill is how
        # an agent fakes a green.
        for drift in ("SPEC_DRIFT", "AXIOM_DRIFT", "TOOLING_DRIFT",
                      "spec_drift", "axiom_drift", "tooling_drift"):
            with self.subTest(drift=drift):
                self.assertEqual(
                    _final_end_reason(True, drift), drift.upper())
                self.assertEqual(
                    _final_end_reason(False, drift), drift.upper())

    def test_sibling_verus_fail_is_terminal(self):
        from run import _final_end_reason
        # A broken sibling/top-level module is not a cheat, but a target-only
        # green is still not "done" — never promote it to COMPLETE.
        self.assertEqual(
            _final_end_reason(True, "SIBLING_VERUS_FAIL"), "SIBLING_VERUS_FAIL")
        self.assertEqual(
            _final_end_reason(False, "sibling_verus_fail"), "SIBLING_VERUS_FAIL")


class ClassifyLemmaInAxiomFile(unittest.TestCase):
    """`run.classify_remaining_admits` must classify a `lemma_*` admit as
    'hard' (an unfinished proof to pursue) even in an axioms.rs file or under
    an 'Axiom:' docstring, while a real `axiom_*` stays 'intentional'.

    Pins the false-green fix (merge-pr2-clean 3cd1183): montgomery_curve_lemmas
    ran 8 rounds, closed 0 of 4 obligations, yet emitted COMPLETE because the
    lemma_* obligations sat under their original 'Axiom:' docstrings and were
    mis-flagged intentional. Without the lemma_* guard, `hard` undercounts and
    a never-proved module gets promoted LIMIT->COMPLETE."""

    def _classify(self, src: str, name: str = "axioms.rs") -> dict:
        from run import classify_remaining_admits
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / name
            p.write_text(src)
            return classify_remaining_admits(p)

    def test_lemma_in_axioms_file_is_hard(self):
        # File basename axioms.rs would flag everything intentional, but a
        # lemma_* admit is an unfinished proof.
        src = (
            "proof fn lemma_foo()\n"
            "    ensures true\n"
            "{\n"
            "    admit()\n"
            "}\n"
        )
        res = self._classify(src, name="axioms.rs")
        self.assertEqual(res["hard"], 1, res["detail"])
        self.assertEqual(res["intentional"], 0, res["detail"])

    def test_lemma_under_axiom_docstring_is_hard(self):
        src = (
            "/// Axiom: this used to be assumed.\n"
            "proof fn lemma_bar()\n"
            "    ensures true\n"
            "{\n"
            "    admit()\n"
            "}\n"
        )
        res = self._classify(src, name="curve_lemmas.rs")
        self.assertEqual(res["hard"], 1, res["detail"])

    def test_axiom_fn_stays_intentional(self):
        src = (
            "proof fn axiom_foundational()\n"
            "    ensures true\n"
            "{\n"
            "    admit()\n"
            "}\n"
        )
        res = self._classify(src, name="axioms.rs")
        self.assertEqual(res["intentional"], 1, res["detail"])
        self.assertEqual(res["hard"], 0, res["detail"])


class AreaTopLevelModules(unittest.TestCase):
    """`run._area_top_level_modules` maps a lemmas/<area>_lemmas sibling to the
    top-level module(s) that consume it — the sibling-verify gate re-checks
    those when the agent edits a helper."""

    def _map(self, p: str):
        from run import _area_top_level_modules
        return _area_top_level_modules(Path(p))

    def test_field_lemmas_maps_to_field_modules(self):
        self.assertEqual(
            self._map("/x/src/lemmas/field_lemmas/u64_5_lemmas.rs"),
            ["field", "backend::serial::u64::field"])

    def test_edwards_and_ristretto_areas(self):
        self.assertEqual(
            self._map("/x/src/lemmas/edwards_lemmas/mul_base_lemmas.rs"),
            ["edwards"])
        self.assertEqual(
            self._map("/x/src/lemmas/ristretto_lemmas/elligator_lemmas.rs"),
            ["ristretto"])

    def test_no_top_level_consumer_returns_empty(self):
        # common_lemmas has no top-level consumer; a non-lemmas path likewise.
        self.assertEqual(
            self._map("/x/src/lemmas/common_lemmas/foo.rs"), [])
        self.assertEqual(self._map("/x/src/edwards.rs"), [])


class AxiomFnNames(unittest.TestCase):
    """`lib.admits.axiom_fn_names` backs the axiom-integrity gate."""

    def test_captures_axiom_names_with_modifiers(self):
        from lib.admits import axiom_fn_names
        src = (
            "pub proof fn axiom_a() { admit() }\n"
            "broadcast proof fn axiom_b() { admit() }\n"
            "pub broadcast proof fn axiom_c() {}\n"
            "proof fn lemma_not_axiom() {}\n"
            "spec fn axiom_lookalike() -> bool { true }\n"  # not a proof fn
        )
        self.assertEqual(
            axiom_fn_names(src), {"axiom_a", "axiom_b", "axiom_c"})

    def test_new_axiom_detected_by_set_diff(self):
        from lib.admits import axiom_fn_names
        before = axiom_fn_names("pub proof fn axiom_a() { admit() }\n")
        after = axiom_fn_names(
            "pub proof fn axiom_a() { admit() }\n"
            "proof fn axiom_cheat() { admit() }\n")
        self.assertEqual(after - before, {"axiom_cheat"})


class EndReasonRegex(unittest.TestCase):
    """`run.END_REASON_RE` parses the agent's `END_REASON:<TOKEN>` line.
    Pin that NEEDS_DECOMP is recognised alongside COMPLETE/LIMIT, that the
    match is case-insensitive and line-anchored, and that prose merely
    mentioning the token does not match."""

    def _last(self, text: str):
        from run import END_REASON_RE
        matches = END_REASON_RE.findall(text)
        return matches[-1].upper() if matches else None

    def test_recognises_all_three_tokens(self):
        self.assertEqual(self._last("END_REASON:COMPLETE"), "COMPLETE")
        self.assertEqual(self._last("END_REASON:LIMIT"), "LIMIT")
        self.assertEqual(self._last("END_REASON:NEEDS_DECOMP"), "NEEDS_DECOMP")

    def test_case_insensitive(self):
        self.assertEqual(self._last("end_reason:needs_decomp"), "NEEDS_DECOMP")

    def test_last_token_wins_over_earlier_mention(self):
        # Agent reasons aloud, then commits on the final line.
        text = ("I considered END_REASON:LIMIT but the lemma is missing.\n"
                "MISSING: lemma_reduce_chain_5\n"
                "END_REASON:NEEDS_DECOMP\n")
        self.assertEqual(self._last(text), "NEEDS_DECOMP")

    def test_inline_prose_mention_does_not_match(self):
        # No line is *just* the token, so nothing matches (line-anchored).
        self.assertIsNone(
            self._last("emit END_REASON:NEEDS_DECOMP when blocked"))


# ---------- admit-skeleton creation (mode-aware) ------------------------
# These pin the *correct*, mode-aware admitter: only proof fn bodies +
# inline proof {} blocks are admitted; axiom_* fns, spec fn defs, and exec
# code are preserved.

class FindProofFnBodyBrace(unittest.TestCase):
    """`find_proof_fn_body_brace` locates the body-opening `{`, skipping
    Verus clause braces (`forall ==> {}`, `by {}`, `if/else {}`)."""

    def test_simple_proof_fn(self):
        code = "proof fn foo() { body }"
        self.assertEqual(
            find_proof_fn_body_brace(code, code.index("proof")),
            code.index("{"))

    def test_body_brace_on_own_line(self):
        code = (
            "proof fn foo(x: int)\n"
            "    requires\n"
            "        x > 0,\n"
            "    ensures\n"
            "        x + 1 > 1,\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("proof"))
        self.assertIsNotNone(r)
        self.assertEqual(code[r], "{")
        self.assertEqual(code[r - 1], "\n")

    def test_skips_forall_implies_brace(self):
        code = (
            "pub proof fn lemma(digits: Seq<Seq<i8>>)\n"
            "    requires\n"
            "        forall|k: int|\n"
            "            0 <= k < digits.len() ==> {\n"
            "                &&& digits[k].len() == 64\n"
            "            },\n"
            "    ensures\n"
            "        result == true,\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("pub proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")

    def test_skips_by_brace_in_clause(self):
        code = (
            "proof fn lemma(x: int)\n"
            "    requires\n"
            "        x > 0,\n"
            "    ensures\n"
            "        (x + 1 > 1) by {\n"
            "            // clause proof\n"
            "        },\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")

    def test_skips_if_else_braces(self):
        code = (
            "proof fn lemma(x: int) -> (r: int)\n"
            "    ensures\n"
            "        r == if x > 0 { x } else { -x },\n"
            "{\n"
            "    admit();\n"
            "    0\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")

    def test_one_liner_fn_sig_line(self):
        code = "pub proof fn lemma_trivial() { admit() }"
        self.assertEqual(
            find_proof_fn_body_brace(code, code.index("pub proof")),
            code.index("{"))

    def test_pub_crate_proof_fn(self):
        code = (
            "pub(crate) proof fn helper()\n"
            "    requires true,\n"
            "{\n"
            "    admit()\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("pub(crate)"))
        self.assertIsNotNone(r)
        self.assertEqual(code[r], "{")

    def test_no_brace_returns_none(self):
        self.assertIsNone(find_proof_fn_body_brace("proof fn foo()", 0))

    def test_real_straus_pattern(self):
        code = (
            "pub proof fn lemma_straus_ct_correct(\n"
            "    scalars: Seq<Scalar>,\n"
            "    digits: Seq<Seq<i8>>,\n"
            ")\n"
            "    requires\n"
            "        scalars.len() == digits.len(),\n"
            "        forall|k: int|\n"
            "            0 <= k < digits.len() ==> {\n"
            "                &&& (#[trigger] digits[k]).len() == 64\n"
            "                &&& radix_16_all_bounded_seq(digits[k])\n"
            "            },\n"
            "    ensures\n"
            "        straus_ct_partial(digits, 0) == true,\n"
            "    decreases scalars.len(),\n"
            "{\n"
            "    let n = scalars.len();\n"
            "}"
        )
        r = find_proof_fn_body_brace(code, code.index("pub proof"))
        self.assertIsNotNone(r)
        body_line_start = code.rfind("\n", 0, r) + 1
        self.assertEqual(code[body_line_start:r].strip(), "")
        self.assertIn("let n = scalars.len();", code[r:])


class AdmitProofFnBodies(unittest.TestCase):
    """`admit_proof_fn_bodies` replaces proof fn bodies with a type-correct
    admit() skeleton, keeping signatures + clauses, skipping
    axiom_*/exec/spec fns."""

    def test_simple_admit(self):
        result = admit_proof_fn_bodies(
            "proof fn foo() {\n    some_proof_code();\n}")
        self.assertIn("admit()", result)
        self.assertNotIn("some_proof_code", result)

    def test_preserves_requires_ensures(self):
        code = (
            "proof fn lemma(x: int)\n"
            "    requires\n"
            "        x > 0,\n"
            "    ensures\n"
            "        x + 1 > 1,\n"
            "{\n"
            "    // complex proof\n"
            "    assert(x + 1 > 1);\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        for s in ("requires", "x > 0", "ensures", "x + 1 > 1", "admit()"):
            self.assertIn(s, result)
        self.assertNotIn("complex proof", result)

    def test_bool_return_type(self):
        code = (
            "proof fn check(x: int) -> (b: bool)\n"
            "    ensures b == (x > 0),\n"
            "{\n"
            "    x > 0\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("admit();", result)
        self.assertIn("true", result)

    def test_int_return_type(self):
        code = (
            "proof fn compute(x: int) -> (n: int)\n"
            "    ensures n >= 0,\n"
            "{\n"
            "    x.abs()\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("admit();", result)
        self.assertIn("\n    0\n", result)

    def test_unit_return_type(self):
        result = admit_proof_fn_bodies(
            "proof fn lemma() {\n    some_proof();\n}")
        self.assertIn("{\n    admit()\n}", result)

    def test_unnamed_return_falls_through(self):
        # Documented & kept boundary: only the named `-> (n: T)` form is
        # detected; an unnamed `-> bool` falls through to a bare admit()
        # (no trailing value). See `_admit_body_for_return`.
        result = admit_proof_fn_bodies(
            "proof fn f() -> bool {\n    real();\n}")
        self.assertIn("{\n    admit()\n}", result)
        self.assertNotIn("true", result)
        self.assertNotIn("real()", result)

    def test_multiple_proof_fns(self):
        code = (
            "proof fn a() {\n    proof_a();\n}\n\n"
            "proof fn b() {\n    proof_b();\n}\n"
        )
        result = admit_proof_fn_bodies(code)
        self.assertEqual(result.count("admit()"), 2)
        self.assertNotIn("proof_a", result)
        self.assertNotIn("proof_b", result)

    def test_skips_non_proof_fns(self):
        # The whole point of the mode-aware admitter: exec bodies survive.
        code = (
            "pub fn exec_fn() { runtime_code(); }\n\n"
            "proof fn lemma() { proof_code(); }\n"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("runtime_code", result)    # exec fn body preserved
        self.assertNotIn("proof_code", result)   # proof fn body admitted
        self.assertIn("admit()", result)

    def test_skips_axiom_fns(self):
        # axiom_* bodies are trusted and must be preserved.
        code = "proof fn axiom_trust() {\n    trusted_axiom_body();\n}\n"
        result = admit_proof_fn_bodies(code)
        self.assertIn("trusted_axiom_body", result)
        self.assertNotIn("admit()", result)

    def test_forall_clause_not_admitted(self):
        code = (
            "pub proof fn lemma(digits: Seq<Seq<i8>>)\n"
            "    requires\n"
            "        forall|k: int|\n"
            "            0 <= k < digits.len() ==> {\n"
            "                &&& digits[k].len() == 64\n"
            "            },\n"
            "{\n"
            "    // proof body\n"
            "}"
        )
        result = admit_proof_fn_bodies(code)
        self.assertIn("digits[k].len() == 64", result)  # clause preserved
        self.assertNotIn("proof body", result)
        self.assertIn("admit()", result)


class AdmitProofBlocks(unittest.TestCase):
    """`admit_proof_blocks` hollows inline `proof { ... }` blocks inside
    exec fns to `{ admit(); }`, preserving surrounding exec code."""

    def test_simple_proof_block(self):
        code = (
            "fn exec_fn() {\n"
            "    let x = 1;\n"
            "    proof {\n"
            "        assert(x > 0);\n"
            "    }\n"
            "    let y = 2;\n"
            "}"
        )
        result = admit_proof_blocks(code)
        self.assertIn("{ admit(); }", result)
        self.assertNotIn("assert(x > 0)", result)
        self.assertIn("let x = 1;", result)
        self.assertIn("let y = 2;", result)


if __name__ == "__main__":
    unittest.main()
