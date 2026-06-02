# program.md — water_cooling

> This file is the **program** (autoresearch-style). It is **human-edited** and thin.
> It does not run anything itself: it tells the agent *which skill to load*, *what the
> fixed domain givens are*, and *what loop to execute in-session*. There is **no Python
> orchestrator** — looping is the agent (you) running the `meta-research` skill in this
> Claude Code session. The deterministic plumbing is the `meta-research` CLI; the
> intelligence (propose → diagnose → decide) is you.

## 0. Load the skill

Read and follow the **`meta-research`** skill (it is shipped in this directory at
`.claude/skills/meta-research/SKILL.md`, with depth in `REFERENCE.md`). That skill is the
domain-agnostic methodology. This `program.md` only supplies the domain wiring below.

Then read the two fixed files in full before doing anything else:

- `prepare.py` — the configuration-as-code (objectives, budget, operating point,
  baselines, `make_evaluator()`). **READ-ONLY.**
- `objective.py` — the `WaterCoolingEvaluator` (numpy cold-plate model + heatmap).
  **READ-ONLY.**
- `domain_spec.md` — the filled problem spec (framing, parametrization, constraints).

## 1. Setup (do once)

- **Run tag.** Agree a run tag with the human and work on the branch
  `meta-research/<tag>` (e.g. `meta-research/coldplate-01`). The skill's
  `meta-research eval --commit` creates/checks out this branch for you via
  `ensure_branch(<tag>)`. Pass it through with `--tag <tag>` (or set it once and reuse).
- **Read-only files.** `prepare.py` and `objective.py` are the fixed contract of the
  problem. **Never edit them.** If the problem framing seems wrong, stop and ask the
  human — do not work around the evaluator.
- **Write target.** The *only* place you write designs is `designs/`. Each candidate is
  one module `designs/<name>.py` exposing `build() -> DesignSpec` (see `domain_spec.md`
  §2 for the param schema). Copy a frontier design, change **one mechanism**, rename.
- **Seed the population.** Run `meta-research seed --commit` once. This evaluates every
  baseline in `prepare.BASELINES` (`straight_fins`, `pin_fins`), writes their experience
  bundles, and initializes `frontier.json` + `results.tsv`. This is Phase 0.

## 2. The loop (run in-session, forever)

Invoke the `meta-research` skill and repeat these steps. **One mechanism change per
candidate.**

1. **Inspect experience.** `meta-research frontier`; read `results.tsv`;
   `git log --oneline`. Then actually **`Read` the `heatmap.png`** and `hypothesis.md`
   of the current frontier designs, the most recent candidates, and any recent failures.
   Use `git show <sha>:experience/<bundle>/design.py` to pull full prior source. The
   heatmap is the diagnostic — look at *where* the plate is hot, not just the number.
2. **Form ONE falsifiable hypothesis** targeting a **mechanism** (geometry / density /
   distribution / arrangement / material / interface — see the skill's `REFERENCE.md`),
   not a parameter nudge. Write it to `hyp.json` (schema in `REFERENCE.md` / DESIGN §7.3):
   `{"axis","parent","expected","reasoning"}`. The `reasoning` must cite which prior
   heatmap/result you inspected and the failure mode you are attacking.
3. **Write `designs/<name>.py`.** Start from a frontier design, change exactly the one
   mechanism. **Dry-run first** (mandatory prototype): import the module, call `build()`,
   and sanity-check the params fit the envelope and pass the feasibility constraints
   (`domain_spec.md` §4) before spending an evaluation.
4. **Evaluate + commit.** `meta-research eval <name> --hypothesis hyp.json --commit
   --tag <tag>`. This scores the design, writes the experience bundle
   (`design.py`, `design_spec.json`, `hypothesis.md`, `result.json`,
   `trace/heatmap.png`, `trace/fields.npz`, `trace/breakdown.json`), recomputes the
   Pareto frontier, appends a `results.tsv` row, and makes **one append-only commit**.
5. **Read the result back.** Open `experience/<iter>_<name>/result.json` and **`Read`
   the new `trace/heatmap.png`**. A candidate is **"kept"** iff it **extends the Pareto
   frontier** (non-dominated on `{thermal_resistance, pressure_drop}`). But it is
   **committed either way** — dominated, infeasible, and crashed candidates are all
   experience and stay in the ledger (status `dominated` / `infeasible` / `crash`).
   Note in your next hypothesis whether the change behaved as `expected`.
6. **Repeat.**

## 3. Hard rules

- **git is APPEND-ONLY.** Every evaluated candidate is committed. **Never `git reset`,
  never amend, never rebase, never delete a bundle.** Discarding deletes exactly the
  diagnostic traces the method depends on.
- **One mechanism per candidate.** No multi-knob sweeps; no fitting constants to the
  surrogate (anti-parameter-tuning / anti-overfitting — see `REFERENCE.md`).
- **The ledger lives here:**
  - `results.tsv` — flat, tab-separated ledger (one row per candidate).
  - `frontier.json` — the current Pareto set + best-per-objective.
  - `experience/<iter:03d>_<name>/` — full per-candidate bundle (source, hypothesis,
    result, trace/heatmap).
- **Never declare the frontier "optimal."** There is always another mechanism to try.

## 4. NEVER STOP

Once seeded, **keep running the loop until the human interrupts you.** Do not pause to
ask "shall I continue?" after each candidate; do not stop because the last few designs
were dominated — that is information, form the next hypothesis from it. The only reasons
to stop are: an explicit human interrupt, or a genuine blocker (the evaluator import
fails, `designs/` is unwritable, git is in a broken state). Report blockers; otherwise
propose the next mechanism and go again.
