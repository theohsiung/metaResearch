# meta-research — Architecture Contract

> This file is the **single source of truth** for the repository. Every module is
> built against the signatures and schemas pinned here. If code and this document
> disagree, this document wins — fix the code.

## 1. What this is

`meta-research` is a **general-purpose autonomous scientific/engineering research
agent framework**. It fuses two ideas:

- **Meta-Harness** (Lee et al., 2026, arXiv:2603.28052) gives the *method*: an
  outer loop where a **coding-agent proposer** reads a **full filesystem of prior
  experience** (every candidate's source code, reasoning, scores, and execution
  traces — never compressed summaries), forms falsifiable hypotheses, and proposes
  new candidates; a **Pareto frontier** is maintained over multiple objectives;
  there is **no parent-selection rule** (the proposer may inspect any prior
  candidate); strong **anti-overfitting / anti-parameter-tuning / prototype-first**
  discipline.
- **autoresearch** (Karpathy, 2026) gives the *ergonomics*: **configuration-as-code
  + markdown-as-program**. There is no config file. A fixed `prepare.py` holds
  constants and the ground-truth evaluator; a human-edited `program.md` is the
  "program"; the agent edits the design artifact(s) directly; **git** is the
  versioned store and `results.tsv` is the flat ledger.

The optimization target is generalized from "an ML model / agent harness" to **a
parametric engineering design** (e.g. the fin layout inside a liquid-cooling cold
plate). The thing being scored is produced by a **pluggable `Evaluator`** that may
be (a) a real-simulation API call, (b) a loaded surrogate model, or (c) a simple
in-process numerical model — all behind one interface, chosen in `prepare.py`.

### 1.1 The fusion rules (non-negotiable)

1. **Configuration-as-code, no YAML.** Objectives, budget, operating point, and
   evaluator selection live as plain Python in a fixed, read-only `prepare.py`.
   Swapping numerical → surrogate → api is editing one factory function there.
2. **git is an append-only experience ledger.** Every evaluated candidate (kept,
   dominated, infeasible, or crashed) is committed. **Never `git reset` to discard.**
   Discarding would delete exactly the diagnostic traces Meta-Harness depends on.
3. **autoresearch's keep/discard is replaced by Pareto-frontier membership.** A
   candidate that is non-dominated enters `frontier.json`; a dominated one is still
   committed as experience, flagged `dominated`.
