# Knowledge Graph (`kg.json`) Implementation Plan

> **For Claude:** Use /tdd to execute this plan. Each task is one RED→GREEN cycle with a stable ID (`T-NNN`). Derive the test code at execution time from the behavior description — do NOT copy or pre-write test code into this plan.

**Goal:** The research agent can `Read` a single derived `kg.json` to see candidate lineage and which parameter changes produced which score changes, then follow bundle pointers into full experience bundles.
**Design doc:** `docs/features/kg-graph/design.md`
**Architecture context:** No `docs/architecture.md`; the binding contract is the repo-root `DESIGN.md` (engine = deterministic tools, agent = intelligence; ledger is append-only; derived views like `frontier.json` are recomputed from bundles each step). The KG follows the `frontier.json` precedent exactly.
**Tech stack:** Python ≥ 3.10, pytest, stdlib-only engine module (no third-party imports in `kg.py`).
**Task IDs:** assigned in creation order, never reused. Withdrawn tasks marked `~~T-NNN~~ Withdrawn`.

Vocabulary (from DESIGN.md): *bundle* (`experience/<iter:03d>_<name>/`), *candidate*,
*hypothesis hand-off* (agent → runner JSON), *front-matter* (`hypothesis.md` YAML block),
*ledger* (append-only experience + git), *frontier* (Pareto set).

---

### T-001: Caller comparing a child's params against its parent's sees changed/added/removed knobs

**Behavior to verify**
A caller (the KG builder, or any tool) comparing two designs' `params` dicts gets
exactly which knobs changed (`{key: [old, new]}`), which were added (`{key: new}`),
and which were removed (`{key: old}`), with values verbatim; non-scalar values
compare as opaque wholes; identical dicts yield three empty maps.

**Public interface this task introduces**
- Level: module-public (engine API)
- New: `meta_research.kg.param_diff(old: dict, new: dict) -> dict` — shallow top-level diff per design.md §2.

**Files**
- Create: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Test framing hints (NOT test code)**
- Ground fixtures in the water-cooling vocabulary (e.g. `n_fins` vs `n_rows`/`arrangement`) — different candidates legitimately have different key sets.
- Pure function: no filesystem, no mocks.

**Anti-patterns to refuse**
- Testing internal helper decomposition; only the returned mapping shape/content matters.

**Definition of done**
- One failing test (RED), minimal implementation (GREEN), survives internal refactor.
- Commit: `feat(kg): param_diff reports changed/added/removed design knobs`

### T-002: Caller comparing child scores against parent scores gets per-objective deltas

**Behavior to verify**
For a child's score dict and its parent's, the caller gets `child − parent` per
child score key, and `null`/`None` where the parent lacks that key (e.g. crashed
parent with no scores).

**Public interface this task introduces**
- Level: module-public (engine API)
- New: `meta_research.kg.score_delta(child: dict, parent: dict) -> dict`

**Files**
- Modify: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Test framing hints (NOT test code)**
- Use objective names from the example domain (`thermal_resistance`, `pressure_drop`).

**Anti-patterns to refuse**
- Direction-aware "improvement" judgments — the delta is a raw fact; direction lives in `Objective`.

**Definition of done**
- RED → GREEN; commit: `feat(kg): score_delta reports per-objective child-parent deltas`

### T-003: The proposer's `inspired_by` hand-off is persisted in the bundle and readable back

**Behavior to verify**
When a candidate is recorded with a hypothesis hand-off containing
`inspired_by: ["a", "b"]`, the written `hypothesis.md` front-matter carries it
(comma-separated scalar) and parsing the bundle's front-matter recovers the names;
absent/empty `inspired_by` round-trips as empty.

**Public interface this task introduces**
- Level: module-public (store contract, DESIGN §7.2/§7.3 extension)
- Modified: `Experience.record(...)` — folds optional `hypothesis["inspired_by"]` into front-matter.

**Files**
- Modify: `meta_research/experience.py`
- Test: `tests/test_experience.py`

**Test framing hints (NOT test code)**
- Drive through `Experience.record(...)` and read the produced `hypothesis.md` /
  `_read_hypothesis`-level parse — the bundle file IS the public artifact (schema 7.1).
- Follow existing `test_experience.py` fixture style.

**Anti-patterns to refuse**
- Asserting private rendering helpers directly.

**Definition of done**
- RED → GREEN; commit: `feat(experience): persist inspired_by in hypothesis front-matter`

### T-004: Rebuilding the KG over a ledger yields one node per bundle (store everything)

**Behavior to verify**
`build_kg(run_dir)` over an experience dir with frontier / dominated / infeasible /
crashed bundles returns a graph dict with one node per bundle —
`id` (= bundle dirname), `iteration`, `name`, `status`, `scores`, `bundle`
(run_dir-relative path) — and an empty experience dir (or none) yields an empty
graph with no error. `build_kg` is importable from `meta_research`.

