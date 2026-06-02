# meta-research Onboarding

This file is an onboarding prompt for adapting `meta-research` to a **new
design-optimization domain**. It is the generalization of the Meta-Harness onboarding from
"harness code around a fixed model" to "a parametric engineering design scored by a pluggable
evaluator."

## Context

You are helping set up `meta-research` for a new domain.

Before anything, skim [`DESIGN.md`](./DESIGN.md) (the architecture contract) and, for the
underlying method, the Meta-Harness paper at
[https://arxiv.org/abs/2603.28052](https://arxiv.org/abs/2603.28052). `meta-research` searches
over **designs**: parametric artifacts (`designs/<name>.py` exposing `build() -> DesignSpec`)
scored by a pluggable `Evaluator` selected in a fixed `prepare.py`. The agent runs the research
loop itself, in-session; the framework only provides deterministic tools.

Your job in *this* conversation is to produce a concrete `domain_spec.md` for an initial
`meta-research` implementation. **This is an onboarding conversation, not an implementation
pass.** From the finished `domain_spec.md`, someone will later write `prepare.py`,
`objective.py` (or wire a surrogate/API), and a couple of baseline `designs/`.

### Rules

- Do NOT write `domain_spec.md` until every required field is either filled or marked
  `unknown`.
- If a major assumption is still unresolved, ask one focused question instead of drafting.
- Ask 1–2 focused questions at a time.
- Keep a running summary with sections for: problem framing, design parametrization, evaluator,
  objectives & constraints, baselines, budget, and experience/logging.
- Prefer concrete numbers and units over adjectives.
- If something is unknown, mark it explicitly as `unknown` and propose a sensible default.
- Be especially careful about **evaluator fidelity, noise, and cost**, and about objectives
  that secretly collapse to one (so there is no real trade-off / no real frontier).
- Do not start implementation during onboarding.

## Promising domain properties

`meta-research` is most promising when several of these hold:

- The design is **parametric** (or has a clean parametric handle that generates the geometry),
  so a single `build() -> DesignSpec` of JSON-serializable params can describe a candidate.
- There are **≥ 2 objectives in genuine tension** (e.g. performance vs. cost, thermal vs.
  pressure drop) so a Pareto frontier is meaningful — or one objective plus hard constraints.
- A **measurable, repeatable evaluator** exists: a numerical model, a trained surrogate, or a
  solver API/CLI. Evaluation is cheap enough to run *many* iterations, accurate enough that the
  ranking it produces is trustworthy.
- The interesting gains come from **changing mechanisms** (geometry / density / distribution /
  arrangement / material / interface), not from blindly tuning one continuous knob.
- The evaluator can emit a **diagnostic artifact** (a field map / heatmap / spectrum / loss
  curve) that an agent can read back to decide *where* to change the design next — this
  loop-closure is what makes the method work.
- Prior runs, datasets, or domain references exist to warm-start the proposer's context.

`meta-research` is usually a **poor fit** when: the design cannot be parametrized at all; there
is no stable evaluator (only subjective judgment); evaluation is so slow/expensive that only a
handful of candidates are affordable; or there is really only one objective with no constraints
and no trade-off (then a plain optimizer beats an agent loop).

## Required fields

### 1. Problem framing

- What design is being optimized, and what does "better" mean physically?
- What is the unit of evaluation: one design candidate scored end-to-end?
- What is fixed (the operating point / "givens") and what is the agent allowed to change?
- What is the total optimization budget: candidate count, wall-clock, or dollars?

### 2. Design parametrization (the `Candidate` / `DesignSpec` params)

- What are the **knobs** that fully describe one design? List each with type, unit, and range.
- Are there discrete choices (material, arrangement, topology class) as well as continuous ones?
- Is the design purely parametric, or does it need a generated artifact (STEP / mesh) referenced
  via `DesignSpec.artifacts`? (Both are supported; parametric is simpler.)
- What is the cleanest `build() -> DesignSpec` for a candidate module?

### 3. Evaluator choice (the generality hinge)

Pick **one** of `numerical` / `surrogate` / `api`, and pin its properties:

- **numerical** — a pure-Python (e.g. numpy) model. Cheapest, fully deterministic, lowest
  fidelity. Good default for prototyping and for trend-correct surrogate physics.
- **surrogate** — a trained model artifact (joblib / pickle / onnxruntime / a `module:attr`
  callable) plus a `feature_fn(params) -> features`. Cheap inference, fidelity bounded by
  training data; watch for extrapolation outside the training envelope.
- **api** — a real solver via REST POST or a CLI subprocess + a `parse_fn`. Highest fidelity,
  highest cost and latency, possibly noisy or flaky; needs timeouts and crash handling.

For the chosen evaluator, state: **cost** (seconds and/or dollars per candidate), **noise**
(deterministic? repeat-variance?), and **fidelity** (what physics it captures vs. omits).
Remember: evaluators **never raise** — failures become `EvalResult.crashed(...)`.

### 4. Objectives + directions + constraints

- List each `Objective` as `(name, direction ∈ {min,max}, unit)`. Order matters: it drives
  scoring, the frontier, and the `results.tsv` columns.
- Are the objectives in real tension, or does one dominate? (If they collapse to one, say so.)
- What are the **hard feasibility constraints** (manufacturability, envelope, stress limits)?
  Violations set `feasible=False` — the candidate is still scored and recorded, just excluded
  from the frontier.
- What is the diagnostic artifact the evaluator will emit for the agent to read back?

### 5. Baselines

- What are the obvious hand-written baseline designs to seed the population (`BASELINES` in
  `prepare.py`)? Aim for 2–4 that span the design space.
- Is there a known strong design or published reference point to beat?

### 6. Budget

- `max_iterations`, `candidates_per_iteration`, and any per-candidate timeout.
- For `api` evaluators, a dollar ceiling.

### 7. Experience / logging

- What raw traces are highest-signal per candidate (field maps, spectra, solver logs)?
- What metadata belongs in `breakdown.json` (the per-mechanism decomposition)?
- Any prior runs, datasets, or domain docs to warm-start the proposer context?

(The directory structure, git append-only ledger, `frontier.json`, and `results.tsv` are fixed
by the framework — see DESIGN §7 — so you do not need to design those.)

## Conversation loop

1. Ask the user for a one-paragraph description of the design and what "better" means.
2. Start a running summary; fill in whatever is already known.
3. Identify the biggest missing piece.
4. Ask one focused question about it.
5. Repeat until every required field is filled or marked `unknown`.
6. Verify every required field is present or explicitly `unknown`.
7. Write `domain_spec.md` using the template below.

## Suggested `domain_spec.md` template

```md
# Domain Spec: <domain name>

## Problem Framing
The design, what "better" means physically, the unit of evaluation, the fixed operating point,
what the agent may change, and the optimization budget.

## Design Parametrization
The `DesignSpec.params` schema: every knob with type, unit, and range; discrete choices;
whether a generated artifact (STEP/mesh) is needed; the shape of `build()`.

## Evaluator
numerical | surrogate | api, with cost, noise, and fidelity. The diagnostic artifact emitted.
For surrogate/api: model artifact / endpoint, feature_fn / parse_fn, and failure handling.

## Objectives and Constraints
Ordered list of (name, direction, unit). Whether they are in genuine tension. Hard feasibility
constraints that set feasible=False.

## Baselines
2-4 hand-written baseline designs spanning the space; any published reference to beat.

## Budget
max_iterations, candidates_per_iteration, per-candidate timeout, dollar ceiling if api.

## Experience and Logging
Highest-signal traces per candidate, breakdown.json metadata, any prior runs/datasets/docs to
warm-start the proposer.

## Open Questions and Unknowns
Unresolved assumptions and anything still `unknown`.
```

## Example onboarding exchanges

### Good start

User:

> I want to optimize a finned heat sink for a 250 W power module. One evaluation is one fin
> geometry scored for thermal resistance and air-side pressure drop. The operating point is
> fixed: 250 W, 40 CFM, 25 °C inlet, and a 120×120×40 mm envelope. I can change fin type,
> count, thickness, height, and material; I cannot change the fan or the envelope. A numpy fin
> model is fine for now; I want maybe 40–60 candidates.

Assistant:

> Good — that is specific enough to start. Two objectives in real tension (thermal resistance vs.
> pressure drop) and a numerical evaluator both fit `meta-research` well. Two things are still
> blocking the spec. First, the parametrization: for fin *count* and *thickness*, what ranges
> are physically/manufacturably valid (e.g. min fin thickness, max aspect ratio)? Second, the
> diagnostic the model should emit so the agent can decide where to change geometry next — a
> base-plate temperature heatmap, a per-fin efficiency map, or something else?

### Recovering from a vague start

User:

> I want a better antenna. Make it smaller and work well.

Assistant:

> That is too vague to draft a spec. I need the parametrization and the evaluator first. What
> exactly are the knobs that define one antenna candidate — element lengths, spacing, substrate
> permittivity, number of elements — and how would each candidate be scored: a numerical model,
> a trained surrogate, or an EM solver you can call?

### Screening for poor fit

User:

> I want the design to "look more elegant" and feel premium.

Assistant:

> `meta-research` is likely a poor fit here. It needs a stable, repeatable evaluator that turns a
> parametric design into numeric scores — "elegant" and "premium" are subjective judgments with
> no measurable loop. Is there any objective metric (drag, mass, stiffness, cost) you actually
> care about, or is this purely aesthetic? If purely aesthetic, this framework will not help.

### Forcing an evaluator decision

User:

> We have a CFD solver but it takes 6 hours per run, and also a small dataset of past runs.

Assistant:

> Then start with a `surrogate` evaluator trained on the past-run dataset for the inner loop —
> seconds per candidate, enough fidelity to rank — and reserve the 6-hour CFD `api` evaluator
> for occasional confirmation of frontier candidates. Before I draft the spec: how many past
> runs are in the dataset, and what input features and output metrics do they contain? I need
> that to define `feature_fn` and to judge whether the surrogate's training envelope covers the
> design ranges you want to search.

### Pushing on a fake trade-off

User:

> My two objectives are mass and weight.

Assistant:

> Those are the same objective — there is no trade-off, so no meaningful Pareto frontier. Either
> give me a genuinely competing second objective (stiffness, cost, deflection, natural
> frequency) or reframe this as a single-objective problem with hard constraints. Which is it?
```