4. **Store everything (per Meta-Harness Fig. 2).** Four categories per candidate:
   proposed design source, reasoning traces (incl. the proposer's thinking blocks),
   evaluation scores, and execution traces (heatmaps, raw fields, solver logs).
5. **The loop closes through diagnostics.** Evaluators emit a `heatmap.png` (and raw
   fields); the agent **reads those images back** next iteration to decide where
   to change the design. This is the whole point — preserve it.
6. **The agent runs the loop in-session (autoresearch model).** There is **no Python
   subprocess orchestrator** and no `claude -p` proposer driver. The human opens
   Claude Code in an experiment dir and points it at `program.md`/the skill; the
   agent itself proposes designs, runs the evaluator, reads diagnostics, commits, and
   repeats. The framework provides only **deterministic tools** (evaluate → store →
   frontier → git); the *intelligence* (propose, diagnose, decide) is the agent
   following the skill. Reasoning is captured by the agent authoring a thorough
   `hypothesis.md` per candidate (what prior evidence/heatmaps it inspected, the
   failure mode targeted, the mechanism changed, the expected effect).

## 2. File tree

```
meta-research/
  README.md                 # what it is, the fusion, quick start
  DESIGN.md                 # this contract
  ONBOARDING.md             # conversation prompt -> produces domain_spec.md for a NEW domain
  pyproject.toml            # package + deps (numpy, matplotlib); optional extras
  .gitignore
  .env.example

  skills/
    meta-research/
      SKILL.md              # THE deliverable: the generalized autoresearch "program" carrying the
                            #   Meta-Harness methodology. Agent runs the loop in-session. < 100 lines.
      REFERENCE.md          # detailed methodology: mechanism axes, anti-overfit rules,
                            #   experience/git/frontier conventions + schemas (progressive disclosure)

  meta_research/            # the reusable ENGINE the skill calls (deterministic tools, NOT an orchestrator)
    __init__.py             # re-exports public API
    interfaces.py           # [DONE, FROZEN] Objective, DesignSpec, EvalResult, Candidate, Evaluator
    logfmt.py               # ANSI color helpers + results.tsv reader/writer
    frontier.py             # Pareto: dominates(), pareto_front(), update_frontier(), classify()
    candidates.py           # load + validate + build a design module from designs/
    experience.py           # experience-bundle store + git APPEND-ONLY ledger
    runner.py               # evaluate_and_record(): score one design -> bundle -> frontier -> results.tsv -> (opt) commit
    cli.py                  # `meta-research eval <design> [--commit]`, `frontier`, `init`
    evaluators/
      __init__.py           # re-export the three adapters
      numerical.py          # NumericalEvaluator: wrap a pure-python simulate() callable
      surrogate.py          # SurrogateEvaluator: load + call a user model artifact
      api_solver.py         # ApiSolverEvaluator: REST POST or CLI subprocess + parse output

  examples/
    water_cooling/
      README.md
      domain_spec.md        # filled spec for this domain
      prepare.py            # FIXED: OBJECTIVES, BUDGET, OPERATING, BASELINES, make_evaluator(), runtime utils
      objective.py          # WaterCoolingEvaluator: numpy fin model + heatmap
      program.md            # the domain "program" -- thin; points the agent at the meta-research skill
      designs/
        __init__.py
        straight_fins.py    # baseline candidate
        pin_fins.py         # baseline candidate
      .claude/skills/meta-research/   # copy of the canonical skill, so it loads when Claude Code runs here
        SKILL.md
        REFERENCE.md

  tests/
    test_interfaces.py
    test_frontier.py
    test_experience.py
    test_runner.py          # eval one design -> bundle written, frontier + tsv updated, append-only commit
    test_water_cooling.py
```

## 3. interfaces.py (FROZEN — already written, do not modify)

Types every module imports. Summary (full source in `meta_research/interfaces.py`):

- `Objective(name, direction="min", unit="", weight=1.0)` — frozen dataclass;
  `direction in {"min","max"}`; `.is_better(a, b) -> bool`.
- `DesignSpec(params: dict, artifacts: dict[str,str]={}, notes: str="")` — frozen;
  `.to_json()/.from_json()`.
- `EvalResult(scores: dict[str,float], feasible=True, metadata={}, artifacts={}, error=None)`
  — frozen; `.ok` property (finite scores, no error); `.to_json()/.from_json()`;
  `EvalResult.crashed(error) -> EvalResult` static ctor.
- `Candidate(Protocol)` — `name: str`, `build() -> DesignSpec`.
- `Evaluator(Protocol)` — `objectives: list[Objective]`, `evaluate(design, out_dir: Path) -> EvalResult`.

Evaluators **must never raise**; wrap failures in `EvalResult.crashed(...)`.

## 4. Framework ↔ experiment contract

The framework discovers the experiment from the **current working directory**
(autoresearch style: you `cd examples/water_cooling && meta-research run`). The
experiment's fixed `prepare.py` MUST expose these module-level symbols:

```python
# prepare.py  (FIXED / read-only by the agent)
OBJECTIVES: list[Objective]          # ordered; drives scoring + frontier + tsv columns
BUDGET: dict                         # {"max_iterations": int, "candidates_per_iteration": int,
                                     #  "proposer_model": str, "proposer_timeout_s": int}
OPERATING: dict                      # the fixed "givens" of the problem (heat load, flow, envelope...)
BASELINES: list[str]                 # module names in designs/ used to seed the population
DESIGNS_DIR: str = "designs"         # where candidate modules live (agent's write target)

def make_evaluator() -> Evaluator:   # configuration-as-code: pick numerical/surrogate/api here
    ...
```

`loop.py`/`cli.py` import these via `importlib` from `./prepare.py` and the designs
package from `./<DESIGNS_DIR>/`.

## 5. Candidate module contract

A candidate is a single module under `designs/`. It MUST expose:

```python
def build() -> DesignSpec: ...
```

Optional module-level `NAME: str` (defaults to the module filename stem).
`candidates.py` provides:

- `load_candidate(path_or_module: str) -> tuple[str, Callable[[], DesignSpec]]`
- `validate_candidate(name, designs_dir) -> tuple[bool, str]` — imports the module,
  calls `build()`, checks the result is a `DesignSpec` with a non-empty `params`
  dict; returns `(ok, message)`. No exceptions escape.
- `build_design(name, designs_dir) -> DesignSpec`.

Validation = interface compliance only (import + build + type check). The Evaluator
performs the actual scoring.

## 6. Framework module responsibilities & signatures

### 6.1 `logfmt.py`
- ANSI helpers: `bold/dim/green/red/yellow/cyan(s) -> str`, `ts() -> str`,
  `elapsed(seconds) -> str`. Respect `sys.stdout.isatty()`.
- `ResultsLog(path: Path, objectives: list[Objective])`:
  - `write_header()` — columns: `iter  name  status  feasible  <obj.name for obj in objectives>  hypothesis` (tab-separated).
  - `append(iter:int, name:str, status:str, result:EvalResult, hypothesis:str)`.
  - `rows() -> list[dict]`.
  - `status` ∈ `{"frontier","dominated","infeasible","crash"}`.

### 6.2 `frontier.py`
- `dominates(a: dict[str,float], b: dict[str,float], objectives) -> bool` — `a`
  dominates `b` iff `a` is no worse on all objectives and strictly better on ≥1.
- `pareto_front(rows: list[dict], objectives) -> list[dict]` — rows are
  `{"name","iter","scores","metadata"}`; returns non-dominated, feasible rows.
- `update_frontier(run_dir: Path, objectives) -> dict` — recompute from the experience
  ledger (see 7.2), write `frontier.json` (schema 7.4), return it.
- `classify(scores, frontier_rows, objectives) -> "frontier"|"dominated"` — helper for
  the loop to label a freshly-evaluated candidate.

### 6.3 `experience.py` — the store (git append-only)
`Experience(run_dir: Path, objectives: list[Objective])`:
- Paths: `run_dir/experience/`, `run_dir/results.tsv`, `run_dir/frontier.json`.
- `bundle_dir(iteration, name) -> Path` → `experience/<iteration:03d>_<name>/`.
- `record(iteration, name, design_src_path, design: DesignSpec, result: EvalResult,
   hypothesis: dict)` — writes the **bundle** (schema 7.1): copies `design.py`,
   writes `design_spec.json`, `hypothesis.md`, `result.json`, copies evaluator
   `result.artifacts` into `trace/`, writes `trace/breakdown.json` from
   `result.metadata`. Returns the bundle dir.
- `history() -> list[dict]` — parse all `result.json` + `hypothesis.md` front-matter
  into rows `{"iteration","name","status","scores","metadata","hypothesis","axis","parent"}`.
- `next_iteration() -> int` — highest existing bundle iteration + 1 (the agent does not
  track iteration counters by hand).
- git helpers (subprocess, cwd=run_dir). git is the **audit/backup layer**; the agent's
  query surface is the filesystem (bundles + `results.tsv`). Both mutating helpers
  first verify that `run_dir` is its own repository root (`git rev-parse
  --show-toplevel` == run_dir) and refuse otherwise — `add -A` / `checkout -b` in a
  nested run_dir would stage host files and switch the host checkout's branch:
  - `ensure_branch(tag: str)` — `git checkout -b meta-research/<tag>` if not present,
    else checkout. Never resets.
  - `commit(message: str)` — `git add -A -- . && git commit -m ...`. **Append-only. No
    reset, ever.** Returns the short HEAD SHA.
  - `annotate_commit(bundle, sha)` — writes `"commit": "<short-sha>"` into the bundle's
    `result.json` after the commit (join key to the full-run snapshot; the git-tracked
    copy lags one commit, since a commit cannot contain its own SHA).
  - `git_log_oneline() -> str`, `git_show(ref: str) -> str` — thin read wrappers
    (auxiliary; bundles on disk are the primary read path).
- Commit message format:
  `iter<NN> <name>: <obj1>=<v1> <obj2>=<v2> [<status>] — <one-line summary>`.
  The summary is the hypothesis `change` line (WHAT was changed — scores + status
  already say what happened); `expected` → `reasoning` → `summary` are fallbacks.
  The same line is the `results.tsv` description column.

### 6.4 `runner.py` — the deterministic step the agent calls
This replaces any outer-loop orchestrator. The **agent** decides *what* to try (per the
skill); `runner` does the *deterministic plumbing* for one design.

`evaluate_and_record(name, experiment, run_dir, *, hypothesis: dict | None = None,
commit: bool = False, tag: str | None = None) -> EvalResult`:
1. `build_design(name, experiment.DESIGNS_DIR)` (validate interface first).
2. `evaluator = experiment.make_evaluator()`; `result = evaluator.evaluate(design, bundle/trace)`.
3. `experience.record(iteration=next_iteration(), name, design_src, design, result, hypothesis)`.
4. `update_frontier(run_dir, experiment.OBJECTIVES)`; `classify` the candidate.
5. append a `results.tsv` row.
6. if `commit`: `ensure_branch(tag)` then `commit(<message>)` — append-only.
7. return the `EvalResult` (the CLI prints scores + the heatmap path).

`seed_baselines(experiment, run_dir, *, commit=False)` — evaluate every
`experiment.BASELINES` design once to seed the population + frontier (Phase 0).

`runner` is also what `test_runner.py` drives directly (no agent, no `claude`).

### 6.5 `cli.py`
`argparse` entry `main()` — loads `./prepare.py` via `importlib` and the designs package
from `./<DESIGNS_DIR>/`:
- `meta-research eval <design> [--commit] [--run-dir DIR] [--hypothesis FILE]` — the
  workhorse the agent calls each step: `evaluate_and_record(...)`, print scores, the
  `heatmap.png` path, and whether it extends the Pareto frontier.
- `meta-research seed [--commit]` — run `seed_baselines` (Phase 0).
- `meta-research frontier` — print the current `frontier.json` (Pareto set + best per objective).
- `meta-research init <name>` — scaffold a new experiment dir (prepare.py stub, designs/,
  program.md, plus a copy of the skill) — thin, optional.
- Console script: `meta-research = meta_research.cli:main`.

> There is intentionally **no `run` subcommand that loops**. Looping = the agent in the
> Claude Code session following the skill. The CLI only exposes deterministic steps.

## 7. Data schemas (PIN EXACTLY)

### 7.1 Experience bundle — `experience/<iter:03d>_<name>/`
```
design.py            # verbatim copy of the candidate module the agent wrote
design_spec.json     # DesignSpec.to_json()
hypothesis.md        # YAML front-matter + prose (schema 7.2)
result.json          # EvalResult.to_json()  (runner adds a "status" field)
trace/
  heatmap.png        # from result.artifacts["heatmap"] (evaluator-produced)
  fields.npz         # optional raw arrays (evaluator-produced)
  breakdown.json     # = result.metadata (per-mechanism breakdown)
  solver.log         # optional evaluator stdout
  proposer_thinking.md  # best-effort: raw thinking blocks harvested from the
                        # session transcript at eval time (thinking.py)
```
`hypothesis.md` IS the curated reasoning trace (authored by the agent, per fusion
rule §1.1.6): it records what prior evidence / heatmaps were inspected and why this
design follows. `trace/proposer_thinking.md` complements it with the *raw* thinking
blocks (options considered and rejected, calculations) harvested automatically by
`meta_research/thinking.py` between `record()` and the commit — session transcripts
are not durable (compaction discards old blocks) and many agent runtimes redact
thinking text entirely (blocks persist with only a signature), so the capture is
strictly best-effort and `hypothesis.md` remains the reasoning trace of record.
Auto-discovered transcripts are harvested only when
their last eval/seed marker references the current candidate (affinity guard), so a
foreign session can never pollute the ledger; `META_RESEARCH_TRANSCRIPT` overrides
discovery explicitly.

### 7.2 `hypothesis.md` front-matter
```
---
name: staggered_pin_v3
iteration: 12
axis: flow_arrangement          # mechanism axis (see SKILL.md)
parent: pin_fins                # what it builds on (free-form, NOT a selection rule)
change: "inline -> staggered pin rows over the outlet half"   # WHAT was changed (one line)
expected: "R_th down, dP up"
status: frontier|dominated|infeasible|crash   # filled by the loop after eval
---
<prose: which prior heatmaps/results were inspected and what failure mode this targets>
```

### 7.3 hypothesis hand-off (agent → `runner`)
There is **no `pending_eval.json`** in the agent-driven model. The agent writes the
design module `designs/<name>.py`, then calls `meta-research eval <name>
--hypothesis hyp.json`, where `hyp.json` is:
```json
{
  "axis": "flow_arrangement",
  "parent": "pin_fins",
  "change": "inline -> staggered pin rows over the outlet half",
  "expected": "thermal_resistance down ~8%, pressure_drop up ~15%",
  "reasoning": "Iter 9's heatmap showed a hot band over the outlet half; staggering the pin rows there raises local h where it matters."
}
```
`runner` folds this into `hypothesis.md` (schema 7.2). `--hypothesis` is optional but
the skill requires it (it is the reasoning trace).

### 7.4 `frontier.json`
```json
{
  "objectives": [{"name":"thermal_resistance","direction":"min","unit":"K/W"},
                 {"name":"pressure_drop","direction":"min","unit":"Pa"}],
  "pareto": [
    {"name":"staggered_pin_v3","iteration":12,
     "scores":{"thermal_resistance":0.042,"pressure_drop":820.0},
     "metadata":{"base_temp_rise_C":8.4,"fin_efficiency":0.78}}
  ],
  "best_per_objective": {
    "thermal_resistance": {"name":"staggered_pin_v3","value":0.042},
    "pressure_drop": {"name":"straight_fins","value":410.0}
  }
}
```

### 7.5 `results.tsv`
Tab-separated. Header + one row per evaluated (or skipped) candidate:
```
iter	name	status	feasible	thermal_resistance	pressure_drop	hypothesis
0	straight_fins	frontier	true	0.061	410.0	baseline straight fins
12	staggered_pin_v3	frontier	true	0.042	820.0	stagger pins over hot zone
```

## 8. Evaluator adapters (`meta_research/evaluators/`)

All declare `objectives: list[Objective]` and implement `evaluate(design, out_dir)`.
None may raise; failures → `EvalResult.crashed(...)`.

### 8.1 `numerical.py` — `NumericalEvaluator`
Base class wrapping a pure-python model.
```python
class NumericalEvaluator:
    def __init__(self, objectives: list[Objective],
                 simulate: Callable[[dict, Path], tuple[dict[str,float], dict, dict[str,str]]]):
        # simulate(params, out_dir) -> (scores, metadata, artifacts)
    def evaluate(self, design, out_dir) -> EvalResult:
        # try/except around simulate; assemble EvalResult; verify scores cover objectives
```
The water-cooling evaluator (objective.py) subclasses or composes this.

### 8.2 `surrogate.py` — `SurrogateEvaluator`
Loads a user model artifact and calls it.
```python
class SurrogateEvaluator:
    def __init__(self, objectives, model, feature_fn: Callable[[dict], "Any"],
                 predict_fn: Callable[["Any","dict"], dict[str,float]] | None = None):
        ...
    @classmethod
    def from_path(cls, objectives, model_path: str, feature_fn, ...):
        # load order: "module:attr" python callable | joblib | pickle | (optional) onnxruntime
```
Document optional deps (`joblib`, `onnxruntime`, `torch`) as soft imports — never
hard-require. Writes a small `prediction.json` artifact; no heatmap required.

### 8.3 `api_solver.py` — `ApiSolverEvaluator`
Calls an external solver. Two modes:
- **REST:** POST `design.params` (JSON) to an endpoint, parse the JSON response with a
  user `parse_fn(response_json) -> (scores, metadata, artifacts)`.
- **CLI:** render a command template with the params / a written input file, run via
  `subprocess` (with timeout), then `parse_fn(out_dir) -> (...)` reads solver output
  files. Save raw stdout to `solver.log` and any produced files as artifacts.
Uses only the stdlib (`urllib.request`, `subprocess`); `requests` optional. Not
runnable without a real endpoint — ship a clear stub + docstring example.

## 9. Water-cooling reference physics (`examples/water_cooling/objective.py`)

`WaterCoolingEvaluator` scores a cold-plate fin design with a **self-contained numpy
model** (trend-correct surrogate, clearly labelled as such; standard textbook
correlations). Operating point comes from `prepare.OPERATING`.

**Design params** (`DesignSpec.params`, produced by `designs/*.py`):
```
fin_type: "straight" | "pin"
L_mm, W_mm: base plate footprint (must fit OPERATING["envelope_mm"])
t_base_mm: base thickness
fin_height_mm, fin_thickness_mm
n_fins (straight) | (n_rows, n_cols) (pin), or pitch_mm
material: "copper" | "aluminum"   (k = 400 / 200 W/mK)
arrangement: "inline" | "staggered"   (pin only)
```

**Operating point** (`OPERATING`):
```
heat_load_W, coolant="water", flow_lpm, inlet_temp_C, envelope_mm{L,W,max_height}
```

**Model** (numpy; water props at ~40 °C: rho≈992, cp≈4178, mu≈6.5e-4, k_f≈0.63, Pr≈4.3):
1. Channel/passage geometry from fins → hydraulic diameter `D_h`, free-flow area,
   total wetted/fin area `A_fin`.
2. Mean velocity from `flow_lpm` and free-flow area; `Re = rho*V*D_h/mu`.
3. Nusselt: laminar (`Re<2300`) developing-flow correlation (e.g. a fixed Nu≈3.66 for
   constant-wall-T fully developed, or a Hausen/Sieder-Tate developing form);
   turbulent → Dittus–Boelter `Nu=0.023 Re^0.8 Pr^0.4`. `h = Nu*k_f/D_h`.
4. Fin efficiency `m=sqrt(2h/(k*t_fin))`, `eta=tanh(m*Lc)/(m*Lc)`; pin fins use the
   pin form. Effective area `A_eff = A_base_unfinned + eta*A_fin`.
5. Resistances: `R_conv = 1/(h*A_eff)`; `R_caloric = 1/(rho*Q_flow*cp)`;
   `R_cond_base = t_base/(k*A_plate)`; `R_th = R_cond_base + R_conv + R_caloric`.
   `base_temp_rise_C = heat_load_W * R_th`.
6. Pressure drop: friction factor (`f=64/Re` laminar, else `0.079 Re^-0.25`),
   `dP_friction = f*(Lflow/D_h)*0.5*rho*V^2`, plus entrance/exit minor losses
   `dP_minor = K*0.5*rho*V^2` (K≈1.5). `pressure_drop = dP_friction + dP_minor`.
7. **Feasibility / manufacturing constraints:** `fin_thickness_mm >= 0.3`, aspect
   ratio `fin_height/fin_thickness <= 30`, fits envelope, `fin_height <= max_height`,
   pitch >= fin_thickness + 0.3. Violations → `feasible=False` (still scored/recorded).

**Artifacts written to `out_dir`:**
- `heatmap.png` — a 2D base-plate temperature field. Build a coarse numpy grid over
  the L×W plate, impose a Gaussian hot spot at the heat-source location, diffuse it
  (a few Jacobi/FFT-Poisson smoothing iterations) with sink strength ∝ `1/R_th`, and
  render with `matplotlib` (`imshow`, colorbar, title with R_th & ΔT). This is the
  diagnostic the agent reads back next step.
- `fields.npz` — the raw temperature grid (+ velocity scalar).
- `breakdown.json` — `{R_cond_base,R_conv,R_caloric,R_th,dP_friction,dP_minor,
  fin_efficiency,h,Re,Nu,V,D_h,A_eff,mass_g}` (this becomes `result.metadata`).

`scores = {"thermal_resistance": R_th, "pressure_drop": pressure_drop}`.

Use `matplotlib.use("Agg")` (no display). Keep the whole model < ~300 lines.

## 10. Coding conventions (apply everywhere)

- Python ≥ 3.10, PEP 8, **type annotations on all signatures**, `from __future__
  import annotations`.
- Prefer immutability: frozen dataclasses for data; return new objects, don't mutate
  inputs. (The interfaces are all frozen.)
- Many small focused files (200–400 lines typical). Comprehensive error handling;
  evaluators never raise. Validate inputs at boundaries.
- Use `logging` or the `logfmt` helpers, not bare `print` for library internals
  (the loop/CLI may print user-facing progress).
- No hardcoded secrets. `ANTHROPIC_API_KEY` from env / subscription auth.
- Determinism: never call `datetime.now()`/`random` inside pure functions; pass tags
  and seeds in. (The loop accepts a `tag`.)

## 11. The skill — `skills/meta-research/{SKILL.md, REFERENCE.md}`

This is the **primary deliverable**: the generalized autoresearch "program" carrying the
Meta-Harness methodology. It is **domain-agnostic** (it never mentions fins or water);
the domain is supplied by `prepare.py` + `program.md` + `designs/`. The agent loads it
and runs the research loop **itself, in the Claude Code session** — there is no
subprocess. Authored per the `write-a-skill` rules.

**`SKILL.md` (< 100 lines)** — frontmatter `name: meta-research`, `description:` with
"Use when ..." triggers (autonomous design/parameter research, optimizing a design
against a simulator/objective, "run meta-research", a repo containing `prepare.py` +
`designs/`). Body:
- **Quick start:** read `program.md` and `prepare.py`; run `meta-research seed` to
  evaluate baselines; then loop.
- **The loop (checklist):**
  1. Inspect experience: `meta-research frontier`, `results.tsv` (the authoritative
     timeline), and **`Read` the actual `heatmap.png`** + `hypothesis.md` of frontier /
     recent / failed candidates (read `experience/<dir>/design.py` directly for full
     prior code — bundles are never rewritten; git is the audit layer, not the query
     interface).
  2. Form ONE falsifiable hypothesis targeting a **mechanism** (not a parameter tweak).
  3. Write `designs/<name>.py` (copy a frontier design, change one mechanism); dry-run
     `build()` + feasibility first (mandatory prototype).
  4. `meta-research eval <name> --hypothesis hyp.json --commit`.
  5. Read the result + new `heatmap.png`; note if it extended the Pareto frontier.
  6. Repeat. **Never stop** until interrupted; never `git reset`; never declare the
     frontier "optimal".
- **Hard rules (brief):** one mechanism per candidate; anti-parameter-tuning;
  anti-overfitting (designs stay physically general). Link to REFERENCE.md for detail.

**`REFERENCE.md`** — the depth (progressive disclosure): the mechanism-axis catalog
(generic: geometry / density / distribution / arrangement / material / interface — and
how a domain maps onto them), anti-parameter-tuning + anti-overfitting rules with
examples, the experience-bundle + `hypothesis.md` schema, the git append-only +
Pareto-frontier conventions, and how to swap the evaluator in `prepare.py`.

The example ships a **copy** at `examples/water_cooling/.claude/skills/meta-research/`
so the skill loads when Claude Code is started in that dir.

## 12. program.md (the domain "program", in the example)

Thin, human-edited, autoresearch-style. Points the agent at the skill and pins the
domain specifics: setup (agree a run tag `meta-research/<tag>`; the in-scope fixed files
`prepare.py`/`objective.py` are read-only; `designs/` is the write target), the loop
(invoke the meta-research skill; per step: write a design → `meta-research eval --commit`
→ read `result.json` + `heatmap.png` → it is "kept" iff it extends the Pareto frontier,
but it is committed either way), the git **append-only** rule (never reset), where the
ledger lives (`results.tsv`, `frontier.json`, `experience/`), and the "**NEVER STOP**
until interrupted" autonomy clause. It must NOT describe a Python orchestrator.