**Public interface this task introduces**
- Level: module-public (engine API)
- New: `meta_research.kg.build_kg(run_dir: Path) -> dict` — pure read of bundles.
- Modified: `meta_research/__init__.py` — re-export.

**Files**
- Modify: `meta_research/kg.py`, `meta_research/__init__.py`
- Test: `tests/test_kg.py`

**Test framing hints (NOT test code)**
- Build fixture bundles on disk via `Experience.record(...)` (the real writer), not hand-rolled files — keeps the test honest against schema 7.1.
- Include a crashed candidate to prove store-everything.

**Anti-patterns to refuse**
- Asserting on internal scan order or private parsing helpers.

**Definition of done**
- RED → GREEN; commit: `feat(kg): build_kg derives candidate nodes from the ledger`

### T-005: A candidate with a parent gets a `mutated-from` edge carrying the facts

**Behavior to verify**
When a child bundle's front-matter names a `parent`, the graph contains one
`mutated-from` edge `src=<parent id> dst=<child id>` with `axis` (child's label),
`param_diff` (parent params → child params), and `score_delta` (child − parent);
baselines (`parent: ""`) get no `mutated-from` edge.

**Public interface this task introduces**
- Level: module-public
- Modified: `build_kg` — edges list per design.md §3.

**Files**
- Modify: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Test framing hints (NOT test code)**
- One parent + one child recorded through `Experience.record` with a real param change (e.g. `arrangement: inline → staggered`).

**Anti-patterns to refuse**
- Asserting prose/`expected` content on edges — locked out by design (facts only).

**Definition of done**
- RED → GREEN; commit: `feat(kg): mutated-from edges carry param_diff and score_delta`

### T-006: A parent name resolves to its latest prior evaluation

**Behavior to verify**
When the same candidate name was evaluated at several iterations, a child naming
that parent links to the **latest bundle of that name with iteration < child's**
— never to a later re-evaluation, never to an earlier one when a newer prior exists.

**Public interface this task introduces**
- Level: module-public
- Modified: `build_kg` (resolution rule; no new entry point).

**Files**
- Modify: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Test framing hints (NOT test code)**
- Record name X at iterations 0 and 2, child at 3 naming X → edge src is the iter-2 bundle; child at 1 naming X → edge src is the iter-0 bundle.

**Anti-patterns to refuse**
- Reaching into a private resolution function — assert through the produced edges.

**Definition of done**
- RED → GREEN; commit: `feat(kg): parent names resolve to the latest prior bundle`

### T-007: `inspired_by` names yield pure lineage pointers

**Behavior to verify**
A child whose bundle persisted `inspired_by: [a, b]` yields one `inspired-by`
edge per resolved name (src=inspiration, dst=child) with **no** `param_diff` /
`score_delta` / `axis` payload.

**Public interface this task introduces**
- Level: module-public
- Modified: `build_kg`.

**Files**
- Modify: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Anti-patterns to refuse**
- Computing diffs on inspired-by edges (cross-design diff semantics are undefined — locked decision Q4).

**Definition of done**
- RED → GREEN; commit: `feat(kg): inspired-by edges record multi-parent lineage`

### T-008: Unresolvable lineage names surface as warnings, never silently and never fatally

**Behavior to verify**
A bundle naming a `parent` or `inspired_by` that matches no prior bundle still
produces its node; the graph's `warnings` list contains a human-readable entry
naming the child and the missing reference; build does not raise.

**Public interface this task introduces**
- Level: module-public
- Modified: `build_kg` (`warnings` key per design.md §3).

**Files**
- Modify: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Definition of done**
- RED → GREEN; commit: `feat(kg): unresolvable lineage names surface as warnings`

### T-009: Rebuilding the same ledger twice is byte-identical

**Behavior to verify**
Two `build_kg` passes over the same experience dir produce equal graphs, with
nodes ordered by `(iteration, name)` and edges by `(dst, kind, src)` — so the
committed `kg.json` diffs cleanly in git across iterations.

**Public interface this task introduces**
- Level: module-public
- Modified: `build_kg` (ordering guarantee).

**Files**
- Modify: `meta_research/kg.py`
- Test: `tests/test_kg.py`

**Definition of done**
- RED → GREEN; commit: `feat(kg): deterministic node/edge ordering`

### T-010: `write_kg` persists a versioned, self-healing `kg.json`

**Behavior to verify**
`write_kg(run_dir)` writes `run_dir/kg.json` containing `version: 1` plus the
built graph and returns the path; a pre-existing stale/corrupt `kg.json` is fully
replaced by the rebuild (self-healing).

**Public interface this task introduces**
- Level: module-public (engine API)
- New: `meta_research.kg.write_kg(run_dir: Path) -> Path`; re-exported from `meta_research`.

**Files**
- Modify: `meta_research/kg.py`, `meta_research/__init__.py`
- Test: `tests/test_kg.py`

**Definition of done**
- RED → GREEN; commit: `feat(kg): write_kg persists versioned kg.json`

### T-011: Evaluating a design updates `kg.json` alongside `frontier.json`

**Behavior to verify**
After `evaluate_and_record(...)` (and therefore `meta-research eval` / `seed`),
`run_dir/kg.json` exists and reflects the just-recorded candidate (its node is
present; its lineage edge is present when a hypothesis named a parent), and the
file is included in the same append-only commit when `commit=True`.

**Public interface this task introduces**
- Level: system-public (the CLI/runner step the agent calls)
- Modified: `runner._finalize` — `write_kg` after `update_frontier`, before tsv/commit.

**Files**
- Modify: `meta_research/runner.py`
- Test: `tests/test_runner.py`

**Test framing hints (NOT test code)**
- Drive through `evaluate_and_record` with the existing test experiment fixtures; assert on the `kg.json` artifact (paired read interface), not internals. For the commit case follow the existing append-only commit test pattern (`git show --name-only` style assertions already used in the suite, if any).

**Anti-patterns to refuse**
- Mocking `write_kg` to "check it was called" — assert the artifact.

**Definition of done**
- RED → GREEN; commit: `feat(runner): rebuild kg.json on every recorded evaluation`

### T-012: A KG rebuild failure never sinks the evaluation

**Behavior to verify**
When `kg.json` cannot be written (e.g. the path is occupied by a directory), the
evaluation still records the bundle / frontier / tsv row, returns the result,
and attaches `kg_error` to `result.metadata` (mirroring `frontier_error`).

**Public interface this task introduces**
- Level: system-public
- Modified: `runner._finalize` (guard only).

**Files**
- Modify: `meta_research/runner.py`
- Test: `tests/test_runner.py`

**Test framing hints (NOT test code)**
- Induce the failure at the filesystem boundary (make `run_dir/kg.json` a directory) — do not monkeypatch internal functions.

**Definition of done**
- RED → GREEN; commit: `feat(runner): isolate kg rebuild failures as kg_error metadata`

### T-013: `meta-research kg` backfills the graph for an existing experiment dir

**Behavior to verify**
Running the `kg` subcommand in a run dir with prior bundles (created before the
KG feature) writes `kg.json` and prints node / edge / warning counts, exiting 0;
it does not require `prepare.py` (pure ledger read, like `frontier`).

**Public interface this task introduces**
- Level: system-public (CLI)
- New: `meta-research kg [--run-dir DIR]`.

**Files**
- Modify: `meta_research/cli.py`
- Test: `tests/test_kg.py` (CLI-level, via `cli.main([...])` as existing CLI tests do)

**Definition of done**
- RED → GREEN; commit: `feat(cli): meta-research kg rebuilds the knowledge graph`

### T-014: Scaffolding from an example never copies a generated `kg.json`

**Behavior to verify**
`meta-research init <name> --from water_cooling` on an example dir containing a
`kg.json` produces a scaffold without it (consistent with `frontier.json` /
`results.tsv` exclusion).

**Public interface this task introduces**
- Level: system-public (CLI)
- Modified: `cmd_init` ignore patterns.

**Files**
- Modify: `meta_research/cli.py`
- Test: `tests/test_init.py`

**Definition of done**
- RED → GREEN; commit: `feat(cli): exclude generated kg.json from init scaffolds`

### T-015: Documentation & skill sync (no test — contract/doc task)

**Behavior to verify**
N/A (docs). DESIGN.md gains: `kg.py` in §2 tree, runner step list §6.4, CLI §6.5,
`inspired_by` in §7.3, new **§7.6 `kg.json`** schema (copy from design.md §3).
SKILL.md loop step 1 + REFERENCE.md gain the navigation-only discipline:
"Read `kg.json` to *locate* relevant prior bundles; every hypothesis must still
cite actually-read bundles/heatmaps." Hypothesis hand-off docs gain `inspired_by`.
Sync the skill copy at `examples/water_cooling/.claude/skills/meta-research/`.

**Files**
- Modify: `DESIGN.md`, `README.md` (project structure + CLI list),
  `skills/meta-research/SKILL.md`, `skills/meta-research/REFERENCE.md`,
  `examples/water_cooling/.claude/skills/meta-research/SKILL.md`,
  `examples/water_cooling/.claude/skills/meta-research/REFERENCE.md`

**Definition of done**
- Docs consistent with shipped behavior; skill copies byte-identical to canonical.
- Commit: `docs: pin kg.json schema + navigation-only KG discipline`
