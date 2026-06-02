"""prepare.py -- FIXED / read-only configuration-as-code for the water-cooling domain.

This module is the autoresearch "config" for the cold-plate fin-optimization
experiment, but it is **plain Python, not YAML** (fusion rule DESIGN.md §1.1.1).
It pins, as module-level constants, everything the framework needs to discover and
run the experiment from the current working directory (DESIGN.md §4):

    OBJECTIVES   ordered list[Objective]   -> scoring + Pareto frontier + results.tsv columns
    BUDGET       dict                       -> loop bookkeeping (max_iterations, ...)
    OPERATING    dict                       -> the fixed "givens" (heat load, flow, envelope)
    BASELINES    list[str]                  -> design module names that seed the population
    DESIGNS_DIR  str                        -> where candidate modules live (the agent's write target)
    make_evaluator() -> Evaluator           -> the pluggable scoring hinge (DESIGN.md §1.1.1, §8)

The agent treats this file (and objective.py) as **read-only**. To change the
problem -- objectives, operating point, or which evaluator backend scores designs
-- a human edits this single file. Swapping the numerical model for a loaded
surrogate or a remote/CLI solver is editing ``make_evaluator`` only; see the
commented alternatives at the bottom (the configuration-as-code demonstration).

Nothing here mutates global state, calls ``datetime.now()`` or ``random``, or has
side effects on import (DESIGN.md §10).
"""

from __future__ import annotations

from meta_research.interfaces import Evaluator, Objective

# --------------------------------------------------------------------------- #
# Objectives (DESIGN.md §4, §7.4, §7.5)
#
# Ordered: this order drives scoring, the Pareto frontier, and the results.tsv
# columns. Both are minimized. ``weight`` is display/scalarization-only -- Pareto
# ranking never consults it (see interfaces.Objective).
# --------------------------------------------------------------------------- #
OBJECTIVES: list[Objective] = [
    Objective(name="thermal_resistance", direction="min", unit="K/W", weight=1.0),
    Objective(name="pressure_drop", direction="min", unit="Pa", weight=0.3),
]

# --------------------------------------------------------------------------- #
# Budget (DESIGN.md §4)
#
# Loop bookkeeping the agent consults while running the research loop in-session.
# There is NO subprocess orchestrator: these are advisory bounds, not a driver.
#   max_iterations           how many outer iterations to attempt before pausing
#   candidates_per_iteration how many fresh designs to propose per iteration
#   proposer_model           which model the human runs Claude Code with
#   proposer_timeout_s       per-proposal wall-clock budget hint
# --------------------------------------------------------------------------- #
BUDGET: dict = {
    "max_iterations": 20,
    "candidates_per_iteration": 3,
    "proposer_model": "opus",
    "proposer_timeout_s": 2400,
}

# --------------------------------------------------------------------------- #
# Operating point (DESIGN.md §4, §9)
#
# The fixed physical "givens" of the problem. The evaluator reads these; designs
# must fit ``envelope_mm`` and respect the manufacturing constraints in §9.
#   heat_load_W   total dissipated power applied to the base plate footprint
#   coolant       working fluid (water properties baked into objective.py)
#   flow_lpm      volumetric coolant flow rate, litres per minute
#   inlet_temp_C  coolant inlet temperature
#   envelope_mm   L x W footprint and the max stack height (base + fins)
# --------------------------------------------------------------------------- #
OPERATING: dict = {
    "heat_load_W": 200.0,
    "coolant": "water",
    "flow_lpm": 1.0,
    "inlet_temp_C": 30.0,
    "envelope_mm": {"L": 40, "W": 40, "max_height": 20},
}

# --------------------------------------------------------------------------- #
# Baselines (DESIGN.md §4) -- module names in DESIGNS_DIR used to seed the
# population + Pareto frontier in Phase 0 (`meta-research seed`).
# --------------------------------------------------------------------------- #
BASELINES: list[str] = ["straight_fins", "pin_fins"]

# Where candidate modules live -- the agent's only write target (DESIGN.md §4, §5).
DESIGNS_DIR: str = "designs"


