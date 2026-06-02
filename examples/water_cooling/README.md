# water_cooling — example experiment

A complete `meta-research` experiment: autonomously push out the Pareto frontier of a
**liquid-cooling cold-plate fin design** over two competing objectives —
**thermal resistance** (K/W) and **coolant pressure drop** (Pa).

This directory is self-contained. It ships a copy of the `meta-research` skill under
`.claude/skills/` so the methodology loads automatically when you open Claude Code here.

## What's in here

| File / dir                         | Role                                                                 |
|------------------------------------|----------------------------------------------------------------------|
| `program.md`                       | The **program** (human-edited, thin). Points the agent at the skill and pins the domain loop. Start here. |
| `domain_spec.md`                   | Filled domain spec: framing, param schema, constraints, evaluator.   |
| `prepare.py`                       | **READ-ONLY** configuration-as-code: `OBJECTIVES`, `BUDGET`, `OPERATING`, `BASELINES`, `make_evaluator()`. |
| `objective.py`                     | **READ-ONLY** `WaterCoolingEvaluator` (numpy cold-plate model + heatmap). |
| `designs/`                         | The agent's **write target**. One module per candidate (`build() -> DesignSpec`). |
| `designs/straight_fins.py`         | Baseline candidate (straight channel fins).                          |
| `designs/pin_fins.py`              | Baseline candidate (pin-fin matrix).                                 |
| `.claude/skills/meta-research/`    | Local copy of the skill (SKILL.md + REFERENCE.md) so it loads here.  |
| `experience/`, `results.tsv`, `frontier.json` | Created at runtime — the append-only ledger.              |

`prepare.py` and `objective.py` are the **fixed contract of the problem** — never edit
them. The agent only ever writes to `designs/`.

## Prerequisites

From the repo root, install the package (editable) so the `meta-research` CLI and the
`meta_research` engine are importable:

```bash
pip install -e .          # numpy + matplotlib; see pyproject.toml for optional extras
```

This example uses only the default **numerical** evaluator — no API keys, no network, no
GPU. (To swap in a surrogate or a real CFD solver, edit `make_evaluator()` in
`prepare.py`; see `domain_spec.md` §6.)

## Option A — let the agent run the loop (intended use)

This is the autoresearch model: the agent runs the research loop **itself, in-session**.
There is no Python orchestrator to launch.

1. Open Claude Code **in this directory**:

   ```bash
   cd examples/water_cooling
   claude
   ```

2. Seed the baseline population (Phase 0):

   ```bash
   meta-research seed --commit
   ```

   This evaluates `straight_fins` and `pin_fins`, writes their experience bundles, and
   initializes `frontier.json` + `results.tsv` on the `meta-research/<tag>` branch.

3. Ask the agent to follow the program:

   > Read `program.md` and follow it. Use the run tag `coldplate-01`. Run the loop and
   > don't stop until I interrupt you.

   The agent loads the `meta-research` skill, inspects the experience (reads the actual
   `heatmap.png`s), forms one mechanism-level hypothesis at a time, writes
   `designs/<name>.py`, runs `meta-research eval <name> --hypothesis hyp.json --commit`,
   reads the new heatmap back, and repeats — committing every candidate (append-only).

## Option B — drive `meta-research eval` manually

You can also act as the proposer yourself and use the CLI as deterministic plumbing:

```bash
cd examples/water_cooling

# Phase 0: seed baselines.
meta-research seed --commit --tag coldplate-01

# Write a new candidate by hand, e.g. designs/taller_copper_fins.py with build().
# (Optional) write a hypothesis: hyp.json = {"axis","parent","expected","reasoning"}.

# Evaluate it, store the bundle, recompute the frontier, append-only commit.
meta-research eval taller_copper_fins --hypothesis hyp.json --commit --tag coldplate-01

# Inspect the current Pareto set + best-per-objective.
meta-research frontier
```

Then read the result and diagnostic for that candidate:

- `experience/<iter>_<name>/result.json` — scores + status.
- `experience/<iter>_<name>/trace/heatmap.png` — base-plate temperature field.
- `results.tsv` — the flat ledger of everything evaluated.

A candidate is **"kept"** iff it **extends the Pareto frontier**; it is **committed
either way** (dominated / infeasible / crashed candidates all stay as experience).

## Reading the results

- **`frontier.json`** — the non-dominated set and the best value seen per objective.
- **`results.tsv`** — one tab-separated row per candidate:
  `iter  name  status  feasible  thermal_resistance  pressure_drop  hypothesis`.
- **`experience/<iter:03d>_<name>/`** — the full bundle for any candidate: its verbatim
  `design.py`, the `hypothesis.md` reasoning trace, `result.json`, and the `trace/`
  diagnostics (heatmap, raw fields, per-mechanism breakdown).
- **git log** — `git log --oneline` on the `meta-research/<tag>` branch is the
  append-only history of the whole search. Use `git show <sha>:experience/...` to recover
  any prior candidate's exact source.

## Ground rules (see `program.md` for the full set)

- git is **append-only**: every candidate is committed; **never `git reset`**.
- **One mechanism change per candidate** (anti-parameter-tuning / anti-overfitting).
- Designs stay **physically general** so they survive a fidelity upgrade of the evaluator.
- **Never stop** the loop until interrupted; never declare the frontier "optimal."
