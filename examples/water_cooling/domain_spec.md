# domain_spec.md — water-cooling cold-plate fin design

> Filled domain specification for the `water_cooling` example. This is the human-readable
> companion to the machine-readable contract in `prepare.py` (configuration-as-code).
> Where this document and `prepare.py` disagree, **`prepare.py` wins** — it is the source
> of truth the framework actually loads. This file explains the *why* and gives the agent
> the param schema, constraints, and conventions it needs to propose designs.

## 1. Problem framing

We are designing the **finned interior of a liquid-cooling cold plate**: a copper or
aluminum base plate with an array of fins (straight channel fins or pin fins) over which
single-phase water flows to carry heat away from a chip mounted on the underside.

The agent searches the **design space of fin geometry and layout** to push out the
Pareto frontier of two competing objectives:

- **Thermal resistance** `R_th` (K/W) — junction-to-coolant resistance. Lower is better
  (cooler chip). Driven by convective area, heat-transfer coefficient, base conduction.
- **Pressure drop** `pressure_drop` (Pa) — hydraulic cost of pushing coolant through the
  fin field. Lower is better (smaller pump, less parasitic power).

These trade off: denser/taller fins lower `R_th` but choke the flow and raise `dP`. There
is no single optimum, only a frontier. The agent's job is to **discover mechanisms** that
move the frontier outward, not to tune one knob.

The evaluator is a **self-contained numpy surrogate** (textbook correlations, trend-correct
but **not** validated CFD). It is clearly labelled as a surrogate. The point of the example
is the *research loop and methodology*, not numerical accuracy. See §6 for swapping in a
real solver.

## 2. Design parametrization (the `DesignSpec.params`)

Each candidate is a module `designs/<name>.py` exposing `build() -> DesignSpec`. The
`DesignSpec.params` dict (see `meta_research/interfaces.py`) carries these keys:

| Key                | Type / values                       | Meaning                                              |
|--------------------|-------------------------------------|------------------------------------------------------|
| `fin_type`         | `"straight"` \| `"pin"`             | Channel fins vs. pin-fin matrix.                     |
| `L_mm`             | float                               | Base-plate length along the flow direction (mm).     |
| `W_mm`             | float                               | Base-plate width across the flow (mm).               |
| `t_base_mm`        | float                               | Base-plate thickness under the fins (mm).            |
| `fin_height_mm`    | float                               | Fin height (mm). Must be `<= envelope max_height`.   |
| `fin_thickness_mm` | float                               | Fin / pin thickness (diameter for pins) (mm).        |
| `n_fins`           | int (straight)                      | Number of straight fins across `W_mm`.               |
| `n_rows`, `n_cols` | int, int (pin)                      | Pin matrix rows (along flow) × cols (across flow).   |
| `pitch_mm`         | float (optional)                    | Center-to-center spacing; overrides count if given.  |
| `material`         | `"copper"` \| `"aluminum"`          | `k = 400` / `200` W/m·K.                             |
| `arrangement`      | `"inline"` \| `"staggered"`         | Pin-fin row arrangement (pin only; ignore for straight). |
| `notes`            | (via `DesignSpec.notes`)            | Free-text rationale (what idea this design encodes). |

Provide the count **or** the pitch for the relevant fin type; the evaluator derives the
other from `W_mm` / `L_mm`. Keep all params JSON-serializable (they are snapshotted into
the experience bundle).

A minimal candidate module:

```python
from __future__ import annotations
from meta_research.interfaces import DesignSpec

NAME = "straight_fins_dense"

def build() -> DesignSpec:
    return DesignSpec(
        params={
            "fin_type": "straight",
            "L_mm": 40.0, "W_mm": 40.0, "t_base_mm": 2.0,
            "fin_height_mm": 8.0, "fin_thickness_mm": 0.5,
            "n_fins": 30, "material": "copper",
        },
        notes="Denser straight-fin baseline: more wetted area at the cost of dP.",
    )
```

## 3. Evaluator (the scoring hinge)

- **Default: numerical.** `make_evaluator()` in `prepare.py` returns a
  `WaterCoolingEvaluator` (in `objective.py`), which composes the framework's
  `NumericalEvaluator` around a pure-python `simulate(params, out_dir)` model. Operating
  point comes from `prepare.OPERATING`.
- **Physics (numpy; water props ~40 °C: ρ≈992, cp≈4178, μ≈6.5e-4, k_f≈0.63, Pr≈4.3):**
  hydraulic diameter & free-flow area from the fin geometry → mean velocity from
  `flow_lpm` → `Re` → Nusselt (laminar Nu≈3.66 / developing form; turbulent
  Dittus–Boelter `Nu=0.023 Re^0.8 Pr^0.4`) → `h = Nu·k_f/D_h` → fin efficiency
  `η = tanh(mL_c)/(mL_c)` → effective area → resistances
  `R_th = R_cond_base + R_conv + R_caloric` → friction factor (`64/Re` laminar,
  `0.079 Re^-0.25` turbulent) → `pressure_drop = dP_friction + dP_minor`.
