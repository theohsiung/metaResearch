# meta-research — REFERENCE

Depth layer for the `meta-research` skill (load when you need detail). This is the
generalized **autoresearch** loop carrying the **Meta-Harness** methodology, applied
to optimizing a *parametric design* against a pluggable `Evaluator`. It is
domain-agnostic; water-cooling fins appear only as a worked example mapping.

---

## 1. Mechanism-axis catalog (generic)

A *mechanism* is a structural lever that changes **how** the design behaves, not just
a number. Each candidate changes **one** mechanism along **one** axis so its effect is
attributable. Tag the axis in `hypothesis.md` front-matter (`axis:`).

| Axis | Question it answers | Generic moves |
|------|--------------------|---------------|
| **geometry** | What is the *shape* of the active element? | swap primitive (straight ↔ pin ↔ louver), change cross-section, taper, fillet |
| **density** | How *much* active material / how many elements? | element count, packing fraction, wall/feature thickness, porosity |
| **spatial distribution** | *Where* is the material concentrated? | uniform → graded, cluster density over the hot/critical region, sparse elsewhere |
| **arrangement** | How are repeated elements *organized*? | inline ↔ staggered, aligned ↔ offset, series ↔ parallel, lattice topology |
| **material** | What *medium* / property set? | conductivity/stiffness/cost trade, single ↔ composite, coating |
| **interface** | How does the design *couple* to its boundary/flow/load? | inlet/outlet routing, manifold split, contact area, boundary-layer trip |

**How a domain maps onto the axes.** Read `prepare.py`/`objective.py` and ask, for the
quantity the diagnostic shows: which axis most directly moves it? The diagnostic
(e.g. a heatmap) localizes the failure; the axis tells you the lever.

### Worked example — water-cooling cold-plate fins
The example domain (`examples/water_cooling/`) scores a fin layout on
`thermal_resistance` (min) and `pressure_drop` (min). Axis mapping:

- **geometry** → `fin_type: straight | pin`; fin cross-section/taper.
- **density** → `n_fins` / `(n_rows,n_cols)` / `pitch_mm`; `fin_thickness_mm`.
- **spatial distribution** → fin pitch graded so fins crowd the hot band the heatmap
  shows over the heat source, sparse over the inlet.
- **arrangement** → `arrangement: inline | staggered` (pin fins).
- **material** → `material: copper | aluminum` (k = 400 / 200 W/mK): R_th vs mass/cost.
- **interface** → base thickness, flow length, entrance/exit losses, manifold routing.

Example: the iteration-9 heatmap shows a hot band over the outlet half →
hypothesis "staggering pin rows there raises local `h` where it matters" → axis
`arrangement`, change inline→staggered only. That is a mechanism, not a tweak.

---

## 2. Anti-parameter-tuning

Parameter tuning is sweeping a scalar hoping a number improves; it overfits the
evaluator and teaches the experience ledger nothing transferable.

- **Bad:** `n_fins` 40 → 42 → 44 → 46, committing each. (knob grinding)
- **Good:** "regrade fin pitch so density tracks the heat-flux profile" — a
  *distribution* mechanism with a stated reason from the diagnostic.
- A parameter may move **as a consequence** of a mechanism change (staggering may
  imply a different pitch). That is fine — the *hypothesis* is about the mechanism.
- One scalar set deliberately to test a feasibility boundary (once) is acceptable as a
  probe; record it as such. Repeated monotone sweeps are not.

Heuristic: if you cannot name the **physical/structural mechanism** and the **failure
mode in the diagnostic** that motivates it, you are tuning — stop and re-inspect.

---

## 3. Anti-overfitting

Meta-Harness keeps everything precisely so you can resist overfitting to the scorer.

- **Stay general.** The design must make sense as a physical/engineering artifact, not
  as an exploit of a correlation in the numerical model. If a change only helps because
  of a known model artifact, reject it.