def make_evaluator() -> Evaluator:
    """Build the ground-truth :class:`Evaluator` that scores every design.

    Configuration-as-code hinge (DESIGN.md §1.1.1, §8): the *only* place the
    scoring backend is selected. The default returns the self-contained numpy
    fin model from ``objective.py`` (a runnable, trend-correct surrogate). To
    score against a loaded ML surrogate or a real/remote solver instead, comment
    this body out and uncomment one of the alternatives below -- nothing else in
    the experiment changes.
    """
    # Imported lazily so importing prepare.py never requires the heavy numpy /
    # matplotlib stack (e.g. when the CLI only needs OBJECTIVES + DESIGNS_DIR).
    from objective import WaterCoolingEvaluator

    return WaterCoolingEvaluator(OPERATING)


# =========================================================================== #
# Configuration-as-code demonstration (DESIGN.md §1.1.1, §8.2, §8.3)
#
# The three evaluator backends all satisfy the same `Evaluator` protocol, so
# swapping which physics scores a design is a one-function edit here -- no change
# to designs/, the loop, the ledger, or the frontier. The blocks below are kept
# commented as ready-to-use templates.
# =========================================================================== #

# --- Alternative A: a loaded ML surrogate (DESIGN.md §8.2) ------------------ #
# Replaces the in-process numpy model with a user-trained surrogate (sklearn /
# joblib / pickle / onnx). `feature_fn` turns DesignSpec.params into the model's
# input vector; the model returns one prediction per objective.
#
# def make_evaluator() -> Evaluator:
#     from meta_research.evaluators import SurrogateEvaluator
#
#     def feature_fn(params: dict) -> list[float]:
#         # Map design knobs -> the surrogate's feature order.
#         return [
#             float(params.get("fin_height_mm", 0.0)),
#             float(params.get("fin_thickness_mm", 0.0)),
#             float(params.get("n_fins", 0)),
#             float(OPERATING["flow_lpm"]),
#             float(OPERATING["heat_load_W"]),
#         ]
#
#     def predict_fn(model, params: dict) -> dict[str, float]:
#         # Adapt the model's raw output to {objective_name: value}.
#         y = model.predict([feature_fn(params)])[0]
#         return {"thermal_resistance": float(y[0]), "pressure_drop": float(y[1])}
#
#     return SurrogateEvaluator.from_path(
#         objectives=OBJECTIVES,
#         model_path="models/coldplate_surrogate.joblib",
#         feature_fn=feature_fn,
#         predict_fn=predict_fn,
#     )

# --- Alternative B: a real solver over REST (DESIGN.md §8.3, REST mode) ----- #
# POSTs DesignSpec.params as JSON to an external CFD/thermal service and parses
# the JSON response into (scores, metadata, artifacts).
#
# def make_evaluator() -> Evaluator:
#     from meta_research.evaluators import ApiSolverEvaluator
#
#     def parse_fn(response_json: dict) -> tuple[dict, dict, dict]:
#         scores = {
#             "thermal_resistance": float(response_json["R_th"]),
#             "pressure_drop": float(response_json["dP_Pa"]),
#         }
#         metadata = dict(response_json.get("diagnostics", {}))
#         artifacts = dict(response_json.get("artifacts", {}))  # name -> local path
#         return scores, metadata, artifacts
#
#     return ApiSolverEvaluator.rest(
#         objectives=OBJECTIVES,
#         endpoint="https://solver.internal/coldplate/evaluate",
#         parse_fn=parse_fn,
#         extra_payload={"operating": OPERATING},
#         timeout_s=BUDGET["proposer_timeout_s"],
#     )

# --- Alternative B': a real solver as a local CLI (DESIGN.md §8.3, CLI mode) - #
# Writes the design to an input file, shells out to a solver binary with a
# timeout, then parses the solver's output files from out_dir.
#
# def make_evaluator() -> Evaluator:
#     from meta_research.evaluators import ApiSolverEvaluator
#
#     def parse_fn(out_dir) -> tuple[dict, dict, dict]:
#         import json
#         data = json.loads((out_dir / "solver_out.json").read_text())
#         scores = {
#             "thermal_resistance": float(data["R_th"]),
#             "pressure_drop": float(data["dP_Pa"]),
#         }
#         return scores, dict(data.get("diagnostics", {})), {"field": "field.vtk"}
#
#     return ApiSolverEvaluator.cli(
#         objectives=OBJECTIVES,
#         command_template="coldplate-solver --in {input_file} --out {out_dir}",
#         parse_fn=parse_fn,
#         timeout_s=BUDGET["proposer_timeout_s"],
#     )