- **Diagnostics written per evaluation** (into the bundle `trace/`):
  - `heatmap.png` — 2D base-plate temperature field (Gaussian hot spot at the source,
    diffused, sink strength ∝ `1/R_th`); title shows `R_th` & `ΔT`. **This is what the
    agent reads back** to decide where to change the design.
  - `fields.npz` — raw temperature grid (+ velocity scalar).
  - `breakdown.json` — `{R_cond_base, R_conv, R_caloric, R_th, dP_friction, dP_minor,
    fin_efficiency, h, Re, Nu, V, D_h, A_eff, mass_g}` (becomes `result.metadata`).
- **Never raises.** Any failure → `EvalResult.crashed(error)` (recorded with status
  `crash`). Infeasible designs are still scored and recorded (status `infeasible`).

## 4. Objectives, directions, constraints

**Objectives** (order = `prepare.OBJECTIVES`; drives scoring, frontier, tsv columns):

| name                 | direction | unit | meaning                          |
|----------------------|-----------|------|----------------------------------|
| `thermal_resistance` | `min`     | K/W  | junction-to-coolant resistance   |
| `pressure_drop`      | `min`     | Pa   | coolant-side hydraulic loss      |

Pareto: a design dominates another iff it is no worse on both and strictly better on at
least one. Non-dominated **feasible** designs enter `frontier.json`.

**Hard feasibility / manufacturing constraints** (violation → `feasible=False`, still
recorded, excluded from the frontier):

- `fin_thickness_mm >= 0.3` (minimum machinable feature).
- aspect ratio `fin_height_mm / fin_thickness_mm <= 30` (no flimsy fins).
- footprint fits the envelope: `L_mm <= envelope.L`, `W_mm <= envelope.W`.
- `fin_height_mm <= envelope.max_height`.
- `pitch_mm >= fin_thickness_mm + 0.3` (clearance between fins/pins).

Dry-run these in your prototype step **before** spending an evaluation.

## 5. Baselines, operating point, budget

- **Operating point** (`prepare.OPERATING`, the fixed givens):
  `heat_load_W` (chip dissipation), `coolant="water"`, `flow_lpm`, `inlet_temp_C`,
  `envelope_mm = {L, W, max_height}`. These are constants of the problem — the agent
  designs *against* them and never changes them.
- **Baselines** (`prepare.BASELINES`, seed the population in Phase 0):
  - `straight_fins` — a moderate straight-channel-fin plate.
  - `pin_fins` — an inline pin-fin matrix.
  Run `meta-research seed --commit` to evaluate both and initialize the frontier.
- **Budget** (`prepare.BUDGET`): advisory only in the agent-driven model — there is no
  subprocess loop reading it. It carries `{max_iterations, candidates_per_iteration,
  proposer_model, proposer_timeout_s}` as guidance; the human interrupts when done
  (see `program.md` §4, "NEVER STOP").

## 6. Swapping the evaluator (configuration-as-code)

Changing fidelity is editing **one factory** in `prepare.py` — nothing else moves:

- **Surrogate model.** Return `SurrogateEvaluator.from_path(OBJECTIVES, model_path=...,
  feature_fn=...)` to load a trained model (`module:attr` callable, joblib, pickle, or
  ONNX) that maps `params → {thermal_resistance, pressure_drop}`. Optional deps
  (`joblib`, `onnxruntime`, `torch`) are soft imports.
- **Real solver (API/CLI).** Return an `ApiSolverEvaluator` — POST `design.params` to a
  CFD REST endpoint, or render a CLI command + input file, run via `subprocess` with a
  timeout, and parse the solver's output files with a `parse_fn`. Raw stdout is saved to
  `solver.log`; produced files become artifacts.

The agent never sees the difference: it still calls `meta-research eval <name>` and reads
back the same `result.json` + `heatmap.png` (a surrogate writes a small `prediction.json`
instead of a heatmap; an API solver may emit its own field plots). Keep designs
physically general so they remain valid as fidelity rises (anti-overfitting).

## 7. Experience & logging

Every evaluated candidate — frontier, dominated, infeasible, or crashed — is stored as a
full bundle and committed (git **append-only**, never reset):

```
experience/<iter:03d>_<name>/
  design.py            # verbatim candidate source
  design_spec.json     # DesignSpec.to_json()
  hypothesis.md        # YAML front-matter (name/iteration/axis/parent/expected/status) + prose
  result.json          # EvalResult.to_json() + "status"
  trace/
    heatmap.png        # the diagnostic the agent reads back
    fields.npz         # raw arrays
    breakdown.json     # per-mechanism breakdown (= result.metadata)
results.tsv            # flat ledger: iter name status feasible thermal_resistance pressure_drop hypothesis
frontier.json          # current Pareto set + best-per-objective
```

`hypothesis.md` **is** the reasoning trace (DESIGN §7.2): it records which prior
heatmaps/results were inspected, the failure mode targeted, the mechanism changed, and
the expected effect. Authoring it well is mandatory — it is what makes the experience
filesystem useful to the next iteration.