- **Don't fixture to one operating point.** `OPERATING` is the *given* problem, not a
  thing to special-case. A design that wins only at exactly this heat load / flow and
  collapses nearby is overfit.
- **Respect manufacturability / hard constraints.** Infeasible designs are recorded
  (`feasible=false`) but never enter the frontier; do not chase scores into infeasible
  geometry.
- **Diversity over greed.** There is **no parent-selection rule** — you may build on any
  prior candidate, including a dominated or failed one whose diagnostic is instructive.
  A falsified hypothesis is a result; record what it ruled out.
- **Prototype first.** Always dry-run `build()` + a feasibility sanity check before
  spending an evaluation (the mandatory prototype step).

---

## 4. Experience bundle + `hypothesis.md` schema

Every evaluated candidate (kept, dominated, infeasible, crashed) is written by the
framework to an append-only bundle. **Read these back** each iteration — they are the
four Meta-Harness experience categories (design source, reasoning, scores, traces).

```
experience/<iter:03d>_<name>/
  design.py            # verbatim copy of the candidate module you wrote
  design_spec.json     # DesignSpec.to_json()
  hypothesis.md        # YAML front-matter + prose (below) — the reasoning trace
  result.json          # EvalResult.to_json() + a "status" field added by the runner
  trace/
    heatmap.png        # diagnostic image (read this back!) — evaluator-produced
    fields.npz         # optional raw arrays
    breakdown.json     # = result.metadata (per-mechanism breakdown)
    solver.log         # optional evaluator stdout
```

### `hypothesis.md` front-matter (you author the reasoning; the runner fills `status`)
```
---
name: staggered_pin_v3
iteration: 12
axis: arrangement              # one of the mechanism axes in §1
parent: pin_fins               # what it builds on (free-form, NOT a selection rule)
expected: "R_th down, dP up"
status: frontier|dominated|infeasible|crash   # filled by the loop after eval
---
<prose: which prior heatmaps/results you inspected, the failure mode this targets,
the single mechanism changed, and the predicted effect on each objective.>
```

### `hyp.json` hand-off (agent → `meta-research eval --hypothesis hyp.json`)
You write the design module, then pass this JSON; the runner folds it into
`hypothesis.md`. There is **no** `pending_eval.json`.
```json
{
  "axis": "arrangement",
  "parent": "pin_fins",
  "expected": "thermal_resistance down ~8%, pressure_drop up ~15%",
  "reasoning": "Iter 9 heatmap showed a hot band over the outlet half; staggering the pin rows there raises local h where it matters."
}
```
`--hypothesis` is optional to the CLI but **required by this skill**: it is the reasoning
trace Meta-Harness depends on. Make it specific and falsifiable.

---

## 5. Experience ledger (filesystem) + git audit trail + Pareto frontier

- **The filesystem is the experience store.** Every candidate — frontier, dominated,
  infeasible, or crashed — keeps its full bundle under `experience/` forever; bundles
  are never rewritten. `results.tsv` is the authoritative flat timeline.
- **git is the append-only audit/backup layer, not a query interface.** Every
  evaluation is committed so the run is tamper-evident, recoverable, and (when
  pushed) backed up off-site. **NEVER `git reset`, rebase-drop, or force-push** —
  that would delete the exact diagnostic traces the method runs on. Discarding is
  replaced by *frontier membership*, not deletion. (Contrast with autoresearch,
  where git *is* the store and the search state — advance on keep, reset on
  discard. Here bundles replaced the store role and the frontier replaced the
  keep/discard role.)
- **The run dir must be its own git repository** (`meta-research init` scaffolds
  this). The runner refuses to commit when `run_dir` is not the repository root:
  `git add -A` / `checkout -b` would otherwise stage host files and switch the
  host checkout's branch.
- **Run on a branch** `meta-research/<tag>` (the runner's `ensure_branch` handles this;
  it never resets). Agree the tag in `program.md` setup.
- **Commit message format** (the runner emits this):
  `iter<NN> <name>: <obj1>=<v1> <obj2>=<v2> [<status>] — <one-line summary>`.
