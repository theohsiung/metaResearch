---
name: meta-research
description: Autonomous, hypothesis-driven research loop for engineering/scientific design — it proposes parametric designs, scores each with a pluggable objective (an in-process numerical model, a loaded surrogate model, or a real-simulation API), reads the diagnostics (e.g. heatmaps) back, and maintains a multi-objective Pareto frontier over an append-only experience ledger (filesystem bundles, mirrored to git as the audit trail). Use this whenever the user wants to autonomously search or optimize a design or a set of parameters against a simulator/objective/surrogate — especially multi-objective trade-offs, "keep iterating overnight", or mechanism-level (not just parameter-tweak) design improvement — when they ask to "run meta-research", or when the working repo contains a prepare.py + designs/ experiment. This is design/parameter search, not ML model training.
---

# meta-research

You are the proposer in an autonomous research loop. The framework gives you
**deterministic tools only** (`meta-research eval|seed|frontier`); the intelligence
— inspecting prior experience, forming hypotheses, deciding what to change — is you,
running the loop yourself, in this session. There is no subprocess orchestrator.

The domain (geometry, physics, scoring) lives entirely in `prepare.py` + `objective.py`
+ `designs/`. This skill is domain-agnostic: you discover the domain by reading those.

## Quick start

0. Confirm the engine is available: `meta-research --help`. If the command is not
   found, install it once with `pip install -e <repo-root>`, or substitute
   `python -m meta_research.cli` everywhere this skill writes `meta-research`. The
   framework supplies the deterministic tools (`eval`/`seed`/`frontier`); you supply
   the research.
1. Read `program.md` (the domain "program") and `prepare.py` (OBJECTIVES, BUDGET,
   OPERATING, BASELINES, `make_evaluator()`). These fixed files are **read-only**;
   `designs/<name>.py` is your only write target.
2. Agree a run tag and seed the population: `meta-research seed --commit`. This
   evaluates every baseline once, populating `results.tsv`, `frontier.json`,
   `experience/`.
3. Then run **The loop** below, forever, until interrupted.

## The loop (checklist — repeat every iteration)

1. **Inspect experience** (never skip): run `meta-research frontier`, read
   `results.tsv` (the authoritative timeline), and run `meta-research progress` to
   refresh the best-so-far curves + Pareto plot. Then **`Read` the actual
   `heatmap.png`** (or whatever diagnostic the evaluator emits) AND the
   `hypothesis.md` of the frontier members, the most recent candidates, and the
   failures. Read `experience/<dir>/design.py` directly for any prior source —
   bundles are append-only files on disk, never rewritten (git is the audit/backup
   layer, not your query interface). The diagnostic image tells you *where* the
   design is failing — that is how the loop closes.
2. **Form ONE falsifiable hypothesis** targeting a **mechanism**, not a parameter
   value (see REFERENCE.md mechanism axes). Name the failure mode the diagnostic
   revealed and the single mechanism you will change to fix it.
3. **Write `designs/<name>.py`**: copy a frontier design and change exactly **one
   mechanism**. Dry-run first (mandatory prototype): import the module, call
   `build()`, sanity-check params fit the envelope / pass obvious feasibility before
   spending an evaluation.
4. **Evaluate + commit**: write `hyp.json` (schema in REFERENCE.md: axis, parent,
   change — the one-line "what was changed", it becomes the commit/TSV summary —
   expected, reasoning), then
   `meta-research eval <name> --hypothesis hyp.json --commit`. Your raw thinking
   for the iteration is auto-captured into the bundle's
   `trace/proposer_thinking.md`; `hypothesis.md` stays the curated hand-off.
5. **Read the result**: open the new `result.json` and the new `heatmap.png`. Did it
   extend the Pareto frontier? Was the predicted effect confirmed or falsified? Note
   the outcome — a falsified hypothesis is valuable experience, not a failure to hide.
6. **Repeat.** **Never stop** until interrupted. **Never `git reset`** — dominated,
   infeasible, and crashed candidates stay committed as experience. **Never declare
   the frontier "optimal"** or "good enough"; there is always another mechanism to try.

## Hard rules

- **One mechanism per candidate.** Change a single mechanism so the result is
  attributable. Do not bundle several edits.
- **Anti-parameter-tuning.** Do not grind a scalar up and down (count 40→42→44…).
  Prefer mechanism changes (inline→staggered topology, swap the primitive shape,
  redistribute density toward the failing region).
- **Anti-overfitting.** Designs must stay physically/structurally general — no
  exploiting evaluator quirks, no fixtures tuned to one operating point.
- **Append-only experience.** Every evaluated candidate is committed, whatever its
  status. The diagnostic traces are the data Meta-Harness depends on.

See [REFERENCE.md](REFERENCE.md) for the mechanism-axis catalog, anti-overfit rules
with examples, the experience-bundle + `hypothesis.md` schema, the git append-only +
Pareto conventions, and how to swap the evaluator in `prepare.py`.
