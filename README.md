# meta-research

**An autoresearch architecture, generalized, carrying the Meta-Harness methodology — shipped as a skill.**

`meta-research` is a general-purpose autonomous design-optimization framework. You point a
coding agent (Claude Code) at a parametric engineering design plus a pluggable evaluator, and
the agent runs a research loop *in-session*: it proposes a design, scores it against a
simulator/objective, reads the resulting diagnostics (e.g. a temperature heatmap), forms the
next falsifiable hypothesis, and repeats — committing every result to git as it goes.

There is **no subprocess orchestrator**. No `claude_wrapper.py`, no `proposer.py`, no looping
`run` command. The framework provides only **deterministic tools** (evaluate → store → frontier
→ git). The intelligence — propose, diagnose, decide — is the agent following the skill.

## The fusion

It fuses two ideas into one architecture:

- **Meta-Harness** (Lee et al., 2026, [arXiv:2603.28052](https://arxiv.org/abs/2603.28052))
  gives the *method*. An outer loop where a coding-agent proposer reads a **full filesystem of
  prior experience** — every candidate's source code, reasoning, scores, and execution traces,
  *never* compressed summaries — forms falsifiable hypotheses, and proposes new candidates. A
  **Pareto frontier** is maintained over multiple objectives; there is **no parent-selection
  rule** (the proposer may inspect any prior candidate); strong anti-overfitting /
  anti-parameter-tuning / prototype-first discipline applies throughout.

- **autoresearch** (Karpathy, 2026) gives the *ergonomics*: **configuration-as-code +
  markdown-as-program**. There is no config file. A fixed `prepare.py` holds the constants and
  the ground-truth evaluator; a human-edited `program.md` is the "program" the agent follows;
  the agent edits the design artifacts directly; **git** is the versioned store and
  `results.tsv` is the flat ledger.

The optimization target is **generalized** from "an ML model / agent harness" to *any
parametric engineering design* — for example, the fin layout inside a liquid-cooling cold
plate. This is **not** ML training. The thing being scored is produced by a pluggable
`Evaluator`, and the whole methodology is domain-agnostic.

## Key design choices

- **Pluggable Evaluator, selected in `prepare.py`.** One interface, three adapters:
  - `NumericalEvaluator` — wrap a pure-Python `simulate()` (the runnable default).
  - `SurrogateEvaluator` — load and call a user-provided model artifact (joblib / pickle /
    onnxruntime / a `module:attr` callable).
  - `ApiSolverEvaluator` — POST to a REST endpoint, or shell out to a real solver CLI.

  Swapping numerical → surrogate → api is editing **one factory function** (`make_evaluator()`)
  in `prepare.py`. No code in the engine changes.

- **Configuration-as-code, no YAML.** Objectives, budget, operating point, baselines, and
  evaluator selection live as plain Python in a fixed, read-only `prepare.py`.

- **The filesystem is the experience store; git is its append-only audit trail.** Every
  evaluated candidate — kept, dominated, infeasible, or crashed — keeps its full bundle under
  `experience/` forever, and every evaluation is committed for tamper-evidence and backup.
  **Never `git reset` to discard.** Discarding would delete exactly the diagnostic traces the
  methodology depends on. (The run dir must be its own git repo — `meta-research init` sets
  this up; the runner refuses to commit from a run dir nested inside another repository.)

- **Pareto frontier replaces keep/discard.** autoresearch's binary keep/discard is replaced by
  Pareto-frontier membership. A non-dominated candidate enters `frontier.json`; a dominated one
  is still committed as experience, flagged `dominated`.

- **The loop closes through diagnostics (the heatmap loop-closure).** Evaluators emit a
  `heatmap.png` (and raw fields). The agent literally **`Read`s those images back** the next
  iteration to decide *where* to change the design. This loop-closure is the whole point.

- **Store everything (per Meta-Harness Fig. 2).** Four categories per candidate: the proposed
  design source, the reasoning trace (`hypothesis.md`, authored by the agent), the evaluation
  scores, and the execution traces (heatmaps, raw fields, solver logs).

## Quick start

The framework ships as a skill plus a small CLI. To try the bundled water-cooling example:

```bash
# 1. Install the engine (editable; numpy + matplotlib).
pip install -e .

# 2. Open Claude Code in the example directory. The meta-research skill is bundled at
#    examples/water_cooling/.claude/skills/meta-research/ and loads automatically.
cd examples/water_cooling
claude

# 3. Inside the session, seed the baselines (Phase 0): evaluates every design in BASELINES,
#    seeding the population and the Pareto frontier.
meta-research seed --commit

# 4. Tell the agent to follow the program:
#    > follow program.md
#    It loads the meta-research skill and runs the loop itself: inspect experience
#    (frontier, results.tsv, and the actual heatmap.png images) → form one
#    falsifiable mechanism-level hypothesis → write designs/<name>.py → eval --commit →
#    read the new heatmap → repeat. It never stops until interrupted, and never resets git.
```

The deterministic CLI steps the agent calls each iteration:

```bash
meta-research seed [--commit]                                # Phase 0: evaluate all baselines
meta-research eval <design> --hypothesis hyp.json --commit   # score one design, store, frontier, commit
meta-research frontier                                       # print the current Pareto set + best per objective
meta-research init <name>                                    # scaffold a new experiment dir
```

There is intentionally **no `run` subcommand that loops**. Looping is the agent in the Claude
Code session following the skill.

## Onboarding a new domain

`meta-research` is domain-agnostic. To adapt it to a *new* design-optimization domain (turbine
blade cooling, antenna geometry, lens stack-up, truss topology, ...), use
[`ONBOARDING.md`](./ONBOARDING.md) — a conversation prompt that interviews you and produces a
concrete `domain_spec.md` (problem framing, design parametrization, evaluator choice, objectives
+ constraints, baselines, budget, experience/logging). From that spec you write a `prepare.py`,
an `objective.py` (or wire up a surrogate/API), and a couple of baseline `designs/`.

## Project structure

```
meta-research/
  README.md                 # this file
  DESIGN.md                 # the architecture contract (single source of truth)
  ONBOARDING.md             # conversation prompt -> produces domain_spec.md for a NEW domain
  pyproject.toml            # package + deps (numpy, matplotlib); optional extras
  .gitignore
  .env.example

  skills/
    meta-research/
      SKILL.md              # THE deliverable: the generalized autoresearch "program" carrying
                            #   the Meta-Harness methodology. Agent runs the loop in-session.
      REFERENCE.md          # depth: mechanism axes, anti-overfit rules, experience/git/frontier

  meta_research/            # the reusable ENGINE the skill calls (deterministic tools, NOT an orchestrator)
    __init__.py             # re-exports public API
    interfaces.py           # FROZEN: Objective, DesignSpec, EvalResult, Candidate, Evaluator
    logfmt.py               # ANSI helpers + results.tsv reader/writer
    frontier.py             # Pareto: dominates(), pareto_front(), update_frontier(), classify()
    candidates.py           # load + validate + build a design module from designs/
    experience.py           # experience-bundle store + git APPEND-ONLY ledger
    runner.py               # evaluate_and_record(): score -> bundle -> frontier -> tsv -> (opt) commit
    cli.py                  # `meta-research eval|seed|frontier|init`
    evaluators/
      __init__.py           # re-export the three adapters
      numerical.py          # NumericalEvaluator: wrap a pure-python simulate() callable
      surrogate.py          # SurrogateEvaluator: load + call a user model artifact
      api_solver.py         # ApiSolverEvaluator: REST POST or CLI subprocess + parse output

  examples/
    water_cooling/          # a runnable reference domain (cold-plate fin layout)
      README.md
      domain_spec.md
      prepare.py            # FIXED: OBJECTIVES, BUDGET, OPERATING, BASELINES, make_evaluator()
      objective.py          # WaterCoolingEvaluator: numpy fin model + heatmap
      program.md            # the domain "program" -- thin; points the agent at the skill
      designs/
        straight_fins.py    # baseline candidate
        pin_fins.py         # baseline candidate
      .claude/skills/meta-research/   # a copy of the skill, so it loads when Claude Code runs here

  tests/                    # pytest: interfaces, frontier, experience, runner, water-cooling
```

## How it runs

The framework provides **deterministic tools**; the **agent runs the loop**. Each iteration the
agent: inspects the full experience ledger (including reading the actual `heatmap.png` images
and `hypothesis.md` files of frontier / recent / failed candidates) → forms one falsifiable
*mechanism-level* hypothesis (not a parameter tweak) → writes a single design module → dry-runs
`build()` + feasibility → calls `meta-research eval --hypothesis hyp.json --commit` → reads the
new diagnostics → notes whether it extended the Pareto frontier → repeats. It never stops until
interrupted, never `git reset`s, and never declares the frontier "optimal".

See [`DESIGN.md`](./DESIGN.md) for the binding architecture contract and
[`skills/meta-research/SKILL.md`](./skills/meta-research/SKILL.md) for the loop the agent runs.

## Acknowledgements

This project stands on the shoulders of two prior works whose ideas it directly builds on:

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — for the autonomous-research
  agent ergonomics and the append-only experience-ledger pattern.
- [stanford-iris-lab/meta-harness](https://github.com/stanford-iris-lab/meta-harness) — for the
  meta-harness methodology that treats the harness itself as the artifact under optimization.

Huge thanks to the authors and contributors of both projects.