- **Commit SHA as join key.** After each commit the runner annotates the bundle's
  `result.json` with `"commit": "<short-sha>"` — the snapshot of the whole run at
  that evaluation (`git show <sha>:frontier.json` replays the frontier as it stood
  then). The git-tracked copy of `result.json` lags one commit behind the disk
  copy, since a commit cannot contain its own SHA.
- **Pareto frontier replaces keep/discard.** `a` dominates `b` iff `a` is no worse on
  every objective and strictly better on ≥1 (using each `Objective.direction`). A
  feasible non-dominated candidate enters `frontier.json`; a dominated one is still
  committed, flagged `dominated`. Infeasible/crashed never enter the frontier.
- **Status values** (in `results.tsv` and `result.json`): `frontier | dominated |
  infeasible | crash`.
- **Inspect, don't trust memory.** `meta-research frontier` (Pareto set + best per
  objective), `results.tsv` (flat timeline), and the bundles themselves —
  `experience/<dir>/design.py`, `hypothesis.md`, `trace/heatmap.png` — read them
  directly from disk; they are never rewritten.
- `frontier.json` shape:
```json
{
  "objectives": [{"name":"thermal_resistance","direction":"min","unit":"K/W"},
                 {"name":"pressure_drop","direction":"min","unit":"Pa"}],
  "pareto": [{"name":"staggered_pin_v3","iteration":12,
              "scores":{"thermal_resistance":0.042,"pressure_drop":820.0},
              "metadata":{"fin_efficiency":0.78}}],
  "best_per_objective": {
    "thermal_resistance": {"name":"staggered_pin_v3","value":0.042},
    "pressure_drop": {"name":"straight_fins","value":410.0}}
}
```

---

## 6. Swapping the evaluator (configuration-as-code, in `prepare.py`)

The `Evaluator` is the generality hinge. Selecting numerical → surrogate → API is
editing the one factory `make_evaluator()` in the fixed `prepare.py`. **You do not
edit `prepare.py` during the loop** — this section is for whoever sets the domain up.
All three adapters share the `Evaluator` Protocol (`objectives: list[Objective]`;
`evaluate(design, out_dir) -> EvalResult`) and **never raise** (failures →
`EvalResult.crashed(...)`).

- **NumericalEvaluator** — in-process pure-python model (the runnable default). Wraps a
  `simulate(params, out_dir) -> (scores, metadata, artifacts)` callable. Use for fast,
  self-contained trend-correct models that emit a diagnostic image.
  ```python
  from meta_research.evaluators import NumericalEvaluator
  def make_evaluator():
      return NumericalEvaluator(OBJECTIVES, simulate=my_model)   # or a domain subclass
  ```
- **SurrogateEvaluator** — load and call a user model artifact. `from_path` resolves a
  `module:attr` callable, joblib, pickle, or (optional) onnxruntime. Provide a
  `feature_fn(params)` and a `predict_fn`. Optional deps (`joblib`, `onnxruntime`,
  `torch`) are soft imports — never hard-required. Writes `prediction.json`; no heatmap
  required.
- **ApiSolverEvaluator** — call an external high-fidelity solver. **REST:** POST
  `design.params` JSON to an endpoint, parse with `parse_fn(response_json)`. **CLI:**
  render a command template + input file, `subprocess` with timeout, then
  `parse_fn(out_dir)` reads outputs; raw stdout → `solver.log`. Stdlib only
  (`urllib.request`, `subprocess`); `requests` optional. Needs a real endpoint.

Because the contract is fixed by `OBJECTIVES`/`DesignSpec`/`EvalResult`, the **same
designs and the same loop** run unchanged when the evaluator is swapped — only fidelity
and cost change.

---

## 7. One-line mental model

Read the diagnostic → name the failure mode → pick the **one mechanism axis** that moves
it → change exactly that → evaluate, commit (append-only), read the new diagnostic →
repeat, building a Pareto frontier of *transferable* design knowledge, forever.
